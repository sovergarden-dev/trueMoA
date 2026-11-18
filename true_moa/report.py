"""Append-only recovery log for the True MoA gate.

One JSON object per line, one file per day. Every entry is a fact about the gate
itself — which slot failed, how many attempts ran, how long the ladder held, when
the aggregator was blocked. No prompt text, no message bodies, no credentials:
slot labels are provider/model names, which is exactly what a reviewer needs and
nothing more.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
REPORT_DIR = PLUGIN_DIR / "reports"

_lock = threading.Lock()


def _path_for(when: float) -> Path:
    return REPORT_DIR / f"gate-{time.strftime('%Y%m%d', time.localtime(when))}.jsonl"


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


def summary() -> dict[str, Any]:
    """Counters for the compliance block: retries, outages, blocked fallbacks."""
    files = sorted(REPORT_DIR.glob("gate-*.jsonl")) if REPORT_DIR.exists() else []
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
