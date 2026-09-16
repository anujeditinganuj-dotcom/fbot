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

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -U "yt-dlp[default]"

COPY . .

RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
