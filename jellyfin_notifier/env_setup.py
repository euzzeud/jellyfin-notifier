"""Reads and writes the .env file from Python, using .env.example as the
field template (keys, help text, defaults, order) - powers the first-run
setup wizard (shown automatically when .env is missing or incomplete) and
the "Environment" admin page that lets .env be edited from the web
interface afterwards, instead of by hand on the server."""

from __future__ import annotations

import re
from pathlib import Path

# Keys the setup wizard treats as secrets: never pre-filled back into the
# form (so they're never shown in the page source), only overwritten if the
# admin actually types a new value.
SECRET_KEYS = {
    "SMTP_PASSWORD", "GMAIL_APP_PASSWORD", "ADMIN_PASSWORD",
    "JELLYFIN_API_KEY", "WEBHOOK_SHARED_SECRET",
}

# Keys required for Config.from_env() to succeed at all (mirrors config.py) -
# used to decide whether the app can boot normally or must show the wizard.
REQUIRED_KEYS = ("SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_RECIPIENTS", "ADMIN_USERNAME", "ADMIN_PASSWORD")

# .env.example lives at the project root, one directory above this package.
ENV_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / ".env.example"

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parses a KEY=VALUE file (comments and blank lines ignored) into a
    dict. Returns {} if the file doesn't exist - a missing .env is a normal,
    expected state (first run), not an error."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(stripped)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def load_into_environ(path: Path) -> None:
    """Loads .env into os.environ (only if not already set there - an env
    var provided by systemd's EnvironmentFile= or the shell always wins).
    Lets the app pick up .env directly from disk, independent of systemd,
    which is what makes the setup wizard's "restart" actually apply the
    values it just wrote."""
    import os

    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)


def field_spec() -> list[dict]:
    """Parses .env.example into an ordered list of fields (help text + key +
    default value) for rendering the setup form - so the form always mirrors
    .env.example instead of duplicating its content by hand and drifting out
    of sync with it."""
    fields: list[dict] = []
    comment_buf: list[str] = []
    if not ENV_EXAMPLE_PATH.exists():
        return fields
    for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            comment_buf = []
            continue
        if line.startswith("#"):
            comment_buf.append(line.lstrip("#").strip())
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, default = match.group(1), match.group(2)
        fields.append({
            "key": key,
            "default": default,
            "help": " ".join(comment_buf),
            "secret": key in SECRET_KEYS,
            "required": key in REQUIRED_KEYS,
        })
        comment_buf = []
    return fields


def write_env_file(env_path: Path, submitted: dict[str, str]) -> None:
    """Writes .env, keeping .env.example's comments/order - each key's value
    comes from `submitted` if present there, else from the existing .env (so
    fields the admin left untouched, notably secrets, are preserved), else
    from .env.example's own default. Any key the file already has that isn't
    in .env.example (a leftover custom variable) is kept, appended at the
    end, instead of silently dropped."""
    existing = parse_env_file(env_path)
    known_keys = {f["key"] for f in field_spec()}

    lines: list[str] = []
    if ENV_EXAMPLE_PATH.exists():
        for raw_line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
            match = _LINE_RE.match(raw_line.strip())
            if match:
                key = match.group(1)
                value = submitted.get(key)
                if value is None:
                    value = existing.get(key, match.group(2))
                lines.append(f"{key}={value}")
            else:
                lines.append(raw_line)
    else:
        for key, value in submitted.items():
            lines.append(f"{key}={value}")

    extra_keys = [k for k in existing if k not in known_keys]
    extra_keys += [k for k in submitted if k not in known_keys and k not in existing]
    for key in dict.fromkeys(extra_keys):
        lines.append(f"{key}={submitted.get(key, existing.get(key, ''))}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def import_env_file(env_path: Path, uploaded_text: str) -> tuple[bool, str]:
    """Validates an uploaded .env file is at least well-formed (KEY=VALUE /
    comment / blank lines only) before overwriting the real one with it."""
    if not uploaded_text.strip():
        return False, "The uploaded file is empty."
    for line in uploaded_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not _LINE_RE.match(stripped):
            return False, f"Invalid line (expected KEY=VALUE): {stripped[:80]!r}"
    text = uploaded_text if uploaded_text.endswith("\n") else uploaded_text + "\n"
    env_path.write_text(text, encoding="utf-8")
    return True, ""


def missing_required_keys(values: dict[str, str]) -> list[str]:
    return [key for key in REQUIRED_KEYS if not values.get(key)]
