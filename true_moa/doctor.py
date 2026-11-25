"""Offline configuration and environment diagnostics for `/true-moa doctor`."""

from __future__ import annotations

import importlib
import json
from typing import Any

import tomllib

from . import gate, i18n, report, state


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
            try:
                import yaml
            except ImportError:
                yaml = importlib.import_module("hermes_yaml")
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


def run_structured(lang: str | None = None) -> dict[str, Any]:
    """Return structured health checks and formatted text in English or Vietnamese."""
    cfg = state.load()
    target_lang = state.normalize_language(lang) if lang is not None else i18n.active_language(cfg)
    issues: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, str]] = []

    def add_check(check_id: str, level: str, col_name: str, detail: str) -> None:
        checks.append({"id": check_id, "level": level, "name": col_name, "detail": detail})

    info = gate.status()
    mode = info.get("mode", "in_process")
    total_seams = int(info.get("total_seams") or len(gate._TARGETS))
    if info["installed"] or mode == "native_hooks":
        seam_desc = (
            i18n.msg("status_mode_native", lang=target_lang)
            if mode == "native_hooks"
            else f"{len(info['wrapped'])}/{total_seams} in-process seams" if target_lang == "en"
            else f"{len(info['wrapped'])}/{total_seams} điểm chặn in-process"
        )
        add_check(
            "gate",
            "ok",
            "gate",
            i18n.msg("doctor_gate_ok", lang=target_lang, plugin_version=info["plugin_version"], core_version=info["core_version"], seam_desc=seam_desc),
        )
    else:
        err = info.get("compat_error") or "gate not installed"
        issues.append(f"gate inactive: {err}")
        add_check("gate", "fail", "gate", i18n.msg("doctor_gate_fail", lang=target_lang, error=err))

    pyproject_path = state.PLUGIN_DIR / "pyproject.toml"
    pm_problems: list[str] = []
    if pyproject_path.is_file():
        try:
            doc = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            bad_tables = [k for k in ("project", "build-system", "dependency-groups") if k in doc]
            if bad_tables:
                pm_problems.append(f"pyproject.toml declares packaging table(s) {bad_tables}")
        except Exception as exc:
            pm_problems.append(f"pyproject.toml parse error: {exc}")
    try:
        pm_decl = importlib.import_module("pm.plugin_declarations")
        decl = pm_decl.read_python_declaration(state.PLUGIN_DIR)
        if decl.installs_packages:
            pm_problems.append("PM read_python_declaration reports installs_packages=True")
    except Exception:
        pass
    if (state.PLUGIN_DIR / "state.json").exists() or (state.PLUGIN_DIR / "reports").exists():
        pm_problems.append("legacy state.json or reports/ still inside plugin source directory")
    if not pm_problems:
        add_check("pyproject_pm", "ok", "pyproject/pm", i18n.msg("doctor_pm_ok", lang=target_lang))
    else:
        issues.extend(pm_problems)
        add_check("pyproject_pm", "fail", "pyproject/pm", i18n.msg("doctor_pm_fail", lang=target_lang, detail="; ".join(pm_problems)))

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
        add_check("plugin_data", "ok", "plugin-data", i18n.msg("doctor_data_ok", lang=target_lang, path=str(data_root)))
    else:
        add_check("plugin_data", "fail", "plugin-data", i18n.msg("doctor_data_fail", lang=target_lang, path=str(state_file)))

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
        add_check("moa_preset", "warn", "moa preset", i18n.msg("doctor_preset_none", lang=target_lang))
    elif not refs:
        warnings.append(f"preset {preset_name!r} has no enabled reference_models")
        add_check("moa_preset", "warn", "moa preset", i18n.msg("doctor_preset_zero", lang=target_lang, preset=preset_name))
    else:
        cred_problems = [p for s in ([aggregator, *refs] if aggregator else refs) if (p := _check_slot_credentials(s))]
        if cred_problems:
            warnings.extend(cred_problems)
            add_check("moa_preset", "warn", "moa preset", i18n.msg("doctor_preset_warn", lang=target_lang, preset=preset_name, count=len(refs), problems="; ".join(cred_problems)))
        else:
            add_check("moa_preset", "ok", "moa preset", i18n.msg("doctor_preset_ok", lang=target_lang, preset=preset_name, count=len(refs)))

    if cfg.get("enable_secondary_fallback"):
        raw_sec = cfg.get("secondary_reference_model", "")
        chain = state.parse_reference_model_chain(raw_sec)
        valid_chain = gate._valid_secondary_chain(raw_sec, aggregator or None)
        if not chain:
            issues.append("enable_secondary_fallback is True but secondary_reference_model is empty/invalid")
            add_check("fallback", "fail", "fallback", i18n.msg("doctor_fb_empty", lang=target_lang))
        elif not valid_chain:
            issues.append("all secondary_reference_model candidates rejected (aggregator collision or moa provider)")
            add_check("fallback", "fail", "fallback", i18n.msg("doctor_fb_collision", lang=target_lang))
        else:
            sec_probs = [p for s in valid_chain if (p := _check_slot_credentials(s))]
            if sec_probs:
                warnings.extend(sec_probs)
                add_check("fallback", "warn", "fallback", i18n.msg("doctor_fb_warn", lang=target_lang, count=len(valid_chain), problems="; ".join(sec_probs)))
            else:
                labels = ", ".join(f"{s['provider']}:{s['model']}" for s in valid_chain)
                add_check("fallback", "ok", "fallback", i18n.msg("doctor_fb_ok", lang=target_lang, labels=labels, attempts=cfg["fallback_trigger_attempts"]))
    else:
        add_check("fallback", "ok", "fallback", i18n.msg("doctor_fb_off", lang=target_lang))

    ladder = state.effective_ladder(cfg)
    budget = float(cfg.get("max_wait_budget_s") or 0.0)
    total_sleep = sum(ladder)
    if 0.0 < budget < (ladder[0] if ladder else 0.0):
        warnings.append(f"max_wait_budget_s ({budget:g}s) is smaller than first ladder step ({ladder[0]:g}s)")
        add_check("ladder", "warn", "ladder", i18n.msg("doctor_ladder_warn", lang=target_lang, budget=budget, first=ladder[0]))
    else:
        liveness_s = float(((hermes_cfg.get("agent") or {}).get("turn_liveness") or {}).get("timeout_s") or 600.0)
        hb_note = i18n.msg("doctor_ladder_hb", lang=target_lang) if total_sleep >= liveness_s * 0.5 else ""
        add_check("ladder", "ok", "ladder", i18n.msg("doctor_ladder_ok", lang=target_lang, attempts=len(ladder) + 1, total_sleep=total_sleep, hb_note=hb_note))

    quorum = int(cfg.get("min_healthy_references") or 0)
    add_check(
        "quorum",
        "ok",
        "quorum",
        i18n.msg("doctor_quorum_all", lang=target_lang) if quorum <= 0 else i18n.msg("doctor_quorum_min", lang=target_lang, count=quorum),
    )

    summary_data = report.summary()
    tel = summary_data.get("reference_telemetry") or {}
    tel_calls = int(tel.get("calls") or 0)
    miss_reasons = tel.get("miss_reasons") or {}
    cache_warnings: list[str] = []
    if str(preset.get("fanout") or "").strip().lower() == "per_iteration" and not cfg.get("adaptive_fanout"):
        cache_warnings.append(i18n.msg("doctor_warn_per_iter", lang=target_lang))
    relay_misses = int(miss_reasons.get("miss_provider_relay") or 0)
    if relay_misses > 0:
        cache_warnings.append(i18n.msg("doctor_warn_relay", lang=target_lang, count=relay_misses))
    avg_out = int(tel.get("avg_output_tokens") or 0)
    if tel_calls >= 3 and avg_out >= 4000 and not cfg.get("concise_advisory"):
        cache_warnings.append(i18n.msg("doctor_warn_verbose", lang=target_lang, avg_out=avg_out))

    on_tag = i18n.msg("state_on", lang=target_lang)
    off_tag = i18n.msg("state_off", lang=target_lang)
    smart_state = i18n.msg(
        "status_smart_summary",
        lang=target_lang,
        adaptive=on_tag if cfg.get("adaptive_fanout") else off_tag,
        trim=on_tag if cfg.get("chunked_context_trim", True) else off_tag,
        concise=on_tag if cfg.get("concise_advisory") else off_tag,
        empty=on_tag if cfg.get("guard_empty_responses", True) else off_tag,
    )
    if cache_warnings:
        warnings.extend(cache_warnings)
        add_check("cache_efficiency", "warn", "cache & cost", i18n.msg("doctor_cache_warn", lang=target_lang, detail="; ".join(cache_warnings)))
    else:
        tel_note = (
            i18n.msg("doctor_cache_measured", lang=target_lang, calls=tel_calls, hit_pct=float(tel.get("cache_hit_pct") or 0.0), avg_out=avg_out)
            if tel_calls > 0
            else i18n.msg("doctor_cache_no_data", lang=target_lang)
        )
        add_check("cache_efficiency", "ok", "cache & cost", i18n.msg("doctor_cache_ok", lang=target_lang, smart_state=smart_state, telemetry_note=tel_note))

    ok = len(issues) == 0
    title = i18n.msg("doctor_title", lang=target_lang)
    verdict_line = (
        i18n.msg("doctor_verdict_healthy", lang=target_lang)
        if ok and not warnings
        else i18n.msg("doctor_verdict_warnings", lang=target_lang)
        if ok
        else i18n.msg("doctor_verdict_issues", lang=target_lang, count=len(issues))
    )
    badge_map = {"ok": "[OK]  ", "warn": "[WARN]", "fail": "[FAIL]"}
    lines = [title, *[f"  {badge_map[c['level']]} {c['name']:<13} {c['detail']}" for c in checks], verdict_line]
    return {
        "ok": ok,
        "has_warnings": bool(warnings),
        "title": title,
        "verdict": verdict_line,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
        "text": "\n".join(lines),
    }


def run(lang: str | None = None) -> tuple[bool, str]:
    """Run static health checks without network calls or token spend."""
    res = run_structured(lang=lang)
    return bool(res["ok"]), str(res["text"])
