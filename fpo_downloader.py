"""
FPO.XXX video downloader engine.

fpo.xxx runs on "KVS" (Kernel Video Sharing), a tube-site CMS completely
different from Faphouse's HLS/m3u8 setup: each video page embeds a player
(/embed/<id>) whose JS config ("flashvars") carries one progressive MP4
URL per available resolution. Those URLs are themselves scrambled — a
video_url that starts with "function/0/" has a 32-character block, deep
in the path, whose characters have been permuted; the correct order
depends on a "license_code" also present in flashvars. This is a known,
publicly-documented technique used by this CMS across many tube sites
(not something specific to fpo.xxx) — the descramble is a fixed,
mechanical character-swap, not a secret only fpo.xxx knows.

IMPORTANT — this module was written without being able to inspect
fpo.xxx's actual embed-page JavaScript (the tool used to research this
strips <script> tags), so the two things most likely to need adjusting
after a real test against the live site are:
  1. _FLASHVARS_RE / _extract_flashvars() — the exact way the flashvars
     object is written into the page may differ slightly (quoting style,
     variable name) from what's assumed here.
  2. Whether fpo.xxx's player version still uses this exact descramble
     scheme at all (KVS has had a few scheme revisions over the years).

LOGIN / PRIVATE VIDEOS (added for member-uploaded "private" videos — most
content on the site needs no login at all, but individual users can mark
their own uploads private, visible only to logged-in members):

fpo.xxx's login form is protected by Cloudflare Turnstile (a JS
challenge) — confirmed via a real screenshot of the login modal. A
plain requests.Session().post() can never solve that challenge, so
POSTing credentials directly (this module's old approach) will never
work no matter how correct the field names/endpoint are.

Instead, this uses SESSION-COOKIE REUSE: log into fpo.xxx once, normally,
in a real browser (which solves Turnstile the normal way a human does),
then export that session's cookies and set them as FPO_COOKIES below.
This module just attaches those cookies to its requests — it never
touches the login form or Turnstile itself. This is the same technique
yt-dlp's own --cookies flag is built around, not a bypass of anything.

FPO_COOKIES accepts either:
  - A raw "name=value; name2=value2" Cookie-header string (easiest to
    copy from a browser's DevTools -> Network tab -> any request to
    fpo.xxx -> Request Headers -> Cookie), or
  - The contents of a Netscape-format cookies.txt export (e.g. from a
    "Get cookies.txt" browser extension) — auto-detected by the
    "# Netscape HTTP Cookie File" header line.

Whichever kt_-prefixed session cookie(s) it grants (kt_member,
kt_acctoken, etc., alongside PHPSESSID) are what mark the session as
logged in server-side. Like any browser session, these expire — if
private videos start failing again after working for a while, the fix
is exporting a fresh cookie string the same way, not touching this code.

Kept free of any Telegram/Mongo/Pyrogram imports, same as
faphouse_downloader.py, so it can be dropped in as an alternative
backend selected purely by which domain a link belongs to.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse, urljoin

import requests
from dotenv import load_dotenv

# Loaded here directly (not just relying on config.py's load_dotenv())
# so FPO_COOKIES is wired into this module on its own, regardless of
# what else has or hasn't imported config.py yet.
load_dotenv()

logger = logging.getLogger(__name__)

BASE_URLS = {
    "fpo.xxx": "https://www.fpo.xxx",
    "www.fpo.xxx": "https://www.fpo.xxx",
}
DEFAULT_BASE_URL = "https://www.fpo.xxx"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Deliberately separate from faphouse_downloader.py's cookie/session
# setup — different site, different account.
#
# See the module docstring's LOGIN section — this is a browser session's
# cookies (either a raw "name=value; ..." header string, or a pasted
# Netscape cookies.txt export), NOT a username/password. Login POSTs are
# gone entirely since Turnstile makes them unworkable — see docstring.
FPO_COOKIES = os.environ.get("FPO_COOKIES", "")
# Mirrors faphouse_downloader.py's SESSION_MAX_AGE — periodically rebuild
# the Session object (re-applying FPO_COOKIES fresh) even though the
# cookie values themselves don't change here; this just bounds how long
# a single requests.Session's internal state is reused.
_SESSION_MAX_AGE = int(os.environ.get("FPO_SESSION_MAX_AGE", str(30 * 60)))
_session_state = {"session": None, "started_at": 0.0}

# Same debug-capture contract as faphouse_downloader.py — see that
# module's _DEBUG_DIR comment. Kept independent (own file) since a
# failure here is a different embed page than a faphouse.com failure.
_DEBUG_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
_DEBUG_HTML_PATH = os.path.join(_DEBUG_DIR, "debug_last_flashvars_fail.html")


def _save_debug_html(html: str):
    if not html:
        return
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        with open(_DEBUG_HTML_PATH, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
    except Exception as e:
        logger.warning(f"Couldn't save debug HTML: {e}")


def fetch_debug_html(video_url: str) -> str | None:
    """Just fetches the embed page's raw HTML — no flashvars extraction.
    Used by main.py's /debughtml admin command to pull the actual current
    embed markup straight from the live site for manual inspection."""
    video_id = _video_id_from_url(video_url)
    if not video_id:
        return None
    base_url = get_base_url(video_url) or DEFAULT_BASE_URL
    embed_url = f"{base_url}/embed/{video_id}"
    try:
        return _fetch_html(embed_url)
    except Exception as e:
        logger.warning(f"fetch_debug_html failed for {embed_url}: {e}")
        return None


def _parse_cookie_string(raw: str) -> dict:
    """Parses FPO_COOKIES into a plain {name: value} dict. Accepts either
    a browser-style "name=value; name2=value2" header, or a pasted
    Netscape cookies.txt export (the format that starts with the
    "# Netscape HTTP Cookie File" comment line — one cookie per line,
    tab-separated, name/value as the last two fields)."""
    raw = (raw or "").strip()
    if not raw:
        return {}

    if "Netscape HTTP Cookie File" in raw or raw.count("\t") > raw.count(";"):
        cookies = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            name, value = fields[5], fields[6]
            if name:
                cookies[name] = value
        return cookies

    cookies = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies[name.strip()] = value.strip()
    return cookies


def set_cookies(raw: str) -> int:
    """Updates FPO_COOKIES at runtime (used by the /setcookies admin
    command) instead of requiring an env var edit + redeploy every time a
    session expires. Also drops the cached Session so _ensure_session()
    rebuilds with the new cookies on its very next call, rather than
    waiting up to _SESSION_MAX_AGE for the periodic rebuild to notice.

    Returns how many individual cookies were parsed out of `raw` (0 if it
    didn't look like either supported format), so the caller can tell the
    admin whether it actually took."""
    global FPO_COOKIES
    FPO_COOKIES = (raw or "").strip()
    _session_state["session"] = None
    return len(_parse_cookie_string(FPO_COOKIES))


def get_cookie_count() -> int:
    """How many cookies are currently attached (0 if none set/expired)."""
    return len(_parse_cookie_string(FPO_COOKIES))


def _ensure_session() -> requests.Session:
    """Builds (or reuses) a requests.Session with FPO_COOKIES attached —
    see the module docstring's LOGIN section for why this is cookie
    reuse rather than a login POST. Does nothing at all (returns a bare
    Session) if FPO_COOKIES isn't set, same as before for the ~all
    content that needs no login."""
    now = time.time()
    expired = _session_state["session"] is not None and (now - _session_state["started_at"] > _SESSION_MAX_AGE)
    if _session_state["session"] is None or expired:
        session = requests.Session()
        session.headers.update({"User-Agent": _UA})
        cookies = _parse_cookie_string(FPO_COOKIES)
        if cookies:
            for name, value in cookies.items():
                session.cookies.set(name, value, domain=".fpo.xxx")
            logger.info(f"fpo.xxx session: attached {len(cookies)} cookie(s) from FPO_COOKIES")
        else:
            logger.info("fpo.xxx session: no FPO_COOKIES set — private/member-only videos won't be reachable")
        _session_state.update({"session": session, "started_at": now})
    return _session_state["session"]


def get_base_url(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return BASE_URLS.get(host)
    except Exception:
        return None


def is_fpo_link(url: str) -> bool:
    return get_base_url(url) is not None


_URL_RE = re.compile(r"https?://\S+")


def extract_fpo_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links: pull
    out every fpo.xxx URL found in text, in order, de-duplicated."""
    if not text:
        return []
    seen = set()
    links = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_fpo_link(url) and url not in seen:
            seen.add(url)
            links.append(url)
    return links


# Matches "/video/<id>/<slug>/" — the numeric id is what /embed/<id> wants.
_VIDEO_ID_RE = re.compile(r"/video/(\d+)/")

# Matches a performer listing page, e.g. "/models/sarah-arabic/".
MODEL_PATH_RE = re.compile(r"^/models/([a-z0-9-]+)/?$", re.IGNORECASE)


def _video_id_from_url(video_url: str) -> str | None:
    m = _VIDEO_ID_RE.search(urlparse(video_url).path)
    return m.group(1) if m else None


def _fetch_html(url: str) -> str:
    session = _ensure_session()
    r = session.get(url, timeout=15, headers={"User-Agent": _UA, "Referer": DEFAULT_BASE_URL})
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------
# flashvars extraction
# ---------------------------------------------------------------------

# KVS embed pages set up their player with a call like:
#   var flashvars = {"video_id":"123", "video_url":"...", ...};
# somewhere in a <script> block. Some KVS installs instead namespace the
# variable per-video (e.g. "var flashvars_591654 = {...};") rather than
# using a bare "flashvars" name — a real, documented variation across
# this CMS family's many deployments, not specific to fpo.xxx — so the
# optional "_<digits>" suffix is matched too.
_FLASHVARS_RE = re.compile(r"flashvars(?:_\d+)?\s*=\s*(\{.*?\})\s*;", re.DOTALL)


def _js_object_to_json(blob: str) -> str:
    """Best-effort conversion of a JS object literal (unquoted/single-quoted
    keys, single-quoted string values, trailing commas) into valid JSON.
    Not a full JS parser — just enough for the flat, string-valued
    flashvars objects KVS emits."""
    # Normalize single-quoted keys/values to double-quoted. This is safe
    # here because flashvars values are URLs/ids/labels with no embedded
    # quotes or escapes in practice.
    blob = re.sub(r"'([^'\\]*)'", lambda m: json.dumps(m.group(1)), blob)
    # Quote any remaining bare keys (word chars followed by a colon).
    blob = re.sub(r"([{,]\s*)([A-Za-z0-9_]+)\s*:", r'\1"\2":', blob)
    # Drop trailing commas before a closing brace.
    blob = re.sub(r",\s*}", "}", blob)
    return blob


def _extract_flashvars(html: str) -> dict:
    match = _FLASHVARS_RE.search(html)
    if not match:
        _save_debug_html(html)
        raise RuntimeError("Couldn't find flashvars on the embed page — "
                            "fpo.xxx's player markup may not match what this was written against. "
                            f"Raw HTML saved to {_DEBUG_HTML_PATH} for inspection "
                            "(or use /debughtml in the bot).")
    raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(_js_object_to_json(raw))





_HASH_LENGTH = 32


def _kvs_license_token(license_code: str) -> str:
    """Ported verbatim (algorithm-for-algorithm) from yt-dlp's generic KVS
    extractor (_extract_kvs -> getlicensetoken) — a public, MIT-licensed
    implementation used across the many, mostly non-adult sites that run
    on this CMS. Not reverse-engineered here; this is the same published
    algorithm, confirmed present in the reference project's own
    ytdl_legacy/extractor/generic.py."""
    modlicense = license_code.replace("$", "").replace("0", "1")
    center = len(modlicense) // 2
    fronthalf = int(modlicense[:center + 1])
    backhalf = int(modlicense[center:])
    modlicense = str(4 * abs(fronthalf - backhalf))

    parts = []
    for o in range(0, center + 1):
        for i in range(1, 5):
            parts.append(str((int(license_code[o + i]) + int(modlicense[o])) % 10))
    return "".join(parts)


def _kvs_unscramble(hash_block: str, license_token: str) -> str:
    """Ported verbatim from the same yt-dlp source as _kvs_license_token
    above (getrealurl's inner 'spells' permutation) — a plain swap of
    positions o/l per step produces the exact same result as the
    dict-based generator yt-dlp uses, just written more plainly."""
    chars = list(hash_block)
    for o in range(len(chars) - 1, -1, -1):
        l = (o + sum(int(n) for n in license_token[o:])) % 32
        chars[o], chars[l] = chars[l], chars[o]
    return "".join(chars)


def _resolve_video_url(scrambled_url: str, license_code: str) -> str:
    if not scrambled_url.startswith("function/0/"):
        return scrambled_url  # not obfuscated on this install/version

    parsed = urlparse(scrambled_url[len("function/0/"):])
    license_token = _kvs_license_token(license_code)
    urlparts = parsed.path.split("/")

    # NOTE: which split index actually holds the 32-char hash block
    # (currently assumed to be index 3) is STILL UNVERIFIED against
    # fpo.xxx's real URL shape — this was written from the general KVS
    # get_file/ URL pattern, not a captured real example. yt-dlp's own
    # reference implementation uses a differently-shaped split (it slices
    # the raw "function/0/https://host/..." string directly instead of
    # re-parsing it with urlparse first), so its index isn't directly
    # comparable to this one. If unscrambling still produces a 404/broken
    # URL after this fix, this is the next thing to check — capture one
    # real scrambled video_url via /debughtml and print urlparts to see
    # which element is actually the hash.
    hash_block = urlparts[3][:_HASH_LENGTH]
    unscrambled = _kvs_unscramble(hash_block, license_token)
    urlparts[3] = unscrambled + urlparts[3][_HASH_LENGTH:]

    return parsed._replace(path="/".join(urlparts)).geturl()


# ---------------------------------------------------------------------
# Public interface — mirrors faphouse_downloader.py's shape so main.py /
# auto_scraper.py can dispatch to either module interchangeably.
# ---------------------------------------------------------------------

def _probe_height(url: str, referer: str = None) -> int | None:
    """Actual pixel height of a resolved video URL, via ffprobe. fpo.xxx's
    flashvars carry no per-quality label at all (confirmed against a real
    embed page — just a bare video_url, no "_text" field, no resolution
    hint anywhere), so guessing a label from the data was never going to
    work; asking the file itself is the only reliable option here.

    Needs a Referer + User-Agent header, same as every other request this
    module makes to fpo.xxx's CDN (see download_video()'s docstring for
    why: the CDN checks that a request looks like it came from a real
    browser in-session, not a bare script) — a header-less ffprobe call
    was silently 403ing on every probe, so BOTH of a video's candidate
    URLs (video_url/video_alt_url — normally two genuinely different
    resolutions) always came back height=None and fell through to the
    same generic "Best available" label, showing as two identical
    buttons with no way to tell them apart (this is that fix)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    headers = f"Referer: {referer}\r\nUser-Agent: {_UA}\r\n" if referer else f"User-Agent: {_UA}\r\n"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-headers", headers, "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=20,
        )
        return int(result.stdout.strip())
    except Exception:
        return None


class FanclubLockedError(RuntimeError):
    """Raised when a video's embed page shows signs of being private/
    member-only and no working FPO_COOKIES session is attached to see it
    anyway. Same attribute name auto_scraper.py's process_and_upload_video
    already generically looks for via getattr(backend, "FanclubLockedError",
    ()) — see that function's docstring — so defining it here is enough on
    its own for auto-upload to permanently skip these instead of endlessly
    retrying an embed page that will never yield a real video URL without
    a login it doesn't have."""
    pass


# Phrases a KVS-CMS site (see module docstring) commonly shows on a
# private/member-locked video's embed page instead of a real player —
# NOT confirmed against fpo.xxx's actual current markup (this module was
# written without being able to inspect it — see the module docstring's
# opening disclosure), so this is only trusted as a private-video signal
# when COMBINED with flashvars having no usable video_url/video_alt_url
# entries at all (see _fetch_flashvars below) — on its own, matching one
# of these phrases proves nothing (could be boilerplate elsewhere on the
# page); the empty-flashvars condition is the actual real signal, this
# just adds confidence about WHY before blaming it on a private video
# instead of a markup-format change bug.
_PRIVATE_VIDEO_MARKERS = (
    "this video is private", "private video", "members only", "member only",
    "members-only", "login to view", "log in to view", "you must be logged in",
    "sign in to view", "restricted video", "video unavailable",
)


def _looks_private(embed_html: str) -> bool:
    lowered = (embed_html or "").lower()
    return any(marker in lowered for marker in _PRIVATE_VIDEO_MARKERS)


def _fetch_flashvars(video_url: str) -> tuple[dict, str]:
    """Returns (flashvars, page_url) — shared by get_available_qualities
    and get_page_meta so both work off the same single page fetch logic.

    Tries the ORIGINAL page URL (video_url itself, the /video/<id>/<slug>/
    page) first, falling back to constructing /embed/{id} only if that
    page doesn't carry the flashvars. A KVS-based reference implementation
    (yt-dlp's own GenericIE._extract_kvs) extracts flashvars straight from
    whatever page it's given — the original page itself, not a separately
    constructed /embed/ URL — which is what pointed at this: always
    forcing the fetch through /embed/ would break resolution if the
    site's /embed/ behavior ever changes (redirect removed, page shape
    changed, blocked, etc.) even while the original page still carries
    the same flashvars markup it always did. Falling back to /embed/
    when the original page doesn't have it means this can only add a
    chance of success, never remove the one that already worked."""
    video_id = _video_id_from_url(video_url)
    if not video_id:
        raise RuntimeError(f"Couldn't find a numeric video id in {video_url!r}")
    base_url = get_base_url(video_url) or DEFAULT_BASE_URL

    def _has_video_url(fv: dict) -> bool:
        return any(
            re.match(r"^video_(?:url|alt_url\d*)$", key) and fv.get(key)
            for key in fv
        )

    page_html = _fetch_html(video_url)
    flashvars = _extract_flashvars(page_html)
    page_url = video_url

    if not _has_video_url(flashvars):
        embed_url = f"{base_url}/embed/{video_id}"
        embed_html = _fetch_html(embed_url)
        embed_flashvars = _extract_flashvars(embed_html)

        if _has_video_url(embed_flashvars):
            flashvars, page_url, page_html = embed_flashvars, embed_url, embed_html
        elif _looks_private(page_html) or _looks_private(embed_html):
            raise FanclubLockedError(
                f"{video_url} looks private/members-only and no working FPO_COOKIES "
                "session is attached — see fpo_downloader.py's module docstring for "
                "how to set one, if this account should actually have access."
            )
        else:
            # Neither the original page nor /embed/ had usable flashvars,
            # and neither looked like a private/members-only gate either
            # — keep the /embed/ attempt (what downstream error
            # messages/get_page_meta's fallback already expect a page
            # from) so the caller still has something real to diagnose
            # from instead of an empty original-page fetch.
            flashvars, page_url = embed_flashvars, embed_url

    return flashvars, page_url


def get_available_qualities(video_url: str) -> list:
    """Same contract as faphouse_downloader.get_available_qualities():
    [{"label": "720p", "height": 720, "url": ...}, ...], best-first.

    "url" here is NOT the resolved direct CDN link (that was the bug —
    see download_video()'s docstring for why passing one of those
    straight to yt-dlp as its target gets a 403: yt-dlp's KVS extractor
    only knows how to parse flashvars out of the ORIGINAL PAGE, not a
    URL that's already past that step). It's just this height as a
    plain string ("720", "480", ...), or "best" for the no-known-height
    fallback — download_video() turns that back into a yt-dlp format
    selector and always hands yt-dlp the real page URL."""
    flashvars, embed_url = _fetch_flashvars(video_url)
    license_code = flashvars.get("license_code", "")
    referer = get_base_url(video_url) or DEFAULT_BASE_URL

    candidate_urls = []
    key_pattern = re.compile(r"^video_(?:url|alt_url\d*)$")
    for key in flashvars:
        if not key_pattern.match(key):
            continue
        raw_url = flashvars.get(key)
        if not raw_url or "/get_file/" not in raw_url:
            continue
        resolved = urljoin(embed_url, _resolve_video_url(raw_url, license_code))
        candidate_urls.append(resolved)

    if not candidate_urls:
        raise RuntimeError("No downloadable video_url/video_alt_url entries found in flashvars.")

    variants = []
    seen_labels = set()
    for url in candidate_urls:
        height = _probe_height(url, referer=referer)
        label = f"{height}p" if height else "Best available"
        if label in seen_labels:
            # Same label as one already added (genuinely the same
            # resolution on both video_url/video_alt_url, or the probe
            # still failed for this one too even with headers) — skip
            # rather than show two identical, indistinguishable buttons.
            continue
        seen_labels.add(label)
        variants.append({"label": label, "height": height, "url": str(height) if height else "best"})

    variants.sort(key=lambda v: (v["height"] or 0), reverse=True)
    return variants


def get_page_meta(video_url: str) -> dict:
    """Same contract as faphouse_downloader.get_page_meta(). fpo.xxx's
    flashvars has no duration field at all (confirmed against a real
    embed page earlier), so duration always comes back None here — the
    caller already shows "Unknown" for that, same as any other backend
    that doesn't know a given field.

    Poster picks the biggest preview_urlN available (preview_heightN
    tells us which is which) rather than always preview_url, since a
    couple of these are just tiny scrubber-strip thumbnails."""
    try:
        flashvars, embed_url = _fetch_flashvars(video_url)
    except Exception as e:
        logger.warning(f"get_page_meta failed for {video_url}: {e}")
        return {"title": None, "author": None, "duration": None, "poster_url": None}

    poster_candidates = []
    for key, val in flashvars.items():
        m = re.match(r"^preview_height(\d*)$", key)
        if not m:
            continue
        url_key = f"preview_url{m.group(1)}"
        if flashvars.get(url_key):
            try:
                poster_candidates.append((int(val), urljoin(embed_url, flashvars[url_key])))
            except (TypeError, ValueError):
                continue
    poster_url = max(poster_candidates, key=lambda p: p[0])[1] if poster_candidates else flashvars.get("preview_url")

    return {
        "title": flashvars.get("video_title"),
        "author": flashvars.get("video_models") or None,
        "duration": None,
        "poster_url": poster_url,
    }


_aria2c_ok = None


def _aria2c_available() -> bool:
    """Same technique as ytdlp_downloader.py's _aria2c_available() and
    "src"'s Akbots/aria2_dl.py — aria2c splits one file across 4 parallel
    connections instead of fpo_downloader's plain single-connection
    requests.get(stream=True) below, which is the actual "fast download"
    difference on most CDNs (they throttle per-connection, not per-file).
    Cached after the first check."""
    global _aria2c_ok
    if _aria2c_ok is not None:
        return _aria2c_ok
    _aria2c_ok = shutil.which("aria2c") is not None
    return _aria2c_ok


_ARIA2_LINE_RE = re.compile(
    r"\[#\S+\s+([\d.]+\S*)/([\d.]+\S*)\((\d+)%\).*?(?:DL:(\S+))?.*?(?:ETA:(\S+))?\]"
)
_SIZE_UNIT_RE = re.compile(r"([\d.]+)\s*([KMGT]i?B)?", re.IGNORECASE)
_UNIT_MULT = {"KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
              "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _size_to_bytes(s):
    if not s:
        return 0
    m = _SIZE_UNIT_RE.match(s.strip())
    if not m:
        return 0
    num, unit = m.groups()
    try:
        return int(float(num) * _UNIT_MULT.get((unit or "").upper(), 1))
    except ValueError:
        return 0


def _aria2c_download(target_url: str, out_path: str, referer: str, cookie_header: str, on_progress, start_time) -> None:
    """Blocking — shells out to aria2c (4 parallel connections + resume),
    parsing its periodic summary line for progress the same way "src"'s
    Akbots/torrent.py._parse_aria2_line does. Raises RuntimeError on
    failure so the caller's existing except/fallback logic doesn't need
    to know this is a different code path from the plain requests one."""
    out_dir = os.path.dirname(out_path) or "."
    out_name = os.path.basename(out_path)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        "aria2c", f"--dir={out_dir}", f"--out={out_name}",
        "--continue=true", "--max-tries=5", "--retry-wait=3",
        "--max-connection-per-server=4", "--split=4", "--min-split-size=1M",
        "--summary-interval=1", "--console-log-level=warn",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        f"--user-agent={_UA}", f"--referer={referer}",
    ]
    if cookie_header:
        cmd.append(f"--header=Cookie: {cookie_header}")

    process = subprocess.Popen(cmd + [target_url], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1)
    tail_lines = []
    for line in process.stdout:
        line = line.strip()
        tail_lines.append(line)
        if len(tail_lines) > 30:
            tail_lines.pop(0)
        if on_progress:
            m = _ARIA2_LINE_RE.search(line)
            if m:
                done, total, pct, speed, eta = m.groups()
                elapsed = time.time() - start_time
                on_progress({
                    "pct": float(pct),
                    "downloaded_bytes": _size_to_bytes(done),
                    "speed_bytes_s": _size_to_bytes(speed) if speed else 0,
                    "eta_s": None,
                    "elapsed_s": elapsed,
                    "duration_s": 0,
                })
    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"aria2c exited with code {process.returncode}: {' | '.join(tail_lines[-5:])}")


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """Downloads an fpo.xxx video using yt-dlp with cookie injection.

    The old approach (flashvars → descramble → direct requests.get() on the
    resolved MP4 URL) reliably produced HTTP 403 because fpo.xxx's CDN checks
    that the download request originates from a valid, in-session browser —
    a plain requests.Session can't replicate that fully even with cookies.

    yt-dlp's generic KVS extractor handles the same flashvars descrambling
    internally AND sends the request with the correct browser fingerprint /
    headers that the CDN accepts — so delegating to yt-dlp fixes the 403
    without any changes to the flashvars logic here.

    FPO_COOKIES are passed to yt-dlp via a temp Netscape cookies file so
    private/member videos still work (same session-cookie-reuse approach,
    just carried through yt-dlp's --cookies flag instead of requests).

    FIXED: yt-dlp's target is now ALWAYS video_url (the actual page) — it
    used to be stream_url when one was given, which briefly produced the
    exact same 403 this whole yt-dlp rewrite exists to avoid: a resolved
    /get_file/... link (get_available_qualities()'s old "url" field) isn't
    a page yt-dlp's KVS extractor can parse flashvars out of, so it fell
    through to yt-dlp's generic extractor, which fetches the URL as a
    webpage rather than a download — exactly the browser-fingerprint
    mismatch this function's whole approach was built to avoid, just
    reintroduced one level up. stream_url is now interpreted as a target
    HEIGHT ("720", "480", ...) or "best" (see get_available_qualities()'s
    docstring) and turned into a yt-dlp format selector instead."""
    try:
        import yt_dlp
        from yt_dlp.networking.impersonate import ImpersonateTarget
        _impersonate_target = ImpersonateTarget.from_str("chrome")
    except ImportError:
        raise RuntimeError("yt-dlp is not installed. Add 'yt-dlp[default]' to requirements.txt.")
    except Exception:
        _impersonate_target = None

    # Check if curl_cffi chrome impersonation is actually available on this host.
    # fpo.xxx is behind Cloudflare — without impersonation, yt-dlp's urllib
    # gets blocked immediately (cf-browser-verification / 403). Same guard
    # as ytdlp_downloader.py's _impersonate_available().
    _impersonate_ok = False
    if _impersonate_target is not None:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _ydl:
                if _ydl._impersonate_target_available(_impersonate_target):
                    _impersonate_ok = True
        except Exception:
            pass

    target = video_url
    if stream_url and stream_url.isdigit():
        format_selector = f"bestvideo[height<={stream_url}]+bestaudio/best[height<={stream_url}]/best"
    else:
        format_selector = "bestvideo+bestaudio/best"
    start_time = time.time()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # Write FPO_COOKIES to a temp Netscape cookies file so yt-dlp can use them.
    cookie_file = None
    cookies = _parse_cookie_string(FPO_COOKIES)
    if cookies:
        import tempfile
        tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        tf.write("# Netscape HTTP Cookie File\n")
        for name, value in cookies.items():
            tf.write(f".fpo.xxx\tTRUE\t/\tFALSE\t0\t{name}\t{value}\n")
        tf.flush()
        tf.close()
        cookie_file = tf.name

    last_progress = [{}]

    def _ytdlp_progress_hook(d):
        if not on_progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            elapsed = time.time() - start_time
            pct = (downloaded / total * 100) if total else None
            eta = d.get("eta")
            prog = {
                "pct": pct,
                "downloaded_bytes": downloaded,
                "speed_bytes_s": speed,
                "eta_s": eta,
                "elapsed_s": elapsed,
                "duration_s": 0,
            }
            last_progress[0] = prog
            on_progress(prog)

    # out_path already has the desired filename/extension — tell yt-dlp to
    # write exactly there (outtmpl handles this; yt-dlp will append .part
    # during download then rename, same as usual).
    ydl_opts = {
        "outtmpl": out_path,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_ytdlp_progress_hook],
        # fpo.xxx is behind Cloudflare — plain urllib gets blocked.
        # Same impersonate guard as ytdlp_downloader.py.
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "http_headers": {
            "User-Agent": _UA,
            "Referer": DEFAULT_BASE_URL,
        },
        "format": format_selector,
        "merge_output_format": "mp4",
        "noplaylist": True,
    }
    if _impersonate_ok:
        ydl_opts["impersonate"] = _impersonate_target
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([target])
    except Exception as e:
        raise RuntimeError(f"yt-dlp download failed for {target}: {e}") from e
    finally:
        if cookie_file and os.path.exists(cookie_file):
            try:
                os.unlink(cookie_file)
            except Exception:
                pass

    # yt-dlp may add/change the extension — check for the exact path first,
    # then fall back to a glob in case it renamed the file.
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path, time.time() - start_time

    import glob
    base = os.path.splitext(out_path)[0]
    candidates = glob.glob(f"{base}.*")
    for c in sorted(candidates, key=os.path.getsize, reverse=True):
        if os.path.getsize(c) > 0:
            logger.info(f"yt-dlp wrote to {c} instead of {out_path} — using that.")
            return c, time.time() - start_time

    raise RuntimeError("yt-dlp reported success but the output file is missing/empty.")


