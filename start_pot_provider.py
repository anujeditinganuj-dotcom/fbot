"""
Starts the bgutil-ytdlp-pot-provider server (see pot_helper.py) as a
background subprocess when the bot boots, so PO token support doesn't
need a second manually-managed service.

This only works in the Docker deployment path, where the Dockerfile
installs Node.js and builds the provider's server/build/main.js during
the image build. It does NOT work on Render's native "env: python"
buildpack (render.yaml) — that buildpack only runs "pip install", with
no way to install Node.js or clone/build a second project alongside it.
On Render as currently configured, leave YOUTUBE_POT_ENABLED unset/false;
pot_helper.py already degrades to plain yt-dlp (no PO token) whenever the
provider isn't reachable, so this being unavailable there doesn't break
anything else — it just means no PO token on that deployment.
"""

import logging
import os
import subprocess
import time

import config

logger = logging.getLogger(__name__)

_PROVIDER_ENTRY = os.getenv("YOUTUBE_POT_PROVIDER_ENTRY", "/opt/bgutil-ytdlp-pot-provider/server/build/main.js")
_provider_process = None


def start_pot_provider_if_enabled():
    """Call once at bot startup. No-op (and safe to call) if PO tokens
    aren't enabled, Node isn't on PATH, or the provider wasn't built into
    this image — logs why and moves on rather than blocking bot startup."""
    global _provider_process

    logger.info(f"pot_helper: YOUTUBE_POT_ENABLED raw env var = {os.getenv('YOUTUBE_POT_ENABLED')!r}, "
                f"config.YOUTUBE_POT_ENABLED = {config.YOUTUBE_POT_ENABLED!r}")

    if not config.YOUTUBE_POT_ENABLED:
        logger.info("pot_helper: PO token provider disabled (YOUTUBE_POT_ENABLED is not true) — skipping startup.")
        return
    if _provider_process is not None and _provider_process.poll() is None:
        return  # already running

    import shutil
    node = shutil.which("node")
    if not node:
        logger.warning("pot_helper: YOUTUBE_POT_ENABLED is set but Node.js isn't on PATH — "
                        "PO token support needs the Docker deployment path. Continuing without it.")
        return
    if not os.path.exists(_PROVIDER_ENTRY):
        logger.warning(f"pot_helper: YOUTUBE_POT_ENABLED is set but {_PROVIDER_ENTRY} doesn't exist — "
                        "the provider wasn't built into this image. Continuing without it.")
        return

    try:
        _provider_process = subprocess.Popen(
            [node, _PROVIDER_ENTRY],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)  # give it a moment to bind its port before the first yt-dlp call
        logger.info(f"pot_helper: started PO token provider (pid={_provider_process.pid})")
        import pot_helper
        pot_helper.clear_availability_cache()
    except Exception as e:
        logger.error(f"pot_helper: failed to start PO token provider: {e}")
        _provider_process = None
