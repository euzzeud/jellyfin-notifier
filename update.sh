#!/usr/bin/env bash
# Updates an EXISTING jellyfin-notifier install in /opt/jellyfin-notifier
# (unlike install.sh, doesn't touch .env or data already present:
# seen_items.json, settings.json, pending_items.json, upcoming.json).
# Run as root, FROM the extracted folder (jellyfin-notifier/).
set -euo pipefail

TARGET=/opt/jellyfin-notifier
SERVICE_USER=jellyshare

if [ "$EUID" -ne 0 ]; then
  echo "Run this script as root (sudo -i then re-run, or 'sudo ./update.sh')." >&2
  exit 1
fi

if [ ! -d "$TARGET" ]; then
  echo "No install found in ${TARGET} - use install.sh for a first-time install." >&2
  exit 1
fi

echo "==> Stopping the service"
systemctl stop jellyfin-notifier || true

echo "==> Quick backup (.env, data) before updating"
BACKUP_DIR="${TARGET}.backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
for f in .env seen_items.json settings.json pending_items.json upcoming.json; do
  [ -f "${TARGET}/${f}" ] && cp "${TARGET}/${f}" "$BACKUP_DIR/"
done
echo "    -> backed up to ${BACKUP_DIR}"

echo "==> Copying the updated code (without overwriting .env or the data)"
shopt -s dotglob
for item in ./*; do
  base="$(basename "$item")"
  case "$base" in
    .env|.env.example|seen_items.json|settings.json|pending_items.json|upcoming.json|update.sh|install.sh|preview.html|venv)
      continue
      ;;
  esac
  rm -rf "${TARGET:?}/${base}"
  cp -r "$item" "${TARGET}/"
done
shopt -u dotglob

cd "$TARGET"

echo "==> Updating Python dependencies"
venv/bin/pip install -r requirements.txt -q

echo "==> Adding new keys to .env if missing (ADMIN_USERNAME/ADMIN_PASSWORD, etc.)"
add_if_missing() {
  local key="$1" default="$2"
  grep -q "^${key}=" .env 2>/dev/null || echo "${key}=${default}" >> .env
}
add_if_missing ADMIN_USERNAME admin
add_if_missing ADMIN_PASSWORD change-me
add_if_missing SETTINGS_PATH settings.json
add_if_missing PENDING_ITEMS_PATH pending_items.json
add_if_missing UPCOMING_PATH upcoming.json

echo "==> Sudo rights for the admin interface (start/stop/restart + logs)"
SUDOERS_FILE=/etc/sudoers.d/jellyfin-notifier-admin
cat > "$SUDOERS_FILE" <<EOF
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/systemctl start jellyfin-notifier, /bin/systemctl stop jellyfin-notifier, /bin/systemctl restart jellyfin-notifier, /usr/bin/journalctl -u jellyfin-notifier *
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" >/dev/null || echo "    WARNING: invalid sudoers file, check ${SUDOERS_FILE} by hand"

echo "==> Permissions"
if id "$SERVICE_USER" &>/dev/null; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET"
fi

echo "==> Reloading the systemd service"
cp jellyfin-notifier.service /etc/systemd/system/jellyfin-notifier.service
systemctl daemon-reload

echo "==> Checking Python syntax"
venv/bin/python -c "
import ast, pathlib
for f in pathlib.Path('jellyfin_notifier').rglob('*.py'):
    ast.parse(f.read_text())
print('OK: all .py files are syntactically valid')
"

echo "==> Restarting the service"
systemctl start jellyfin-notifier
sleep 2
systemctl status jellyfin-notifier --no-pager -l | head -n 12

echo
echo "Update complete."
echo "IMPORTANT: edit ${TARGET}/.env to set ADMIN_USERNAME / ADMIN_PASSWORD if not already done,"
echo "then 'systemctl restart jellyfin-notifier'."
echo "Admin interface: http://<lxc-ip>:5005/"
