"""
YouTube search — ported from src's Akbots/ytsearch.py and adapted to fbot's
own UI patterns (make_button/BTN_PRIMARY/SC/ParseMode from main.py).

Adds two commands:
  /search <query>   — search YouTube, show numbered results with quality picker
  /yts <query>      — same (short alias)

Also adds plain-text auto-search: if a user sends a text message with a
known media keyword (song, video, episode, movie, …) and no URL, fbot now
shows a search engine picker (YouTube / …) just like src does.

Register in main.py at startup:
    import ytsearch  # noqa: F401 — registers handlers on import
"""

import asyncio
import uuid
import logging
import re

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from pyrogram.enums import ParseMode

logger = logging.getLogger("faphouse_bot")

try:
    import yt_dlp as _yt_dlp
except ImportError:
    _yt_dlp = None

# ── Lazy import from main to avoid circular imports ───────────────────────────
def _main():
    import main as _m
    return _m

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_CHUNK_SIZE = 30    # how many YT results to fetch per API call
SEARCH_PAGE_SIZE  = 5     # how many results to show per page in Telegram


# ── Core search ───────────────────────────────────────────────────────────────
def _search_youtube_sync(query: str, chunk_size: int = SEARCH_CHUNK_SIZE) -> list:
    """Flat (metadata-only) YouTube search — fast, no per-video info fetch."""
    if _yt_dlp is None:
        raise RuntimeError("yt-dlp not installed")
    with _yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "default_search": "ytsearch",
    }) as ydl:
        info = ydl.extract_info(f"ytsearch{chunk_size}:{query}", download=False)

    entries = (info or {}).get("entries") or []
    results = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        dur = entry.get("duration")
        dur_str = ""
        if isinstance(dur, (int, float)) and dur > 0:
            m, s = divmod(int(dur), 60)
            h, m = divmod(m, 60)
            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        results.append({
            "id":       entry["id"],
            "title":    (entry.get("title") or "Untitled")[:70],
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": dur_str,
        })
    return results


# ── State caches ──────────────────────────────────────────────────────────────
# msg_id -> {"query": str, "results": list, "exhausted": bool}
_SEARCH_CACHE: dict = {}


def _trim_cache(cache: dict, limit: int = 500):
    while len(cache) > limit:
        cache.pop(next(iter(cache)), None)


# ── Formatting ────────────────────────────────────────────────────────────────
def _results_text(query: str, results: list, page: int, exhausted: bool) -> str:
    start = page * SEARCH_PAGE_SIZE
    end   = min(start + SEARCH_PAGE_SIZE, len(results))
    lines = [f"🔍 <b>YouTube Search:</b> <i>{query}</i>\n"]
    for i, r in enumerate(results[start:end], start=start + 1):
        meta = " — ".join(x for x in (r["uploader"], r["duration"]) if x)
        line = f"{i}. {r['title']}"
        if meta:
            line += f"\n    <i>{meta}</i>"
        lines.append(line)
    if exhausted and end >= len(results):
        lines.append(f"\n<i>Showing {end} of {len(results)} results.</i>")
    else:
        lines.append("\n<i>Tap a number to download.</i>")
    return "\n".join(lines)


