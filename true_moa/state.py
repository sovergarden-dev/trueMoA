"""Runtime configuration for the True MoA gate.

The live Hermes PluginContext settings are authoritative while the gateway is
running. ``<HERMES_HOME>/plugin-data/true-moa/state.json`` is the standalone CLI
snapshot and fallback when no PluginContext has been bound — never inside the
plugin source directory, which Hermes PM hashes for workspace currency.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
# Optional sandbox override used by offline tests; None resolves dynamically to plugin-data/true-moa/state.json.
STATE_PATH: Path | None = None

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sync_hermes_retries": False,
    "custom_retries": 0,
    "ladder": [15, 30, 60, 60, 60],
    "block_aggregator_fallback": True,
    "max_wait_budget_s": 0.0,
    "enable_secondary_fallback": False,
    "secondary_reference_model": "",
    "fallback_trigger_attempts": 2,
    "min_healthy_references": 0,
}


def resolve_hermes_home() -> Path:
    """Resolve the active Hermes home at call time (supports multi-profile scopes)."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        pass
    raw_env = os.environ.get("HERMES_HOME", "").strip()
    if raw_env:
        return Path(os.path.expanduser(os.path.expandvars(raw_env)))
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / f"hermes{suffix}"
    return Path.home() / f".hermes{suffix}"


def plugin_data_root() -> Path:
    """Return ``<HERMES_HOME>/plugin-data/true-moa`` outside the plugin install tree."""
    return resolve_hermes_home() / "plugin-data" / "true-moa"


def resolve_state_path(*, ignore_override: bool = False) -> Path:
    """Return the active state.json path (sandbox override if set, else plugin-data)."""
    if not ignore_override and STATE_PATH is not None:
        return STATE_PATH
    return plugin_data_root() / "state.json"


def _parse_single_reference_model(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        p = str(raw.get("provider") or "").strip()
        m = str(raw.get("model") or "").strip()
        if p and m:
            return {"provider": p, "model": m, "enabled": True}
        return None
    if not isinstance(raw, str):
        return None
    val = raw.strip()
    if not val or ":" not in val:
        return None
    provider, model = val.split(":", 1)
    if provider.strip() and model.strip():
        return {"provider": provider.strip(), "model": model.strip(), "enabled": True}
    return None


def parse_reference_model_chain(raw: Any) -> list[dict[str, Any]]:
    """Parse comma-separated 'provider:model' specs or list/dict into deduplicated slot specs."""
    items: list[Any]
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, dict):
        items = [raw]
    else:
        return []
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        parsed = _parse_single_reference_model(item)
        if parsed is None:
            continue
        key = (parsed["provider"].lower(), parsed["model"].lower())
        if key not in seen:
            seen.add(key)
            chain.append(parsed)
    return chain


def parse_reference_model_string(raw: Any) -> dict[str, Any] | None:
    """Parse 'provider:model' or dict into a valid Hermes reference model spec."""
    if isinstance(raw, str) and "," in raw:
        chain = parse_reference_model_chain(raw)
        return chain[0] if chain else None
    return _parse_single_reference_model(raw)


def _parse_custom_retries(val: Any) -> int:
    try:
        f = float(val or 0)
        return max(0, min(25, int(f))) if math.isfinite(f) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _read_hermes_api_max_retries() -> int:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        val = int(cfg.get("agent", {}).get("api_max_retries", 3))
        return max(1, min(25, val))
    except Exception:
        pass
    try:
        import yaml
        cfg = yaml.safe_load((resolve_hermes_home() / "config.yaml").read_text(encoding="utf-8")) or {}
        val = int(cfg.get("agent", {}).get("api_max_retries", 3))
        return max(1, min(25, val))
    except Exception:
        return 3


def generate_ladder(attempts: int) -> list[float]:
    if attempts <= 1:
        return []
    intervals = min(24, attempts - 1)
    base_ramp = [10.0, 15.0, 20.0, 30.0, 45.0, 60.0]
    if intervals <= len(base_ramp):
        return [float(x) for x in base_ramp[:intervals]]
    return [float(x) for x in (base_ramp + [60.0] * (intervals - len(base_ramp)))]


def effective_ladder(config: dict[str, Any] | None = None) -> list[float]:
    if config is None:
        config = load()
    custom = _parse_custom_retries(config.get("custom_retries"))
    if custom > 0:
        return generate_ladder(custom)
    if config.get("sync_hermes_retries"):
        attempts = _read_hermes_api_max_retries()
        return generate_ladder(attempts)
    return [float(x) for x in config.get("ladder") or DEFAULTS["ladder"]]


