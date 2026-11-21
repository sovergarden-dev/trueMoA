"""`/true-moa` — in-session bilingual control switch (`en` default + `vi`)."""

from __future__ import annotations

import json
import math

from . import doctor, gate, i18n, report, state


def configure_language(tokens: list[str]) -> tuple[bool, str]:
    """Show or update the active True MoA language (`en` or `vi`)."""
    if not tokens:
        lang = i18n.active_language()
        return True, i18n.msg("lang_current", lang=lang)
    raw = " ".join(tokens).strip()
    if not state.is_supported_language(raw):
        return False, i18n.msg("lang_invalid")
    norm = state.normalize_language(raw)
    state.save({"language": norm})
    return True, i18n.msg("lang_set", lang=norm)


def configure_ladder(tokens: list[str]) -> tuple[bool, str]:
    """Show or update the retry ladder from slash or CLI tokens."""
    lang = i18n.active_language()
    if not tokens:
        cfg = state.load()
        current = state.effective_ladder(cfg)
        source, attempts = state.ladder_source(cfg)
        return True, i18n.msg("ladder_show", lang=lang, ladder=" → ".join(f"{x:g}s" for x in current), source=source, attempts=attempts)

    head = tokens[0].strip().lower()
    if head == "sync":
        cfg = state.save({"sync_hermes_retries": True, "custom_retries": 0})
        current = state.effective_ladder(cfg)
        _src, attempts = state.ladder_source(cfg)
        return True, i18n.msg("ladder_synced", lang=lang, attempts=attempts, ladder=" → ".join(f"{x:g}s" for x in current))

    if head == "custom":
        if len(tokens) < 2:
            return False, i18n.msg("ladder_custom_usage", lang=lang)
        try:
            count = int(tokens[1].strip())
        except ValueError:
            return False, i18n.msg("ladder_custom_invalid", lang=lang, raw=tokens[1])
        if count < 0 or count > 25:
            return False, i18n.msg("ladder_custom_range", lang=lang)
        cfg = state.save({"custom_retries": count})
        current = state.effective_ladder(cfg)
        src, attempts = state.ladder_source(cfg)
        return True, i18n.msg("ladder_custom_set", lang=lang, source=src, attempts=attempts, ladder=" → ".join(f"{x:g}s" for x in current))

    raw_parts = [part.strip() for tok in tokens for part in tok.split(",") if part.strip()]
    parsed: list[float] = []
    for item in raw_parts:
        try:
            val = float(item)
        except ValueError:
            return False, i18n.msg("ladder_step_invalid", lang=lang, raw=item)
        if not math.isfinite(val) or val < 0:
            return False, i18n.msg("ladder_step_finite", lang=lang, raw=item)
        parsed.append(val)
    if not parsed:
        return False, i18n.msg("ladder_usage", lang=lang)

    cfg = state.save({"ladder": parsed[:24], "sync_hermes_retries": False, "custom_retries": 0})
    current = state.effective_ladder(cfg)
    return True, i18n.msg("ladder_manual_set", lang=lang, attempts=len(current) + 1, ladder=" → ".join(f"{x:g}s" for x in current))


