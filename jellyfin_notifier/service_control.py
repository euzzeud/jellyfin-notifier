"""Management of the systemd service (status/start/stop/restart) and
reading journalctl logs, exposed by the admin interface.

The service runs as the "jellyshare" user (see jellyfin-notifier.service),
so start/stop/restart need sudo -> a dedicated NOPASSWD sudoers rule is
added by install.sh/update.sh (see ADMIN_SUDOERS_HINT below)."""

from __future__ import annotations

import re
import subprocess

SERVICE_NAME = "jellyfin-notifier"

# journalctl captures the process's stdout/stderr as-is: each line
# emitted by logging.basicConfig() (see run.py) has the form
# "LEVEL:logger.name:message", to which journalctl prepends its own
# prefix (date, host, process name, pid) - kept as-is in "raw" for
# display, we just extract it to categorize.
_LEVEL_RE = re.compile(r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL):(?P<logger>[\w.]+):(?P<msg>.*)$")

# Categorizes each line by "domain" rather than exact module name:
# "interface" = web admin actions/requests, "service" = background loop
# (poller/webhook), "mail" = SMTP sends - lets the Logs page clearly
# separate what comes from a click in the admin from what runs on its own.
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
    """Extracts the severity level and category from a raw journalctl log
    line. Lines without a recognized LEVEL:logger: prefix (continuation of
    a multi-line traceback, a "system" message - waitress startup, systemd
    unit, etc) fall into the "other" category, as ERROR if they look like
    an exception trace."""
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
    # Each _run() result is only meaningful when `ok` - on failure (not
    # found, permission denied, timeout...) the second value is an error
    # message, not a status string, and must NOT be displayed as if it were
    # one (that used to dump the whole "'systemctl' was not found on this
    # system..." explanation into the status pill - technically accurate,
    # but a wall of red text where a plain "unknown" belongs).
    active_ok, active_state = _run(["systemctl", "is-active", SERVICE_NAME])
    _, sub_state = _run(["systemctl", "show", SERVICE_NAME, "--property=SubState", "--value"])
    since_ok, since = _run(["systemctl", "show", SERVICE_NAME, "--property=ActiveEnterTimestamp", "--value"])
    return {
        "active": active_state.strip() if active_ok and active_state.strip() else "unknown",
        "sub_state": sub_state.strip(),
        "is_running": active_ok and active_state.strip() == "active",
        "since": since.strip() if since_ok else "",
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
    """Same logs as get_logs(), but parsed line by line for the admin's
    Logs page (filters by category, coloring by severity)."""
    raw_text = get_logs(lines)
    return [parse_log_line(line) for line in raw_text.splitlines() if line.strip()]
