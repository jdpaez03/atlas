#!/usr/bin/env bash
# ATLAS server health check (docs/SERVER_LINUX.md § Day to day). Read-only: it changes nothing.
#
#   bash ~/atlas/deploy/linux/healthcheck.sh
#
# ✓ = fine · ! = worth a look · ✗ = broken. Exit code = number of ✗.
set -uo pipefail
ATLAS_DIR="${ATLAS_DIR:-$HOME/atlas}"
LOCAL_DIR="$(dirname "$ATLAS_DIR")/atlas-local"
API="http://127.0.0.1:8000"
export PATH="$HOME/.local/bin:$PATH"
FAIL=0
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
section() { printf '\n\033[1m%s\033[0m\n' "$*"; }
get() { curl -fsS --max-time 20 "$API$1" 2>/dev/null; }

section "Services"
for s in atlas-xvfb atlas-api atlas-web; do
  if [ "$(systemctl is-active "$s")" = active ]; then
    ok "$s active (since $(systemctl show -p ActiveEnterTimestamp --value "$s" | cut -d' ' -f2-3))"
  else
    bad "$s $(systemctl is-active "$s") → journalctl -u $s -n 50"
  fi
done
if [ "$(systemctl show -p NeedDaemonReload --value atlas-api 2>/dev/null)" = yes ]; then
  warn "unit files changed on disk → sudo systemctl daemon-reload && sudo systemctl restart atlas-api"
fi

section "ATLAS"
if h="$(get /health)"; then ok "API up · version $(echo "$h" | jq -r .version)"; else bad "API not answering on $API"; fi
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 http://127.0.0.1:3000/)"
[ "$code" = 200 ] && ok "Command Center up (HTTP 200)" || bad "Command Center HTTP $code"
if c="$(get /config)"; then
  if [ "$(echo "$c" | jq -r .live_available)" = true ]; then
    ok "live agents: $(echo "$c" | jq -r .backend) · web search $(echo "$c" | jq -r .web_search)"
  else
    bad "live agents unavailable: $(echo "$c" | jq -r .live_hint)"
  fi
fi
if a="$(get /agents/availability)"; then
  down="$(echo "$a" | jq -r 'to_entries[] | select(.value.available | not) | "\(.key) (\(.value.reason // "?"))"')"
  n="$(echo "$a" | jq 'length')"
  if [ -z "$down" ]; then ok "all $n agents available"; else
    warn "$(echo "$down" | wc -l) of $n agents unavailable:"; echo "$down" | sed 's/^/      /'; fi
fi
[ -s "$HOME/.claude/.credentials.json" ] && ok "Claude Code signed in (~/.claude)" ||
  warn "no ~/.claude/.credentials.json: run \`claude\` then /login (ignore if you use an API key)"

section "Microsoft 365"
if i="$(get /inbox/status)"; then
  if [ "$(echo "$i" | jq -r .connected)" = true ]; then
    ok "Outlook connected · $(echo "$i" | jq -r '.account // "?"') · last scan $(echo "$i" | jq -r '.last_scan // "never"' | cut -c1-16)"
  else
    warn "Outlook: $(echo "$i" | jq -r .detail) $(echo "$i" | jq -r '.hint // ""')"
  fi
fi
(cd "$ATLAS_DIR/apps/api" && uv run --frozen -q atlas-graph status 2>&1) | sed 's/^/    /'
if out="$(cd "$ATLAS_DIR/apps/api" && uv run --frozen -q atlas-graph roots 2>&1)"; then
  ok "file roots:"; echo "$out" | sed 's/^/      /'
else
  warn "file roots:"; echo "$out" | sed 's/^/      /'
fi

section "ARGOS"
if s="$(get /argos/status)"; then
  while IFS=$'\t' read -r state label when note; do
      case "$state" in
        ok) ok "$label · $when" ;;
        "not configured"|"never run"|partial) warn "$label: $state · $note" ;;
        *) bad "$label: $state · $note" ;;
      esac
    done < <(echo "$s" | jq -r '.checks[] | select(.enabled) | "\(.state)\t\(.label)\t\(.last_run // "never" | .[0:16])\t\(.note // "" | .[0:110])"')
  echo "    next watch $(echo "$s" | jq -r '.next_run // "off"' | cut -c1-16) · next brief $(echo "$s" | jq -r '.next_brief // "off"' | cut -c1-16)"
  [ "$(echo "$s" | jq -r '.config_error // empty')" ] && warn "watch.yaml: $(echo "$s" | jq -r .config_error)"
