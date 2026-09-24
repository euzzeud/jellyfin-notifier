"""Management of the systemd service (status/start/stop/restart) and
reading journalctl logs, exposed by the admin interface.

The service runs as the "jellyshare" user (see jellyfin-notifier.service),
so start/stop/restart need sudo -> a dedicated NOPASSWD sudoers rule is
added by install.sh/update.sh (see ADMIN_SUDOERS_HINT below)."""

from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "jellyfin-notifier"

# jellyfin_notifier/service_control.py -> parents[1] is the repo root that
# holds run.py, venv/ and .env - same layout install.sh sets up under
# /opt/jellyfin-notifier, just wherever THIS copy actually happens to be
# running from (a manual git clone, tested locally, etc). Used to generate
# a systemd unit for an install that didn't go through install.sh.
_APP_DIR = Path(__file__).resolve().parents[1]

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
        # confusing on its own, so this says what's actually missing.
        # get_status() turns this into a plain "unknown" for the status
        # pill rather than showing it directly; service_action()/get_logs()
        # still surface it (toast/logs page), so it's kept short there too.
        return False, f"'{cmd[0]}' was not found on this system."
    except Exception as exc:
        return False, str(exc)


def is_unit_installed() -> bool:
    """Whether the jellyfin-notifier.service UNIT FILE exists at all (not
    whether it's running) - `systemctl show` doesn't error out for a
    missing unit like `systemctl status` does, it just reports
    LoadState=not-found, which is what turns the dashboard's "Systemd
    status" pill into "unknown" (see get_status() below): most often this
    means the app is being run directly (`python3 run.py`, e.g. during
    local testing) rather than through the systemd service install.sh
    sets up, not that anything is actually broken."""
    ok, load_state = _run(["systemctl", "show", SERVICE_NAME, "--property=LoadState", "--value"])
    return ok and load_state.strip() == "loaded"


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
        "unit_installed": is_unit_installed(),
    }


def get_systemd_install_command() -> str | None:
    """A copy-paste shell block that installs THIS already-running copy of
    the app (wherever it was cloned to, whatever venv/user it's using) as a
    systemd service - the same end result as install.sh's systemd steps,
    but generated for an install that got here by just running
    `python3 run.py` directly (e.g. local/dev testing) instead of going
    through that script, so the admin doesn't have to hand-adapt the
    packaged jellyfin-notifier.service (which hardcodes /opt/jellyfin-notifier
    and a "jellyshare" user) themselves.

    Deliberately NOT run automatically from here: writing into
    /etc/systemd/system and /etc/sudoers.d needs root, and this process
    itself only has whatever privileges the admin it's running as has - with
    no sudo rights configured yet (that's exactly what's missing), a
    non-interactive `sudo -n` from inside the web app would just fail
    silently or hang waiting for a password nobody can type into it. A
    one-time copy-paste into a real terminal is the honest way to cross
    that privilege boundary.

    Returns None when `systemctl` isn't even installed (e.g. WSL without
    systemd enabled) - the command would just fail outright there, so the
    caller should show that limitation ("enable systemd in .wslconfig
    first") instead of a command that can't work.
    """
    if shutil.which("systemctl") is None:
        return None

    venv_python = _APP_DIR / "venv" / "bin" / "python"
    python_bin = str(venv_python) if venv_python.exists() else sys.executable
    user = getpass.getuser()

    unit_file = (
        "[Unit]\n"
        "Description=Jellyfin notifier - sends an email when new content is added\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_APP_DIR}\n"
        f"EnvironmentFile={_APP_DIR}/.env\n"
        f"ExecStart={python_bin} {_APP_DIR}/run.py\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"User={user}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    sudoers_line = (
        f"{user} ALL=(root) NOPASSWD: /bin/systemctl start {SERVICE_NAME}, "
        f"/bin/systemctl stop {SERVICE_NAME}, /bin/systemctl restart {SERVICE_NAME}, "
        f"/usr/bin/journalctl -u {SERVICE_NAME} *"
    )
    sudoers_path = f"/etc/sudoers.d/{SERVICE_NAME}-admin"

    return (
        f"sudo tee /etc/systemd/system/{SERVICE_NAME}.service > /dev/null <<'EOF'\n"
        f"{unit_file}"
        "EOF\n"
        f"sudo tee {sudoers_path} > /dev/null <<'EOF'\n"
        f"{sudoers_line}\n"
        "EOF\n"
        f"sudo chmod 440 {sudoers_path}\n"
        "sudo systemctl daemon-reload\n"
        f"sudo systemctl enable --now {SERVICE_NAME}"
    )


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
