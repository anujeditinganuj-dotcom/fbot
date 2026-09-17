FROM python:3.12-slim

WORKDIR /app

# libcairo2-dev/libpango1.0-dev/libjpeg-dev/libgif-dev/librsvg2-dev: native
# build deps for the npm "canvas" package — pot_provider.py's
# `deno install --allow-scripts=npm:canvas` step (PO-token provider for
# YouTube's full quality ladder — see pot_provider.py) needs these or that
# install just fails (build-essential/gcc alone isn't enough; canvas
# specifically needs cairo/pango headers). pot_provider.py fails
# gracefully without them (YouTube just stays capped at ios/tv/mweb-level
# quality), but having them means it actually works.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libffi-dev python3-dev ffmpeg git aria2 curl unzip \
    libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

# YouTube now requires solving a JS challenge before yt-dlp can get a
# playable URL — yt-dlp needs an external JS runtime to do that (Deno is
# its default/recommended one). Without this, YouTube extraction fails
# with "Failed to extract any player response" even on an up-to-date
# yt-dlp — this isn't a bug in yt-dlp itself, just a missing runtime.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -U "yt-dlp[default]"

COPY . .

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