fi

section "Agents' browser"
(cd "$ATLAS_DIR/apps/api" && uv run --frozen -q atlas-browser status 2>&1) | sed 's/^/    /'

section "Access (Tailscale)"
if tailscale status >/dev/null 2>&1; then
  host="$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')"
  ok "tailnet up · https://$host"
  sudo -n tailscale serve status 2>/dev/null | grep -q 3000 && ok "serving the Command Center (443 → 3000)" ||
    warn "check \`sudo tailscale serve status\` (443 → 127.0.0.1:3000, 8443 → 127.0.0.1:8000)"
else
  bad "tailscale down → sudo tailscale up"
fi

section "Machine"
read -r used free < <(df -h / | awk 'NR==2 {print $5, $4}')
if [ "${used%\%}" -lt 85 ]; then ok "disk ${used} used · ${free} free"; else bad "disk ${used} used · ${free} free"; fi
read -r u t < <(free -m | awk '/Mem:/ {printf "%d %d\n", $3, $2}')
ok "memory ${u} / ${t} MB · load $(cut -d' ' -f1-3 /proc/loadavg) · up $(uptime -p | sed 's/up //')"
cm="$(cat /sys/bus/platform/drivers/ideapad_acpi/VPC2004:*/conservation_mode 2>/dev/null | head -1)"
[ "$cm" = 1 ] && ok "battery conservation on" || warn "battery conservation ${cm:-n/a}"
bat="$(cat /sys/class/power_supply/BAT*/capacity 2>/dev/null | head -1)"
ac="$(cat /sys/class/power_supply/A*/online 2>/dev/null | head -1)"
[ -n "$bat" ] && { [ "$ac" = 1 ] && ok "on AC power · battery ${bat}%" || warn "ON BATTERY · ${bat}% (power cut or unplugged?)"; }
[ -f /etc/systemd/logind.conf.d/atlas-lid.conf ] && ok "lid closed keeps running" || warn "lid setting missing (rerun install-server.sh)"
up="$( (apt list --upgradable 2>/dev/null || true) | grep -c upgradable)"
[ "$up" -lt 30 ] && ok "$up system updates pending" || warn "$up system updates pending → sudo apt upgrade"
[ -f /var/run/reboot-required ] && warn "a reboot is pending (after updates) → sudo reboot"

section "Data"
if [ -f "$LOCAL_DIR/atlas.db" ]; then
  if command -v sqlite3 >/dev/null; then
    r="$(sqlite3 -readonly "file:$LOCAL_DIR/atlas.db?mode=ro" 'PRAGMA quick_check;' 2>&1 | head -1)"
    [ "$r" = ok ] && ok "database ok · $(du -h "$LOCAL_DIR/atlas.db" | cut -f1)" || bad "database: $r"
  fi
else
  bad "no $LOCAL_DIR/atlas.db"
fi
for d in agents brand; do [ -d "$LOCAL_DIR/$d" ] && ok "atlas-local/$d present" || warn "atlas-local/$d missing"; done
errs="$(journalctl -u atlas-api --since '24 hours ago' -p err --no-pager -q 2>/dev/null | wc -l)"
[ "$errs" -eq 0 ] && ok "no API errors in the last 24 h" ||
  warn "$errs API error line(s) in 24 h → journalctl -u atlas-api --since '24 hours ago' -p err"
git -C "$ATLAS_DIR" fetch -q 2>/dev/null
behind="$(git -C "$ATLAS_DIR" rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)"
[ "$behind" = 0 ] && ok "code up to date ($(git -C "$ATLAS_DIR" log -1 --format='%h %cr'))" ||
  warn "$behind new commit(s) on GitHub → bash $ATLAS_DIR/deploy/linux/update.sh"

printf '\n'
[ "$FAIL" -eq 0 ] && printf '\033[32mAll good.\033[0m\n' || printf '\033[31m%d problem(s).\033[0m\n' "$FAIL"
exit "$FAIL"
