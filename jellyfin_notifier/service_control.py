"""Gestion du service systemd (status/start/stop/restart) et lecture des
logs journalctl, exposées par l'interface admin.

Le service tourne sous l'utilisateur "jellyshare" (cf. jellyfin-notifier.service),
donc start/stop/restart nécessitent sudo -> une règle sudoers NOPASSWD dédiée
est ajoutée par install.sh/update.sh (voir ADMIN_SUDOERS_HINT ci-dessous)."""

from __future__ import annotations

import re
import subprocess

SERVICE_NAME = "jellyfin-notifier"

# journalctl capture le stdout/stderr du process tel quel : chaque ligne
# émise par logging.basicConfig() (cf. run.py) a la forme
# "LEVEL:logger.name:message", à laquelle journalctl ajoute son propre
# préfixe devant (date, host, nom du process, pid) - qu'on garde tel quel
# dans "raw" pour l'affichage, on l'extrait juste pour catégoriser.
_LEVEL_RE = re.compile(r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):(?P<logger>[\w.]+):(?P<msg>.*)$")

# Catégorise chaque ligne par "domaine" plutôt que par nom de module exact :
# "interface" = actions/requêtes de l'admin web, "service" = boucle de fond
# (poller/webhook), "mail" = envois SMTP - permet à la page Logs de séparer
# clairement ce qui vient d'un clic dans l'admin de ce qui tourne tout seul.
_CATEGORY_BY_LOGGER_PREFIX = (
    ("jellyfin_notifier.email_sender", "mail"),
    ("jellyfin_notifier.interface", "interface"),
    ("jellyfin_notifier.admin", "interface"),
    ("werkzeug", "interface"),
    ("jellyfin_notifier.poller", "service"),
    ("jellyfin_notifier.webhook", "service"),
    ("jellyfin_notifier.buffer", "service"),
    ("jellyfin_notifier.jellyfin_client", "service"),
)

CATEGORIES = ("interface", "service", "mail", "other")


def parse_log_line(line: str) -> dict:
    """Extrait le niveau de sévérité et la catégorie d'une ligne de log
    journalctl brute. Les lignes sans préfixe LEVEL:logger: reconnu (suite
    d'une traceback multi-lignes, message "système" - démarrage de waitress,
    unité systemd, etc) tombent dans la catégorie "other", en ERROR si elles
    ressemblent à une trace d'exception."""
    match = _LEVEL_RE.search(line)
    if not match:
        level = "ERROR" if ("Traceback" in line or "Exception" in line or " raise " in line) else "OTHER"
        return {"raw": line, "level": level, "category": "other", "logger": ""}

    level = match.group("level")
    logger_name = match.group("logger")
    category = "other"
    for prefix, cat in _CATEGORY_BY_LOGGER_PREFIX:
        if logger_name == prefix or logger_name.startswith(prefix + "."):
            category = cat
            break
    return {"raw": line, "level": level, "category": category, "logger": logger_name}

ADMIN_SUDOERS_HINT = (
    f"jellyshare ALL=(root) NOPASSWD: "
    f"/bin/systemctl start {SERVICE_NAME}, "
    f"/bin/systemctl stop {SERVICE_NAME}, "
    f"/bin/systemctl restart {SERVICE_NAME}, "
    f"/usr/bin/journalctl -u {SERVICE_NAME} *"
)


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except FileNotFoundError:
        # `systemctl`/`journalctl`/`sudo` don't exist at all (ex: running
        # locally on Windows/macOS for development) - a raw OS error message
        # ("[WinError 2] The system cannot find the file specified") is
        # confusing on its own, so it's worth spelling out WHY explicitly:
        # this whole card/page only works under a real systemd deployment
        # (the target LXC), not a local dev run.
        return False, (
            f"'{cmd[0]}' was not found on this system. Service control and "
            "the Logs page require systemd (journalctl/systemctl), which "
            "only exists on the target Linux deployment - not when running "
            "locally for development (e.g. on Windows or macOS)."
        )
    except Exception as exc:
        return False, str(exc)


def get_status() -> dict:
    _, active_state = _run(["systemctl", "is-active", SERVICE_NAME])
    _, sub_state = _run(["systemctl", "show", SERVICE_NAME, "--property=SubState", "--value"])
    _, since = _run(["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestamp", "--value"])
    return {
        "active": active_state.strip() or "unknown",
        "sub_state": sub_state.strip(),
        "is_running": active_state.strip() == "active",
        "since": since.strip(),
    }


def service_action(action: str) -> tuple[bool, str]:
    if action not in ("start", "stop", "restart"):
        return False, "Unknown action."
    ok, output = _run(["sudo", "-n", "systemctl", action, SERVICE_NAME])
    if not ok and ("password" in output.lower() or "sudo" in output.lower() or not output):
        output += (
            "\n\nHint: the service account may be missing the required sudo "
            f"rights. Add this line with `visudo`:\n{ADMIN_SUDOERS_HINT}"
        )
    return ok, output


def get_logs(lines: int = 200) -> str:
    ok, output = _run(["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"], timeout=20)
    if ok and output:
        return output
    ok2, output2 = _run(
        ["sudo", "-n", "journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"], timeout=20
    )
    if ok2 and output2:
        return output2
    return output or output2 or "Unable to read logs (insufficient permissions?)."


def get_logs_structured(lines: int = 300) -> list[dict]:
    """Mêmes logs que get_logs(), mais parsés ligne par ligne pour la page
    Logs de l'admin (filtres par catégorie, coloration par sévérité)."""
    raw_text = get_logs(lines)
    return [parse_log_line(line) for line in raw_text.splitlines() if line.strip()]
