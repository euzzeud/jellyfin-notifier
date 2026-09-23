# jellyfin-notifier

Self-hosted Python/Flask service that sends a mail (via any standard SMTP provider) whenever a new movie or series is added to a Jellyfin server. Each recipient gets their own individual mail (`To:` with a single address, no address leaking between recipients).

## How it works

No Jellyfin webhook (found unreliable in practice): a **poller** queries the Jellyfin API every `POLL_INTERVAL_SECONDS` (300s by default), compares against already-seen IDs (`seen_items.json`), and detects new items (`Movie`/`Series` by default). A widened fetch window (`Limit=200`, sorted by `DateCreated,SortName`) prevents a large bulk import from "floating" between two polls, and `BoxSet`s (auto-generated collections) are excluded.

On first run, the entire existing catalog is recorded as "already seen" without sending any mail (bootstrap).

## Features

- **Per-recipient mail**, enriched (spoiler-safe truncated synopsis, rating, genres, formatted duration).
- **False-positive protection** on bulk imports.
- **Admin interface** (site root `/`, protected by a dedicated login page):
  - Dashboard: systemd service + poller status, send window, pending queue, logs, start/stop/restart.
  - Schedule: allowed days/hours for sending (handles crossing midnight), max synopsis length.
  - Outside-window queue: items detected outside the allowed window, sent as a single digest once the next window opens.
  - Jinja2 template editor (simple form + raw HTML with real-time syntax validation), save history.
  - Preview of the next mail.
  - Upcoming titles: manual announcement of content not yet in the library.
  - Ad-hoc Jellyfin API console.
  - Mail server (SMTP): host/port/encryption, credentials, sender, recipients — any standard SMTP provider, plus sending a test mail.
  - systemd service management + `journalctl` logs from the admin.
- `/health` endpoint for monitoring (Telegraf/Grafana or other).

## Structure

```
jellyfin_notifier/
  config.py           # fixed config from .env
  settings.py         # settings that can be changed on the fly (settings.json)
  schedule.py         # allowed time window/days
  pending.py          # queue of items outside the allowed window
  upcoming.py         # manually announced "upcoming" titles
  service_control.py  # systemd start/stop/restart + logs
  api_console.py       # ad-hoc Jellyfin API requests (admin)
  jellyfin_client.py   # Jellyfin HTTP client
  email_sender.py      # building/sending the mail (Jinja2 + SMTP)
  poller.py            # polling thread
  admin.py             # Flask blueprint, mounted at the site root
  webhook.py            # /health, /poll-now, the old webhook endpoint
  templates/email.html
  templates/admin/*.html
run.py            # Flask entry point
install.sh         # first-time install (+ sudoers rule)
update.sh          # updating an existing install
.env.example
jellyfin-notifier.service
preview_email.py   # local HTML preview, no network needed
```

## Installation

```bash
cp .env.example .env   # then edit .env
./install.sh
```

`install.sh` installs the service under systemd (`jellyfin-notifier.service`) and sets up the NOPASSWD `sudoers` rule needed so the admin can start/stop/restart the service.

To update an existing install without touching `.env` or the data (`update.sh` adds any missing keys):

```bash
./update.sh
```

## Configuration

See `.env.example` for the full list of variables (SMTP credentials, recipients, Jellyfin connector, schedule, admin credentials, data file paths).

## Reference infrastructure

- Jellyfin: a Docker container on a Proxmox LXC.
- Service: runs outside the Jellyfin container, on the LXC, managed by systemd, port `5005`.
- Monitoring: the `/health` endpoint is scraped (e.g. Telegraf `inputs.exec`, to guarantee a data point even when the service is down).

## License

Personal project, not intended for public distribution.
