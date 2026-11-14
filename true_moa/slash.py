"""`/true-moa` — the live switch.

Registered as an in-session slash command, so the flip happens from the chat the
user is already in: no restart, no settings page, no core edit. The handler
returns a string, which the desktop renders in the transcript.
"""

from __future__ import annotations

import json

from . import gate, report, state

_USAGE = "usage: /true-moa on | off | status | retry | report"


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
