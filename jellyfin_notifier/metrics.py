"""In-memory counters for detailed monitoring (the /metrics endpoint),
meant to be scraped by Telegraf (`inputs.http`, `data_format = "json"`)
and graphed/alerted on in Grafana + InfluxDB - complementing /health, which
stays deliberately minimal (just enough to know the service is up and
"healthy", for a simple Uptime Kuma-style alert).

Like any in-process counter, these values reset to zero on every service
restart: that's the standard behavior InfluxDB/Grafana expect for this kind
of cumulative counter (see Grafana's `non_negative_derivative()` function,
built precisely to compute a per-second rate despite restarts), not a bug
to fix by persisting the counters to disk."""

from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime

_lock = threading.Lock()
_START_TIME = datetime.now().astimezone()

_http_by_class: Counter = Counter()  # "2xx" / "3xx" / "4xx" / "5xx"
_mail_sent: Counter = Counter()  # by scope: new / upcoming / test
_mail_failed: Counter = Counter()
_login: Counter = Counter()  # success / failure

_poll_cycles_total = 0
_poll_errors_total = 0
_poll_last_duration_ms = 0.0
_poll_duration_ms_sum = 0.0


def inc_http(status_code: int) -> None:
    cls = f"{status_code // 100}xx"
    with _lock:
        _http_by_class[cls] += 1


def inc_mail(scope: str, success: bool) -> None:
    with _lock:
        (_mail_sent if success else _mail_failed)[scope] += 1


def inc_login(success: bool) -> None:
    with _lock:
        _login["success" if success else "failure"] += 1


def record_poll(duration_seconds: float, success: bool) -> None:
    global _poll_cycles_total, _poll_errors_total, _poll_last_duration_ms, _poll_duration_ms_sum
    with _lock:
        _poll_cycles_total += 1
        if not success:
            _poll_errors_total += 1
        ms = duration_seconds * 1000
        _poll_last_duration_ms = ms
        _poll_duration_ms_sum += ms


def snapshot() -> dict:
    """Snapshot of all counters, deliberately FLAT (a single level) - the
    "classic" Telegraf (v1) JSON parser doesn't necessarily descend into
    nested objects. Same keys from one call to the next (even at 0), so
    Telegraf/InfluxDB always sees the same field schema."""
    with _lock:
        now = datetime.now().astimezone()
        uptime = (now - _START_TIME).total_seconds()
        avg_poll_ms = (_poll_duration_ms_sum / _poll_cycles_total) if _poll_cycles_total else 0.0
        return {
            "process_start_time": _START_TIME.isoformat(timespec="seconds"),
            "uptime_seconds": round(uptime, 1),

            "http_requests_total": sum(_http_by_class.values()),
            "http_2xx_total": _http_by_class["2xx"],
            "http_3xx_total": _http_by_class["3xx"],
            "http_4xx_total": _http_by_class["4xx"],
            "http_5xx_total": _http_by_class["5xx"],

            "login_success_total": _login["success"],
            "login_failure_total": _login["failure"],

            "poll_cycles_total": _poll_cycles_total,
            "poll_errors_total": _poll_errors_total,
            "poll_last_duration_ms": round(_poll_last_duration_ms, 1),
            "poll_avg_duration_ms": round(avg_poll_ms, 1),

            "mail_sent_total": sum(_mail_sent.values()),
            "mail_failed_total": sum(_mail_failed.values()),
            "mail_new_sent_total": _mail_sent["new"],
            "mail_new_failed_total": _mail_failed["new"],
            "mail_upcoming_sent_total": _mail_sent["upcoming"],
            "mail_upcoming_failed_total": _mail_failed["upcoming"],
            "mail_test_sent_total": _mail_sent["test"],
            "mail_test_failed_total": _mail_failed["test"],
        }
