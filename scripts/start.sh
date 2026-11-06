#!/usr/bin/env bash
# Start the PO-token provider, then uvicorn.
#
# The provider listens on 127.0.0.1:4416 and is used by yt-dlp through the
# youtubepot-bgutilhttp plugin. If it cannot start we still boot the API --
# YouTube downloads degrade, everything else keeps working.
set -uo pipefail

DEST="${BGUTIL_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.bgutil}"
POT_PORT="${BGUTIL_PORT:-4416}"
APP_PORT="${PORT:-8000}"

# Locate deno: the pip "deno" package installs it into the venv, which is often NOT on PATH
# in Render's build/start shells, so `command -v deno` alone silently skips the PO-token provider.
DENO_BIN="$(command -v deno 2>/dev/null || true)"
if [ -z "$DENO_BIN" ]; then
  DENO_BIN="$(python3 -c 'import deno; print(deno.find_deno_bin())' 2>/dev/null || python -c 'import deno; print(deno.find_deno_bin())' 2>/dev/null || true)"
fi
[ -n "$DENO_BIN" ] && [ -x "$DENO_BIN" ] || DENO_BIN=""

if [ -f "$DEST/server/src/main.ts" ] && [ -n "$DENO_BIN" ]; then
  echo "[start] launching PO-token provider on 127.0.0.1:$POT_PORT"
  ( cd "$DEST/server" && exec "$DENO_BIN" run --v8-flags=--max-old-space-size="${POT_MAX_HEAP_MB:-160}" --allow-all src/main.ts --port "$POT_PORT" ) >/tmp/pot.log 2>&1 &
  POT_PID=$!

  # Wait up to 60s so the first download does not race the provider.
  ready=0
  for _ in $(seq 1 60); do
    if curl -sf -m 2 "http://127.0.0.1:$POT_PORT/ping" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done

  if [ "$ready" = "1" ]; then
    echo "[start] provider ready"
    export YT_POT_PROVIDER_URL="${YT_POT_PROVIDER_URL:-http://127.0.0.1:$POT_PORT}"
  else
    echo "[start] WARNING: provider did not answer; continuing without it"
    tail -20 /tmp/pot.log || true
    kill "$POT_PID" 2>/dev/null || true
  fi
else
  echo "[start] PO-token provider not prepared (main.ts present: $([ -f "$DEST/server/src/main.ts" ] && echo yes || echo no), deno: ${DENO_BIN:-missing}); skipping"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 --port "$APP_PORT" \
  --proxy-headers --forwarded-allow-ips '*' \
  --timeout-keep-alive 75 --no-access-log
