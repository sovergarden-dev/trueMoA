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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def summary(window_days: int = RETENTION_DAYS) -> dict[str, Any]:
    """Counters and per-reference cache telemetry over the active retention window."""
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
        "adaptive_fanout_skipped": 0,
        "sticky_fallback_reused": 0,
        "chunked_trim_applied": 0,
        "empty_response_rejected": 0,
    }
    ref_calls = 0
    total_input = 0
    total_cache_read = 0
    total_cache_write = 0
    total_output = 0
    total_reasoning = 0
    total_latency_ms = 0
    prefix_match_sum = 0.0
    prefix_match_samples = 0
    miss_reasons: dict[str, int] = {}
    model_buckets: dict[str, dict[str, Any]] = {}

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
                    if not isinstance(row, dict):
                        continue
                    name = row.get("event")
                    if name in counters:
                        counters[name] += 1
                    elif name == "reference_call":
                        ref_calls += 1
                        inp = max(0, _as_int(row.get("input_tokens")))
                        cr = max(0, _as_int(row.get("cache_read_tokens")))
                        cw = max(0, _as_int(row.get("cache_write_tokens")))
                        out_tok = max(0, _as_int(row.get("output_tokens")))
                        reas_tok = max(0, _as_int(row.get("reasoning_tokens")))
                        lat_ms = max(0, _as_int(row.get("latency_ms")))
                        diag = str(row.get("diagnosis") or "miss_other")
                        lbl = str(row.get("label") or row.get("model_key") or "unknown")

                        total_input += inp
                        total_cache_read += cr
                        total_cache_write += cw
                        total_output += out_tok
                        total_reasoning += reas_tok
                        total_latency_ms += lat_ms
                        miss_reasons[diag] = miss_reasons.get(diag, 0) + 1
                        if diag != "first_call":
                            prefix_match_sum += _as_float(row.get("prefix_match_pct"))
                            prefix_match_samples += 1

                        bucket = model_buckets.get(lbl)
                        if bucket is None:
                            bucket = {
                                "label": lbl,
                                "calls": 0,
                                "prompt_tokens": 0,
                                "cache_read_tokens": 0,
                                "output_tokens": 0,
                                "latency_ms": 0,
                                "diag_counts": {},
                            }
                            model_buckets[lbl] = bucket
                        bucket["calls"] += 1
                        bucket["prompt_tokens"] += inp + cr + cw
                        bucket["cache_read_tokens"] += cr
                        bucket["output_tokens"] += out_tok
                        bucket["latency_ms"] += lat_ms
                        diag_map = bucket["diag_counts"]
                        diag_map[diag] = diag_map.get(diag, 0) + 1
                    last = row
        except OSError:
            continue

    total_prompt = total_input + total_cache_read + total_cache_write
    by_model: list[dict[str, Any]] = []
    for bucket in sorted(model_buckets.values(), key=lambda b: (-b["calls"], b["label"]))[:8]:
        calls_n = bucket["calls"]
        b_prompt = bucket["prompt_tokens"]
        b_diag_counts = bucket["diag_counts"]
        top_diag = max(b_diag_counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if b_diag_counts else "first_call"
        by_model.append({
            "label": bucket["label"],
            "calls": calls_n,
            "cache_hit_pct": round(100.0 * bucket["cache_read_tokens"] / b_prompt, 1) if b_prompt > 0 else 0.0,
            "avg_output_tokens": round(bucket["output_tokens"] / calls_n) if calls_n > 0 else 0,
            "avg_latency_ms": round(bucket["latency_ms"] / calls_n) if calls_n > 0 else 0,
            "top_diagnosis": top_diag,
        })

    reference_telemetry = {
        "calls": ref_calls,
        "total_input_tokens": total_input,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_write_tokens": total_cache_write,
        "total_output_tokens": total_output,
        "total_reasoning_tokens": total_reasoning,
        "cache_hit_pct": round(100.0 * total_cache_read / total_prompt, 1) if total_prompt > 0 else 0.0,
        "avg_output_tokens": round(total_output / ref_calls) if ref_calls > 0 else 0,
        "avg_reasoning_pct": round(100.0 * total_reasoning / total_output, 1) if total_output > 0 else 0.0,
        "avg_latency_ms": round(total_latency_ms / ref_calls) if ref_calls > 0 else 0,
        "avg_prefix_match_pct": round(prefix_match_sum / prefix_match_samples, 1) if prefix_match_samples > 0 else 0.0,
        "miss_reasons": miss_reasons,
        "by_model": by_model,
    }
    return {
        "days": days,
        "counters": counters,
        "reference_telemetry": reference_telemetry,
        "last": last,
    }