# ─────────────────────────────────────────────────────────────────────────────
#  Listing / scraping helpers — used by auto_scraper.py's fpo_uploader_worker
#  and fpo_live_monitor. Mirror the exact function contract that eporner_scraper
#  exposes so auto_scraper.py can call fpo_downloader the same way.
# ─────────────────────────────────────────────────────────────────────────────

import random as _random

_VIDEO_LISTING_RE = re.compile(
    r'<a[^>]+href="(/video/(\d+)/[^"]+)"[^>]*>',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r'<(?:h3|div)[^>]+class="[^"]*(?:title|video-title)[^"]*"[^>]*>\s*<a[^>]+>([^<]+)</a>', re.IGNORECASE)

_BROAD_TERMS_FPO = [
    "teen", "milf", "amateur", "asian", "latina", "hardcore",
    "lesbian", "anal", "pov", "blonde", "brunette", "creampie",
    "big-tits", "public", "homemade", "threesome", "mature", "solo",
]


def _parse_listing_page(html_text: str, base_url: str) -> list[dict]:
    """Extract video items from an fpo.xxx HTML listing page.
    Returns list of {"slug", "url", "title"} — same contract as eporner_scraper."""
    items = []
    seen_ids = set()
    for m in _VIDEO_LISTING_RE.finditer(html_text):
        path, video_id = m.group(1), m.group(2)
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        full_url = f"{base_url}{path}" if path.startswith("/") else path
        slug = f"fpo-{video_id}"
        items.append({"slug": slug, "url": full_url, "title": slug})
    return items


