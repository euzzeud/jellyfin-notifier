#!/usr/bin/env bash
# Met à jour une installation EXISTANTE de jellyfin-notifier dans /opt/jellyfin-notifier
# (contrairement à install.sh, ne touche pas au .env ni aux données déjà présentes :
# seen_items.json, settings.json, pending_items.json, upcoming.json).
# À lancer en root, DEPUIS le dossier décompressé (jellyfin-notifier/).
set -euo pipefail

TARGET=/opt/jellyfin-notifier
SERVICE_USER=jellyshare

if [ "$EUID" -ne 0 ]; then
  echo "Lance ce script en root (sudo -i puis relance, ou 'sudo ./update.sh')." >&2
  exit 1
fi

if [ ! -d "$TARGET" ]; then
  echo "Aucune installation trouvée dans ${TARGET} - utilise install.sh pour une première installation." >&2
  exit 1
fi

echo "==> Arrêt du service"
systemctl stop jellyfin-notifier || true

echo "==> Sauvegarde rapide (.env, données) avant mise à jour"
BACKUP_DIR="${TARGET}.backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
for f in .env seen_items.json settings.json pending_items.json upcoming.json; do
  [ -f "${TARGET}/${f}" ] && cp "${TARGET}/${f}" "$BACKUP_DIR/"
done
echo "    -> sauvegardé dans ${BACKUP_DIR}"

echo "==> Copie du code mis à jour (sans écraser .env ni les données)"
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

echo "==> Mise à jour des dépendances Python"
venv/bin/pip install -r requirements.txt -q

echo "==> Ajout des nouvelles clés dans .env si absentes (ADMIN_USERNAME/ADMIN_PASSWORD, etc.)"
add_if_missing() {
  local key="$1" default="$2"
  grep -q "^${key}=" .env 2>/dev/null || echo "${key}=${default}" >> .env
}
add_if_missing ADMIN_USERNAME admin
add_if_missing ADMIN_PASSWORD change-moi
add_if_missing SETTINGS_PATH settings.json
add_if_missing PENDING_ITEMS_PATH pending_items.json
add_if_missing UPCOMING_PATH upcoming.json

echo "==> Droits sudo pour l'interface d'admin (start/stop/restart + logs)"
SUDOERS_FILE=/etc/sudoers.d/jellyfin-notifier-admin
cat > "$SUDOERS_FILE" <<EOF
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/systemctl start jellyfin-notifier, /bin/systemctl stop jellyfin-notifier, /bin/systemctl restart jellyfin-notifier, /usr/bin/journalctl -u jellyfin-notifier *
EOF
chmod 440 "$SUDOERS_FILE"
visudo -c -f "$SUDOERS_FILE" >/dev/null || echo "    ATTENTION: fichier sudoers invalide, vérifie ${SUDOERS_FILE} à la main"

echo "==> Permissions"
if id "$SERVICE_USER" &>/dev/null; then
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$TARGET"
fi

echo "==> Rechargement du service systemd"
cp jellyfin-notifier.service /etc/systemd/system/jellyfin-notifier.service
systemctl daemon-reload

echo "==> Vérification de la syntaxe Python"
venv/bin/python -c "
import ast, pathlib
for f in pathlib.Path('jellyfin_notifier').rglob('*.py'):
    ast.parse(f.read_text())
print('OK: tous les fichiers .py sont syntaxiquement valides')
"

echo "==> Redémarrage du service"
systemctl start jellyfin-notifier
sleep 2
systemctl status jellyfin-notifier --no-pager -l | head -n 12

echo
echo "Mise à jour terminée."
echo "IMPORTANT : édite ${TARGET}/.env pour définir ADMIN_USERNAME / ADMIN_PASSWORD si ce n'est pas déjà fait,"
echo "puis 'systemctl restart jellyfin-notifier'."
echo "Interface d'admin : http://<ip-du-lxc>:5005/admin"
