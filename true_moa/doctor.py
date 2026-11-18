"""Offline configuration and environment diagnostics for `/true-moa doctor`."""

from __future__ import annotations

import importlib
import json
from typing import Any

import tomllib

from . import gate, state


def _load_hermes_config() -> dict[str, Any]:
    try:
        cfg_mod = importlib.import_module("hermes_cli.config")
        loaded = cfg_mod.load_config()
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    cfg_path = state.resolve_hermes_home() / "config.yaml"
    if cfg_path.is_file():
        try:
            import yaml
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return {}


def _check_slot_credentials(slot: dict[str, Any]) -> str | None:
    provider = str(slot.get("provider") or "").strip()
    model = str(slot.get("model") or "").strip()
    if not provider or not model:
        return "missing provider or model"
    if provider.lower() == "moa":
        return "recursive 'moa' provider is forbidden"
    try:
        rt_mod = importlib.import_module("hermes_cli.runtime_provider")
        rt = rt_mod.resolve_runtime_provider(requested=provider, target_model=model)
        api_key = str(rt.get("api_key") or "").strip()
        base_url = str(rt.get("base_url") or "").strip().lower()
        is_local = any(host in base_url for host in ("127.0.0.1", "localhost", "0.0.0.0"))
        if (not api_key or api_key == "no-key-required") and not is_local:
            return f"credential for '{provider}:{model}' resolved to empty/placeholder"
    except Exception as exc:
        return f"provider '{provider}' resolution error ({exc})"
    return None


