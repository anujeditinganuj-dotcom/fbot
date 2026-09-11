FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential gcc libffi-dev python3-dev ffmpeg git aria2 curl unzip && rm -rf /var/lib/apt/lists/*

# YouTube now requires solving a JS challenge before yt-dlp can get a
# playable URL — yt-dlp needs an external JS runtime to do that (Deno is
# its default/recommended one). Without this, YouTube extraction fails
# with "Failed to extract any player response" even on an up-to-date
# yt-dlp — this isn't a bug in yt-dlp itself, just a missing runtime.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

# YouTube PO token provider — the server built here is started by
# entrypoint.sh (only when YOUTUBE_POT_ENABLED=true, on by default below).
# Built unconditionally either way since skipping it is a no-op, and
# rebuilding the whole image just to turn this on later would be more
# annoying than the ~30s this adds to the build.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g yarn \
    && git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && yarn install --frozen-lockfile \
    && npx tsc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -U "yt-dlp[default]"

COPY . .

# Baked in so PO token support is on by default in this image without
# needing to set it manually on the hosting platform — still overridable
# by setting YOUTUBE_POT_ENABLED at the platform level if ever needed
# (an explicit runtime env var wins over this ENV default).
ENV YOUTUBE_POT_ENABLED=true

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
