"""
Sets up and runs Brainicism's bgutil-ytdlp-pot-provider (a local
"proof-of-origin" token generator for YouTube — see
https://github.com/Brainicism/bgutil-ytdlp-pot-provider) as a background
HTTP server, so yt-dlp's "web" YouTube client becomes usable again —
that's the ONLY client that exposes the full 1080p/720p/480p/360p (and,
on eligible videos, 2K/4K) format ladder; every other client
(ios/tv/mweb/tv_embedded/android) reliably caps out around 1080p.
ytdlp_downloader.py checks is_ready() before deciding whether to include
"web" in player_client at all.

WHY THIS EXISTS: a previous attempt at PO-token support in this bot was
removed entirely because it was never actually wired up — pot_helper.py,
which the old startup script imported, didn't exist anywhere in this
repo, so "web" just silently got near-empty format lists. This is a real
implementation: it clones the provider's server code, builds it with
Deno (already a dependency here for YouTube's JS challenge — see
_is_youtube() usage in ytdlp_downloader.py), and runs it as a background
HTTP server on 127.0.0.1:4416, which is where the PyPI
`bgutil-ytdlp-pot-provider` plugin (see requirements.txt) looks for it
by default — no extra yt-dlp configuration needed once it's running.

THIS CAN LEGITIMATELY FAIL on some hosts, and that's handled gracefully
on purpose: the provider's "canvas" npm dependency needs native
build tooling (a C/C++ compiler, and system graphics libraries like
libcairo/libpango) that a minimal Python-only host (e.g. Render's native
Python buildpack, as opposed to a Docker build with apt access) may not
have. Every step here is wrapped so a failure just leaves is_ready()
False forever — ytdlp_downloader.py falls back to the same
ios/tv/mweb/tv_embedded/android client list this bot used before this
existed, so YouTube downloads keep working at capped-but-real quality
either way. Check this module's logger output ("[pot-provider] ...") to
see exactly which step failed if "web" never becomes available.
"""

import logging
import os
import shutil
import socket
import subprocess
import threading
import time

logger = logging.getLogger("faphouse_bot")

POT_HOME = os.path.expanduser("~/bgutil-ytdlp-pot-provider")
POT_SERVER_DIR = os.path.join(POT_HOME, "server")
POT_PORT = 4416
POT_REPO_URL = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"

_ready = threading.Event()
_server_process: subprocess.Popen | None = None


def is_ready() -> bool:
    """True once the local PO-token HTTP server is actually up and
    accepting connections on 127.0.0.1:4416. False (forever, if setup
    failed) means callers should stick to the PO-token-free client list."""
    return _ready.is_set()


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run(cmd: list, cwd: str = None, timeout: int = 120) -> bool:
    """Run a setup command to completion, logging output only on
    failure (success is just a debug line — this can be noisy)."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        if result.returncode != 0:
            out = (result.stdout or b"").decode("utf-8", errors="replace")[-2000:]
            logger.warning(f"[pot-provider] command failed ({' '.join(cmd)}): {out}")
            return False
        logger.debug(f"[pot-provider] ok: {' '.join(cmd)}")
        return True
    except subprocess.TimeoutExpired:
        logger.warning(f"[pot-provider] command timed out ({timeout}s): {' '.join(cmd)}")
        return False
    except Exception as e:
        logger.warning(f"[pot-provider] command errored ({' '.join(cmd)}): {e}")
        return False


def _setup_and_start():
    """Runs once, in a background thread — never blocks bot startup.
    Every failure path just returns early with is_ready() left False."""
    global _server_process

    if not shutil.which("deno"):
        logger.warning(
            "[pot-provider] deno not found on PATH — skipping PO-token "
            "provider setup, YouTube stays on the capped-quality client list."
        )
        return

    # ── 1. Clone the server code (skip if already present from a previous boot on the same disk) ──
    already_cloned = os.path.isdir(os.path.join(POT_SERVER_DIR, "src"))
    if not already_cloned:
        if not shutil.which("git"):
            logger.warning("[pot-provider] git not found on PATH — skipping setup.")
            return
        logger.info(f"[pot-provider] cloning provider server to {POT_HOME}...")
        if os.path.isdir(POT_HOME):
            shutil.rmtree(POT_HOME, ignore_errors=True)
        if not _run(["git", "clone", "--depth", "1", POT_REPO_URL, POT_HOME], timeout=60):
            return

    # ── 2. Build it with Deno (installs the npm deps it needs, incl. "canvas" — the step most likely to fail on a host without native build tools) ──
    built_marker = os.path.join(POT_SERVER_DIR, "node_modules")
    if not os.path.isdir(built_marker):
        logger.info("[pot-provider] building provider server with deno (can take a minute)...")
        if not _run(
            ["deno", "install", "--allow-scripts=npm:canvas", "--frozen"],
            cwd=POT_SERVER_DIR, timeout=240,
        ):
            logger.warning(
                "[pot-provider] build failed — this usually means the 'canvas' npm "
                "dependency couldn't compile (needs a C/C++ toolchain + libcairo/"
                "libpango, which a minimal Python-only host may not have). "
                "YouTube stays on the capped-quality client list."
            )
            return

    # ── 3. Start the HTTP server in the background (only if nothing's already listening — e.g. a leftover process from a previous attempt this boot) ──
    if _port_open("127.0.0.1", POT_PORT):
        logger.info(f"[pot-provider] something's already listening on :{POT_PORT}, assuming it's ready.")
        _ready.set()
        return

    logger.info(f"[pot-provider] starting provider HTTP server on 127.0.0.1:{POT_PORT}...")
    try:
        _server_process = subprocess.Popen(
            ["deno", "run", "--allow-env", "--allow-net", "--allow-ffi=.", "--allow-read=.",
             "../src/main.ts", "--host", "127.0.0.1", "--port", str(POT_PORT)],
            cwd=os.path.join(POT_SERVER_DIR, "node_modules"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"[pot-provider] failed to start server process: {e}")
        return

    # Give it a few seconds to come up, polling rather than a fixed sleep.
    for _ in range(20):
        if _port_open("127.0.0.1", POT_PORT):
            _ready.set()
            logger.info("[pot-provider] ✅ PO-token provider is up — YouTube's full quality ladder is available.")
            return
        if _server_process.poll() is not None:
            logger.warning(f"[pot-provider] server process exited early (code {_server_process.returncode}).")
            return
        time.sleep(1)
    logger.warning("[pot-provider] server didn't come up within 20s — giving up for this boot.")


def start_background():
    """Call once at bot startup. Non-blocking — runs setup in a daemon
    thread so a slow/failed clone+build never delays the bot itself from
    starting up and serving everything else."""
    threading.Thread(target=_setup_and_start, name="pot-provider-setup", daemon=True).start()
