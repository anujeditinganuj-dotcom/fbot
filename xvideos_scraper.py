"""
xvideos.com auto-scraper backend for auto_scraper.py's xvideos_uploader_worker
— same role as pornhub_scraper.py/xhamster_scraper.py, just for XVideos.

URL patterns (yt-dlp confirmed):
  - performer page: https://www.xvideos.com/profiles/<slug>
  - channel page:   https://www.xvideos.com/channels/<slug>

FIX v3:
  + requests+BeautifulSoup HTML scraping fallback for get_latest_videos()
    (yt-dlp playlist fetch fails on Render/cloud IPs due to IP-level block)
  + xvideos JSON API endpoint used for latest listing (more reliable than HTML)
  + impersonate=chrome-124 kept for individual video pages (still works)
"""

import asyncio
import json
import logging
import re
import urllib.request
from urllib.parse import urlparse

import yt_dlp

logger = logging.getLogger(__name__)

PER_PAGE = 30
_MODEL_URL   = "https://www.xvideos.com/profiles/{slug}"
_STUDIO_URL  = "https://www.xvideos.com/channels/{slug}"

_DOMAINS = [
    "https://www.xvideos.com",
    "https://www.xvideos.red",
]

# XVideos JSON API endpoints — these return structured JSON, not HTML,
# so yt-dlp playlist-parse is not needed (avoids the Unsupported URL error).
_API_LATEST_URLS = [
    "https://www.xvideos.com/api/search?sort=uploaddate&duration=all&p=0&nb_videos_per_page=30",
    "https://www.xvideos.com/api/search-videos?sort=uploaddate&p=0&nb_videos_per_page=30",
]

# Fallback HTML scrape pattern
_HTML_LATEST_URLS = [
    "https://www.xvideos.com/new-videos/0",
    "https://www.xvideos.red/new-videos/0",
]

_UNKNOWN_TOTAL_PAGES = 10_000
_CHROME_TARGET = "chrome-124"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")


def _fetch_url(url: str, timeout: int = 15) -> str:
    """Simple HTTP GET with browser headers, no yt-dlp needed."""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _items_from_xvideos_json(raw: str) -> list:
    """Parse xvideos JSON API response into item dicts."""
    try:
        data = json.loads(raw)
    except Exception:
        return []
    videos = data.get("videos") or data.get("results") or []
    items = []
    for v in videos:
        vid_url = v.get("url") or v.get("link") or v.get("u")
        if not vid_url:
            continue
        if not vid_url.startswith("http"):
            vid_url = "https://www.xvideos.com" + vid_url
        vid_id = str(v.get("id") or vid_url)
        items.append({
            "slug": f"xvideos-{vid_id}",
            "url": vid_url,
            "title": v.get("title") or v.get("tf") or vid_id,
        })
    return items


def _items_from_xvideos_html(html: str) -> list:
    """Scrape video URLs from xvideos listing HTML page."""
    # xvideos embeds video data as JSON in a JS variable
    # e.g. xvideos_list_videos_69_full = [{...}]
    pattern = re.compile(r'xv\.conf\s*=\s*(\{.*?\});', re.DOTALL)
    match = pattern.search(html)
    if match:
        try:
            conf = json.loads(match.group(1))
            videos = conf.get("videos") or []
            items = []
            for v in videos:
                vid_url = v.get("url") or v.get("u")
                if not vid_url or not vid_url.startswith("http"):
                    continue
                vid_id = str(v.get("id") or vid_url)
                items.append({
                    "slug": f"xvideos-{vid_id}",
                    "url": vid_url,
                    "title": v.get("tf") or v.get("title") or vid_id,
                })
            if items:
                return items
        except Exception:
            pass

    # Fallback: regex scrape href="/video<id>/"
    urls = re.findall(r'href="(https?://(?:www\.xvideos\.com|www\.xvideos\.red)/video\d+/[^"]+)"', html)
    items = []
    seen = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            vid_id = re.search(r'/video(\d+)/', u)
            slug_id = vid_id.group(1) if vid_id else u
            items.append({"slug": f"xvideos-{slug_id}", "url": u, "title": slug_id})
    return items


def _flat_entries(url: str, playlist_start: int, playlist_end: int) -> list:
    opts = {
        "quiet":          True,
        "no_warnings":    True,
        "extract_flat":   "in_playlist",
        "playliststart":  playlist_start,
        "playlistend":    playlist_end,
        "skip_download":  True,
        "socket_timeout": 20,
        "nocheckcertificate": True,
    }
    try:
        with yt_dlp.YoutubeDL({**opts, "impersonate": _CHROME_TARGET}) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = (info or {}).get("entries") or []
        if entries:
            return entries
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return (info or {}).get("entries") or []
    except Exception as e:
        logger.debug(f"[xvideos] _flat_entries failed for {url}: {e}")
        return []