def _scrape_page(url: str) -> tuple[list[dict], int]:
    """Fetch one listing page and return (items, total_pages).
    total_pages is estimated from the last page-link found, or 1 if unparseable."""
    try:
        html_text = _fetch_html(url)
    except Exception as e:
        raise RuntimeError(f"fpo.xxx listing fetch failed for {url}: {e}") from e

    base = DEFAULT_BASE_URL
    items = _parse_listing_page(html_text, base)

    # Detect total pages from pagination links like /page/42/ or ?page=42
    page_nums = re.findall(r'[?&/]page[=/](\d+)', html_text)
    total_pages = max((int(p) for p in page_nums), default=1)
    if not items:
        total_pages = 1  # empty page → we're past the end

    return items, total_pages


def get_model_page_videos(model_name: str, page: int = 1) -> tuple[list[dict], int]:
    """Fetch one page of a performer's video listing from fpo.xxx.

    model_name can be either:
      - A performer slug like "sarah-arabic" (looked up at /models/<slug>/)
      - A full fpo.xxx model page URL (used as-is, with ?page= appended)

    Returns (items, total_pages) — same contract as eporner_scraper."""
    if model_name.startswith("http"):
        base = model_name.rstrip("/")
    else:
        slug = re.sub(r"[^a-z0-9-]", "-", model_name.lower().strip()).strip("-")
        base = f"{DEFAULT_BASE_URL}/models/{slug}/"
    url = f"{base}?page={page}" if page > 1 else base
    return _scrape_page(url)


