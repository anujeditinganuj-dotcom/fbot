"""
yt-dlp-backed downloader for sites with a dedicated, well-tested
extractor in the real `yt-dlp` package (PyPI: yt-dlp) — eporner.com,
pornhub.com, xhamster.com, xnxx.com, xvideos.com, spankbang.com,
youporn.com, beeg.com — used instead of porn_fetch_downloader.py's route
for all of these.

Grew from eporner_downloader.py (eporner-only) -> also pornhub -> now
also xhamster/xnxx/xvideos/spankbang/youporn/beeg, moved over from
porn_fetch_downloader.py's SITE_REGISTRY (EchterAlsFake's per-site
GitHub packages) once those packages' calling conventions turned out
wrong repeatedly in production (wrong constructor kwargs, wrong
download() signature guesses, three separate rounds of "confirmed wrong,
fixed again") — yt-dlp is the same actively-maintained, extremely widely
used PyPI package used for every site that doesn't need something more
site-specific, with real dedicated extractors for all of the above
(verified via yt_dlp.extractor.gen_extractors() — XHamster, XNXX,
XVideos, SpankBang, YouPorn, Beeg all present). None of the
download/quality/metadata logic below is site-specific — adding another
yt-dlp-supported site going forward is just adding its host pattern to
HOST_PATTERNS.

porn_fetch_downloader.py is still used for xfreehd.com only now — the
one site among the original 7 with no yt-dlp extractor at all.

Same function contract as faphouse_downloader.py / fpo_downloader.py, so
main.py's _downloader_for() dispatch doesn't need to know which backend
it's calling:
  is_supported_link(url) -> bool
  extract_supported_links(text) -> list[str]
  get_available_qualities(video_url) -> [{"label","height","url"}, ...] best-first
  download_video(video_url, out_path, on_progress=None, stream_url=None) -> (out_path, elapsed_s)
  get_page_meta(video_url) -> {"title","author","duration","poster_url"}
"""

import logging
import os
import re
import shutil
import time
from urllib.parse import urlparse

import config
import pot_provider

logger = logging.getLogger(__name__)

_aria2c_ok = None  # cached tri-state, same pattern as _impersonate_ok below

# Eporner-specific workaround — see _normalize_eporner_url()'s own
# docstring below for the full story (yt-dlp issue #16277, open/unfixed
# as of the version this bot was last checked against).
_EPORNER_ID_RE = re.compile(r"eporner\.com/(?:video-|embed/)([A-Za-z0-9]+)", re.IGNORECASE)


def _normalize_eporner_url(url: str) -> str:
    """Rewrites an eporner.com "watch page" URL
    (eporner.com/video-<id>/<title-slug>/) to its /embed/<id>/ form.

    BUG this works around: eporner.com changed its watch-page HTML at
    some point, and it no longer contains the `hash` value yt-dlp's
    EpornerIE extractor's regex expects — every single eporner.com link
    now fails with "ERROR: [Eporner] <id>: Unable to extract hash",
    regardless of which yt-dlp version is installed (confirmed: this is
    an upstream site-side breakage, not a stale/outdated yt-dlp build —
    see https://github.com/yt-dlp/yt-dlp/issues/16277, still open/
    unfixed). The community-confirmed workaround on that same issue is
    that the video's /embed/<id>/ URL still works fine — EpornerIE
    extracts it through a completely different, still-functional
    "downloading video JSON" code path instead of the broken hash-regex
    one. Rewriting the URL here means every caller in this module
    (get_page_meta, get_available_qualities, get_stream_url,
    download_video) benefits automatically without needing its own fix.

    A no-op for non-eporner URLs, and for a URL that's already in
    /embed/ form or otherwise doesn't match (fails open — the original
    URL is returned as-is rather than raising, so a future eporner.com
    URL-shape change just goes back to yt-dlp's normal behavior instead
    of breaking harder)."""
    if "eporner.com" not in url.lower():
        return url
    m = _EPORNER_ID_RE.search(url)
    if not m:
        return url
    return f"https://www.eporner.com/embed/{m.group(1)}/"


# Matches youtube.com/watch|shorts|live, youtu.be/..., youtube-nocookie.com —
# used only to decide whether _base_opts() needs YouTube's extractor_args
# below (HOST_PATTERNS/is_supported_link above is unrelated — YouTube isn't
# one of those 8 sites, it's always routed here via is_generically_supported()).
_YOUTUBE_RE = re.compile(
    r"(?:^|\.)(?:youtube(?:-nocookie)?\.com|youtu\.be)$", re.IGNORECASE
)


