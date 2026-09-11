import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.error import URLError
from urllib.parse import urlparse, parse_qs, quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)
_PORT = int(os.environ.get("PORT", 8080))
_PING_INTERVAL = 300
_SERVER: HTTPServer | None = None
_PING_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()

PLAYER_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Faphouse Player</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ background:#0a0a0a; height:100%; width:100%; overflow:hidden; font-family:sans-serif; }}
  .wrap {{ width:100vw; height:100vh; display:flex; align-items:center; justify-content:center; }}
  video {{ width:100%; height:100%; max-width:1000px; max-height:100vh; background:#000; }}
  .msg {{ color:#f5c518; font-size:0.9rem; text-align:center; padding:1rem; }}
</style>
</head>
<body>
<div class="wrap">
  <video id="player" controls autoplay playsinline></video>
</div>
<script>
  var src = {src_json};
  var video = document.getElementById('player');
  if (!src) {{
    document.querySelector('.wrap').innerHTML = '<div class="msg">⚠️ Could not resolve this video.</div>';
  }} else if (video.canPlayType('application/vnd.apple.mpegurl')) {{
    video.src = src;  // Safari/iOS: native HLS support
  }} else if (window.Hls && Hls.isSupported()) {{
    var hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(video);
  }} else {{
    document.querySelector('.wrap').innerHTML = '<div class="msg">⚠️ Your browser can\\'t play HLS streams.</div>';
  }}
</script>
</body>
</html>"""


def _resolve_stream_url(video_url: str) -> str | None:
    """Tries every backend that can produce a real, directly-playable URL
    (not just faphouse) — same domain-based ordering as main.py's
    _downloader_for(), minus porn_fetch_downloader's sites, which don't
    expose a resolvable URL separately from their own download() call."""
    try:
        import fpo_downloader
        if fpo_downloader.is_fpo_link(video_url):
            variants = fpo_downloader.get_available_qualities(video_url)
            return variants[0]["url"] if variants else None
    except Exception as exc:
        log.warning("fpo player resolve failed for %s: %s", video_url, exc)

    try:
        import ytdlp_downloader
        if ytdlp_downloader.is_supported_link(video_url):
            return ytdlp_downloader.get_stream_url(video_url)
    except Exception as exc:
        log.warning("ytdlp player resolve failed for %s: %s", video_url, exc)

    try:
        import faphouse_downloader
        return faphouse_downloader.client.get_m3u8_url(video_url)
    except Exception as exc:
        log.warning("faphouse player resolve failed for %s: %s", video_url, exc)
        return None


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/play":
            self._handle_play(parsed)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Anujkumar alive")

    def _handle_play(self, parsed):
        """Resolves a faphouse video page link to its m3u8 stream and serves
        a minimal hls.js player page for it — this is what the bot's
        "Stream Link" button points at instead of handing out a bare .m3u8
        URL, which most phone browsers can't do anything useful with on
        their own."""
        video_url = (parse_qs(parsed.query).get("url") or [None])[0]
        stream_url = None
        if video_url:
            try:
                stream_url = _resolve_stream_url(video_url)
            except Exception as exc:
                log.warning("Player resolve failed for %s: %s", video_url, exc)

        import json
        html = PLAYER_PAGE_HTML.format(src_json=json.dumps(stream_url))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    # A /play request resolves a video (a few seconds of network I/O) —
    # without threading that would block the plain health-check GET/HEAD
    # requests hosts use to decide the service is alive, which could look
    # like a hung/unhealthy service and trigger an unwanted restart.
    daemon_threads = True


def get_public_url() -> str:
    """The externally-reachable base URL for this process, used to build
    the /play player link. Same detection order as _ping_target()."""
    for env_name in ("PUBLIC_URL", "APP_URL", "PING_URL", "HEALTHCHECK_URL", "RENDER_EXTERNAL_URL"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value.rstrip("/")

    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().strip("/")
    if render_host:
        return f"https://{render_host}"

    return ""


def player_url(video_url: str) -> str:
    """Builds the /play link for a given faphouse video page URL. Returns
    "" if no public URL is configured (caller should fall back to
    something else, e.g. the raw m3u8 link, in that case)."""
    base = get_public_url()
    if not base:
        return ""
    return f"{base}/play?url={quote(video_url, safe='')}"


def _ping_target() -> str:
    configured = get_public_url()
    if configured:
        return configured
    return f"http://127.0.0.1:{_PORT}"


def _ping_loop():
    target = _ping_target()
    log.info("Keep-alive ping target: %s (every %ss)", target, _PING_INTERVAL)

    while True:
        try:
            req = Request(target, method="HEAD")
            with urlopen(req, timeout=20) as resp:
                log.info("Keep-alive ping ok: %s", getattr(resp, "status", 200))
        except URLError as exc:
            log.warning("Keep-alive ping failed: %s", exc)
        except Exception as exc:
            log.warning("Keep-alive ping error: %s", exc)

        time.sleep(_PING_INTERVAL)


keep_alive = None  # alias set below


def Anujkumar_keep_alive(real_server_started: bool = False):
    """real_server_started=True means something else (Akbots/filetolink's
    server) already bound $PORT — on single-port hosts (Render/Railway/
    Replit) that's the same _PORT this would try to bind too, which would
    just fail with "Address already in use". So in that case, skip binding
    our own HTTP server entirely and only start the self-ping thread —
    that's the part that actually matters for beating Render's free-tier
    inactivity spin-down (see keep_alive.py module docstring / bot.py's
    caller comment), and it doesn't need a port of its own."""
    global _SERVER, _PING_THREAD

    with _LOCK:
        if not real_server_started and _SERVER is None:
            try:
                _SERVER = _ThreadingHTTPServer(("0.0.0.0", _PORT), _HealthHandler)
            except OSError as exc:
                log.warning("Health server unavailable on :%s: %s", _PORT, exc)
                # Fall through — still start the self-ping thread below even
                # though the health-check HTTP server itself didn't bind.
            else:
                thread = threading.Thread(
                    target=_SERVER.serve_forever,
                    daemon=True,
                    name="Anujkumar-health",
                )
                thread.start()

        if _PING_THREAD is None or not _PING_THREAD.is_alive():
            _PING_THREAD = threading.Thread(
                target=_ping_loop,
                daemon=True,
                name="Anujkumar-self-ping",
            )
            _PING_THREAD.start()

    if real_server_started:
        log.info("Keep-alive: reusing the already-bound port, self-ping thread started.")
    else:
        log.info("Health server on :%s", _PORT)
    return True


# Alias so bot.py can import `keep_alive` directly
keep_alive = Anujkumar_keep_alive
