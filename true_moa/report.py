"""Append-only recovery log for the True MoA gate.

One JSON object per line, one file per day. Every entry is a fact about the gate
itself — which slot failed, how many attempts ran, how long the ladder held, when
the aggregator was blocked. No prompt text, no message bodies, no credentials:
slot labels are provider/model names, which is exactly what a reviewer needs and
nothing more.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PLUGIN_DIR / "reports"
RETENTION_DAYS = 90

_lock = threading.Lock()
_last_pruned_day = ""


def _path_for(when: float) -> Path:
    return REPORT_DIR / f"gate-{time.strftime('%Y%m%d', time.localtime(when))}.jsonl"


def _prune_expired(now: float) -> None:
    global _last_pruned_day
    today_str = time.strftime("%Y%m%d", time.localtime(now))
    if _last_pruned_day == today_str or RETENTION_DAYS <= 0:
        return
    _last_pruned_day = today_str
    cutoff = datetime.datetime.fromtimestamp(now).date() - datetime.timedelta(days=RETENTION_DAYS)
    for path in REPORT_DIR.glob("gate-*.jsonl"):
        stem = path.stem
        date_part = stem[5:] if stem.startswith("gate-") else ""
        if len(date_part) != 8 or not date_part.isdigit():
            continue
        try:
            file_date = datetime.date(int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8]))
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def event(name: str, **fields: Any) -> None:
    """Record one gate event. Never raises — a broken log must not break a turn."""
    now = time.time()
    record = {"ts": round(now, 3), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "event": name}
    for key, value in fields.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            record[key] = value
        elif isinstance(value, (list, tuple, set)):
            record[key] = [str(item) for item in value][:16]
        else:
            record[key] = str(value)[:400]
    try:
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError):
        return
    try:
        with _lock:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            _prune_expired(now)
            with _path_for(now).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        logger.debug("true-moa: report write failed: %s", exc)


def tail(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent entries, newest last. Used by `/true-moa status`."""
    try:
        files = sorted(REPORT_DIR.glob("gate-*.jsonl"))
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for path in reversed(files):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
            if len(rows) >= limit:
                return list(reversed(rows))
    return list(reversed(rows))


def summary(window_days: int = RETENTION_DAYS) -> dict[str, Any]:
    """Counters for the compliance block over the active retention window."""
    files = sorted(REPORT_DIR.glob("gate-*.jsonl")) if REPORT_DIR.exists() else []
    if window_days > 0 and len(files) > window_days:
        files = files[-window_days:]
    counters = {
        "reference_retry": 0,
        "reference_fallback_switched": 0,
        "reference_outage": 0,
        "aggregator_fallback_blocked": 0,
        "gate_installed": 0,
    }
    days = len(files)
    last: dict[str, Any] | None = None
    for path in files:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    name = row.get("event")
                    if name in counters:
                        counters[name] += 1
                    last = row
        except OSError:
            continue
    return {"days": days, "counters": counters, "last": last}