def _is_youtube(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    return bool(_YOUTUBE_RE.search(host))


def _cookies_for(url: str):
    """Netscape-format cookies.txt path for this URL's site, or None.
    Ported from "src"'s ytdl.py _cookies_for() — same per-site env vars."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    if ("instagram.com" in host or "instagr.am" in host) and config.INSTA_COOKIES and os.path.exists(config.INSTA_COOKIES):
        return config.INSTA_COOKIES
    if ("facebook.com" in host or "fb.watch" in host) and config.FB_COOKIES and os.path.exists(config.FB_COOKIES):
        return config.FB_COOKIES
    if ("vk.com" in host or "vk.ru" in host) and config.VK_COOKIES and os.path.exists(config.VK_COOKIES):
        return config.VK_COOKIES
    if _is_youtube(url) and config.YT_COOKIES and os.path.exists(config.YT_COOKIES):
        return config.YT_COOKIES
    return None


def _aria2c_available() -> bool:
    """Same "src" technique for fast downloads: yt-dlp natively supports
    delegating the actual file transfer to an external aria2c process
    instead of its own (single-connection) built-in downloader. aria2c
    splits one file across multiple parallel connections
    (--max-connection-per-server / --split below) — often 2-4x faster
    than a single connection on CDNs that throttle per-connection
    bandwidth, which is most of them — and keeps a resume-capable control
    file, so a retried download continues instead of restarting from
    byte 0. Checked once and cached (matches _impersonate_available()'s
    pattern), not on every call — shutil.which() is cheap but there's no
    reason to repeat it per download."""
    global _aria2c_ok
    if _aria2c_ok is not None:
        return _aria2c_ok
    _aria2c_ok = shutil.which("aria2c") is not None
    if not _aria2c_ok:
        logger.info("ytdlp_downloader: aria2c not found on PATH — falling back to yt-dlp's built-in downloader (slower, single-connection).")
    return _aria2c_ok

try:
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
    # Same reason "src"'s ytdl.py pre-builds this once at import time
    # instead of passing the raw string "chrome" into YoutubeDL(...):
    # yt-dlp's Python API doesn't auto-convert a plain impersonate
    # string into an ImpersonateTarget the way its CLI does — passing
    # the string directly makes an internal assert fail with a
    # completely blank AssertionError() on every single YoutubeDL(...)
    # construction.
    _CHROME_IMPERSONATE_TARGET = ImpersonateTarget.from_str("chrome")
    # Facebook specifically: yt-dlp's generic/latest chrome impersonation
    # profile above still hits "[facebook] Cannot parse data" on a lot of
    # videos — confirmed as an ongoing, still-open upstream bug
    # (github.com/yt-dlp/yt-dlp/issues/15161, "impersonate needed"),
    # where Facebook's own request-fingerprint gatekeeping (their
    # "Tahoe" API) rejects newer Chrome TLS/JA3 fingerprints specifically,
    # but accepts an OLDER one — multiple people in that thread confirm
    # --impersonate "Chrome-99" (and only that, not a newer Chrome
    # target) gets past it. Kept as its own separate target rather than
    # just changing _CHROME_IMPERSONATE_TARGET everywhere, since the
    # other sites this module handles have no reason to also downgrade
    # to an older, more easily-fingerprinted-as-stale TLS profile.
    _CHROME_99_IMPERSONATE_TARGET = ImpersonateTarget.from_str("chrome-99")
except ImportError:
    yt_dlp = None
    ImpersonateTarget = None
    _CHROME_IMPERSONATE_TARGET = None
    _CHROME_99_IMPERSONATE_TARGET = None
    logger.error("yt-dlp isn't installed — ytdlp_downloader is disabled. Add yt-dlp[default] to requirements.txt.")

# Every site this module handles — eporner/pornhub were the original
# two; xhamster/xnxx/xvideos/spankbang/youporn/beeg moved over from
# porn_fetch_downloader.py (see module docstring for why). Each pattern
# covers that site's main domain plus its most common variant
# (premium/member subdomain, numbered mirror, etc.) the same way the
# faphouse_downloader/porn_fetch_downloader host patterns already did.
HOST_PATTERNS = (
    re.compile(r"(?:^|\.)eporner\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)pornhub(?:premium)?\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)xhamster(?:live)?\d*\.[a-z.]{2,}$", re.IGNORECASE),
    # xhaccess.com — an xHamster mirror domain that doesn't contain the
    # string "xhamster" itself, so the pattern above never matched it.
    # Same site/content, just a different front-door domain (like
    # xhamster2.com/xhamster18.com above), so it needs its own explicit
    # pattern rather than being folded into the xhamster one.
    re.compile(r"(?:^|\.)xhaccess\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)xnxx\d*\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)xvideos\d*\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)spankbang\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)you-?porn\d*\.[a-z.]{2,}$", re.IGNORECASE),
    re.compile(r"(?:^|\.)beeg\.[a-z.]{2,}$", re.IGNORECASE),
)
_URL_RE = re.compile(r"https?://\S+")

_impersonate_ok = None  # cached tri-state: None = not checked yet, True/False after
_impersonate_99_ok = None  # separate cache for the chrome-99 target (Facebook only)


def _impersonate_available() -> bool:
    """Same guard as "src"'s ytdl.py — on hosts where curl_cffi's chrome-
    impersonation binary isn't actually functional, setting
    opts["impersonate"] unconditionally makes every yt-dlp call fail
    immediately before it ever reaches the network. Checked once and
    cached, not on every call."""
    global _impersonate_ok
    if _impersonate_ok is not None:
        return _impersonate_ok
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _ydl:
            if not _ydl._impersonate_target_available(_CHROME_IMPERSONATE_TARGET):
                raise RuntimeError("chrome impersonate target not registered")
        _impersonate_ok = True
    except Exception as e:
        logger.info(f"ytdlp_downloader: curl_cffi chrome impersonation unavailable, falling back to plain requests: {e}")
        _impersonate_ok = False
    return _impersonate_ok


def _impersonate_99_available() -> bool:
    """Same check as _impersonate_available(), for the Facebook-specific
    chrome-99 target — curl_cffi doesn't build every impersonation
    profile into every install, so this can be False even when the
    generic "chrome" target above is True."""
    global _impersonate_99_ok
    if _impersonate_99_ok is not None:
        return _impersonate_99_ok
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as _ydl:
            if not _ydl._impersonate_target_available(_CHROME_99_IMPERSONATE_TARGET):
                raise RuntimeError("chrome-99 impersonate target not registered")
        _impersonate_99_ok = True
    except Exception as e:
        logger.info(f"ytdlp_downloader: curl_cffi chrome-99 impersonation unavailable (Facebook fix won't apply): {e}")
        _impersonate_99_ok = False
    return _impersonate_99_ok


def _base_opts(url: str = "") -> dict:
    """Shared yt-dlp options for both the metadata probe and the real
    download — ported from "src"'s ytdl.py _base_opts() (the generally-
    useful, site-agnostic parts; that function's YouTube/Instagram-
    specific branches don't apply here):
      - socket_timeout/retries/fragment_retries/extractor_retries: a
        slow-but-reachable CDN gets retried instead of failing on the
        very first hiccup (a real IP/geo-block still fails the same as
        before — no retry count fixes that, this only helps transient
        slowness).
      - impersonate: routes every request through curl_cffi's browser-
        TLS fingerprint instead of urllib's, which is what actually gets
        past a Cloudflare anti-bot challenge if either site ever put one
        in front of its pages — gated behind _impersonate_available() so
        this doesn't break the whole module on a host where curl_cffi's
        impersonation binary doesn't work.
      - cookiefile: per-site cookies (YouTube/Instagram/Facebook/VK) via
        _cookies_for() — fixes "Sign in to confirm you're not a bot" on
        some videos.
      - YouTube extractor_args (player_client/geo_bypass): BUG FIX —
        ported verbatim from "src"'s ytdl.py, which has this exact note:
        "do NOT skip dash/hls here — YouTube's real per-resolution
        streams (1080p/720p/480p/360p/...) are only exposed as separate
        DASH video-only formats. Skipping dash leaves only the old
        'combined' muxed formats, which today is usually just one
        low-res option — that's what was collapsing every YouTube link
        down to a single 'Best available' button instead of a real
        quality ladder." Without this block (the state this file was in
        before), yt-dlp's default client selection is exactly that
        collapsed case — matches the "sirf best quality deta hai, phir
        error aata hai" symptom this was fixing: the single format it
        does return often needs a PO token to actually download even
        though it listed fine, so the later download step 403s."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
    }
    if "eporner.com" in (url or ""):
        # Belt-and-suspenders alongside the aria2c exclusion above — a
        # missing Referer is another common reason a CDN link connects
        # but never sends data, so this is set explicitly here rather
        # than trusting the extractor's own per-format headers alone.
        opts["http_headers"] = {"Referer": "https://www.eporner.com/"}
    elif url and re.search(r"xhamster|xhaccess", url, re.I):
        # xHamster CDN checks Referer before serving the stream —
        # without it, the request connects but returns 0 bytes. Matches
        # xhaccess.com too (an xHamster mirror domain — see HOST_PATTERNS
        # above) since the CDN itself is the same regardless of which
        # front-door domain the link used.
        opts["http_headers"] = {"Referer": "https://xhamster.com/"}
    elif url and re.search(r"xvideos", url, re.I):
        # Same hotlink protection on xVideos CDN.
        opts["http_headers"] = {"Referer": "https://www.xvideos.com/"}
    cookies = _cookies_for(url) if url else None
    if cookies:
        opts["cookiefile"] = cookies
    if url and re.search(r"facebook\.com|fb\.watch", url, re.I) and _impersonate_99_available():
        # See _CHROME_99_IMPERSONATE_TARGET's definition above — Facebook
        # specifically needs the older chrome-99 fingerprint, not the
        # generic/latest one every other site here uses.
        opts["impersonate"] = _CHROME_99_IMPERSONATE_TARGET
    elif _impersonate_available():
        opts["impersonate"] = _CHROME_IMPERSONATE_TARGET
    if _aria2c_available() and "eporner.com" not in (url or ""):
        # Disabled specifically for eporner: its CDN links are
        # short-lived signed tokens, and splitting one into 4 parallel
        # range-requests (aria2c's whole speed advantage — see
        # _aria2c_available()'s docstring) appears to make it just hang
        # — TCP connects, then zero bytes ever arrive ("Waiting for
        # first data..." forever in the progress UI), no clean
        # error/rejection to react to. yt-dlp's own single-connection
        # downloader doesn't have this problem, so eporner falls back to
        # that; every other site here still gets aria2c's speed-up.
        # Same 4-connection + resume setup "src" uses (Akbots/aria2_dl.py) —
        # ported to yt-dlp's own external_downloader hook instead of a
        # separate hand-rolled subprocess wrapper, since yt-dlp already
        # has one built in and it's the only thing calling this here.
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {
            "aria2c": [
                "--max-connection-per-server=4", "--split=4", "--min-split-size=1M",
                "--continue=true", "--max-tries=5", "--retry-wait=3",
                "--summary-interval=1", "--console-log-level=warn",
            ]
        }
    # ── Speed optimizations (all non-YouTube sites including eporner) ──────
    if not _is_youtube(url):
        # Skip SSL cert verification — eporner/similar CDNs don't need it
        # and cert checks add a measurable round-trip on every request.
        opts["nocheckcertificate"] = True
        # Concurrent fragment download — HLS/DASH videos download faster
        # when segments are fetched in parallel (yt-dlp's built-in).
        opts["concurrent_fragment_downloads"] = 4
        # Reduce socket timeout for faster failure detection
        opts["socket_timeout"] = 15
        # Skip slow "is this a playlist" check for direct video links
        opts["noplaylist"] = True
        # Use only the site's dedicated extractor, skip Generic extractor
        # probing which adds 2-5s per request on unknown patterns
        opts["extract_flat"] = False

    if url and _is_youtube(url):
        opts["geo_bypass"] = True

        # PO-token ("web" client) support was removed entirely — see
        # config.py's note on this. "web" is the only client that exposes
        # the full 1080p/720p/480p/360p ladder, but it REQUIRES a valid PO
        # token or YouTube silently serves it a near-empty format list
        # (1-2 formats) instead of erroring — which is exactly what was
        # happening here, since the PO-token server was never actually
        # wired into yt-dlp's requests (pot_helper.py, imported by the
        # now-deleted start_pot_provider.py, never existed in this repo).
        #
        # PO-token support: pot_provider.py runs a local bgutil-ytdlp-
        # pot-provider HTTP server in the background (started once at bot
        # startup — see main.py). When it's actually up, "web" becomes
        # usable and exposes the full 1080p/720p/480p/360p (and, on
        # eligible videos, 2K/4K) ladder — that's the whole point of
        # running it. "tv" stays first in the list either way since it's
        # PO-token-free and still gives good formats as a floor.
        #
        # If the provider ISN'T up (still starting, or setup failed on
        # this host — see pot_provider.py's docstring for why that can
        # happen), "web" gets left out entirely rather than included
        # anyway: without a real token, YouTube serves "web" a near-empty
        # format list instead of erroring, which is the exact bug this
        # was fixed for the first time round (see the removed PO-token
        # flow this replaced, noted in config.py's history on this).
        if pot_provider.is_ready():
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["tv", "web", "ios", "mweb", "tv_embedded", "android"],
                }
            }
        else:
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["tv", "ios", "mweb", "tv_embedded", "android"],
                }
            }
    return opts