def ladder_source(config: dict[str, Any] | None = None) -> tuple[str, int]:
    """Returns (source_name, attempts_count) e.g. ('custom', 5), ('sync', 13), ('manual', len(ladder)+1)."""
    if config is None:
        config = load()
    lad = effective_ladder(config)
    attempts = len(lad) + 1
    custom = _parse_custom_retries(config.get("custom_retries"))
    if custom > 0:
        return "custom", attempts
    if config.get("sync_hermes_retries"):
        return "sync", attempts
    return "manual", attempts


_lock = threading.RLock()
_ctx_ref: Any = None
_legacy_state_checked = False


def migrate_legacy_state(legacy_path: Path | None = None, target_path: Path | None = None) -> bool:
    """Evacuate legacy ``plugins/true-moa/state.json`` into ``plugin-data/true-moa/state.json``."""
    global _legacy_state_checked
    default_paths = legacy_path is None and target_path is None
    if default_paths and _legacy_state_checked:
        return False
    src = legacy_path if legacy_path is not None else (PLUGIN_DIR / "state.json")
    dst = target_path if target_path is not None else resolve_state_path(ignore_override=True)
    with _lock:
        if default_paths and _legacy_state_checked:
            return False
        if not src.exists():
            if default_paths:
                _legacy_state_checked = True
            return False
        try:
            if src.resolve() == dst.resolve():
                if default_paths:
                    _legacy_state_checked = True
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                handle, temp_path = tempfile.mkstemp(prefix=".state-mig-", suffix=".json", dir=str(dst.parent))
                os.close(handle)
                try:
                    shutil.copy2(src, temp_path)
                    os.replace(temp_path, dst)
                except BaseException:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise
            src.unlink(missing_ok=True)
            if default_paths and not src.exists():
                _legacy_state_checked = True
            return True
        except OSError as exc:
            logger.warning("true-moa: could not migrate legacy state.json (%s)", exc)
            return False


def bind_context(ctx: Any) -> None:
    """Bind the process PluginContext so the settings UI and gate share one source."""
    global _ctx_ref
    with _lock:
        _ctx_ref = ctx


def _read_state_file() -> dict[str, Any] | None:
    if STATE_PATH is None:
        migrate_legacy_state()
    target = resolve_state_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("true-moa: state.json unreadable (%s) — using defaults", exc)
        return None
    return value if isinstance(value, dict) else None


def _read_ctx_settings() -> dict[str, Any] | None:
    ctx = _ctx_ref
    if ctx is None or not callable(getattr(ctx, "get_config", None)):
        return None
    values: dict[str, Any] = {}
    for key in DEFAULTS:
        try:
            value = ctx.get_config(key, None)
        except Exception as exc:
            logger.warning("true-moa: could not read plugin setting %s (%s)", key, exc)
            continue
        if value is not None:
            values[key] = value
    return values or None


