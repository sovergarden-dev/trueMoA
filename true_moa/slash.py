"""`/true-moa` — the live switch.

Registered as an in-session slash command, so the flip happens from the chat the
user is already in: no restart, no settings page, no core edit. The handler
returns a string, which the desktop renders in the transcript.
"""

from __future__ import annotations

import json
import math

from . import gate, report, state

_USAGE = (
    "usage: /true-moa on | off | status | retry | report | "
    "ladder [sync | custom <N> | <s1,s2,...>] | fallback [off | [<attempts>] <provider:model[,...]>]"
)


def configure_ladder(tokens: list[str]) -> tuple[bool, str]:
    """Show or update the retry ladder from slash or CLI tokens."""
    if not tokens:
        cfg = state.load()
        current = state.effective_ladder(cfg)
        source, attempts = state.ladder_source(cfg)
        return True, f"true-moa ladder: {' → '.join(f'{x:g}s' for x in current)} → halt [{source}: {attempts} attempts]"

    head = tokens[0].strip().lower()
    if head == "sync":
        cfg = state.save({"sync_hermes_retries": True, "custom_retries": 0})
        current = state.effective_ladder(cfg)
        _src, attempts = state.ladder_source(cfg)
        return True, f"true-moa ladder synced to Hermes API retries ({attempts} attempts): {' → '.join(f'{x:g}s' for x in current)} → halt"

    if head == "custom":
        if len(tokens) < 2:
            return False, "usage: /true-moa ladder custom <1..25>"
        try:
            count = int(tokens[1].strip())
        except ValueError:
            return False, f"invalid custom attempt count: {tokens[1]!r}"
        if count < 0 or count > 25:
            return False, "custom attempt count must be between 0 and 25"
        cfg = state.save({"custom_retries": count})
        current = state.effective_ladder(cfg)
        src, attempts = state.ladder_source(cfg)
        return True, f"true-moa ladder updated [{src}: {attempts} attempts]: {' → '.join(f'{x:g}s' for x in current)} → halt"

    raw_parts = [part.strip() for tok in tokens for part in tok.split(",") if part.strip()]
    parsed: list[float] = []
    for item in raw_parts:
        try:
            val = float(item)
        except ValueError:
            return False, f"invalid ladder step {item!r} (expected non-negative seconds, 'sync', or 'custom <N>')"
        if not math.isfinite(val) or val < 0:
            return False, f"ladder step {item!r} must be a finite non-negative number"
        parsed.append(val)
    if not parsed:
        return False, "usage: /true-moa ladder [sync | custom <N> | <s1,s2,...>]"

    cfg = state.save({"ladder": parsed[:24], "sync_hermes_retries": False, "custom_retries": 0})
    current = state.effective_ladder(cfg)
    return True, f"true-moa manual ladder set ({len(current) + 1} attempts): {' → '.join(f'{x:g}s' for x in current)} → halt"


def configure_fallback(tokens: list[str]) -> tuple[bool, str]:
    """Show or update secondary reference fallback from slash or CLI tokens."""
    if not tokens:
        cfg = state.load()
        if cfg.get("enable_secondary_fallback") and cfg.get("secondary_reference_model"):
            return (
                True,
                f"true-moa secondary fallback: enabled → {cfg['secondary_reference_model']} "
                f"after {cfg['fallback_trigger_attempts']} failure(s)",
            )
        return True, "true-moa secondary fallback: disabled"

    if len(tokens) == 1 and tokens[0].strip().lower() in {"off", "disable", "none"}:
        state.save({"enable_secondary_fallback": False})
        return True, "true-moa secondary fallback disabled."

    trigger: int | None = None
    spec_tokens = tokens
    if len(tokens) >= 2 and tokens[0].strip().isdigit():
        trigger = int(tokens[0].strip())
        if trigger < 1 or trigger > 10:
            return False, "fallback trigger attempts must be between 1 and 10"
        spec_tokens = tokens[1:]

    raw_spec = ",".join(part.strip() for tok in spec_tokens for part in tok.split(",") if part.strip())
    segments = [seg.strip() for seg in raw_spec.split(",") if seg.strip()]
    chain = state.parse_reference_model_chain(raw_spec)
    if not segments or len(chain) == 0:
        return False, "invalid secondary model spec (expected provider:model[,provider2:model2])"
    for seg in segments:
        if state.parse_reference_model_string(seg) is None:
            return False, f"invalid model entry {seg!r} (expected provider:model)"
    if any(entry["provider"].lower() == "moa" for entry in chain):
        return False, "secondary reference model cannot use the 'moa' provider"

    normalized_spec = ", ".join(f"{entry['provider']}:{entry['model']}" for entry in chain)
    patch: dict[str, object] = {
        "enable_secondary_fallback": True,
        "secondary_reference_model": normalized_spec,
    }
    if trigger is not None:
        patch["fallback_trigger_attempts"] = trigger
    cfg = state.save(patch)
    return (
        True,
        f"true-moa secondary fallback armed → {cfg['secondary_reference_model']} "
        f"after {cfg['fallback_trigger_attempts']} failure(s).",
    )


