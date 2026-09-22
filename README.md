# jellyfin-notifier

Service Python/Flask auto-hébergé qui envoie un mail (Gmail) quand un nouveau film ou série est ajouté à un serveur Jellyfin. Chaque destinataire reçoit son propre mail individuel (`To:` avec une seule adresse, pas de fuite d'adresse entre destinataires).

## Fonctionnement

Pas de webhook Jellyfin (jugé peu fiable en pratique) : un **poller** interroge l'API Jellyfin toutes les `POLL_INTERVAL_SECONDS` (300s par défaut), compare aux IDs déjà vus (`seen_items.json`), et détecte les nouveaux items (`Movie`/`Series` par défaut). Une fenêtre de récupération élargie (`Limit=200`, tri `DateCreated,SortName`) évite qu'un gros import en masse "flotte" entre deux polls, et les `BoxSet` (collections auto-générées) sont exclus.

Au premier lancement, tout le catalogue existant est enregistré comme "déjà vu" sans envoyer de mail (bootstrap).

## Fonctionnalités

- **Mail par destinataire**, enrichi (synopsis tronqué anti-spoiler, note, genres, durée formatée).
- **Anti faux-positifs** sur les imports en masse.
- **Interface d'admin** (`/admin`, HTTP Basic Auth) :
  - Dashboard : état du service systemd + du poller, créneau d'envoi, file d'attente, logs, start/stop/restart.
  - Planning : jours/plage horaire autorisés pour l'envoi (gère le passage de minuit), longueur max du synopsis.
  - File d'attente hors créneau : items détectés hors créneau, envoyés en un digest à l'ouverture du prochain créneau.
  - Éditeur de template Jinja2 (formulaire simple + HTML brut avec validation de syntaxe en temps réel), historique de sauvegardes.
  - Aperçu du prochain mail.
  - Titres à venir : annonce manuelle de contenus pas encore dans la bibliothèque.
  - Console API Jellyfin ad-hoc.
  - Gestion du service systemd + logs `journalctl` depuis l'admin.
- Endpoint `/health` pour le monitoring (Telegraf/Grafana ou autre).

## Structure

```
jellyfin_notifier/
  config.py           # config figée depuis .env
  settings.py         # paramètres modifiables à chaud (settings.json)
  schedule.py         # fenêtre horaire/jours autorisés
  pending.py          # file d'attente des items hors créneau
  upcoming.py         # titres "à venir" annoncés manuellement
  service_control.py  # start/stop/restart systemd + logs
  api_console.py       # requêtes API Jellyfin ad-hoc (admin)
  jellyfin_client.py   # client HTTP Jellyfin
  email_sender.py      # construction/envoi du mail (Jinja2 + SMTP Gmail)
  poller.py            # thread de polling
  admin.py             # blueprint Flask /admin
  webhook.py            # /health, /poll-now, ancien endpoint webhook
  templates/email.html
  templates/admin/*.html
run.py            # entrée Flask
install.sh         # première installation (+ règle sudoers)
update.sh          # mise à jour d'une installation existante
.env.example
jellyfin-notifier.service
preview_email.py   # aperçu HTML local sans réseau
```

## Installation

```bash
cp .env.example .env   # puis éditer .env
./install.sh
```

`install.sh` installe le service sous systemd (`jellyfin-notifier.service`) et met en place la règle `sudoers` NOPASSWD nécessaire pour que l'admin puisse start/stop/restart le service.

Pour mettre à jour une installation existante sans toucher `.env` ni les données (`update.sh` ajoute les clés manquantes) :

```bash
./update.sh
```

## Configuration

Voir `.env.example` pour la liste complète des variables (identifiants Gmail, destinataires, connexion Jellyfin, planning, identifiants admin, chemins des fichiers de données).

## Infrastructure de référence

- Jellyfin : conteneur Docker sur un LXC Proxmox.
- Service : tourne hors du conteneur Jellyfin, sur le LXC, piloté par systemd, port `5005`.
- Monitoring : endpoint `/health` scrapé (ex. Telegraf `inputs.exec` pour garantir un point de données même si le service est down).

## Licence

Projet personnel, non destiné à la distribution publique.
