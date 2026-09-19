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
    chromium xvfb fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# FlareSolverr (Cloudflare JS-challenge bypass — see cf_bypass.py) runs in
# this SAME container as its own background process, in its own isolated
# venv (NOT pip-installed alongside this project's own requirements.txt —
# FlareSolverr pins its own selenium/undetected-chromedriver versions,
# and mixing those into this project's environment risks a real version
# conflict with something else here needing a different pinned version
# of a shared dependency). chromium+xvfb above are what it actually
# drives; flaresolverr_bootstrap.py starts both it and Xvfb at bot
# startup — see that file's docstring. Pinned to the v3.5.0 tag rather
# than a branch so this build doesn't silently start pulling in
# FlareSolverr's own future breaking changes.
RUN git clone --branch v3.5.0 --depth 1 https://github.com/FlareSolverr/FlareSolver.git /opt/flaresolver \
    && python3 -m venv /opt/flaresolver/venv \
    && /opt/flaresolver/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/flaresolver/venv/bin/pip install --no-cache-dir -r /opt/flaresolver/requirements.txt

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
