#!/usr/bin/env bash
# deploy.sh
# Production deployment of jellyfin-notifier on this LXC.
# Run as root: sudo ./deploy.sh
#
# Idempotent: safe to re-run. Pulls the latest code, creates .env from
# .env.example if it doesn't exist yet (never overwrites an existing one),
# runs install.sh, starts the service, and checks that it is actually
# reachable before declaring success. .env does NOT need to be filled in
# beforehand: if it's missing required values, the app boots in "setup
# mode" and the first visit to the interface lands on the /setup wizard,
# which fills in and saves .env for you - no nano/manual editing required.

set -euo pipefail

# ---- Configuration ----------------------------------------------------------
REPO_URL="https://github.com/<your-account>/<your-repo>.git"   # <-- edit this
SRC_DIR="/opt/jellyfin-notifier-src"
TARGET_DIR="/opt/jellyfin-notifier"
SERVICE_USER="jellyshare"
SERVICE_NAME="jellyfin-notifier"

log()  { echo -e "\n==> $*"; }
fail() { echo -e "\n!! $*" >&2; exit 1; }

# ---- 0. Prerequisites --------------------------------------------------------
log "Checking prerequisites"
[ "$EUID" -eq 0 ] || fail "Run this script as root (sudo)."
command -v git    >/dev/null || fail "git is not installed."
command -v python3 >/dev/null || fail "python3 is not installed."
python3 -c "import venv" 2>/dev/null || fail "python3-venv is not installed (apt install python3-venv)."
id "$SERVICE_USER" &>/dev/null || fail "System user '$SERVICE_USER' does not exist. Create it first: useradd --system --no-create-home --shell /usr/sbin/nologin $SERVICE_USER"

# ---- 1. Fetch the code -------------------------------------------------------
log "Fetching code from GitHub"
if [ -d "$SRC_DIR" ]; then
  log "Source directory already exists, updating (git pull)"
  git -C "$SRC_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$SRC_DIR"
fi

# ---- 2. Prepare .env ----------------------------------------------------------
cd "$SRC_DIR"
if [ ! -f "$TARGET_DIR/.env" ] && [ ! -f .env ]; then
  log "Creating .env from .env.example"
  cp .env.example .env
fi

# ---- 3. Install -----------------------------------------------------------------
log "Running install.sh (copies to $TARGET_DIR, venv, systemd service, sudoers rule)"
./install.sh

# ---- Check .env completeness (informational only) ---------------------------------
# Never overwrite an existing .env - check whichever copy is authoritative
# (the one already deployed, if this is a redeploy; otherwise the fresh one).
ENV_TO_CHECK="$TARGET_DIR/.env"
[ -f "$ENV_TO_CHECK" ] || ENV_TO_CHECK="$SRC_DIR/.env"

# Leftover placeholders no longer block the deploy - everything below is
# configurable from the /setup wizard once the service is up, so an
# incomplete .env here just means the service starts in setup mode instead
# of fully configured. This asks the app's own env_setup.unresolved_keys()
# (via the venv install.sh just created) instead of re-implementing the
# placeholder list here in bash, so it can never drift out of sync with
# what the setup wizard itself considers "still a placeholder".
log "Checking .env completeness"
NEEDS_SETUP=0
UNRESOLVED=$("$TARGET_DIR/venv/bin/python3" -c "
import sys
sys.path.insert(0, '$TARGET_DIR')
from pathlib import Path
from jellyfin_notifier.env_setup import parse_env_file, unresolved_keys
print(','.join(unresolved_keys(parse_env_file(Path('$ENV_TO_CHECK')))))
" 2>/dev/null || true)
if [ -n "$UNRESOLVED" ]; then
  NEEDS_SETUP=1
  echo "   Still using example/placeholder values for: $UNRESOLVED"
  log "Some values are still placeholders - the service will start in setup mode"
else
  log ".env looks complete"
fi

# ---- 4. Start and verify systemd -------------------------------------------------
# `restart` (not `enable --now`) so a re-deploy onto an already-running
# service actually picks up the new code - `enable --now`'s "start" is a
# no-op when the unit is already active, which left a stale process serving
# old code in memory even though every file on disk had been updated.
log "Restarting the service"
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  journalctl -u "$SERVICE_NAME" -n 40 --no-pager
  fail "Service failed to start - logs above."
fi
log "Service active ($(systemctl show "$SERVICE_NAME" --property=SubState --value))"

# ---- 5. Application health check ---------------------------------------------------
PORT=$(grep -E '^PORT=' "$TARGET_DIR/.env" | cut -d= -f2 || echo 5005)
PORT=${PORT:-5005}

# Best-effort detection of this machine's LAN IP, for the summary URLs below -
# falls back to a placeholder if it can't be determined (e.g. no `ip`/`hostname -I`).
LXC_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$LXC_IP" ]; then
  LXC_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
fi
LXC_IP=${LXC_IP:-<lxc-ip>}

log "Checking /health (port $PORT)"
sleep 2
HTTP_CODE=$(curl -s -o /tmp/health_response.json -w "%{http_code}" "http://127.0.0.1:${PORT}/health" || echo "000")
if [ "$HTTP_CODE" != "200" ]; then
  cat /tmp/health_response.json 2>/dev/null || true
  fail "/health returned $HTTP_CODE (expected 200). Check the logs: journalctl -u $SERVICE_NAME -f"
fi
log "/health returned 200 - service is operational"
cat /tmp/health_response.json

# ---- 6. Interface check ----------------------------------------------------------
# The admin blueprint is mounted at the site root (no /admin prefix) - the
# login page is at /login, not /admin/login. -L follows the redirect to
# /setup that happens when .env is still incomplete, so this check passes
# in both cases (fully configured -> /login, setup mode -> /setup).
log "Checking that the interface responds"
ADMIN_CODE=$(curl -sL -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/login" || echo "000")
[ "$ADMIN_CODE" = "200" ] || fail "The interface did not respond ($ADMIN_CODE) on /login."
log "Interface reachable: http://${LXC_IP}:${PORT}/"

# ---- 7. Cleanup -------------------------------------------------------------------
log "Cleaning up the source directory"
rm -rf "$SRC_DIR"

log "Deployment finished successfully."
echo "  - Service   : systemctl status $SERVICE_NAME"
echo "  - Logs      : journalctl -u $SERVICE_NAME -f"
echo "  - Interface : http://${LXC_IP}:${PORT}/"
echo "  - Health    : http://${LXC_IP}:${PORT}/health"
echo "  - Metrics   : http://${LXC_IP}:${PORT}/metrics"
if [ "$NEEDS_SETUP" -eq 1 ]; then
  echo
  echo "  .env is not fully filled in yet - open the interface above, you'll"
  echo "  land on the setup wizard (http://${LXC_IP}:${PORT}/setup) to finish"
  echo "  the configuration (or import an existing settings file there)."
fi
