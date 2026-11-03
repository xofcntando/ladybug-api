#!/usr/bin/env bash
# Fetch and prepare the bgutil PO-token provider.
#
# YouTube now demands a "PO token" for anonymous downloads. On datacenter IPs
# like Render's, the bot check is otherwise unavoidable. This provider generates
# those tokens locally, which is the supported fix.
#
# Runs at build time; the prepared server is reused at start time.
set -euo pipefail

DEST="${BGUTIL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.bgutil}"
TAG="${BGUTIL_TAG:-2.0.2}"

if [ -f "$DEST/server/src/main.ts" ]; then
  echo "[pot] already prepared at $DEST"
  exit 0
fi

echo "[pot] fetching bgutil-ytdlp-pot-provider $TAG"
rm -rf "$DEST"
git clone --depth 1 --branch "$TAG" \
  https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$DEST" \
  || git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$DEST"

if command -v deno >/dev/null 2>&1; then
  # The server imports npm packages (express, canvas, @swc/core); without this
  # the first run fails with "Could not resolve express".
  echo "[pot] installing node dependencies"
  ( cd "$DEST/server" && deno install ) || ( cd "$DEST/server" && npm install --omit=dev )

  echo "[pot] warming deno cache"
  ( cd "$DEST/server" && deno cache src/main.ts ) || echo "[pot] cache warm failed (non-fatal)"
else
  echo "[pot] WARNING: deno not on PATH; provider will not start"
fi

echo "[pot] ready"