def configure_fallback(tokens: list[str]) -> tuple[bool, str]:
    """Show or update secondary reference fallback from slash or CLI tokens."""
    lang = i18n.active_language()
    if not tokens:
        cfg = state.load()
        if cfg.get("enable_secondary_fallback") and cfg.get("secondary_reference_model"):
            return True, i18n.msg("fb_show_enabled", lang=lang, models=cfg["secondary_reference_model"], attempts=cfg["fallback_trigger_attempts"])
        return True, i18n.msg("fb_show_disabled", lang=lang)

    if len(tokens) == 1 and tokens[0].strip().lower() in {"off", "disable", "none"}:
        state.save({"enable_secondary_fallback": False})
        return True, i18n.msg("fb_off", lang=lang)

    trigger: int | None = None
    spec_tokens = tokens
    if len(tokens) >= 2 and tokens[0].strip().isdigit():
        trigger = int(tokens[0].strip())
        if trigger < 1 or trigger > 10:
            return False, i18n.msg("fb_trigger_range", lang=lang)
        spec_tokens = tokens[1:]

    raw_spec = ",".join(part.strip() for tok in spec_tokens for part in tok.split(",") if part.strip())
    segments = [seg.strip() for seg in raw_spec.split(",") if seg.strip()]
    chain = state.parse_reference_model_chain(raw_spec)
    if not segments or len(chain) == 0:
        return False, i18n.msg("fb_spec_invalid", lang=lang)
    for seg in segments:
        if state.parse_reference_model_string(seg) is None:
            return False, i18n.msg("fb_entry_invalid", lang=lang, raw=seg)
    if any(entry["provider"].lower() == "moa" for entry in chain):
        return False, i18n.msg("fb_moa_forbidden", lang=lang)

    normalized_spec = ", ".join(f"{entry['provider']}:{entry['model']}" for entry in chain)
    patch: dict[str, object] = {
        "enable_secondary_fallback": True,
        "secondary_reference_model": normalized_spec,
    }
    if trigger is not None:
        patch["fallback_trigger_attempts"] = trigger
    cfg = state.save(patch)
    return True, i18n.msg("fb_armed", lang=lang, models=cfg["secondary_reference_model"], attempts=cfg["fallback_trigger_attempts"])


def configure_quorum(tokens: list[str]) -> tuple[bool, str]:
    """Show or update min_healthy_references (`all` or `1..8`)."""
    lang = i18n.active_language()
    if not tokens:
        cfg = state.load()
        q = int(cfg.get("min_healthy_references") or 0)
        label = i18n.msg("status_quorum_all", lang=lang) if q <= 0 else i18n.msg("status_quorum_min", lang=lang, count=q)
        return True, i18n.msg("quorum_show", lang=lang, label=label)

    head = tokens[0].strip().lower()
    if head in {"all", "0", "strict", "off"}:
        state.save({"min_healthy_references": 0})
        return True, i18n.msg("quorum_all_set", lang=lang)

    try:
        count = int(head)
    except ValueError:
        return False, i18n.msg("quorum_usage", lang=lang)
    if count < 1 or count > 8:
        return False, i18n.msg("quorum_range", lang=lang)
    cfg = state.save({"min_healthy_references": count})
    return True, i18n.msg("quorum_set", lang=lang, count=cfg["min_healthy_references"])


def _status_block() -> str:
    cfg = state.load()
    lang = i18n.active_language(cfg)
    info = gate.status()
    summary = report.summary()
    counters = summary["counters"]
    ladder = " → ".join(f"{item:g}s" for item in info["ladder"])
    budget = float(info.get("max_wait_budget_s") or 0.0)
    budget_label = i18n.msg("status_budget_cap", lang=lang, budget=budget) if budget > 0 else i18n.msg("status_budget_unbounded", lang=lang)
    source = str(info.get("ladder_source") or "manual")
    attempts = int(info.get("ladder_attempts") or (len(info["ladder"]) + 1))
    quorum = int(info.get("min_healthy_references") or 0)
    quorum_desc = i18n.msg("status_quorum_all", lang=lang) if quorum <= 0 else i18n.msg("status_quorum_min", lang=lang, count=quorum)
    mode_tag = (
        i18n.msg("status_mode_native", lang=lang)
        if info.get("mode") == "native_hooks"
        else i18n.msg("status_mode_seams", lang=lang, count=len(info["wrapped"]))
    )
    headline = (
        i18n.msg("status_armed", lang=lang)
        if info["installed"] and info["enabled"]
        else i18n.msg("status_standby", lang=lang)
        if info["installed"]
        else i18n.msg("status_uninstalled", lang=lang)
    )
    fallback_desc = (
        i18n.msg("status_fb_enabled", lang=lang, models=info.get("secondary_reference_model", ""), attempts=info.get("fallback_trigger_attempts", 2))
        if info.get("enable_secondary_fallback") and info.get("secondary_reference_model")
        else i18n.msg("status_fb_disabled", lang=lang)
    )
    text = i18n.msg(
        "status_block",
        lang=lang,
        headline=headline,
        core_version=info["core_version"],
        target_core=info["target_core"],
        plugin_version=info["plugin_version"],
        mode_tag=mode_tag,
        wrapped=", ".join(info["wrapped"]) or "none",
        ladder=ladder,
        budget_label=budget_label,
        source=source,
        attempts=attempts,
        budget=budget,
        quorum_desc=quorum_desc,
        impersonation=i18n.msg("status_blocked" if info["block_aggregator_fallback"] else "status_allowed", lang=lang),
        fallback_desc=fallback_desc,
        cache_desc=i18n.msg("status_cache_unconditional", lang=lang),
        days=summary["days"],
        retries=counters["reference_retry"],
        switches=counters["reference_fallback_switched"],
        quorum_hits=counters.get("quorum_accepted", 0),
        outages=counters["reference_outage"],
        blocked=counters["aggregator_fallback_blocked"],
    )
    if info["compat_error"]:
        extra = [i18n.msg("status_compat_failed", lang=lang, error=info["compat_error"])]
        extra.extend(f"                  · {item}" for item in info["compat_missing"])
        return text + "\n" + "\n".join(extra)
    return text


