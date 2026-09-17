"""
mat6tube.com auto-scraper backend for auto_scraper.py.

mat6tube.com is a VK-sourced video aggregator — watch URLs use VK-style
IDs like /watch/-<ownerId>_<videoId>. The site has no public API, so
this module scrapes HTML pages directly.

Browse/search URL patterns confirmed from the site:
    Homepage (latest):  https://mat6tube.com/
    New videos:         https://mat6tube.com/new/
    Search:             https://mat6tube.com/search/<query>/
    Tag/category:       https://mat6tube.com/tags/<tag>/
    Model:              https://mat6tube.com/models/<model>/

Video links on listing pages follow the pattern:
    href="/watch/-<id>"

All meta-data needed for the caption (title, actor, duration, thumbnail)
comes from the watch page's own <meta> tags — see mat6tube_downloader.py.

Function contract auto_scraper.py expects (same as eporner_scraper):
    get_model_page_videos(term, page=1) -> (list[{slug,url,title}], total_pages)
    get_random_page_videos()            -> list[{slug,url,title}]
    get_latest_videos()                 -> list[{slug,url,title}]
"""

import logging
import random
import re
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://mat6tube.com"
PER_PAGE = 24  # mat6tube shows ~24 videos per listing page

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE_URL + "/",
}

# Broad search terms for random mode
_BROAD_TERMS = [
    "amateur", "teen", "milf", "asian", "latina", "ebony", "blonde",
    "brunette", "anal", "lesbian", "hardcore", "big tits", "creampie",
    "pov", "public", "homemade", "solo", "threesome", "mature", "indian",
]

# Regex to find watch links on listing pages
_WATCH_LINK_RE = re.compile(r'href=["\'](/watch/(-?\d+_\d+))["\']')
_TITLE_RE = re.compile(r'title=["\'](.*?)["\']', re.IGNORECASE)

# Simple pagination — mat6tube uses ?page=N or /page/<N>/ patterns
_TOTAL_RE = re.compile(r'(\d[\d,]*)\s*(?:videos?|results?)', re.IGNORECASE)


def _fetch(url: str) -> str | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning(f"mat6tube fetch failed for {url}: {e}")
        return None


def _parse_videos(html: str) -> list[dict]:
    """Extract video slugs/URLs/titles from a listing page."""
    items = []
    seen = set()

    # Find all watch links with their surrounding context for title extraction
    # mat6tube listing HTML pattern: <a href="/watch/ID" title="TITLE">
    for m in re.finditer(
        r'<a\s[^>]*href=["\'](/watch/(-?\d+_\d+))["\'][^>]*(?:title=["\'](.*?)["\'])?[^>]*>',
        html, re.IGNORECASE | re.DOTALL
    ):
        path = m.group(1)
        vid_id = m.group(2)
        title = m.group(3) or vid_id

        if vid_id in seen:
            continue
        seen.add(vid_id)

        url = BASE_URL + path
        slug = f"mat6tube-{vid_id}"
        items.append({"slug": slug, "url": url, "title": title})

    # Fallback: just find href="/watch/ID" without title
    if not items:
        for m in _WATCH_LINK_RE.finditer(html):
            vid_id = m.group(2)
            if vid_id in seen:
                continue
            seen.add(vid_id)
            url = BASE_URL + m.group(1)
            slug = f"mat6tube-{vid_id}"
            items.append({"slug": slug, "url": url, "title": vid_id})

    return items


def _estimate_total_pages(html: str, page: int) -> int:
    """Try to extract total video count from page, estimate total pages."""
    m = _TOTAL_RE.search(html)
    if m:
        try:
            total = int(m.group(1).replace(",", ""))
            return max(1, -(-total // PER_PAGE))  # ceiling division
        except (ValueError, TypeError):
            pass
    # Check if there's a "next page" link
    if re.search(r'[?&/]page[=/](\d+)', html):
        return page + 1
    return page


def _search_url(query: str, page: int) -> str:
    q = quote(query.strip().replace(" ", "+"), safe="+")
    base = f"{BASE_URL}/search/{q}/"
    return base if page <= 1 else f"{base}?page={page}"


def _model_url(model: str, page: int) -> str:
    m = quote(model.strip().replace(" ", "-").lower(), safe="-")
    base = f"{BASE_URL}/models/{m}/"
    return base if page <= 1 else f"{base}?page={page}"


async def get_model_page_videos(term: str, page: int = 1) -> tuple[list, int]:
    """
    term: performer name, tag, or any keyword.
    First tries the model page, falls back to search if empty.
    Same contract as eporner_scraper.get_model_page_videos().
    """
    import asyncio

    def _fetch_model():
        # Try model page first
        html = _fetch(_model_url(term, page))
        if html:
            items = _parse_videos(html)
            if items:
                total_pages = _estimate_total_pages(html, page)
                return items, total_pages

        # Fall back to search
        html = _fetch(_search_url(term, page))
        if not html:
            return [], 1
        items = _parse_videos(html)
        total_pages = _estimate_total_pages(html, page)
        return items, total_pages

    return await asyncio.to_thread(_fetch_model)


async def get_random_page_videos() -> list:
    """
    Approximates random by picking a random search term + random page.
    Same contract as eporner_scraper.get_random_page_videos().
    """
    import asyncio

    def _fetch_random():
        term = random.choice(_BROAD_TERMS)
        page = random.randint(1, 10)
        html = _fetch(_search_url(term, page))
        if not html:
            return []
        return _parse_videos(html)

    return await asyncio.to_thread(_fetch_random)


async def get_latest_videos() -> list:
    """
    Polls the /new/ page for recently uploaded videos.
    Same contract as eporner_scraper.get_latest_videos().
    """
    import asyncio

    def _fetch_latest():
        html = _fetch(f"{BASE_URL}/new/")
        if not html:
            # Fallback to homepage
            html2 = _fetch(BASE_URL + "/")
            if not html2:
                return []
            return _parse_videos(html2)
        return _parse_videos(html)

    return await asyncio.to_thread(_fetch_latest)
