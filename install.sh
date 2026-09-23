#!/usr/bin/env bash
# Installs jellyfin-notifier into /opt/jellyfin-notifier and sets up the systemd service.
# Run as root, FROM the extracted folder (jellyfin-notifier/).
set -euo pipefail

TARGET=/opt/jellyfin-notifier
SERVICE_USER=jellyshare

if [ "$EUID" -ne 0 ]; then
  echo "Run this script as root (sudo -i then re-run, or 'sudo ./install.sh')." >&2
  exit 1
fi

echo "==> Copying files to ${TARGET}"
mkdir -p "$TARGET"
shopt -s dotglob   # so the glob below also includes hidden files (.env.example)
cp -r ./* "$TARGET"/
shopt -u dotglob
rm -rf "$TARGET"/install.sh "$TARGET"/preview.html

cd "$TARGET"

echo "==> Creating the virtualenv"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example (TO BE FILLED IN)"
  cp .env.example .env
  echo "    -> now edit: nano ${TARGET}/.env"
else
  echo "==> .env already exists, leaving it as-is"
fi

echo "==> Permissions"
if id "$SERVICE_USER" &>/dev/null; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET"
else
  echo "    (user $SERVICE_USER not found, ownership left as-is)"
fi

echo "==> Installing the systemd service"
cp jellyfin-notifier.service /etc/systemd/system/jellyfin-notifier.service
systemctl daemon-reload

echo "==> Sudo rights for the admin interface (start/stop/restart + logs)"
SUDOERS_FILE=/etc/sudoers.d/jellyfin-notifier-admin
cat > "$SUDOERS_FILE" <<'EOF'
jellyshare ALL=(root) NOPASSWD: /bin/systemctl start jellyfin-notifier, /bin/systemctl stop jellyfin-notifier, /bin/systemctl restart jellyfin-notifier, /usr/bin/journalctl -u jellyfin-notifier *
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" >/dev/null || echo "    WARNING: invalid sudoers file, check ${SUDOERS_FILE} by hand"

echo
echo "Installed into ${TARGET}."
echo "Remaining steps:"
echo "  1. systemctl enable --now jellyfin-notifier"
echo "  2. journalctl -u jellyfin-notifier -f     (to check it starts correctly)"
echo "  3. Interface: http://<ip>:5005/"
echo "     If ${TARGET}/.env isn't filled in yet, this lands on the /setup wizard"
echo "     to fill it in from the browser - no need to edit it by hand first"
echo "     (nano ${TARGET}/.env also works, if you prefer)."
