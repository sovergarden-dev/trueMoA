"""Append-only recovery log for the True MoA gate.

One JSON object per line, one file per day under
``<HERMES_HOME>/plugin-data/true-moa/reports/``. Storing logs in ``plugin-data``
keeps the plugin install directory immutable so Hermes PM's workspace stamp
remains clean across every turn. No prompt text, no message bodies, no credentials.
"""

from __future__ import annotations

import datetime
import json
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import state

logger = logging.getLogger(__name__)

# Optional sandbox override used by offline tests; None resolves dynamically to plugin-data/true-moa/reports.
REPORT_DIR: Path | None = None
RETENTION_DAYS = 90

_lock = threading.Lock()
_last_pruned_key: tuple[Path | None, str] = (None, "")
_legacy_reports_checked = False


def resolve_report_dir(*, ignore_override: bool = False) -> Path:
    """Return the active reports directory (sandbox override if set, else plugin-data)."""
    if not ignore_override and REPORT_DIR is not None:
        return REPORT_DIR
    return state.plugin_data_root() / "reports"


def migrate_legacy_reports(legacy_dir: Path | None = None, target_dir: Path | None = None) -> int:
    """Evacuate legacy ``plugins/true-moa/reports`` JSONL files into ``plugin-data/true-moa/reports``."""
    global _legacy_reports_checked
    default_paths = legacy_dir is None and target_dir is None
    if default_paths and _legacy_reports_checked:
        return 0
    src_dir = legacy_dir if legacy_dir is not None else (state.PLUGIN_DIR / "reports")
    dst_dir = target_dir if target_dir is not None else resolve_report_dir(ignore_override=True)
    with _lock:
        if default_paths and _legacy_reports_checked:
            return 0
        if not src_dir.exists() or not src_dir.is_dir():
            if default_paths:
                _legacy_reports_checked = True
            return 0
        try:
            if src_dir.resolve() == dst_dir.resolve():
                if default_paths:
                    _legacy_reports_checked = True
                return 0
            dst_dir.mkdir(parents=True, exist_ok=True)
            moved = 0
            for item in sorted(src_dir.iterdir()):
                if not item.is_file():
                    continue
                if item.name.startswith("gate-") and item.name.endswith(".jsonl"):
                    dest = dst_dir / item.name
                    if not dest.exists():
                        shutil.move(str(item), str(dest))
                    else:
                        content = item.read_text(encoding="utf-8", errors="replace")
                        if content:
                            with dest.open("a", encoding="utf-8") as fh:
                                fh.write(content if content.endswith("\n") else content + "\n")
                        item.unlink(missing_ok=True)
                    moved += 1
                else:
                    item.unlink(missing_ok=True)
            shutil.rmtree(src_dir, ignore_errors=True)
            if default_paths and not src_dir.exists():
                _legacy_reports_checked = True
            return moved
        except OSError as exc:
            logger.warning("true-moa: could not migrate legacy reports (%s)", exc)
            return 0


def _path_for(report_dir: Path, when: float) -> Path:
    return report_dir / f"gate-{time.strftime('%Y%m%d', time.localtime(when))}.jsonl"


def _prune_expired(report_dir: Path, now: float) -> None:
    global _last_pruned_key
    today_str = time.strftime("%Y%m%d", time.localtime(now))
    prune_key = (report_dir, today_str)
    if _last_pruned_key == prune_key or RETENTION_DAYS <= 0:
        return
    _last_pruned_key = prune_key
    cutoff = datetime.datetime.fromtimestamp(now).date() - datetime.timedelta(days=RETENTION_DAYS)
    for path in report_dir.glob("gate-*.jsonl"):
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
    if REPORT_DIR is None:
        migrate_legacy_reports()
    active_dir = resolve_report_dir()
    try:
        with _lock:
            active_dir.mkdir(parents=True, exist_ok=True)
            _prune_expired(active_dir, now)
            with _path_for(active_dir, now).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as exc:
        logger.debug("true-moa: report write failed: %s", exc)


def tail(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent entries, newest last. Used by `/true-moa status`."""
    if REPORT_DIR is None:
        migrate_legacy_reports()
    active_dir = resolve_report_dir()
    try:
        files = sorted(active_dir.glob("gate-*.jsonl")) if active_dir.exists() else []
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
    if REPORT_DIR is None:
        migrate_legacy_reports()
    active_dir = resolve_report_dir()
    files = sorted(active_dir.glob("gate-*.jsonl")) if active_dir.exists() else []
    if window_days > 0 and len(files) > window_days:
        files = files[-window_days:]
    counters = {
        "reference_retry": 0,
        "reference_fallback_switched": 0,
        "reference_outage": 0,
        "aggregator_fallback_blocked": 0,
        "quorum_accepted": 0,
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
