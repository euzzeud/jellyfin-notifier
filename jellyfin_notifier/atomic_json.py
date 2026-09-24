"""Shared helper for writing JSON (or any text) to disk without risking a
half-written/corrupted file if the process is interrupted mid-write - a
`systemctl restart` racing an in-flight save, a full disk, a `kill -9`, a
power loss. Every JSON store in this app (settings, the pending-items
queue, upcoming titles, mail history, the poller's seen-items set) used to
write via `Path.write_text()` directly: on a crash between "file
truncated" and "new content flushed", that leaves a 0-byte or truncated
file behind, which the matching `_load()` (a bare try/except around
`json.loads()`) then silently treats as "no data" - a lost settings file,
pending queue, or seen-items set, not just a startup error.

Every store now writes through atomic_write_text() instead: it writes the
new content to a temp file in the SAME directory as the target (so the
final swap is a same-filesystem rename, atomic on both POSIX and Windows -
a temp file elsewhere, e.g. in /tmp, would risk a slow/non-atomic copy
across filesystems), fsyncs it, then swaps it into place with
os.replace(). The file at `path` is therefore always either the old
content or the new content in full - never a partial write - and a reader
never needs to handle a "torn" file mid-write.

This only protects a single write from being torn by a crash. It doesn't
serialize two concurrent writers (the admin web UI saving a settings
change while the poller thread is also writing) - each call site still
needs its own lock for that, same as before (see settings.py, pending.py,
upcoming.py, mail_history.py, poller.py)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure (including
        # Ctrl-C/SystemExit, hence BaseException) - os.replace() itself is
        # atomic so it can't leave `path` half-written, but a failure
        # before that point (e.g. disk full while writing the temp file)
        # would otherwise leave a stray .tmp file behind indefinitely.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