def _results_kb(results: list, page: int, exhausted: bool, status_msg_id: int) -> InlineKeyboardMarkup:
    m = _main()
    btn = lambda text, cd: m.make_button(text, callback_data=cd, style=m.BTN_PRIMARY)

    start = page * SEARCH_PAGE_SIZE
    end   = min(start + SEARCH_PAGE_SIZE, len(results))

    rows, row = [], []
    for abs_idx in range(start, end):
        row.append(btn(str(abs_idx + 1), f"ytsr:{abs_idx}:{status_msg_id}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row:
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(btn("◀️ Prev", f"ytsrpg:{page - 1}:{status_msg_id}"))
    nav.append(btn(f"Page {page + 1}", "ytsr:noop"))
    if not exhausted or end < len(results):
        nav.append(btn("Next ▶️", f"ytsrpg:{page + 1}:{status_msg_id}"))
    rows.append(nav)

    return InlineKeyboardMarkup(rows)


# ── Core search flow ──────────────────────────────────────────────────────────
async def _do_search(client: Client, message: Message, query: str, status: Message = None):
    if _yt_dlp is None:
        text = "❌ <b>yt-dlp not installed.</b>"
        if status:
            return await status.edit_text(text, parse_mode=ParseMode.HTML)
        return await message.reply_text(text, parse_mode=ParseMode.HTML)

    if status is None:
        status = await message.reply_text("🔍 <b>Searching YouTube...</b>", parse_mode=ParseMode.HTML)
    else:
        await status.edit_text("🔍 <b>Searching YouTube...</b>", parse_mode=ParseMode.HTML)

    try:
        results = await asyncio.to_thread(_search_youtube_sync, query)
    except Exception as e:
        return await status.edit_text(f"❌ <b>Search failed:</b>\n<code>{e}</code>", parse_mode=ParseMode.HTML)

    if not results:
        return await status.edit_text(f"❌ <b>No results for:</b> <i>{query}</i>", parse_mode=ParseMode.HTML)

    exhausted = len(results) < SEARCH_CHUNK_SIZE
    _SEARCH_CACHE[status.id] = {"query": query, "results": results, "exhausted": exhausted}
    _trim_cache(_SEARCH_CACHE)

    await status.edit_text(
        _results_text(query, results, page=0, exhausted=exhausted),
        parse_mode=ParseMode.HTML,
        reply_markup=_results_kb(results, page=0, exhausted=exhausted, status_msg_id=status.id),
    )


# ── Commands ──────────────────────────────────────────────────────────────────
@Client.on_message(filters.command(["search", "yts"]) & filters.private)
async def search_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "🔍 <b>Usage:</b> <code>/search &lt;song or video name&gt;</code>\n"
            "<i>e.g.</i> <code>/search Believer Imagine Dragons</code>",
            parse_mode=ParseMode.HTML,
        )
    query = message.text.split(None, 1)[1].strip()
    await _do_search(client, message, query)


# ── Callbacks ─────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ytsr:noop$"))
async def _cb_noop(_, cq: CallbackQuery):
    await cq.answer()


@Client.on_callback_query(filters.regex(r"^ytsr:(\d+):(\d+)$"))
async def _cb_select(client: Client, cq: CallbackQuery):
    m = cq.matches[0]
    idx, msg_id = int(m.group(1)), int(m.group(2))

    cached = _SEARCH_CACHE.get(msg_id)
    if not cached:
        return await cq.answer("Search expired — please search again.", show_alert=True)
    results = cached["results"]
    if idx < 0 or idx >= len(results):
        return await cq.answer("Invalid selection.", show_alert=True)

    await cq.answer("⏳ Fetching qualities...")
    video_url = f"https://www.youtube.com/watch?v={results[idx]['id']}"

    # Reuse fbot's own show_quality_menu by storing in LINK_CACHE
    mn = _main()
    link_id = uuid.uuid4().hex[:8]
    mn.LINK_CACHE[link_id] = video_url
    await mn.show_quality_menu(client, cq, link_id, video_url)


@Client.on_callback_query(filters.regex(r"^ytsrpg:(\d+):(\d+)$"))
async def _cb_page(client: Client, cq: CallbackQuery):
    m = cq.matches[0]
    page, msg_id = int(m.group(1)), int(m.group(2))

    cached = _SEARCH_CACHE.get(msg_id)
    if not cached:
        return await cq.answer("Search expired — please search again.", show_alert=True)

    results, query = cached["results"], cached["query"]
    exhausted = cached.get("exhausted", False)

    if not exhausted:
        page_start = page * SEARCH_PAGE_SIZE
        while page_start >= len(results) and not exhausted:
            await cq.answer("Loading more results...")
            try:
                more = await asyncio.to_thread(_search_youtube_sync, query)
                if not more or len(more) < SEARCH_CHUNK_SIZE:
                    exhausted = True
                    cached["exhausted"] = True
                existing_ids = {r["id"] for r in results}
                for r in more:
                    if r["id"] not in existing_ids:
                        results.append(r)
                        existing_ids.add(r["id"])
                cached["results"] = results
            except Exception as e:
                return await cq.answer(f"Error: {e}", show_alert=True)
            page_start = page * SEARCH_PAGE_SIZE

    await cq.answer()
    await cq.message.edit_text(
        _results_text(query, results, page=page, exhausted=exhausted),
        parse_mode=ParseMode.HTML,
        reply_markup=_results_kb(results, page=page, exhausted=exhausted, status_msg_id=msg_id),
    )


# ── Plain-text auto-search ────────────────────────────────────────────────────
# Auto-detect for messages that clearly mean "search YouTube for X" — narrowed
# to actual "yt"/"youtube"/"search" trigger words (previously any of ~30
# generic media keywords like "song"/"video"/"movie"/"dance" fired this, which
# could misfire on ordinary chat that just happened to mention one of those
# words). Now it only fires when the message itself names the feature, and
# goes straight to search — no extra "pick an engine" tap, since there's only
# one engine wired up here anyway.
_URL_HINT = filters.regex(r"https?://|www\.|t\.me/|magnet:\?")

_YT_TRIGGER = r"(?:yt|yts|youtube)"
_YT_SEARCH_PATTERNS = [
    re.compile(rf"(?i)^\s*{_YT_TRIGGER}\s+search\s+(.+)$"),           # "yt search <q>" / "youtube search <q>"
    re.compile(rf"(?i)^\s*search\s+(.+?)\s+on\s+{_YT_TRIGGER}\s*$"),  # "search <q> on yt"
    re.compile(rf"(?i)^\s*{_YT_TRIGGER}\s+(.+)$"),                    # "yt <q>" / "youtube <q>"
    re.compile(rf"(?i)^\s*(.+?)\s+on\s+{_YT_TRIGGER}\s*$"),           # "<q> on yt"
    re.compile(r"(?i)^\s*search\s+(.+)$"),                            # "search <q>"
]


def _extract_yt_query(text: str) -> str | None:
    """Pulls the actual search term out of a natural "yt search X" style
    message, trying each phrasing in turn. Returns None if none match (so
    the message just falls through as ordinary chat)."""
    stripped = text.strip()
    for pat in _YT_SEARCH_PATTERNS:
        m = pat.match(stripped)
        if m:
            q = m.group(1).strip()
            if q:
                return q
    return None


# Cheap filter-level pre-check (word boundary, case-insensitive) before the
# more precise _extract_yt_query() parsing runs in the handler itself.
_YT_SEARCH_HINT = filters.regex(r"(?i)\b(?:yt|yts|youtube|search)\b")


@Client.on_message(
    filters.text & filters.private
    & ~filters.regex(r"^/")
    & ~_URL_HINT
    & _YT_SEARCH_HINT,
    group=9,
)
async def _plain_text_search(client: Client, message: Message):
    text = (message.text or "")
    query = _extract_yt_query(text)
    if not query or not (2 <= len(query) <= 120):
        return
    await _do_search(client, message, query)
