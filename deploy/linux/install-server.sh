#!/usr/bin/env bash
# ATLAS server on Ubuntu Server 24.04 LTS (docs/SERVER_LINUX.md).
#
#   curl -fsSL https://raw.githubusercontent.com/jdpaez03/atlas/main/deploy/linux/install-server.sh | bash
#   (or, from a clone:  bash deploy/linux/install-server.sh)
#
# Run it as the user ATLAS will run as (not root; it asks for sudo). Safe to run again: every step checks first.
# It installs: git, Xvfb, fonts (DejaVu + Selawik), Node.js 22, uv, Google Chrome, Tailscale, Claude Code; clones
# the repo to ~/atlas (data in ~/atlas-local); builds the Command Center; installs the systemd services
# atlas-xvfb, atlas-api, atlas-web; keeps the laptop awake with the lid closed and its battery at ~60%; and
# publishes ATLAS on your tailnet only (tailscale serve: https://<machine>.<tailnet>.ts.net).
set -euo pipefail

REPO_URL="${ATLAS_REPO_URL:-https://github.com/jdpaez03/atlas.git}"
ATLAS_DIR="${ATLAS_DIR:-$HOME/atlas}"
LOCAL_DIR="$(dirname "$ATLAS_DIR")/atlas-local"
API_PORT=8000
WEB_PORT=3000
API_PUBLIC_PORT=8443
ME="$(id -un)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (it uses sudo when needed), not as root."; exit 1
fi
sudo -v
export PATH="$HOME/.local/bin:$PATH"

say "System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git curl ca-certificates unzip jq xvfb \
  fonts-dejavu-core fontconfig sqlite3 htop

say "Selawik font (Segoe UI metrics, for the PDFs SCRIBE renders)"
if [ ! -f /usr/local/share/fonts/selawik/selawk.ttf ]; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/selawik.zip" https://github.com/microsoft/Selawik/releases/download/1.01/Selawik_Release.zip
  sudo mkdir -p /usr/local/share/fonts/selawik
  sudo unzip -o -j "$tmp/selawik.zip" '*.ttf' -d /usr/local/share/fonts/selawik >/dev/null
  sudo fc-cache -f >/dev/null
  rm -rf "$tmp"
fi

say "Node.js 22"
if ! have node || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi

say "uv (Python)"
have uv || curl -LsSf https://astral.sh/uv/install.sh | sh

say "Google Chrome (the agents' browser; runs on a virtual display)"
if ! have google-chrome; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/chrome.deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$tmp/chrome.deb"
  rm -rf "$tmp"
fi

say "Tailscale"
have tailscale || curl -fsSL https://tailscale.com/install.sh | sh

say "Claude Code (the Max plan login ATLAS uses)"
have claude || curl -fsSL https://claude.ai/install.sh | bash

say "ATLAS code in $ATLAS_DIR"
if [ -d "$ATLAS_DIR/.git" ]; then
  git -C "$ATLAS_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$ATLAS_DIR"
fi
mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"

say "Laptop as a server: lid closed = keep running, never sleep, battery conservation"
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' |
  sudo tee /etc/systemd/logind.conf.d/atlas-lid.conf >/dev/null
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
# Lenovo IdeaPad: stop charging at ~60% (the laptop is always plugged in). Harmless on other machines.
echo 'w /sys/bus/platform/drivers/ideapad_acpi/VPC2004:*/conservation_mode - - - - 1' |
  sudo tee /etc/tmpfiles.d/atlas-battery.conf >/dev/null
sudo systemd-tmpfiles --create /etc/tmpfiles.d/atlas-battery.conf 2>/dev/null || true
sudo systemctl restart systemd-logind || true

say "Tailscale sign-in (open the link it prints, on your phone or PC)"
if ! tailscale status >/dev/null 2>&1; then
  sudo tailscale up --ssh
fi
TS_HOST="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
if [ -z "$TS_HOST" ] || [ "$TS_HOST" = "null" ]; then
  echo "Could not read this machine's tailnet name. Run 'sudo tailscale up --ssh' and run this script again."; exit 1
fi
WEB_URL="https://$TS_HOST"
API_URL="https://$TS_HOST:$API_PUBLIC_PORT"
echo "ATLAS will be at $WEB_URL (API $API_URL), reachable only from your tailnet."

say "Settings (.env)"
ENV_FILE="$ATLAS_DIR/.env"
[ -f "$ENV_FILE" ] || cp "$ATLAS_DIR/.env.example" "$ENV_FILE"
chmod 600 "$ENV_FILE"
python3 "$ATLAS_DIR/deploy/linux/server_env.py" "$ENV_FILE" --web "$WEB_URL" --api "$API_URL"

say "Python dependencies"
(cd "$ATLAS_DIR/apps/api" && uv sync)

say "Command Center build"
(cd "$ATLAS_DIR/apps/web" && npm ci --no-audit --no-fund && NEXT_PUBLIC_ATLAS_API_URL="$API_URL" npm run build)

say "systemd services"
UV_BIN="$(command -v uv)"
NPM_BIN="$(command -v npm)"
for unit in atlas-xvfb atlas-api atlas-web; do
  sed -e "s|@USER@|$ME|g" -e "s|@ATLAS_DIR@|$ATLAS_DIR|g" -e "s|@UV@|$UV_BIN|g" -e "s|@NPM@|$NPM_BIN|g" \
      -e "s|@HOME@|$HOME|g" -e "s|@API_PORT@|$API_PORT|g" -e "s|@WEB_PORT@|$WEB_PORT|g" \
      "$ATLAS_DIR/deploy/linux/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now atlas-xvfb atlas-api atlas-web
sudo systemctl restart atlas-api atlas-web

say "Publishing on the tailnet (HTTPS, private)"
sudo tailscale serve --bg --https=443 "http://127.0.0.1:$WEB_PORT" ||
  echo "!! Enable MagicDNS + HTTPS certificates in the Tailscale admin console (DNS page), then run this again."
sudo tailscale serve --bg --https=$API_PUBLIC_PORT "http://127.0.0.1:$API_PORT" || true

say "Waiting for the API"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:$API_PORT/health" >/dev/null && echo "API is up." || echo "API not up yet: journalctl -u atlas-api -n 50"

cat <<EOF

ATLAS is installed.  Open: $WEB_URL   (from any device signed in to your Tailscale)

Next (docs/SERVER_LINUX.md):
  3. Your data from the PC:  bash $ATLAS_DIR/deploy/linux/import-from-pc.sh ~/atlas-transfer.tgz
  4. Claude Max plan:        claude            then type /login and open the link on your phone/PC
     Microsoft 365:          cd $ATLAS_DIR/apps/api && uv run atlas-graph login
Logs:     journalctl -u atlas-api -f
Update:   bash $ATLAS_DIR/deploy/linux/update.sh
EOF
