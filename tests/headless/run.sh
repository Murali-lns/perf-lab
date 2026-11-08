#!/usr/bin/env bash
# Build the harness bundle, start the real backend with real measured data, drive it with Playwright.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
HERMES="${HERMES:-$HOME/.hermes/hermes-agent}"
NM="$HERMES/apps/desktop/node_modules"
PY="${PY:-$HERMES/venv/bin/python}"
PORT="${PORT:-8792}"
W="$(mktemp -d)"; mkdir -p "$HERE/out"
cp "$HERE"/{sdk.jsx,main.jsx,index.html} "$W/"
sed "s#'@hermes/plugin-sdk'#'./sdk.jsx'#" "$ROOT/desktop/plugin.js" > "$W/plugin.js"
(cd "$HERMES/apps/desktop" && NODE_PATH="$NM" "$NM/.bin/esbuild" "$W/main.jsx" --bundle --outfile="$W/bundle.js" \
  --jsx=automatic --resolve-extensions=.jsx,.js --define:process.env.NODE_ENV='"development"' >/dev/null)
PL_ROOT="$ROOT" PL_WEB="$W" PORT="$PORT" "$PY" "$HERE/server.py" & SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 120); do curl -sf "http://127.0.0.1:$PORT/api/summary" >/dev/null && break; sleep 1; done
(cd "$HERMES/apps/desktop" && CHROME="${CHROME:-$(ls $HOME/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell 2>/dev/null | tail -1)}" SHOTS="$HERE/out" PORT="$PORT" NODE_PATH="$NM:$HERMES/node_modules/.pnpm/playwright@1.64.0/node_modules" node "$HERE/drive.cjs")
