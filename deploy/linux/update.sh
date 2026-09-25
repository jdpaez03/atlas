#!/usr/bin/env bash
# Update ATLAS on the server: pull, dependencies, rebuild the Command Center, restart (docs/SERVER_LINUX.md).
set -euo pipefail
ATLAS_DIR="${ATLAS_DIR:-$HOME/atlas}"
export PATH="$HOME/.local/bin:$PATH"
API_URL="$(grep -E '^NEXT_PUBLIC_ATLAS_API_URL=' "$ATLAS_DIR/.env" | tail -1 | cut -d= -f2-)"
git -C "$ATLAS_DIR" pull --ff-only
(cd "$ATLAS_DIR/apps/api" && uv sync)
(cd "$ATLAS_DIR/apps/web" && npm ci --no-audit --no-fund && NEXT_PUBLIC_ATLAS_API_URL="$API_URL" npm run build)
sudo systemctl restart atlas-api atlas-web
echo "Updated to $(git -C "$ATLAS_DIR" log -1 --format='%h %s')"