def is_supported_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return False
    return any(pattern.search(host) for pattern in HOST_PATTERNS)


def extract_supported_links(text: str) -> list[str]:
    """Same contract as faphouse_downloader.extract_faphouse_links()."""
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        if is_supported_link(url) and url not in seen:
            seen.add(url)
            out.append(url)
    return out


_extractor_classes = None


def _dedicated_extractors():
    """yt-dlp's ~1800 DEDICATED site extractors — deliberately excludes
    its catch-all "Generic" extractor, which matches literally any
    http(s) URL as a last resort and would make every random pasted link
    (a news article, a forum post, anything) look like a "supported
    video" if it weren't filtered out here. Built once and cached — yt-dlp
    has to import every extractor module to build this list, which isn't
    instant."""
    global _extractor_classes
    if _extractor_classes is None:
        _extractor_classes = [
            ie for ie in yt_dlp.extractor.gen_extractor_classes()
            if ie.ie_key() != "Generic"
        ]
    return _extractor_classes


def is_generically_supported(url: str) -> bool:
    """True if any of yt-dlp's dedicated extractors recognizes this URL —
    same "full yt-dlp support" idea as "src"'s urluploader.py generic
    fallback (has_quality_formats), but pure regex matching via each
    extractor's own suitable(url) classmethod instead of a real network
    probe, so it's cheap enough to run on every pasted link rather than
    only after everything else has already failed. Used as the last-
    resort backend in main.py's _downloader_for() — after faphouse/fpo/
    the 8 sites above/porn_fetch_downloader's xfreehd have all had first
    claim on a link, whatever's left over gets this generic yt-dlp check
    before finally giving up."""
    if yt_dlp is None:
        return False
    try:
        normalized = _normalize_url(url)
        return any(ie.suitable(normalized) for ie in _dedicated_extractors())
    except Exception:
        return False