def _normalise(raw: Any) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return config
    if isinstance(raw.get("enabled"), bool):
        config["enabled"] = raw["enabled"]

    ladder = raw.get("ladder")
    if isinstance(ladder, str):
        try:
            ladder = json.loads(ladder)
        except ValueError:
            try:
                ladder = [float(item.strip()) for item in ladder.split(",") if item.strip()]
            except ValueError:
                ladder = None
    if isinstance(ladder, list):
        clean = [float(item) for item in ladder[:24]
                 if isinstance(item, (int, float)) and not isinstance(item, bool)
                 and math.isfinite(float(item)) and item >= 0]
        if clean:
            config["ladder"] = clean

    if isinstance(raw.get("sync_hermes_retries"), bool):
        config["sync_hermes_retries"] = raw["sync_hermes_retries"]

    custom = raw.get("custom_retries")
    if (isinstance(custom, (int, float)) and not isinstance(custom, bool)
            and math.isfinite(float(custom)) and custom >= 0):
        config["custom_retries"] = int(min(25, max(0, int(custom))))
    elif isinstance(custom, str):
        try:
            val = int(custom.strip())
            config["custom_retries"] = int(min(25, max(0, val)))
        except ValueError:
            pass

    if isinstance(raw.get("block_aggregator_fallback"), bool):
        config["block_aggregator_fallback"] = raw["block_aggregator_fallback"]
    budget = raw.get("max_wait_budget_s")
    if (isinstance(budget, (int, float)) and not isinstance(budget, bool)
            and math.isfinite(float(budget)) and budget >= 0):
        config["max_wait_budget_s"] = float(budget)

    if isinstance(raw.get("enable_secondary_fallback"), bool):
        config["enable_secondary_fallback"] = raw["enable_secondary_fallback"]

    sec_model = raw.get("secondary_reference_model")
    if isinstance(sec_model, str):
        config["secondary_reference_model"] = sec_model.strip()

    trigger = raw.get("fallback_trigger_attempts")
    if (isinstance(trigger, (int, float)) and not isinstance(trigger, bool)
            and math.isfinite(float(trigger)) and trigger > 0):
        config["fallback_trigger_attempts"] = int(min(10, max(1, int(trigger))))
    elif isinstance(trigger, str):
        try:
            val = int(trigger.strip())
            config["fallback_trigger_attempts"] = int(min(10, max(1, val)))
        except ValueError:
            pass

    quorum = raw.get("min_healthy_references")
    if (isinstance(quorum, (int, float)) and not isinstance(quorum, bool)
            and math.isfinite(float(quorum)) and quorum >= 0):
        config["min_healthy_references"] = int(min(8, max(0, int(quorum))))
    elif isinstance(quorum, str):
        try:
            q_val = int(quorum.strip())
            config["min_healthy_references"] = int(min(8, max(0, q_val)))
        except ValueError:
            pass

    return config


def _write_state_file(value: dict[str, Any]) -> None:
    if STATE_PATH is None:
        migrate_legacy_state()
    target = resolve_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    handle, temp_path = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _write_ctx_settings(patch: dict[str, Any]) -> None:
    ctx = _ctx_ref
    setter = getattr(ctx, "set_config", None) if ctx is not None else None
    if callable(setter):
        for key, value in patch.items():
            if key in DEFAULTS:
                setter(key, value)
        return
    if not patch:
        return
    try:
        from hermes_cli.config import load_config, save_config
        config = load_config()
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = config["plugins"] = {}
        entries = plugins.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = plugins["entries"] = {}
        entry = entries.setdefault("true-moa", {})
        if not isinstance(entry, dict):
            entry = entries["true-moa"] = {}
        settings = entry.setdefault("settings", {})
        if not isinstance(settings, dict):
            settings = entry["settings"] = {}
        settings.update({key: value for key, value in patch.items() if key in DEFAULTS})
        save_config(config)
    except Exception as exc:
        raise RuntimeError(f"could not persist True MoA settings: {exc}") from exc


def _seed_missing_ctx_settings(value: dict[str, Any]) -> None:
    """Seed fields omitted by older releases without overwriting saved user values."""
    ctx = _ctx_ref
    getter = getattr(ctx, "get_config", None) if ctx is not None else None
    setter = getattr(ctx, "set_config", None) if ctx is not None else None
    if not callable(getter) or not callable(setter):
        return
    for key in DEFAULTS:
        try:
            if getter(key, None) is None:
                setter(key, value[key])
        except Exception as exc:
            logger.warning("true-moa: could not initialize plugin setting %s (%s)", key, exc)


def load() -> dict[str, Any]:
    """Resolve live plugin settings first, then state.json, then safe defaults."""
    with _lock:
        raw_file = _read_state_file()
        raw_ctx = _read_ctx_settings()
        value = _normalise({**(raw_file or {}), **(raw_ctx or {})})
        if _ctx_ref is not None:
            _seed_missing_ctx_settings(value)
            raw_ctx = _read_ctx_settings()
            value = _normalise({**(raw_file or {}), **(raw_ctx or {})})
        if raw_ctx and raw_file != value:
            try:
                _write_state_file(value)
            except OSError as exc:
                logger.warning("true-moa: could not refresh state.json from plugin settings (%s)", exc)
        return dict(value)


def enabled() -> bool:
    return bool(load()["enabled"])


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Persist a partial update to live plugin settings and the CLI snapshot."""
    with _lock:
        merged = dict(load())
        merged.update(patch)
        merged = _normalise(merged)
        _write_ctx_settings(patch)
        _write_state_file(merged)
        return dict(merged)


def set_enabled(flag: bool) -> dict[str, Any]:
    return save({"enabled": bool(flag)})
