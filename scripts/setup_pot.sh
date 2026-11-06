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

# Locate deno: the pip "deno" package installs it into the venv, which is often NOT on PATH
# in Render's build/start shells, so `command -v deno` alone silently skips the PO-token provider.
# The app passes the binary it found via $DENO_BIN; otherwise look on PATH, then in whichever python has the package.
DENO_BIN="${DENO_BIN:-$(command -v deno 2>/dev/null || true)}"
if [ -z "$DENO_BIN" ]; then
  for PY in "${PYTHON:-}" python3 python; do
    [ -n "$PY" ] || continue
    DENO_BIN="$("$PY" -c 'import deno; print(deno.find_deno_bin())' 2>/dev/null || true)"
    [ -n "$DENO_BIN" ] && break
  done
fi
[ -n "$DENO_BIN" ] && [ -x "$DENO_BIN" ] || DENO_BIN=""

# "Prepared" means sources AND node_modules. A checkout without dependencies (e.g. an earlier run that could not
# find deno) fails with "Could not resolve express", so it must be completed, not skipped.
if [ -f "$DEST/server/src/main.ts" ] && [ -d "$DEST/server/node_modules/express" ]; then
  echo "[pot] already prepared at $DEST"
  exit 0
fi

if [ ! -f "$DEST/server/src/main.ts" ]; then
  echo "[pot] fetching bgutil-ytdlp-pot-provider $TAG"
  rm -rf "$DEST"
  git clone --depth 1 --branch "$TAG" \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$DEST" \
    || git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$DEST"
fi

if [ -n "$DENO_BIN" ]; then
  # The server imports npm packages (express, canvas, @swc/core); without this
  # the first run fails with "Could not resolve express".
  echo "[pot] installing node dependencies"
  ( cd "$DEST/server" && "$DENO_BIN" install ) || ( cd "$DEST/server" && npm install --omit=dev )

  echo "[pot] warming deno cache"
  ( cd "$DEST/server" && "$DENO_BIN" cache src/main.ts ) || echo "[pot] cache warm failed (non-fatal)"
else
  echo "[pot] deno not found; installing dependencies with npm"
  ( cd "$DEST/server" && npm install --omit=dev ) || { echo "[pot] ERROR: dependency install failed"; exit 1; }
fi

[ -d "$DEST/server/node_modules/express" ] || { echo "[pot] ERROR: node_modules/express missing after install"; exit 1; }

echo "[pot] ready"