def extract_generic_links(text: str) -> list[str]:
    """Same shape as extract_supported_links(), but for is_generically_
    supported() instead of the 8 hardcoded HOST_PATTERNS — kept as a
    separate function (not merged into extract_supported_links) since
    main.py only wants to pay _dedicated_extractors()'s one-time build
    cost when nothing more specific has already matched a link."""
    if not text or yt_dlp is None:
        return []
    seen, out = set(), []
    for match in _URL_RE.findall(text):
        url = match.rstrip(").,!?>'\"")
        normalized = _normalize_url(url)
        if normalized not in seen and is_generically_supported(normalized):
            seen.add(normalized)
            out.append(normalized)  # use normalized URL so yt-dlp can fetch it
    return out


def _require_yt_dlp():
    if yt_dlp is None:
        raise RuntimeError("yt-dlp isn't installed on this server — add yt-dlp to requirements.txt and redeploy.")


def _normalize_url(url: str) -> str:
    """Normalize share/redirect URLs to canonical form that yt-dlp extractors recognize.
    
    - facebook.com/share/r/<id>   → facebook.com/reel/<id>
    - facebook.com/share/v/<id>   → facebook.com/watch/?v=<id>  
    - facebook.com/share/<id>     → facebook.com/watch/?v=<id>
    - fb.watch/<id>               → unchanged (yt-dlp handles it)
    """
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        host = p.netloc.lower()
        path = p.path
        if "facebook.com" in host:
            # /share/r/<id> → /reel/<id>
            m = re.match(r"^/share/r/([A-Za-z0-9_-]+)/?$", path)
            if m:
                return urlunparse(p._replace(path=f"/reel/{m.group(1)}/", query=""))
            # /share/v/<id> or /share/<id> → /watch/?v=<id>
            m = re.match(r"^/share/(?:v/)?([A-Za-z0-9_-]+)/?$", path)
            if m:
                return urlunparse(p._replace(path="/watch/", query=f"v={m.group(1)}"))
    except Exception:
        pass
    return url