def _status_block() -> str:
    info = gate.status()
    summary = report.summary()
    counters = summary["counters"]
    ladder = " → ".join(f"{item:g}s" for item in info["ladder"])
    budget = float(info.get("max_wait_budget_s") or 0.0)
    budget_label = f" (cap {budget:g}s)" if budget > 0 else " (unbounded)"
    source = str(info.get("ladder_source") or "manual")
    attempts = int(info.get("ladder_attempts") or (len(info["ladder"]) + 1))
    source_tag = f" [{source}: {attempts} attempts]"
    lines = [
        "True MoA — " + ("ARMED" if info["installed"] and info["enabled"] else
                          "STANDBY (core fail-open)" if info["installed"] else "NOT INSTALLED"),
        f"  core            {info['core_version']} (target {info['target_core']})",
        f"  gate            {info['plugin_version']} · {len(info['wrapped'])} seam(s): "
        + (", ".join(info["wrapped"]) or "none"),
        f"  ladder          {ladder} → halt{budget_label}{source_tag}",
        f"  wait budget     retry sleep cap {budget:g}s; in-flight Reference calls are not included",
        "  aggregator fallback for references   "
        + ("blocked" if info["block_aggregator_fallback"] else "allowed"),
        (f"  secondary fallback   switches to {info.get('secondary_reference_model', '')} after {info.get('fallback_trigger_attempts', 2)} failure(s)"
         if info.get("enable_secondary_fallback") and info.get("secondary_reference_model")
         else "  secondary fallback   disabled (primary references only)"),
        "  cache protection   unconditional (failed advisories never cached)",
        f"  log             {summary['days']} day(s) · retries {counters['reference_retry']} · "
        f"switches {counters['reference_fallback_switched']} · "
        f"outages {counters['reference_outage']} · blocked fallbacks "
        f"{counters['aggregator_fallback_blocked']}",
    ]
    if info["compat_error"]:
        lines.append(f"  compatibility   FAILED — {info['compat_error']}")
        for item in info["compat_missing"]:
            lines.append(f"                  · {item}")
    return "\n".join(lines)


def handle(raw_args: str) -> str:
    parts = (raw_args or "").strip().split()
    action = parts[0].lower() if parts else "status"

    if action in {"on", "enable", "arm"}:
        config = state.set_enabled(True)
        installed = gate.install()
        if not installed.get("ok"):
            return ("True MoA could not arm: " + installed.get("error", "unknown reason")
                    + "\n" + "\n".join(f"  · {item}" for item in installed.get("missing", []))
                    + "\nMoA still runs with Hermes' stock fail-open behaviour.")
        report.event("gate_armed")
        ladder = " → ".join(f"{item:g}s" for item in state.effective_ladder(config))
        return f"True MoA armed. References retry {ladder} and the turn halts if they never answer."

    if action in {"off", "disable", "disarm"}:
        state.set_enabled(False)
        report.event("gate_disarmed")
        return ("True MoA disarmed. MoA falls back to Hermes' stock fail-open behaviour; "
                "the gate is still loaded, so `/true-moa on` restores it instantly.")

    if action in {"retry", "reinstall"}:
        result = gate.install(force=True)
        if not result.get("ok"):
            return "Re-check failed: " + result.get("error", "unknown") + "\n" + "\n".join(
                f"  · {item}" for item in result.get("missing", [])
            )
        return f"Gate re-verified against Hermes {result['core_version']} — {len(result.get('wrapped', []))} seam(s) wrapped."

    if action == "ladder":
        _ok, msg = configure_ladder(parts[1:])
        return msg

    if action == "fallback":
        _ok, msg = configure_fallback(parts[1:])
        return msg

    if action == "report":
        summary = report.summary()
        rows = report.tail(10)
        body = "\n".join(
            f"  {row.get('utc', '')} {row.get('event', '')} "
            + json.dumps({k: v for k, v in row.items() if k not in {'ts', 'utc', 'event'}}, ensure_ascii=False)
            for row in rows
        )
        return (f"True MoA log — {summary['days']} day(s), counters {json.dumps(summary['counters'])}\n"
                + (body or "  (empty)"))

    if action in {"status", "show", ""}:
        return _status_block()

    return _USAGE
