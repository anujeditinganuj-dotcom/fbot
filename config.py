import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
# No longer required — was only used by the old Telethon-auth flow from
# the previous downloader, which this bot doesn't use anymore (faphouse
# downloading needs no Telegram user session). Kept optional so old .env
# files with it set still work, but a fresh setup doesn't need to provide it.
OWNER_ID = int(os.environ["OWNER_ID"])

TG_BOT_WORKERS = int(os.getenv("TG_BOT_WORKERS", "8"))   # 4→8: zyada parallel Telegram connections
DOWNLOAD_DIR = "downloads"
MAX_CONCURRENT_DOWNLOADS = 3   # 5→3: bandwidth ek file pe focus karega, sabka upload tez hoga

# MongoDB connection. MONGO_URI is required (e.g. a MongoDB Atlas
# connection string). MONGO_DB_NAME defaults to "faphouse_bot".
MONGO_URI = os.environ["MONGO_URI"]
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "faphouse_bot")

# ---------------------------------------------------------------------
# Extra settings for premium / admin / cache features.
# ADMINS: comma-separated user ids in the ADMINS env var. OWNER_ID is
# always treated as an admin even if not listed.
# ---------------------------------------------------------------------
ADMINS = list({OWNER_ID, *[int(x) for x in os.getenv("ADMINS", "8931907813").split(",") if x.strip()]})

# Photo shown on /start. Can be a URL or a local file path.
START_PHOTO_URL = os.getenv("START_PHOTO_URL", "https://iili.io/n2jHVj9.jpg")

# Free (non-premium) users can download this many files per day (UTC).
DAILY_FREE_LIMIT = int(os.getenv("DAILY_FREE_LIMIT", "5"))

# If > 0, delivered videos are auto-deleted from the chat after this many
# seconds (the caption warns the user to forward it first). 0 disables it.
# Default: 3600 seconds = 1 hour.
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "3600"))

# Optional: channel id (e.g. -100xxxxxxxxxx) where new-user/download logs
# are posted. Leave unset/empty to disable logging.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "-1003925649805")) or None

# Optional: comma-separated channel ids to also receive a copy of every
# delivered video (a simple off-site backup). Leave empty to disable.
BACKUP_CHANNEL_IDS = [int(x) for x in os.getenv("BACKUP_CHANNEL_IDS", "-1003925649805").split(",") if x.strip()]

# Optional: a private channel (bot must be admin there) where one copy of
# every freshly-uploaded video is stored. Cache hits are then served with
# copy_message() from this channel instead of a bare file_id — this gives
# Telegram a fresh file_reference for the recipient, which fixes cache
# hits failing/redownloading when a *different* user requests a link that
# someone else already downloaded (the old file_id is only guaranteed
# valid for the chat it was originally sent to). Leave unset to fall back
# to the old file_id-only behaviour.
CACHE_CHANNEL_ID = int(os.getenv("CACHE_CHANNEL_ID", "-1003925649805")) or None

# ---------------------------------------------------------------------
# Auto-scraper / auto-uploader (/autoupload) — scrapes faphouse.com's
# public /videos listing and bulk-uploads new videos to a chat/channel.
# ---------------------------------------------------------------------
SITE_URL = os.getenv("SITE_URL", "https://faphouse2.com")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)
# Non-admin cooldown between auto-uploaded videos in the same chat (seconds).
AUTO_UPLOAD_COOLDOWN = int(os.getenv("AUTO_UPLOAD_COOLDOWN", "60"))
# Telegram's per-file limit for regular bots.
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(2000 * 1024 * 1024)))
# Target size per part when a video exceeds MAX_FILE_SIZE and gets split
# instead of skipped. Deliberately below MAX_FILE_SIZE — ffmpeg's
# time-based segmenting only *estimates* each part's size off the whole
# file's average bitrate, so a part can land a bit over that estimate on
# a higher-bitrate stretch. ~7% headroom absorbs that without needing a
# second corrective re-split pass in the common case.
SPLIT_PART_TARGET_BYTES = int(os.getenv("SPLIT_PART_TARGET_BYTES", str(int(MAX_FILE_SIZE * 0.93))))
# Optional: a channel id for the 24/7 live monitor (watches for brand-new
# releases and posts them here automatically). Leave unset to disable it.
DEFAULT_CHANNEL = int(os.getenv("DEFAULT_CHANNEL", "-1003925649805")) or None
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "180"))

# eporner.com is a massive general tube site (non-stop firehose uploads),
# not a curated-release site like faphouse — "watch and push instantly"
# there would mean hundreds of essentially-random new videos per
# MONITOR_INTERVAL if left uncapped. This caps how many NEW (not-yet-
# uploaded) videos eporner_live_monitor pushes per cycle; anything past
# the cap just waits for the next cycle instead of getting dropped.
EPORNER_MONITOR_MAX_PER_CYCLE = int(os.getenv("EPORNER_MONITOR_MAX_PER_CYCLE", "5"))

# ---------------------------------------------------------------------
# YouTube (via ytdlp_downloader.py) — cookies + PO-token support.
#
# YT_COOKIES: path to a Netscape-format cookies.txt file. Fixes
# "Sign in to confirm you're not a bot" on some videos/IPs. Optional —
# yt-dlp still works cookie-less via the tv_embedded/android player
# clients, just capped to lower-res formats on videos that need a login.
YT_COOKIES = os.getenv("YT_COOKIES", "")
INSTA_COOKIES = os.getenv("INSTA_COOKIES", "")
FB_COOKIES = os.getenv("FB_COOKIES", "")
VK_COOKIES = os.getenv("VK_COOKIES", "")

# YOUTUBE_POT_ENABLED: set true to start the bgutil-ytdlp-pot-provider
# background server (see entrypoint.sh) — this is what lets the "web"
# player client (the only one exposing the FULL 1080p/720p/480p/360p
# quality ladder) pass YouTube's bot check. Baked on by default in the
# Docker image, same as the reference "src" project. Without it (or on a
# non-Docker deploy with no Node.js), YouTube falls back to the
# tv_embedded/android clients — still works, just capped to ~360p.
YOUTUBE_POT_ENABLED = os.getenv("YOUTUBE_POT_ENABLED", "false").strip().lower() in ("1", "true", "yes")
