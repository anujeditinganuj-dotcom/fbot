#!/bin/sh
# PO-token (bgutil-ytdlp-pot-provider) support: no changes needed here —
# it's handled from inside the bot itself now (pot_provider.py, started
# by main.py at boot in a background thread) rather than as a separate
# process this script would need to launch first. See pot_provider.py's
# own docstring for how it works and why it fails gracefully if this
# host is missing what it needs (e.g. no Docker/apt access, as on
# Render's native Python buildpack — see render.yaml).
exec python3 main.py
