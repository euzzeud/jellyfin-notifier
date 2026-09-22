#!/usr/bin/env bash
# Installe jellyfin-notifier dans /opt/jellyfin-notifier et configure le service systemd.
# À lancer en root, DEPUIS le dossier décompressé (jellyfin-notifier/).
set -euo pipefail

TARGET=/opt/jellyfin-notifier
SERVICE_USER=jellyshare

if [ "$EUID" -ne 0 ]; then
  echo "Lance ce script en root (sudo -i puis relance, ou 'sudo ./install.sh')." >&2
  exit 1
fi

echo "==> Copie des fichiers vers ${TARGET}"
mkdir -p "$TARGET"
shopt -s dotglob   # pour que le glob ci-dessous inclue aussi les fichiers cachés (.env.example)
cp -r ./* "$TARGET"/
shopt -u dotglob
rm -rf "$TARGET"/install.sh "$TARGET"/preview.html

cd "$TARGET"

echo "==> Création du virtualenv"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  echo "==> Création de .env à partir de .env.example (À COMPLÉTER)"
  cp .env.example .env
  echo "    -> édite maintenant : nano ${TARGET}/.env"
else
  echo "==> .env existe déjà, je ne l'écrase pas"
fi

echo "==> Permissions"
if id "$SERVICE_USER" &>/dev/null; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET"
else
  echo "    (utilisateur $SERVICE_USER introuvable, ownership laissé tel quel)"
fi

echo "==> Installation du service systemd"
cp jellyfin-notifier.service /etc/systemd/system/jellyfin-notifier.service
systemctl daemon-reload

echo "==> Droits sudo pour l'interface d'admin (start/stop/restart + logs)"
SUDOERS_FILE=/etc/sudoers.d/jellyfin-notifier-admin
cat > "$SUDOERS_FILE" <<'EOF'
jellyshare ALL=(root) NOPASSWD: /bin/systemctl start jellyfin-notifier, /bin/systemctl stop jellyfin-notifier, /bin/systemctl restart jellyfin-notifier, /usr/bin/journalctl -u jellyfin-notifier *
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" >/dev/null || echo "    ATTENTION: fichier sudoers invalide, vérifie ${SUDOERS_FILE} à la main"

echo
echo "Installation copiée dans ${TARGET}."
echo "Étapes restantes :"
echo "  1. nano ${TARGET}/.env         (renseigne GMAIL_APP_PASSWORD, NOTIFY_RECIPIENTS, JELLYFIN_API_KEY, JELLYFIN_URL, ADMIN_USERNAME/ADMIN_PASSWORD)"
echo "  2. systemctl enable --now jellyfin-notifier"
echo "  3. journalctl -u jellyfin-notifier -f     (pour vérifier que ça démarre bien)"
echo "  4. Interface d'admin : http://<ip-du-lxc>:5005/"