def run() -> tuple[bool, str]:
    """Run static health checks without network calls or token spend."""
    issues: list[str] = []
    warnings: list[str] = []
    lines: list[str] = ["True MoA Doctor — Offline Health Check"]

    info = gate.status()
    mode = info.get("mode", "in_process")
    if info["installed"] or mode == "native_hooks":
        seam_desc = "native hooks" if mode == "native_hooks" else f"{len(info['wrapped'])}/7 in-process seams"
        lines.append(f"  [OK]   gate          v{info['plugin_version']} on Hermes {info['core_version']} ({seam_desc})")
    else:
        err = info.get("compat_error") or "gate not installed"
        issues.append(f"gate inactive: {err}")
        lines.append(f"  [FAIL] gate          inactive — {err}")

    pyproject_path = state.PLUGIN_DIR / "pyproject.toml"
    pyproject_ok = True
    if pyproject_path.is_file():
        try:
            doc = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            bad_tables = [k for k in ("project", "build-system", "dependency-groups") if k in doc]
            if bad_tables:
                pyproject_ok = False
                issues.append(f"pyproject.toml declares packaging table(s) {bad_tables}")
        except Exception as exc:
            pyproject_ok = False
            issues.append(f"pyproject.toml parse error: {exc}")
    try:
        pm_decl = importlib.import_module("pm.plugin_declarations")
        decl = pm_decl.read_python_declaration(state.PLUGIN_DIR)
        if decl.installs_packages:
            pyproject_ok = False
            issues.append("PM read_python_declaration reports installs_packages=True")
    except Exception:
        pass
    legacy_present = (state.PLUGIN_DIR / "state.json").exists() or (state.PLUGIN_DIR / "reports").exists()
    if legacy_present:
        pyproject_ok = False
        issues.append("legacy state.json or reports/ still inside plugin source directory")
    if pyproject_ok:
        lines.append("  [OK]   pyproject/pm  tooling-only (installs_packages=False, plugin dir clean)")
    else:
        lines.append("  [FAIL] pyproject/pm  workspace stamp hazard detected")

    data_root = state.plugin_data_root()
    state_file = state.resolve_state_path()
    storage_ok = "plugins" not in data_root.parts
    if state_file.is_file():
        try:
            parsed_state = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(parsed_state, dict):
                storage_ok = False
                issues.append("state.json root is not a JSON object")
        except Exception as exc:
            storage_ok = False
            issues.append(f"state.json unreadable: {exc}")
    if storage_ok:
        lines.append(f"  [OK]   plugin-data   {data_root}")
    else:
        lines.append(f"  [FAIL] plugin-data   invalid storage state at {state_file}")

    hermes_cfg = _load_hermes_config()
    raw_moa = hermes_cfg.get("moa")
    moa_cfg: dict[str, Any] = raw_moa if isinstance(raw_moa, dict) else {}
    raw_presets = moa_cfg.get("presets")
    presets: dict[str, Any] = raw_presets if isinstance(raw_presets, dict) else {}
    first_preset = next(iter(presets), "")
    preset_name = str(moa_cfg.get("default_preset") or first_preset).strip()
    raw_preset = presets.get(preset_name)
    preset: dict[str, Any] = raw_preset if isinstance(raw_preset, dict) else {}
    raw_agg = preset.get("aggregator")
    aggregator: dict[str, Any] = raw_agg if isinstance(raw_agg, dict) else {}
    raw_refs = preset.get("reference_models")
    refs = [s for s in (raw_refs if isinstance(raw_refs, list) else []) if isinstance(s, dict) and s.get("enabled", True)]

    if not presets:
        warnings.append("no moa.presets found in Hermes config.yaml")
        lines.append("  [WARN] moa preset    none configured in config.yaml")
    elif not refs:
        warnings.append(f"preset {preset_name!r} has no enabled reference_models")
        lines.append(f"  [WARN] moa preset    '{preset_name}' has 0 enabled references")
    else:
        cred_problems = [p for s in ([aggregator, *refs] if aggregator else refs) if (p := _check_slot_credentials(s))]
        if cred_problems:
            warnings.extend(cred_problems)
            lines.append(f"  [WARN] moa preset    '{preset_name}' ({len(refs)} ref): {'; '.join(cred_problems)}")
        else:
            lines.append(f"  [OK]   moa preset    '{preset_name}' ({len(refs)} enabled reference(s), credentials resolved)")

    cfg = state.load()
    if cfg.get("enable_secondary_fallback"):
        raw_sec = cfg.get("secondary_reference_model", "")
        chain = state.parse_reference_model_chain(raw_sec)
        valid_chain = gate._valid_secondary_chain(raw_sec, aggregator or None)
        if not chain:
            issues.append("enable_secondary_fallback is True but secondary_reference_model is empty/invalid")
            lines.append("  [FAIL] fallback      enabled with empty or malformed secondary_reference_model")
        elif not valid_chain:
            issues.append("all secondary_reference_model candidates rejected (aggregator collision or moa provider)")
            lines.append("  [FAIL] fallback      secondary_reference_model collides with aggregator or uses 'moa'")
        else:
            sec_probs = [p for s in valid_chain if (p := _check_slot_credentials(s))]
            if sec_probs:
                warnings.extend(sec_probs)
                lines.append(f"  [WARN] fallback      {len(valid_chain)} backup(s): {'; '.join(sec_probs)}")
            else:
                labels = ", ".join(f"{s['provider']}:{s['model']}" for s in valid_chain)
                lines.append(f"  [OK]   fallback      {labels} (after {cfg['fallback_trigger_attempts']} failure(s))")
    else:
        lines.append("  [OK]   fallback      disabled (primary references only)")

    ladder = state.effective_ladder(cfg)
    budget = float(cfg.get("max_wait_budget_s") or 0.0)
    total_sleep = sum(ladder)
    if 0.0 < budget < (ladder[0] if ladder else 0.0):
        warnings.append(f"max_wait_budget_s ({budget:g}s) is smaller than first ladder step ({ladder[0]:g}s)")
        lines.append(f"  [WARN] ladder        budget {budget:g}s caps even first step {ladder[0]:g}s")
    else:
        liveness_s = float(((hermes_cfg.get("agent") or {}).get("turn_liveness") or {}).get("timeout_s") or 600.0)
        hb_note = " (liveness heartbeat active)" if total_sleep >= liveness_s * 0.5 else ""
        lines.append(f"  [OK]   ladder        {len(ladder) + 1} attempt(s), max sleep {total_sleep:g}s{hb_note}")

    quorum = int(cfg.get("min_healthy_references") or 0)
    lines.append(
        "  [OK]   quorum        "
        + ("all enabled references required" if quorum <= 0 else f"minimum {quorum} healthy reference(s)")
    )

    ok = len(issues) == 0
    verdict = "HEALTHY" if ok and not warnings else ("HEALTHY (with warnings)" if ok else f"ISSUES FOUND ({len(issues)})")
    lines.append(f"Verdict: {verdict}")
    return ok, "\n".join(lines)
