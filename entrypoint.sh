#!/bin/sh
# Starts the bgutil PO-token HTTP server in the background (only if
# YOUTUBE_POT_ENABLED=true — Dockerfile sets this by default), waits a
# moment, then prints whether it's actually reachable before starting the
# bot — so "is it working" is answered by the container logs on every
# boot instead of staying a silent guess.
#
# BUG FIX: the Dockerfile already builds this server (git clone +
# yarn install + npx tsc into /opt/bgutil-ytdlp-pot-provider), but nothing
# was ever starting it — CMD just ran `python main.py` directly, and the
# Python-side start_pot_provider.py that was meant to launch it as a
# subprocess was never called from main.py (and imports a pot_helper.py
# module that doesn't exist in this repo). Without the server actually
# running, YouTube's "web" player client (the only one exposing the full
# 1080p/720p/480p/360p ladder) can't get a PO token and gets bot-blocked —
# ytdlp_downloader.py then falls back to tv_embedded/android, which cap
# out around 360p regardless of what quality is requested. This script
# (ported from the reference "src" project's proven entrypoint.sh) starts
# it for real, the same way "src" does.
if [ "$YOUTUBE_POT_ENABLED" = "true" ] || [ "$YOUTUBE_POT_ENABLED" = "1" ]; then
    node /opt/bgutil-ytdlp-pot-provider/server/build/main.js > /tmp/bgutil-pot.log 2>&1 &
    BGUTIL_PID=$!

    # Poll for up to ~15s instead of a fixed sleep — on slower/cold
    # instances (e.g. Render free tier) the Node server can take longer
    # than a couple seconds to bind its port.
    BGUTIL_UP=0
    for i in $(seq 1 15); do
        if curl -sf http://127.0.0.1:4416/ping >/dev/null 2>&1; then
            BGUTIL_UP=1
            break
        fi
        sleep 1
    done

    if [ "$BGUTIL_UP" = "1" ] && kill -0 "$BGUTIL_PID" 2>/dev/null; then
        echo "[bgutil-pot] OK — PO token server is up on :4416 (YouTube 'web' client should get the full quality ladder)"
    else
        echo "[bgutil-pot] WARNING — PO token server did not come up after 15s. Last log lines:"
        tail -n 20 /tmp/bgutil-pot.log 2>/dev/null
        echo "[bgutil-pot] Bot will still run — YouTube just falls back to tv_embedded/android's lower-res formats."
    fi
else
    echo "[bgutil-pot] YOUTUBE_POT_ENABLED is not 'true' — skipping PO token server. YouTube will use tv_embedded/android (capped ~360p)."
fi

exec python3 main.py
