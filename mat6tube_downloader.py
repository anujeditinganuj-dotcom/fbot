"""
mat6tube.com video downloader engine.

Unusually straightforward compared to the other sites in this codebase:
a watch page's own <meta> tags directly expose a same-domain, direct MP4
file URL —

    <meta property="ya:ovs:content_url"
          content="https://mat6tube.com/videofile/<id>.mp4">

where <id> is exactly the same id that's in the watch URL itself
(https://mat6tube.com/watch/<id>) — a VK-style "-<ownerId>_<videoId>"
pair, since this site appears to source its catalog from VK-hosted
videos, though that's just an observation about where the ids come
from, not something this module needs to care about; the file URL
pattern is what matters and it's on mat6tube.com's own domain either
way. No embed-following, no JS execution, no descrambling — confirmed
directly against a real page fetch, not guessed.

The same meta tags also carry everything needed for the caption: title,
actor/author, duration (seconds), like count, view count, and upload
date, all as plain non-obfuscated values — richer and simpler than most
other sites this codebase supports.
"""

import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://mat6tube.com"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_WATCH_ID_RE = re.compile(r"/watch/(-?\d+_\d+)")


def is_mat6tube_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host in ("mat6tube.com", "www.mat6tube.com")
    except Exception:
        return False


_URL_RE = re.compile(r"https?://\S+")


def extract_mat6tube_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links()."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_mat6tube_link(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _video_id_from_url(video_url: str) -> str | None:
    m = _WATCH_ID_RE.search(video_url)
    return m.group(1) if m else None


def _fetch_html(url: str) -> str:
    r = requests.get(url, timeout=15, headers={"User-Agent": _UA, "Referer": BASE_URL})
    r.raise_for_status()
    return r.text


def _meta_content(html: str, prop: str) -> str | None:
    """Pulls a <meta property="X" content="Y"> or <meta name="X" content="Y">
    value — mat6tube uses both property= and name= across different tags,
    so this checks either rather than assuming one."""
    m = re.search(
        rf'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


def get_available_qualities(video_url: str) -> list:
    """Same contract as faphouse_downloader.get_available_qualities().
    Only ever one quality — the direct file URL doesn't come with
    alternate resolutions the way a KVS or HLS site's does."""
    video_id = _video_id_from_url(video_url)
    if not video_id:
        raise RuntimeError(f"Couldn't find a watch id in {video_url!r}")

    html = _fetch_html(video_url)
    content_url = _meta_content(html, "ya:ovs:content_url")
    if not content_url:
        # Fall back to the predictable pattern directly — the meta tag
        # not being present (site markup change) doesn't necessarily
        # mean the file itself moved.
        content_url = f"{BASE_URL}/videofile/{video_id}.mp4"

    return [{"label": "Best available", "height": None, "url": content_url}]


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """Same contract as faphouse_downloader.download_video() — a plain
    progressive-MP4 download (requests, streamed to disk), same as
    fpo.xxx's own download_video() since this is the same kind of direct
    file, not an HLS/m3u8 stream."""
    target_url = stream_url or get_available_qualities(video_url)[0]["url"]

    start_time = time.time()
    with requests.get(target_url, stream=True, timeout=30,
                       headers={"User-Agent": _UA, "Referer": video_url}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    elapsed = time.time() - start_time
                    pct = (downloaded / total * 100) if total else None
                    on_progress({
                        "pct": pct,
                        "downloaded_bytes": downloaded,
                        "speed_bytes_s": downloaded / elapsed if elapsed > 0 else 0,
                        "eta_s": ((total - downloaded) / (downloaded / elapsed)) if (total and downloaded and elapsed > 0) else None,
                        "elapsed_s": elapsed,
                        "duration_s": 0,
                    })

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError("Download finished but the output file is missing/empty.")
    return out_path, time.time() - start_time


def get_page_meta(video_url: str) -> dict:
    """Same contract as faphouse_downloader.get_page_meta(). Everything
    here comes straight off the watch page's own meta tags — no
    secondary API call needed, unlike most other backends in this
    codebase."""
    try:
        html = _fetch_html(video_url)
    except Exception as e:
        logger.warning(f"get_page_meta failed for {video_url}: {e}")
        return {"title": None, "author": None, "duration": None, "poster_url": None,
                "view_count": None, "like_count": None, "comment_count": None, "upload_date": None}

    duration_raw = _meta_content(html, "video:duration")
    try:
        duration = int(duration_raw) if duration_raw else None
    except ValueError:
        duration = None

    views_raw = _meta_content(html, "ya:ovs:views_total")
    likes_raw = _meta_content(html, "ya:ovs:likes")

    return {
        "title": _meta_content(html, "og:title"),
        "author": _meta_content(html, "video:actor"),
        "duration": duration,
        "poster_url": _meta_content(html, "og:image"),
        "view_count": int(views_raw) if views_raw and views_raw.isdigit() else None,
        "like_count": int(likes_raw) if likes_raw and likes_raw.isdigit() else None,
        "comment_count": None,
        "upload_date": _meta_content(html, "ya:ovs:upload_date"),
    }