def handle(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    action = parts[0].lower() if parts else "status"
    lang = i18n.active_language()

    if action in {"on", "enable", "arm"}:
        config = state.set_enabled(True)
        installed = gate.install()
        if not installed.get("ok"):
            missing = "\n".join(f"  · {item}" for item in installed.get("missing", []))
            return i18n.msg("armed_fail", lang=lang, error=installed.get("error", "unknown"), missing=missing)
        report.event("gate_armed")
        ladder = " → ".join(f"{item:g}s" for item in state.effective_ladder(config))
        return i18n.msg("armed_ok", lang=lang, ladder=ladder)

    if action in {"off", "disable", "disarm"}:
        state.set_enabled(False)
        report.event("gate_disarmed")
        return i18n.msg("disarmed_ok", lang=lang)

    if action in {"retry", "reinstall"}:
        result = gate.install(force=True)
        if not result.get("ok"):
            missing = "\n".join(f"  · {item}" for item in result.get("missing", []))
            return i18n.msg("reverify_fail", lang=lang, error=result.get("error", "unknown"), missing=missing)
        if result.get("mode") == "native_hooks":
            return i18n.msg("reverified_native", lang=lang, core_version=result["core_version"], wrapped=", ".join(result.get("wrapped", [])))
        return i18n.msg("reverified_seams", lang=lang, core_version=result["core_version"], count=len(result.get("wrapped", [])))

    if action in {"lang", "language", "locale"}:
        _ok, msg_text = configure_language(parts[1:])
        return msg_text

    if action == "ladder":
        _ok, msg_text = configure_ladder(parts[1:])
        return msg_text

    if action == "fallback":
        _ok, msg_text = configure_fallback(parts[1:])
        return msg_text

    if action == "quorum":
        _ok, msg_text = configure_quorum(parts[1:])
        return msg_text

    if action == "doctor":
        _ok, msg_text = doctor.run()
        return msg_text

    if action == "report":
        summary = report.summary()
        rows = report.tail(10)
        body = "\n".join(
            f"  {row.get('utc', '')} {row.get('event', '')} "
            + json.dumps({k: v for k, v in row.items() if k not in {"ts", "utc", "event"}}, ensure_ascii=False)
            for row in rows
        )
        return i18n.msg(
            "report_header",
            lang=lang,
            days=summary["days"],
            counters=json.dumps(summary["counters"]),
            body=body or i18n.msg("report_empty", lang=lang),
        )

    if action in {"status", "show", ""}:
        return _status_block()

    return i18n.msg("usage", lang=lang)
