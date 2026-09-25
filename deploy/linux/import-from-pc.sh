#!/usr/bin/env bash
# Bring your ATLAS data from the Windows PC to the server (docs/SERVER_LINUX.md, step 3).
#
#   On the PC:      powershell -ExecutionPolicy Bypass -File deploy\windows\pack-for-server.ps1
#   Copy the file:  scp $HOME\Desktop\atlas-transfer.tgz <user>@<server>:~/
#   On the server:  bash ~/atlas/deploy/linux/import-from-pc.sh ~/atlas-transfer.tgz
#
# The file holds atlas-local (missions, memory, lessons, brand kit, private prompts; without the Windows-only
# browser profiles and DPAPI token cache), your .env and the agents' browser sessions. It is deleted at the end:
# it contains secrets.
set -euo pipefail
ZIP="${1:?usage: import-from-pc.sh <atlas-transfer.tgz>}"
ATLAS_DIR="${ATLAS_DIR:-$HOME/atlas}"
LOCAL_DIR="$(dirname "$ATLAS_DIR")/atlas-local"
export PATH="$HOME/.local/bin:$PATH"
[ -f "$ZIP" ] || { echo "No file $ZIP"; exit 1; }

sudo systemctl stop atlas-api || true
tmp="$(mktemp -d)"
chmod 700 "$tmp"
tar -xzf "$ZIP" -C "$tmp"

if [ -d "$tmp/atlas-local" ]; then
  if [ -e "$LOCAL_DIR/atlas.db" ]; then
    backup="$LOCAL_DIR.before-import-$(date +%Y%m%d-%H%M%S)"
    echo "Keeping the server's previous data in $backup"
    mv "$LOCAL_DIR" "$backup"
  fi
  mkdir -p "$LOCAL_DIR"
  cp -a "$tmp/atlas-local/." "$LOCAL_DIR/"
  rm -rf "$LOCAL_DIR/browser" "$LOCAL_DIR"/inbox/msal_cache.bin* "$LOCAL_DIR/graphcache"
  chmod 700 "$LOCAL_DIR"
  echo "Data restored to $LOCAL_DIR"
fi

if [ -f "$tmp/atlas.env" ]; then
  WEB="$(grep -E '^ATLAS_CORS_ORIGINS=' "$ATLAS_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
  API="$(grep -E '^NEXT_PUBLIC_ATLAS_API_URL=' "$ATLAS_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2-)"
  install -m 600 "$tmp/atlas.env" "$ATLAS_DIR/.env"
  python3 "$ATLAS_DIR/deploy/linux/server_env.py" "$ATLAS_DIR/.env" --web "$WEB" --api "$API"
  echo "Settings (.env) adapted for the server"
fi

if [ -d "$tmp/claude-agents" ]; then
  mkdir -p "$HOME/.claude/agents"
  cp -a "$tmp/claude-agents/." "$HOME/.claude/agents/"
  echo "Claude Code agents copied to ~/.claude/agents (EOS division)"
fi

for session in "$tmp"/sessions/*.json; do
  [ -f "$session" ] || continue
  agent="$(basename "$session" .json)"
  (cd "$ATLAS_DIR/apps/api" && DISPLAY=:99 uv run atlas-browser import-session "$agent" "$session") ||
    echo "!! could not import the browser session of $agent (run atlas-browser login later)"
done

rm -rf "$tmp"
rm -f "$ZIP"
sudo systemctl start atlas-api
sudo systemctl restart atlas-web
echo "Done. The transfer file was deleted."
