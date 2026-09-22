"""Gestion du service systemd (status/start/stop/restart) et lecture des
logs journalctl, exposées par l'interface admin.

Le service tourne sous l'utilisateur "jellyshare" (cf. jellyfin-notifier.service),
donc start/stop/restart nécessitent sudo -> une règle sudoers NOPASSWD dédiée
est ajoutée par install.sh/update.sh (voir ADMIN_SUDOERS_HINT ci-dessous)."""

from __future__ import annotations

import subprocess

SERVICE_NAME = "jellyfin-notifier"

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