def _try_urls_with_fallback(urls: list, playlist_start: int, playlist_end: int) -> list:
    for url in urls:
        try:
            entries = _flat_entries(url, playlist_start, playlist_end)
            if entries:
                logger.debug(f"[xvideos] got {len(entries)} entries from {url}")
                return entries
        except Exception as e:
            logger.debug(f"[xvideos] failed {url}: {e}")
    logger.warning(f"[xvideos] all URLs failed. Tried: {urls}")
    return []


def _entry_to_item(entry: dict) -> dict | None:
    video_url = entry.get("webpage_url") or entry.get("url")
    if not video_url or not video_url.startswith("http"):
        return None
    video_id = entry.get("id") or video_url
    slug = f"xvideos-{video_id}"
    return {"slug": slug, "url": video_url, "title": entry.get("title") or slug}


def _term_to_slug(term: str, url_path_prefixes: tuple) -> str:
    if term.startswith("http"):
        path = urlparse(term).path.strip("/")
        segments = path.split("/")
        if len(segments) >= 2 and segments[0] in url_path_prefixes:
            return segments[1]
        if len(segments) >= 1 and segments[0] in url_path_prefixes:
            return term.rstrip("/").rsplit("/", 1)[-1]
    return _slugify(term)


def _fetch_latest_items_sync() -> list:
    """
    Tries multiple strategies to get latest xvideos listings:
    1. JSON API endpoints (most reliable, no yt-dlp needed)
    2. HTML scraping fallback
    3. yt-dlp playlist (last resort, often blocked on server IPs)
    """
    # Strategy 1: JSON API
    for url in _API_LATEST_URLS:
        try:
            raw = _fetch_url(url)
            items = _items_from_xvideos_json(raw)
            if items:
                logger.debug(f"[xvideos] latest via JSON API: {len(items)} items from {url}")
                return items
        except Exception as e:
            logger.debug(f"[xvideos] JSON API failed {url}: {e}")

    # Strategy 2: HTML scrape
    for url in _HTML_LATEST_URLS:
        try:
            html = _fetch_url(url)
            items = _items_from_xvideos_html(html)
            if items:
                logger.debug(f"[xvideos] latest via HTML scrape: {len(items)} items from {url}")
                return items
        except Exception as e:
            logger.debug(f"[xvideos] HTML scrape failed {url}: {e}")

    # Strategy 3: yt-dlp (often blocked but try anyway)
    for url in _HTML_LATEST_URLS:
        try:
            entries = _flat_entries(url, 1, PER_PAGE)
            if entries:
                items = [item for e in entries if (item := _entry_to_item(e)) is not None]
                if items:
                    return items
        except Exception as e:
            logger.debug(f"[xvideos] yt-dlp failed {url}: {e}")

    return []


async def get_model_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    slug  = _term_to_slug(term, ("profiles",))
    start = (page - 1) * PER_PAGE + 1
    end   = page * PER_PAGE
    urls = [f"https://www.xvideos.com/profiles/{slug}",
            f"https://www.xvideos.red/profiles/{slug}"]
    try:
        entries = await asyncio.to_thread(_try_urls_with_fallback, urls, start, end)
    except Exception as e:
        logger.warning(f"[xvideos] get_model_page_videos failed for {term!r}: {e}")
        return [], 0
    items = [item for e in entries if (item := _entry_to_item(e)) is not None]
    return items, _UNKNOWN_TOTAL_PAGES


async def get_studio_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    slug  = _term_to_slug(term, ("channels",))
    start = (page - 1) * PER_PAGE + 1
    end   = page * PER_PAGE
    urls = [f"https://www.xvideos.com/channels/{slug}",
            f"https://www.xvideos.red/channels/{slug}"]
    try:
        entries = await asyncio.to_thread(_try_urls_with_fallback, urls, start, end)
    except Exception as e:
        logger.warning(f"[xvideos] get_studio_page_videos failed for {term!r}: {e}")
        return [], 0
    items = [item for e in entries if (item := _entry_to_item(e)) is not None]
    return items, _UNKNOWN_TOTAL_PAGES


async def get_latest_videos() -> list:
    """
    Multi-strategy latest video fetch:
    1. XVideos JSON API (no yt-dlp, works even when playlist URLs are blocked)
    2. HTML scraping fallback
    3. yt-dlp as last resort
    """
    try:
        items = await asyncio.to_thread(_fetch_latest_items_sync)
        return items
    except Exception as e:
        logger.warning(f"[xvideos] get_latest_videos failed: {e}")
        return []


async def get_random_page_videos() -> list:
    return await get_latest_videos()