# ── Info-dict cache (avoids re-extracting same URL) ────────────────────────
import json as _json
import threading as _threading

_INFO_CACHE_FILE = os.path.join(os.environ.get("DOWNLOAD_DIR", "downloads"), "ytdlp_info_cache.json")
_INFO_CACHE_TTL  = int(os.environ.get("YTDLP_CACHE_TTL", str(4 * 3600)))  # 4 hours
_info_cache: dict = {}
_info_cache_lock = _threading.Lock()

def _load_info_cache():
    global _info_cache
    try:
        with open(_INFO_CACHE_FILE, "r") as f:
            data = _json.load(f)
        now = time.time()
        _info_cache = {k: v for k, v in data.items() if now - v.get("ts", 0) < _INFO_CACHE_TTL}
    except Exception:
        _info_cache = {}

def _save_info_cache():
    try:
        os.makedirs(os.path.dirname(_INFO_CACHE_FILE) or ".", exist_ok=True)
        with _info_cache_lock:
            snapshot = dict(_info_cache)
        with open(_INFO_CACHE_FILE, "w") as f:
            _json.dump(snapshot, f)
    except Exception as e:
        logger.warning(f"ytdlp info cache save failed: {e}")

_load_info_cache()


def _extract_info(video_url: str) -> dict:
    _require_yt_dlp()
    video_url = _normalize_eporner_url(video_url)

    # Check cache first (skip entire extract_info network round-trip)
    cache_key = video_url.split("?")[0].strip()
    now = time.time()
    with _info_cache_lock:
        entry = _info_cache.get(cache_key)
    if entry and (now - entry.get("ts", 0)) < _INFO_CACHE_TTL:
        logger.info(f"✅ yt-dlp info from cache (age: {int(now - entry['ts'])}s, instant).")
        return entry["info"]

    opts = {**_base_opts(video_url), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except yt_dlp.utils.ExtractorError as e:
        err = str(e)
        # YouTube bot-detection error — helpful message
        if _is_youtube(video_url) and (
            "Failed to extract any player response" in err
            or "Sign in to confirm" in err
            or "bot" in err.lower()
        ):
            cookies_set = bool(config.YT_COOKIES and os.path.exists(config.YT_COOKIES))
            hint = ""
            if not cookies_set:
                hint = (
                    " | FIX: Render env mein YT_COOKIES set karo "
                    "(Netscape format cookies.txt ka path) — "
                    "bina cookies ke cloud server IPs pe YouTube block karta hai."
                )
            raise yt_dlp.utils.ExtractorError(
                f"YouTube ne bot detect kiya (cloud IP block).{hint} | Original: {err}",
                expected=True
            )
        raise
    # Cache successful result (only non-YouTube — YT tokens expire fast)
    if not _is_youtube(video_url):
        with _info_cache_lock:
            _info_cache[cache_key] = {"info": info, "ts": time.time()}
            if len(_info_cache) > 300:
                oldest = sorted(_info_cache, key=lambda k: _info_cache[k].get("ts", 0))
                for old_k in oldest[:50]:
                    del _info_cache[old_k]
        _save_info_cache()
        logger.info("💾 yt-dlp info cached to disk.")
    return info


def get_page_meta(video_url: str) -> dict:
    """Same contract as faphouse.get_page_meta() / fpo's equivalent —
    used for the upload caption/thumbnail. thumbnail comes straight from
    yt-dlp's own extracted 'thumbnail' field (the site's real poster
    image), same idea as faphouse's og:image use.

    views/upload_date/likes/comments come straight off yt-dlp's own
    info dict too — it already parses these for every site it supports,
    no extra scraping needed here. All four are None when yt-dlp itself
    didn't get a value for this particular video (varies by site/video,
    not a bug)."""
    try:
        info = _extract_info(video_url)
    except Exception as e:
        logger.warning(f"ytdlp get_page_meta failed for {video_url}: {e}")
        return {
            "title": None, "author": None, "author_url": None, "duration": None, "poster_url": None,
            "views": None, "upload_date": None, "likes": None, "comments": None, "category": None,
            "description": None, "site_name": None,
        }

    upload_date = info.get("upload_date")  # yt-dlp's own format: "YYYYMMDD"
    if upload_date and len(upload_date) == 8:
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return {
        "title": info.get("title"),
        "author": info.get("uploader") or info.get("channel"),
        "author_url": info.get("uploader_url") or info.get("channel_url"),
        "duration": info.get("duration"),
        "poster_url": info.get("thumbnail"),
        "views": info.get("view_count"),
        "upload_date": upload_date,
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        # yt-dlp's own 'categories' (a list, e.g. ["Babe"]) is what
        # most of these sites actually populate; 'tags' also gets tried
        # since a handful of extractors use that field instead — first
        # one found wins, joined if there's more than one.
        "category": ", ".join(info.get("categories") or info.get("tags") or []) or None,
        # Full page description straight from yt-dlp's own info dict, and
        # the site's display name (extractor_key, e.g. "PornHub",
        # "Eporner") — together these back the "📄 Full Description"
        # button, same idea as showing a site's full listing text rather
        # than just the short title.
        "description": info.get("description"),
        "site_name": info.get("extractor_key") or info.get("extractor"),
    }


def get_available_qualities(video_url: str) -> list:
    """[{"label": "720p", "height": 720, "url": <format_id>}, ...]
    best-first, plus a leading "Auto (Best)" entry (url=None — same
    "let the downloader decide" convention as faphouse_downloader's
    fallback entry). One entry per distinct height, picking yt-dlp's
    highest-bitrate format at that height when it offers more than one
    (e.g. separate h264/av1 renditions at the same resolution)."""
    fallback = [{"label": "Auto (Best)", "height": None, "url": None}]

    try:
        info = _extract_info(video_url)
    except Exception as e:
        logger.warning(f"ytdlp get_available_qualities failed for {video_url}: {e}")
        return fallback

    formats = info.get("formats") or []
    by_height = {}
    for f in formats:
        # Skip audio-only renditions (vcodec == 'none') — a quality menu
        # entry should always be a real video stream.
        if not f.get("height") or f.get("vcodec") == "none":
            continue
        height = f["height"]
        current = by_height.get(height)
        # Prefer the higher-bitrate format at this height (tbr = total
        # bitrate, yt-dlp's usual proxy for "better encode" at a given
        # resolution); fall back to filesize if tbr's missing on both.
        this_rank = f.get("tbr") or f.get("filesize") or f.get("filesize_approx") or 0
        if current is None or this_rank > (current.get("tbr") or current.get("filesize") or current.get("filesize_approx") or 0):
            by_height[height] = f

    if not by_height:
        return fallback

    variants = [
        {"label": f"{height}p", "height": height, "url": f["format_id"], "direct_url": f.get("url")}
        for height, f in by_height.items()
    ]
    variants.sort(key=lambda v: v["height"], reverse=True)
    return fallback + variants


def get_stream_url(video_url: str) -> str | None:
    """A real, directly-playable media URL (not a format_id) for the
    highest-quality variant available — used for the Stream Link / web
    player, which needs something a browser/hls.js can actually open,
    unlike get_available_qualities()'s "url" values (format_ids, meant
    only as input to yt-dlp's own downloader)."""
    variants = get_available_qualities(video_url)
    for v in variants:
        if v.get("direct_url"):
            return v["direct_url"]
    return None


def download_video(video_url: str, out_path: str, on_progress=None, stream_url: str = None) -> tuple[str, float]:
    """stream_url, if given, is one of get_available_qualities()'s "url"
    values (a yt-dlp format_id) — passed straight to yt-dlp's own
    `format` option. None (the "Auto (Best)" entry) lets yt-dlp pick its
    own best video+audio combination.

    on_progress, if given, is called with the same dict shape as
    faphouse_downloader.download_video: {pct, downloaded_bytes,
    speed_bytes_s, eta_s, elapsed_s, duration_s}. yt-dlp's own
    progress_hooks dict already carries equivalent fields under
    different names — this just renames/reshapes them, no new logic.

    out_path is used as-is (extension included) via yt-dlp's outtmpl —
    the caller (main.py) already picks the out_path extension per
    convention used for the other backends, so no separate merge-output
    format is forced here beyond what yt-dlp defaults to for the chosen
    format(s)."""
    _require_yt_dlp()
    video_url = _normalize_eporner_url(video_url)
    start_time = time.time()

    # Fire an immediate "0% - connecting" callback so callers can show
    # something right away — yt-dlp can take 10-30s to resolve the video
    # URL and start downloading before the first progress_hook fires.
    if on_progress:
        on_progress({
            "pct": 0,
            "downloaded_bytes": 0,
            "speed_bytes_s": 0,
            "eta_s": 0,
            "elapsed_s": 0,
            "duration_s": None,
            "connecting": True,
        })

    def _hook(d):
        if not on_progress:
            return
        status = d.get("status", "")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = (downloaded / total * 100) if total else None
            if pct is None:
                # Fragmented (HLS/DASH) downloads — how sites like eporner
                # commonly serve video — often never populate
                # downloaded_bytes/total_bytes at all; yt-dlp instead
                # exposes fragment_index/fragment_count, which is what
                # actually moves for these. Without this fallback pct
                # stayed None for the entire download, which is why the
                # progress bar looked frozen/never showed real movement.
                frag_idx = d.get("fragment_index")
                frag_count = d.get("fragment_count")
                if frag_idx is not None and frag_count:
                    pct = frag_idx / frag_count * 100
            elapsed = time.time() - start_time
            on_progress({
                "pct": pct,
                "downloaded_bytes": downloaded,
                "speed_bytes_s": d.get("speed") or (downloaded / elapsed if elapsed > 0 else 0),
                "eta_s": d.get("eta"),
                "elapsed_s": elapsed,
                "duration_s": None,
                "connecting": False,
            })

    out_dir = os.path.dirname(out_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    # yt-dlp appends its own extension via %(ext)s — out_path's own
    # extension (if any) is stripped first so we don't end up with
    # "video.mp4.mp4"; the final filename is recovered from
    # requested_downloads/prepare_filename below rather than assumed.
    out_base, _ = os.path.splitext(out_path)
    outtmpl = out_base + ".%(ext)s"

    fmt = (
        f"{stream_url}+bestaudio/{stream_url}/best"
        if stream_url
        else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    )

    opts = {
        **_base_opts(video_url),
        "format": fmt,
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "progress_hooks": [_hook],
    }

    def _run(download_opts):
        with yt_dlp.YoutubeDL(download_opts) as ydl:
            try:
                # Reuse the cached extraction (populated by
                # get_available_qualities(), if the quality menu ran
                # first) instead of re-resolving the video from scratch —
                # process_ie_result() applies THIS ydl instance's real
                # format/outtmpl/progress_hooks options to the
                # already-known formats list, so it downloads exactly
                # the same as a fresh extract_info(download=True) would,
                # just without repeating the network resolve.
                cached_info = _extract_info(video_url)
                info = ydl.process_ie_result(dict(cached_info), download=True)
            except Exception as e:
                logger.warning(f"Reusing cached extraction failed ({e}) — falling back to a fresh extract.")
                info = ydl.extract_info(video_url, download=True)
            final_path = ydl.prepare_filename(info)
            # merge_output_format can change the actual extension after a
            # video+audio merge — requested_downloads (when present) reports
            # the real post-merge path; prepare_filename alone can be stale
            # in that case.
            requested = info.get("requested_downloads") or []
            if requested and requested[0].get("filepath"):
                final_path = requested[0]["filepath"]
        return final_path

    try:
        final_path = _run(opts)
    except Exception as e:
        # BUG FIX: aria2c exiting non-zero (disk full, a network block on
        # the external process specifically, permission issues, a flag
        # this aria2c build doesn't support, etc.) used to fail the whole
        # download outright — "❌ Unexpected error: aria2c exited with
        # code 1" — even though yt-dlp's own built-in (single-connection,
        # slower but far more reliable — no separate process, no separate
        # cookie/header handoff to get wrong) downloader could very
        # plausibly still have pulled the same video down fine. Same
        # verify-and-fall-back pattern fpo_downloader.py's
        # _aria2c_download callers already use, ported here since this
        # file had no such fallback at all before.
        if opts.get("external_downloader") == "aria2c" and "aria2c" in str(e).lower():
            logger.warning(f"aria2c failed for {video_url} ({e}) — retrying with yt-dlp's built-in downloader.")
            fallback_opts = {k: v for k, v in opts.items() if k not in ("external_downloader", "external_downloader_args")}
            final_path = _run(fallback_opts)
        else:
            raise

    if final_path != out_path and os.path.exists(final_path):
        os.replace(final_path, out_path)
        final_path = out_path

    if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
        raise RuntimeError("yt-dlp finished but the output file is missing/empty.")

    final_path = _ensure_faststart(final_path)

    return final_path, time.time() - start_time


def _ensure_faststart(path: str) -> str:
    """Remux (stream-copy, no re-encode — fast and lossless) so the MP4's
    moov atom sits at the FRONT of the file instead of the end.

    BUG this fixes: "video downloads and plays fine from the start, but
    skipping forward/back (+10s etc.) just restarts from 0" on xhamster
    (and other sites needing a bestvideo+bestaudio merge). yt-dlp's own
    merge step (FFmpegMergerPP, triggered by merge_output_format above)
    muxes the two streams together but doesn't pass -movflags +faststart,
    so the merged MP4's moov atom (the index a player needs to jump to an
    arbitrary timestamp) ends up at the END of the file — fine once the
    whole file is already downloaded, but Telegram's player streams
    progressively and can't seek ahead of what's downloaded so far without
    that index up front, so a seek attempt just falls back to playing from
    0. Sites that hand back one single already-progressive file (no merge
    needed) skip this codepath entirely and are usually faststart already
    from the source CDN — this only needed fixing for the merge path.

    Only applies to MP4-family containers (.mp4/.m4v/.mov) — faststart is
    an MP4-specific concept; MKV/WebM (yt-dlp's merge_output_format only
    ever produces mp4 here, but a "best" progressive fallback could still
    land on something else) don't have a moov atom to reposition and are
    left untouched. Fails open: any problem running ffmpeg just logs a
    warning and returns the original file, exactly as before this existed,
    rather than failing the whole download over a seek nicety."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp4", ".m4v", ".mov"):
        return path
    tmp_path = path + ".faststart.tmp" + ext
    try:
        import subprocess
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", tmp_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
        if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, path)
        else:
            logger.warning(f"faststart remux failed for {path} (ffmpeg exit {result.returncode}) — uploading as-is.")
    except Exception as e:
        logger.warning(f"faststart remux failed for {path}: {e} — uploading as-is.")
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return path