def get_random_page_videos() -> list[dict]:
    """Fetch a random page of fpo.xxx videos — used by fpo_live_monitor
    and fpo_uploader_worker's random mode. Rotates through a handful of
    category pages and random page offsets so the same videos don't repeat
    every cycle."""
    category = _random.choice(_BROAD_TERMS_FPO)
    page = _random.randint(1, 15)
    url = f"{DEFAULT_BASE_URL}/categories/{category}/?mode=latest&page={page}"
    try:
        items, _ = _scrape_page(url)
        return items
    except Exception as e:
        logger.warning(f"fpo.xxx random page fetch failed ({url}): {e}")
        return []


def get_latest_videos() -> list[dict]:
    """Fetch fpo.xxx's newest videos from the front page — used by
    fpo_live_monitor to detect and push newly published content.
    Returns a deduplicated flat list of {"slug","url","title"} items."""
    merged: dict[str, dict] = {}
    for url in [
        f"{DEFAULT_BASE_URL}/?mode=latest",
        f"{DEFAULT_BASE_URL}/categories/amateur/?mode=latest",
    ]:
        try:
            items, _ = _scrape_page(url)
            for it in items:
                merged.setdefault(it["slug"], it)
        except Exception as e:
            logger.warning(f"fpo.xxx get_latest_videos failed for {url}: {e}")
    return list(merged.values())
