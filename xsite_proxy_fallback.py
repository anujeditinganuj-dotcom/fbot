"""
xhamster / xvideos IP-block fallback for Akbots/ytdl.py.

Render/Replit ke server IPs ko xhamster aur xvideos dono block karte hain
(HTTP 404 jo actually IP-ban hai). Yeh module ek lightweight proxy-through-
ScraperAPI approach use karta hai:

  - Agar SCRAPING_PROXY env var set hai (format: http://user:pass@host:port
    ya http://host:port) — yt-dlp ke andar woh proxy inject karta hai.
  - Agar SCRAPERAPI_KEY set hai — ScraperAPI ka proxy URL build karta hai.
  - Dono nahi hain — False return karta hai (caller apna normal error dikhata hai).

Render pe setup:
  Option A (ScraperAPI — free 1000 req/month):
    1. https://www.scraperapi.com pe free account banao
    2. API key lao
    3. Render env var: SCRAPERAPI_KEY = bf89797c23c2c6b48ae9f67d57a8238c

  Option B (koi bhi HTTP/SOCKS5 proxy):
    Render env var: SCRAPING_PROXY = http://user:pass@proxyhost:port

Ek baar configure hone ke baad dono sites automatically is fallback se
download honge jab bhi yt-dlp ka direct attempt 404/403 se fail ho.
"""

import os
import re
import logging
import asyncio
import shutil
import uuid

logger = logging.getLogger(__name__)

_SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "").strip()
_SCRAPING_PROXY = os.environ.get("SCRAPING_PROXY", "").strip()

_IP_BLOCK_SIGNS = re.compile(
    r"http error 404|http error 403|403:?\s*forbidden|unable to download webpage|"
    r"not found|sign in|bot|rate.?limit|429|too many",
    re.IGNORECASE,
)


def _get_proxy_url() -> str | None:
    if _SCRAPING_PROXY:
        return _SCRAPING_PROXY
    if _SCRAPERAPI_KEY:
        return f"http://scraperapi:{_SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8001"
    return None


def is_configured() -> bool:
    return bool(_get_proxy_url())


def looks_like_ip_block(error_text: str) -> bool:
    return bool(_IP_BLOCK_SIGNS.search(str(error_text or "")))


def _download_via_proxy(url: str, out_dir: str, quality_id: str | None = None) -> tuple:
    """yt-dlp download with proxy injected. Returns (filepath, info_dict)."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp not installed")

    proxy = _get_proxy_url()
    if not proxy:
        raise RuntimeError("No proxy configured (set SCRAPERAPI_KEY or SCRAPING_PROXY)")

    outtmpl = os.path.join(out_dir, "%(title)s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "proxy": proxy,
        "socket_timeout": 60,
        "nocheckcertificate": True,
    }

    if quality_id:
        opts["format"] = quality_id
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Find downloaded file
    filepath = ydl.prepare_filename(info)
    if not os.path.exists(filepath):
        # Try to find any video file in the dir
        for f in os.listdir(out_dir):
            full = os.path.join(out_dir, f)
            if os.path.isfile(full) and f.split(".")[-1] in ("mp4", "mkv", "webm", "avi"):
                filepath = full
                break

    return filepath, info


async def try_xsite_proxy_fallback(client, chat_id: int, reply_to: int, url: str,
                                    status=None, quality_id: str | None = None) -> bool:
    """
    Try downloading xhamster/xvideos URL via proxy when direct yt-dlp fails.
    Returns True if file was sent successfully, False otherwise.
    """
    proxy = _get_proxy_url()
    if not proxy:
        logger.debug("[xsite_proxy] No proxy configured — skipping fallback")
        return False

    if status is not None:
        try:
            await status.edit_text(
                "<b>🔄 Direct download blocked — retrying via proxy...</b>"
            )
        except Exception:
            pass

    session_dir = os.path.join(
        os.environ.get("DOWNLOAD_DIR", "downloads"),
        f"proxy_{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(session_dir, exist_ok=True)

    try:
        filepath, info = await asyncio.to_thread(
            _download_via_proxy, url, session_dir, quality_id
        )

        if not os.path.exists(filepath):
            logger.warning(f"[xsite_proxy] Download via proxy succeeded but file missing: {filepath}")
            return False

        # Upload to Telegram
        title = info.get("title") or "Video"
        duration = int(info.get("duration") or 0)
        width = info.get("width") or 0
        height = info.get("height") or 0

        if status is not None:
            try:
                await status.edit_text("<b>📤 Uploading...</b>")
            except Exception:
                pass

        await client.send_video(
            chat_id,
            filepath,
            caption=f"<b>{title}</b>\n<i>(via proxy fallback)</i>",
            duration=duration,
            width=width,
            height=height,
            reply_to_message_id=reply_to,
            parse_mode="html",
        )

        if status is not None:
            try:
                await status.delete()
            except Exception:
                pass

        logger.info(f"[xsite_proxy] Successfully delivered {url} via proxy")
        return True

    except Exception as e:
        logger.warning(f"[xsite_proxy] Proxy fallback failed for {url}: {e}")
        return False
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)
