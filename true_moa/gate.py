"""The strict gate itself.

Hermes' Mixture of Agents is fail-open by design: a reference model that raises
becomes a ``[failed: ...]`` string, the aggregator is told to "act on your own
judgment", the failure is written into the fan-out cache, one-shot ``/moa``
swallows aggregation exceptions, and the auxiliary client may answer a failed
reference call with the aggregator's own model. Every one of those paths is
closed here across the verified runtime seams:

1. ``MoAChatCompletions._run_fanout`` — retries only failed slots on the configured
   ladder, supports cascading secondary reference chains, wakes immediately on user
   interrupt, never caches a failure, and raises when exhausted.
2. ``MoAChatCompletions._build_guidance`` — ensures an all-failed fan-out never
   reaches the aggregator prompt, including from any pre-existing cache.
3. ``MoAChatCompletions.prepare`` / ``create`` — defers outage/interrupt exceptions
   into Hermes' API retry-loop error handler so turns finalize cleanly.
4. ``moa_loop.aggregate_moa_context`` / ``turn_api_request.build_api_request`` —
   extends the exact same fail-closed gate to one-shot ``/moa`` turns.
5. ``auxiliary_client._try_main_agent_model_fallback`` — blocks main-agent /
   aggregator impersonation on ``moa_reference`` tasks.

Nothing here edits a Hermes file. Wrappers live in-process only, verify signatures
before patching, and restore stock methods cleanly on unload.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import inspect
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import i18n, report, state

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "1.6.1"
TARGET_CORE = ">=0.23.0"

_MOA_MODULE = "agent.moa_loop"
_AUX_MODULE = "agent.auxiliary_client"
_TURN_API_REQ_MODULE = "agent.turn_api_request"
_SLICE = 0.1
_LIVENESS_TOUCH_INTERVAL_S = 10.0
_MAX_VERDICT_STATES = 64
_INTERRUPTED_SENTINEL = "[skipped: interrupted by user]"
_TRANSIENT_FAILURE_HINTS = (
    "429", "rate limit", "rate_limit", "too many requests", "timed out",
    "timeout", "overloaded", "502", "503", "504", "connection", "temporarily",
    "cloudflare", "ray id", "just a moment", "challenge", "turnstile", "waf",
    "quota", "capacity", "busy", "try again",
)
_NON_RETRYABLE_RE = re.compile(
    r"(?:\b(?:error\s+code:\s*(?:401|404)|http\s*(?:401|404)|"
    r"status(?:_code|\s+code)?\s*[:=]\s*(?:401|404)|401\s+unauthorized|"
    r"invalid[_\s-]api[_\s-]key|incorrect\s+api\s+key|authentication[_\s-]error|"
    r"permission[_\s-]denied|insufficient[_\s-]permissions|access[_\s-]denied|"
    r"model[_\s-]not[_\s-]found)\b|does\s+not\s+exist|not\s+found\s+for\s+api\s+version)",
    re.IGNORECASE,
)

# name -> (attribute, required parameter names in order). Verified before patching.
_TARGETS: dict[str, tuple[str, tuple[str, ...]]] = {
    "moa.prepare": ("prepare", ("self", "messages")),
    "moa.create": ("create", ("self",)),
    "moa._run_fanout": (
        "_run_fanout",
        ("self", "preset", "ref_messages", "reference_models", "aggregator",
         "aggregator_temperature", "cache_key"),
    ),
    "moa._build_guidance": (
        "_build_guidance",
        ("self", "reference_outputs", "aggregator", "degraded_reference_policy"),
    ),
    "moa.aggregate_moa_context": (
        "aggregate_moa_context",
        ("user_prompt", "api_messages", "reference_models", "aggregator"),
    ),
    "assembly.build_api_request": (
        "build_api_request",
        ("agent", "api_messages", "_moa_prepared_request"),
    ),
    "aux._try_main_agent_model_fallback": (
        "_try_main_agent_model_fallback",
        ("failed_provider", "task", "reason", "failed_model", "failed_base_url", "failure_scope"),
    ),
}

_lock = threading.RLock()
_patched: list[tuple[Any, str, Callable[..., Any]]] = []
_native_mode_active = False
_verdict_turn_state: dict[str, Any] = {}
_compat: dict[str, Any] = {
    "ok": False,
    "installed": False,
    "mode": "in_process",
    "core_version": "",
    "plugin_version": PLUGIN_VERSION,
    "checked": [],
    "missing": [],
    "error": "",
}


class MoaReferenceOutage(RuntimeError):
    """Every reference model failed the ladder. The aggregator must not act."""

    def __init__(
        self,
        labels: tuple[str, ...],
        attempts: int,
        waited: float,
        ladder: list[float] | None = None,
        budget_capped: bool = False,
    ) -> None:
        self.labels = labels
        self.attempts = attempts
        self.waited = waited
        self.ladder = ladder
        self.budget_capped = budget_capped
        lang = i18n.active_language()
        names = ", ".join(labels) if labels else "unknown"
        ladder_desc = (
            i18n.msg("outage_ladder_exhausted", lang=lang, ladder=ladder)
            if ladder and not budget_capped
            else i18n.msg("outage_budget_reached", lang=lang)
            if budget_capped
            else ""
        )
        super().__init__(
            i18n.msg(
                "outage_message",
                lang=lang,
                names=names,
                attempts=attempts,
                waited=waited,
                ladder_desc=ladder_desc,
            )
        )


def _core_version() -> str:
    """Identity of the running core, read from its own install stamp."""
    try:
        spec = importlib.util.find_spec("hermes_cli")
        root = Path(spec.origin).resolve().parent.parent if spec and spec.origin else None
    except Exception:
        root = None
    if root is not None:
        for stamp_path in (root / "install-stamp.json", root.parent / "install-stamp.json"):
            try:
                data = json.loads(stamp_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            display = str(data.get("displayVersion") or data.get("baseVersion") or "")
            if display:
                return display
        try:
            text = (root / "hermes_cli" / "__init__.py").read_text(encoding="utf-8", errors="replace")
            match = re.search(r'__release_date__\s*=\s*"([^"]+)"', text)
            if match:
                return f"release {match.group(1)}"
        except OSError:
            pass
    return "unknown"


def _signature_problem(target: Any, expected: tuple[str, ...]) -> str:
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError) as exc:
        return f"signature unavailable ({exc})"
    names: list[str] = []
    for param in signature.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.append(param.name)
    missing = [name for name in expected if name not in names]
    if missing:
        return f"missing parameter(s) {missing}; core has {names}"
    return ""


def is_non_retryable_failure(text: Any) -> bool:
    """True for deterministic auth/permission/missing-model errors where sleeping cannot recover."""
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    lowered = stripped.lower()
    if not lowered.startswith("[failed:"):
        return False
    if any(hint in lowered for hint in _TRANSIENT_FAILURE_HINTS):
        return False
    return bool(_NON_RETRYABLE_RE.search(stripped))


def _is_interrupted(agent: Any, outputs: list[tuple[str, str, Any]] | None = None) -> bool:
    if agent is not None and bool(getattr(agent, "_interrupt_requested", False)):
        return True
    if outputs:
        return any(str(text).strip() == _INTERRUPTED_SENTINEL for _lbl, text, _acct in outputs)
    return False


def _touch_liveness(agent: Any, description: str) -> None:
    touch = getattr(agent, "_touch_activity", None)
    if callable(touch):
        with contextlib.suppress(Exception):
            touch(description)


def _sleep(seconds: float, agent: Any = None, *, label: str = "") -> str:
    """Wait *seconds*, keeping turn-liveness warm and waking immediately on disable or interrupt."""
    wait_s = max(0.0, float(seconds))
    deadline = time.monotonic() + wait_s
    desc = label or "True MoA: waiting on reference retry backoff"
    last_touch = time.monotonic()
    if wait_s > 0.0:
        _touch_liveness(agent, desc)
    while True:
        if not state.enabled():
            return "disabled"
        if agent is not None and bool(getattr(agent, "_interrupt_requested", False)):
            return "interrupted"
        now = time.monotonic()
        if wait_s > 0.0 and (now - last_touch) >= _LIVENESS_TOUCH_INTERVAL_S:
            last_touch = now
            _touch_liveness(agent, desc)
        remaining = deadline - now
        if remaining <= 0:
            return "slept"
        time.sleep(min(_SLICE, remaining))


def _emit_progress(owner: Any, done: int, total: int, label: str) -> None:
    emit = getattr(owner, "_emit", None)
    if not callable(emit):
        return
    try:
        emit("moa.progress", refs_done=done, refs_total=total, label=label)
    except Exception:
        pass


def _slot_identity(slot: dict[str, Any]) -> tuple[str, str]:
    """Canonical (endpoint-or-provider, model) key so custom aliases ('be' vs 'beat') compare equal."""
    prov = str(slot.get("provider") or "").strip().lower()
    model = str(slot.get("model") or "").strip().lower()
    if prov:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider
            rt = resolve_runtime_provider(requested=prov, target_model=model)
            base_url = str(rt.get("base_url") or "").strip().rstrip("/").lower()
            if base_url:
                return (f"url:{base_url}", model)
        except Exception:
            pass
    return (prov, model)


def _valid_secondary_chain(raw_spec: Any, aggregator: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Filter parsed secondary specs against moa recursion and aggregator impersonation."""
    candidates = state.parse_reference_model_chain(raw_spec)
    safe: list[dict[str, Any]] = []
    agg_p = str((aggregator or {}).get("provider") or "").lower()
    agg_m = str((aggregator or {}).get("model") or "").lower()
    agg_id = _slot_identity(aggregator) if (aggregator and agg_p) else None

    for cand in candidates:
        sec_p = str(cand.get("provider") or "").lower()
        sec_m = str(cand.get("model") or "").lower()
        label = f"{cand.get('provider')}:{cand.get('model')}"
        if sec_p == "moa":
            logger.warning("true-moa: secondary reference %s cannot use 'moa' provider — candidate skipped", label)
            continue
        if aggregator:
            same_slot = (_slot_identity(cand) == agg_id) if (sec_p and agg_id is not None) else (sec_m == agg_m)
            if same_slot:
                logger.warning(
                    "true-moa: secondary reference %s matches aggregator %s — candidate skipped to prevent impersonation",
                    label, aggregator,
                )
                continue
        safe.append(cand)
    return safe


def _sync_facade_merged_outputs(
    facade: Any,
    moa_loop: Any,
    cache_key: Any,
    merged_outputs: list[tuple[str, str, Any]],
    *,
    allow_cache: bool = True,
) -> None:
    """Keep cache, trace and reference metrics coherent after selective slot retries."""
    facade._ref_cache_key = cache_key if allow_cache else None
    facade._ref_cache_outputs = list(merged_outputs) if allow_cache else []
    privacy_mode = getattr(facade, "_privacy_mode", "")
    redact_txt = getattr(moa_loop, "_redact_reference_text", lambda x: x)
    redact_acct = getattr(moa_loop, "_redact_trace_accounting", lambda x: x)
    trace_refs = (
        [(lbl, redact_txt(txt), redact_acct(acct)) for lbl, txt, acct in merged_outputs]
        if privacy_mode
        else list(merged_outputs)
    )
    pending_trace = getattr(facade, "_pending_trace", None)
    if isinstance(pending_trace, dict):
        pending_trace["reference_outputs"] = trace_refs
    try:
        from agent.moa_trace import slot_metrics
        facade._last_reference_metrics = [slot_metrics(acct, lbl, output=txt) for lbl, txt, acct in trace_refs]
    except Exception:
        pass


def _emit_final_fanout_ui(
    facade: Any,
    moa_loop: Any,
    final_outputs: list[tuple[str, str, Any]],
    aggregator: dict[str, Any],
) -> None:
    """Emit advisor answers and the aggregator phase once after retries settle."""
    emit = getattr(facade, "_emit", None)
    if not callable(emit):
        return
    is_failed = getattr(moa_loop, "_is_failed_reference", lambda t: str(t).lstrip().lower().startswith(("[failed:", "[skipped:")))
    redact_txt = getattr(moa_loop, "_redact_reference_text", lambda x: x)
    slot_label_fn = getattr(
        moa_loop, "_slot_label",
        lambda s: f"{s.get('provider')}:{s.get('model')}" if s.get("provider") else str(s.get("model")),
    )
    privacy_mode = getattr(facade, "_privacy_mode", "")
    healthy = [(lbl, txt, acct) for lbl, txt, acct in final_outputs if not is_failed(txt)]
    ref_count = len(healthy)
    for idx, (lbl, txt, _acct) in enumerate(healthy, start=1):
        emit("moa.reference", index=idx, count=ref_count, label=lbl, text=redact_txt(txt) if privacy_mode else txt)
    if ref_count:
        agg_label = slot_label_fn(aggregator)
        emit("moa.phase", phase="aggregator", refs_done=ref_count, refs_total=len(final_outputs), aggregator=agg_label)
        emit("moa.aggregating", aggregator=agg_label, ref_count=ref_count)


def _required_healthy_count(config: dict[str, Any], total_slots: int) -> int:
    if total_slots <= 0:
        return 0
    raw = int(config.get("min_healthy_references") or 0)
    return total_slots if raw <= 0 else max(1, min(total_slots, raw))


class _FanoutDecisionState:
    """Single source of truth for selective retries, secondary cascades, and quorum checks."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        initial_slots: list[dict[str, Any]],
        aggregator: dict[str, Any] | None,
        preset_name: str,
        slot_label_fn: Callable[[dict[str, Any]], str],
    ) -> None:
        self.preset_name = preset_name
        self.slot_label_fn = slot_label_fn
        self.slots: list[dict[str, Any]] = [dict(m) for m in initial_slots]
        self.total_slots = len(self.slots)
        self.required_healthy = _required_healthy_count(config, self.total_slots)
        self.ladder = [float(item) for item in state.effective_ladder(config)]
        self.max_wait_budget = float(config.get("max_wait_budget_s") or 0.0)
        self.enable_fallback = bool(config.get("enable_secondary_fallback"))
        self.fallback_trigger = int(config.get("fallback_trigger_attempts") or 2)
        self.secondary_chain = (
            _valid_secondary_chain(config.get("secondary_reference_model"), aggregator)
            if self.enable_fallback
            else []
        )
        self.secondary_idx = 0
        self.next_switch_attempt = self.fallback_trigger
        self.fallback_activated = False
        self.switched_from: list[str] = []
        self.resolved: list[Any | None] = [None] * self.total_slots
        self.last_failed_by_slot: dict[int, Any] = {}
        self.permanently_failed_labels: list[str] = []
        self.pending_indices: list[int] = list(range(self.total_slots))
        self.failed_identities: set[tuple[str, str]] = set()
        self.waited_s = 0.0

    def assemble_outputs(self) -> list[Any]:
        merged: list[Any] = []
        for idx in range(self.total_slots):
            entry = self.resolved[idx] if self.resolved[idx] is not None else self.last_failed_by_slot.get(idx)
            if entry is not None:
                merged.append(entry)
        return merged

    def evaluate_batch(
        self,
        batch_items: list[tuple[str, str, Any, bool]],
        *,
        attempt_num: int,
    ) -> dict[str, Any]:
        still_pending: list[int] = []
        failed_labels: list[str] = []
        non_retryable_slots: set[int] = set()

        for pos, slot_idx in enumerate(self.pending_indices):
            if pos >= len(batch_items):
                lbl = self.slot_label_fn(self.slots[slot_idx])
                still_pending.append(slot_idx)
                failed_labels.append(lbl)
                self.failed_identities.add(_slot_identity(self.slots[slot_idx]))
                continue
            lbl, txt, payload, is_fail = batch_items[pos]
            if is_fail:
                still_pending.append(slot_idx)
                failed_labels.append(lbl)
                self.last_failed_by_slot[slot_idx] = payload
                self.failed_identities.add(_slot_identity(self.slots[slot_idx]))
                if is_non_retryable_failure(txt):
                    non_retryable_slots.add(slot_idx)
            else:
                self.resolved[slot_idx] = payload
                self.last_failed_by_slot.pop(slot_idx, None)

        healthy_count = sum(1 for r in self.resolved if r is not None)
        if not still_pending:
            if healthy_count >= self.required_healthy:
                all_clean = (healthy_count == self.total_slots) and not self.permanently_failed_labels
                return {
                    "action": "accept",
                    "outputs": self.assemble_outputs(),
                    "allow_cache": all_clean,
                    "quorum": not all_clean,
                    "healthy": healthy_count,
                    "failed": list(self.permanently_failed_labels),
                }
            return {
                "action": "abort",
                "failed": list(dict.fromkeys([*self.permanently_failed_labels, *failed_labels])),
                "budget_capped": False,
            }

        attempt_idx = max(0, attempt_num - 1)
        budget_exhausted = self.max_wait_budget > 0.0 and self.waited_s >= self.max_wait_budget
        has_next = (attempt_idx < len(self.ladder)) and not budget_exhausted

        threshold_due = attempt_num >= self.next_switch_attempt
        fast_switch_due = bool(non_retryable_slots)
        switched_now: set[int] = set()
        switched_targets: list[str] = []

        if (self.enable_fallback and self.secondary_idx < len(self.secondary_chain)
                and has_next and (threshold_due or fast_switch_due)):
            eligible = (
                [i for i in still_pending if i in non_retryable_slots]
                + [i for i in still_pending if i not in non_retryable_slots]
                if threshold_due
                else [i for i in still_pending if i in non_retryable_slots]
            )
            occupied = {
                _slot_identity(self.slots[i])
                for i in range(self.total_slots)
                if self.resolved[i] is not None or i in still_pending
            }
            for target_idx in eligible:
                if self.secondary_idx >= len(self.secondary_chain):
                    break
                target_old_id = _slot_identity(self.slots[target_idx])
                chosen: dict[str, Any] | None = None
                while self.secondary_idx < len(self.secondary_chain):
                    cand = self.secondary_chain[self.secondary_idx]
                    self.secondary_idx += 1
                    cand_id = _slot_identity(cand)
                    if cand_id in self.failed_identities or cand_id in (occupied - {target_old_id}):
                        logger.warning(
                            "true-moa: secondary reference %s:%s matches a failed or active slot — skipping candidate",
                            cand.get("provider"), cand.get("model"),
                        )
                        continue
                    chosen = cand
                    break
                if chosen is not None:
                    old_lbl = self.slot_label_fn(self.slots[target_idx])
                    sec_lbl = self.slot_label_fn(chosen)
                    occupied.discard(target_old_id)
                    self.slots[target_idx] = dict(chosen)
                    occupied.add(_slot_identity(chosen))
                    non_retryable_slots.discard(target_idx)
                    switched_now.add(target_idx)
                    switched_targets.append(sec_lbl)
                    self.fallback_activated = True
                    if old_lbl not in self.switched_from:
                        self.switched_from.append(old_lbl)
                    report.event(
                        "reference_fallback_switched",
                        preset=self.preset_name,
                        primary=[old_lbl],
                        secondary=sec_lbl,
                        attempt=attempt_num,
                        fast_switch=fast_switch_due and not threshold_due,
                    )
                    logger.warning(
                        "true-moa: reference %s failed %d attempt(s) — switched to secondary reference %s",
                        old_lbl, attempt_num, sec_lbl,
                    )
            if threshold_due:
                self.next_switch_attempt = attempt_num + self.fallback_trigger

        if non_retryable_slots:
            for dead_idx in sorted(non_retryable_slots):
                raw_dead = self.last_failed_by_slot.get(dead_idx)
                dead_lbl = raw_dead[0] if isinstance(raw_dead, tuple) and raw_dead else self.slot_label_fn(self.slots[dead_idx])
                if dead_lbl not in self.permanently_failed_labels:
                    self.permanently_failed_labels.append(str(dead_lbl))
            still_pending = [idx for idx in still_pending if idx not in non_retryable_slots]

        self.pending_indices = still_pending
        healthy_count = sum(1 for r in self.resolved if r is not None)
        max_possible_healthy = healthy_count + len(self.pending_indices)

        if not self.pending_indices or not has_next or max_possible_healthy < self.required_healthy:
            all_failed_names = list(dict.fromkeys([*self.permanently_failed_labels, *failed_labels]))
            if healthy_count >= self.required_healthy:
                return {
                    "action": "accept",
                    "outputs": self.assemble_outputs(),
                    "allow_cache": False,
                    "quorum": True,
                    "healthy": healthy_count,
                    "failed": all_failed_names,
                }
            return {
                "action": "abort",
                "failed": all_failed_names,
                "budget_capped": bool(
                    budget_exhausted
                    and max_possible_healthy >= self.required_healthy
                    and attempt_idx < len(self.ladder)
                    and bool(self.pending_indices)
                ),
            }

        only_fast_switched = bool(switched_now) and set(self.pending_indices).issubset(switched_now) and not threshold_due
        delay = 0.0 if only_fast_switched else self.ladder[attempt_idx]
        if self.max_wait_budget > 0.0 and delay > 0.0:
            delay = min(delay, max(0.0, self.max_wait_budget - self.waited_s))
        return {
            "action": "retry",
            "slots": [dict(self.slots[i]) for i in self.pending_indices],
            "delay_s": delay,
            "failed": failed_labels,
            "healthy": healthy_count,
            "switched_to": switched_targets,
        }


def _run_gated_fanout(
    *,
    config: dict[str, Any],
    moa_loop: Any,
    initial_slots: list[dict[str, Any]],
    aggregator: dict[str, Any],
    preset_name: str,
    agent: Any,
    progress_owner: Any,
    run_batch: Callable[[list[dict[str, Any]]], list[tuple[str, str, Any]]],
    on_failure_purge: Callable[[], None] | None = None,
    on_outage_record: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[tuple[str, str, Any]], int, bool]:
    """Shared selective-retry, secondary-cascade, quorum and interruptible-sleep engine."""
    is_failed = getattr(moa_loop, "_is_failed_reference")
    slot_label_fn = getattr(
        moa_loop, "_slot_label",
        lambda s: f"{s.get('provider')}:{s.get('model')}" if s.get("provider") else str(s.get("model")),
    )
    decision = _FanoutDecisionState(
        config=config,
        initial_slots=initial_slots,
        aggregator=aggregator,
        preset_name=preset_name,
        slot_label_fn=slot_label_fn,
    )
    attempt = 0
    started = time.monotonic()

    while True:
        if _is_interrupted(agent):
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt + 1)
            raise InterruptedError("MoA reference fan-out interrupted by user")

        batch_slots = [decision.slots[i] for i in decision.pending_indices]
        batch_outputs = run_batch(batch_slots)

        if _is_interrupted(agent, batch_outputs):
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt + 1)
            raise InterruptedError("MoA reference fan-out interrupted by user")

        batch_items = [(out[0], out[1], out, bool(is_failed(out[1]))) for out in batch_outputs]
        step = decision.evaluate_batch(batch_items, attempt_num=attempt + 1)

        if step["action"] == "accept":
            if step["quorum"]:
                if on_failure_purge is not None:
                    on_failure_purge()
                report.event(
                    "quorum_accepted",
                    preset=preset_name,
                    healthy=step["healthy"],
                    total=decision.total_slots,
                    failed=step["failed"],
                )
            return list(step["outputs"]), attempt, bool(step["allow_cache"])

        if on_failure_purge is not None:
            on_failure_purge()

        if step["action"] == "abort":
            waited = time.monotonic() - started
            effective_labels = list(step["failed"])
            budget_capped = bool(step["budget_capped"])
            outage = {
                "labels": effective_labels,
                "attempts": attempt + 1,
                "waited_s": round(waited, 1),
                "preset": preset_name,
                "budget_capped": budget_capped,
                "ladder": list(decision.ladder),
                "fallback_activated": decision.fallback_activated,
                "switched_from": list(decision.switched_from),
            }
            if on_outage_record is not None:
                on_outage_record(outage)
            report.event(
                "reference_outage",
                preset=preset_name,
                failed=effective_labels,
                attempts=attempt + 1,
                waited_s=round(waited, 1),
                budget_capped=budget_capped,
            )
            logger.error(
                "true-moa: reference outage holds the turn — %s failed %d attempt(s) over %.0fs%s",
                ", ".join(effective_labels), attempt + 1, waited,
                " (retry-sleep budget reached)" if budget_capped else "",
            )
            raise MoaReferenceOutage(
                tuple(effective_labels),
                attempt + 1,
                waited,
                ladder=list(decision.ladder),
                budget_capped=budget_capped,
            )

        delay = float(step["delay_s"])
        failed_labels = list(step["failed"])
        switched_to = list(step.get("switched_to") or [])
        lang = i18n.active_language(config)
        attempt += 1
        report.event(
            "reference_retry",
            preset=preset_name,
            failed=failed_labels,
            attempt=attempt,
            delay_s=delay,
        )
        logger.warning(
            "true-moa: reference %s failed — retry %d/%d in %.0fs (aggregator held)",
            ", ".join(failed_labels), attempt, len(decision.ladder), delay,
        )
        if switched_to:
            _emit_progress(
                progress_owner,
                int(step["healthy"]),
                decision.total_slots,
                i18n.msg("progress_switched", lang=lang, secondary=", ".join(switched_to)),
            )
        progress_msg = i18n.msg(
            "progress_retry",
            lang=lang,
            delay=delay,
            models=", ".join(failed_labels),
            attempt=attempt,
            total=len(decision.ladder),
        )
        _emit_progress(progress_owner, int(step["healthy"]), decision.total_slots, progress_msg)
        sleep_started = time.monotonic()
        sleep_state = _sleep(delay, agent=agent, label=f"True MoA: retry {attempt}/{len(decision.ladder)} backoff ({delay:g}s)")
        decision.waited_s += max(0.0, time.monotonic() - sleep_started)
        if sleep_state == "disabled":
            logger.warning("true-moa: gate disabled during reference retry — standing down")
            report.event("gate_stood_down", preset=preset_name)
            fallback_by_pos = dict(zip(decision.pending_indices, batch_outputs, strict=False))
            stood_down_outputs = [
                item
                for i in range(decision.total_slots)
                if (item := decision.resolved[i] or fallback_by_pos.get(i)) is not None
            ]
            return stood_down_outputs, attempt, False
        if sleep_state == "interrupted":
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt)
            raise InterruptedError("MoA reference retry interrupted by user")


def _mark_wrapper(wrapper: Callable[..., Any], original: Callable[..., Any], fallback_name: str) -> Callable[..., Any]:
    meta: Any = wrapper
    meta.__name__ = getattr(original, "__name__", fallback_name)
    meta.__doc__ = getattr(original, "__doc__", None)
    meta.__true_moa_original__ = original
    return wrapper


def _wrap_run_fanout(original: Callable[..., Any]) -> Callable[..., Any]:
    def _run_fanout(
        self: Any,
        preset: dict[str, Any],
        ref_messages: list[dict[str, Any]],
        reference_models: list[dict[str, Any]],
        aggregator: dict[str, Any],
        aggregator_temperature: Any,
        cache_key: Any,
    ) -> Any:
        config = state.load()
        if not config["enabled"] or not reference_models:
            return original(self, preset, ref_messages, reference_models, aggregator,
                            aggregator_temperature, cache_key)

        moa_loop: Any = importlib.import_module(_MOA_MODULE)

        def _purge_cache() -> None:
            self._ref_cache_key = None
            self._ref_cache_outputs = []
            self._true_moa_cache_poisoned = True

        def _record_outage(outage: dict[str, Any]) -> None:
            self._true_moa_outage = outage

        orig_emit = getattr(self, "_emit", None)
        had_instance_emit = "_emit" in getattr(self, "__dict__", {})

        def _progress_only_emit(event_name: str, **kwargs: Any) -> None:
            if event_name == "moa.progress" and callable(orig_emit):
                orig_emit(event_name, **kwargs)

        if callable(orig_emit):
            self._emit = _progress_only_emit
        try:
            final_outputs, attempts_used, allow_cache = _run_gated_fanout(
                config=config,
                moa_loop=moa_loop,
                initial_slots=reference_models,
                aggregator=aggregator,
                preset_name=str(getattr(self, "preset_name", "") or ""),
                agent=getattr(self, "_agent", None),
                progress_owner=self,
                run_batch=lambda bs: original(
                    self, preset, ref_messages, bs, aggregator, aggregator_temperature, cache_key
                ),
                on_failure_purge=_purge_cache,
                on_outage_record=_record_outage,
            )
        finally:
            if callable(orig_emit):
                if had_instance_emit:
                    self._emit = orig_emit
                else:
                    self.__dict__.pop("_emit", None)

        self._true_moa_outage = None
        if not allow_cache or attempts_used > 0 or len(final_outputs) != len(getattr(self, "_ref_cache_outputs", []) or []):
            _sync_facade_merged_outputs(self, moa_loop, cache_key, final_outputs, allow_cache=allow_cache)
        _emit_final_fanout_ui(self, moa_loop, final_outputs, aggregator)
        return final_outputs

    return _mark_wrapper(_run_fanout, original, "_run_fanout")


def _synthesize_oneshot_guidance(
    *,
    moa_loop: Any,
    user_prompt: str,
    final_outputs: list[tuple[str, str, Any]],
    aggregator: dict[str, Any],
    aggregator_temperature: float | None,
    degraded_reference_policy: str,
    agent: Any,
    on_outage_record: Callable[[dict[str, Any]], None],
) -> str:
    """Synthesize verified one-shot `/moa` advisor outputs using Hermes' own helpers."""
    privacy_full = False
    with contextlib.suppress(Exception):
        cfg_mod = importlib.import_module("hermes_cli.config")
        privacy_full = moa_loop._moa_privacy_mode((cfg_mod.load_config() or {}).get("moa")) == "full"
    agg_refs, degraded, all_failed = moa_loop._guidance_inputs(
        final_outputs, privacy_full, degraded_reference_policy
    )
    if all_failed:
        failed_labels = [lbl for lbl, _t, _a in final_outputs]
        outage: dict[str, Any] = {"labels": failed_labels, "attempts": 1, "waited_s": 0.0, "preset": "oneshot"}
        on_outage_record(outage)
        raise MoaReferenceOutage(tuple(failed_labels), 1, 0.0)

    joined = moa_loop._join_reference_outputs(agg_refs, degraded)
    synth_prompt = (
        "You are the aggregator in a Mixture of Agents process. Synthesize the "
        "reference responses into concise, actionable guidance for the main "
        "Hermes agent. Focus on next steps, tool-use strategy, risks, and any "
        "disagreements. Do not answer the user directly unless that is all that "
        "is needed; produce context the main agent should use in its normal loop.\n\n"
        f"Original user prompt:\n{user_prompt}\n\n"
        f"Reference responses:\n{joined}"
    )
    agg_label = moa_loop._slot_label(aggregator)
    agg_runtime = moa_loop._slot_runtime(aggregator)
    moa_loop._touch_fanout_progress(agent, f"MoA: synthesizing aggregator guidance ({agg_label})")
    try:
        synthesis = moa_loop._extract_text(moa_loop.call_llm(
            task="moa_aggregator",
            messages=[{"role": "user", "content": synth_prompt}],
            temperature=aggregator_temperature,
            reasoning_config=moa_loop._aggregator_reasoning_config(aggregator),
            **agg_runtime,
        ))
    except Exception as exc:
        logger.warning("MoA aggregator model %s failed: %s", agg_label, exc)
        synthesis = ""

    return (
        "[Mixture of Agents context — use this as private guidance for the "
        "normal Hermes agent loop. You may call tools, continue reasoning, or "
        "finish normally.]\n"
        f"Aggregator: {agg_label}\n"
        f"References: {', '.join(lbl for lbl, _t, _a in agg_refs)}\n\n"
        f"{(synthesis or joined).strip()}"
    )


def _wrap_aggregate_moa_context(original: Callable[..., Any]) -> Callable[..., Any]:
    def aggregate_moa_context(
        *,
        user_prompt: str,
        api_messages: list[dict[str, Any]],
        reference_models: list[dict[str, Any]],
        aggregator: dict[str, Any],
        temperature: float | None = None,
        aggregator_temperature: float | None = None,
        reference_max_tokens: int | None = None,
        reference_timeout: float | None = None,
        degraded_reference_policy: str = "loud",
        agent: Any = None,
    ) -> str:
        config = state.load()
        enabled_slots = [dict(s) for s in (reference_models or []) if s.get("enabled", True)]
        if not config["enabled"] or not enabled_slots:
            return str(original(
                user_prompt=user_prompt, api_messages=api_messages, reference_models=reference_models,
                aggregator=aggregator, temperature=temperature, aggregator_temperature=aggregator_temperature,
                reference_max_tokens=reference_max_tokens, reference_timeout=reference_timeout,
                degraded_reference_policy=degraded_reference_policy, agent=agent,
            ))

        moa_loop: Any = importlib.import_module(_MOA_MODULE)
        ref_msgs = moa_loop._reference_messages(api_messages)

        def _record_oneshot_outage(outage: dict[str, Any]) -> None:
            if agent is not None:
                agent._true_moa_oneshot_outage = outage

        try:
            final_outputs, _attempts, _allow_cache = _run_gated_fanout(
                config=config,
                moa_loop=moa_loop,
                initial_slots=enabled_slots,
                aggregator=aggregator,
                preset_name="oneshot",
                agent=agent,
                progress_owner=None,
                run_batch=lambda bs: moa_loop._run_references_parallel(
                    bs, ref_msgs, temperature=temperature, max_tokens=reference_max_tokens,
                    reference_timeout=reference_timeout, agent=agent,
                ),
                on_failure_purge=None,
                on_outage_record=_record_oneshot_outage,
            )
        except InterruptedError:
            if agent is not None:
                agent._true_moa_oneshot_interrupted = True
            raise

        return _synthesize_oneshot_guidance(
            moa_loop=moa_loop,
            user_prompt=user_prompt,
            final_outputs=final_outputs,
            aggregator=aggregator,
            aggregator_temperature=aggregator_temperature,
            degraded_reference_policy=degraded_reference_policy,
            agent=agent,
            on_outage_record=_record_oneshot_outage,
        )

    return _mark_wrapper(aggregate_moa_context, original, "aggregate_moa_context")


def _wrap_build_api_request(original: Callable[..., Any]) -> Callable[..., Any]:
    def build_api_request(agent: Any, **kwargs: Any) -> Any:
        if agent is not None:
            interrupted = bool(getattr(agent, "_true_moa_oneshot_interrupted", False))
            if interrupted:
                agent._true_moa_oneshot_interrupted = False
                raise InterruptedError("MoA reference retry interrupted by user")
            outage = getattr(agent, "_true_moa_oneshot_outage", None)
            if isinstance(outage, dict) and outage:
                agent._true_moa_oneshot_outage = None
                raw_ladder = outage.get("ladder")
                ladder = list(raw_ladder) if isinstance(raw_ladder, (list, tuple)) else None
                raise MoaReferenceOutage(
                    tuple(outage.get("labels") or ()),
                    int(outage.get("attempts") or 0),
                    float(outage.get("waited_s") or 0.0),
                    ladder=ladder,
                    budget_capped=bool(outage.get("budget_capped")),
                )
        return original(agent, **kwargs)

    return _mark_wrapper(build_api_request, original, "build_api_request")


def _wrap_build_guidance(original: Callable[..., Any]) -> Callable[..., Any]:
    def _build_guidance(
        self: Any,
        reference_outputs: list[tuple[str, str, Any]],
        aggregator: dict[str, Any],
        degraded_reference_policy: str,
        stale: bool = False,
    ) -> Any:
        if state.enabled() and reference_outputs:
            moa_loop: Any = importlib.import_module(_MOA_MODULE)
            guidance_inputs = getattr(moa_loop, "_guidance_inputs")
            privacy_full = getattr(self, "_privacy_mode", "") == "full"
            _refs, _degraded, all_failed = guidance_inputs(
                reference_outputs, privacy_full, degraded_reference_policy
            )
            if all_failed:
                labels = tuple(
                    label for label, text, _acct in reference_outputs
                    if getattr(moa_loop, "_is_failed_reference")(text)
                )
                self._true_moa_outage = {
                    "labels": list(labels),
                    "attempts": 0,
                    "waited_s": 0.0,
                    "preset": str(getattr(self, "preset_name", "") or ""),
                }
                self._ref_cache_key = None
                self._ref_cache_outputs = []
                self._true_moa_cache_poisoned = True
                report.event(
                    "reference_outage",
                    preset=getattr(self, "preset_name", ""),
                    failed=list(labels),
                    stage="guidance",
                )
                raise MoaReferenceOutage(labels, 0, 0.0)
        return original(self, reference_outputs, aggregator, degraded_reference_policy, stale)

    return _mark_wrapper(_build_guidance, original, "_build_guidance")


def _wrap_prepare(original: Callable[..., Any]) -> Callable[..., Any]:
    def prepare(self: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
        # Scope outage/interrupt state to one turn so fan-out cache cannot replay across turns.
        self._true_moa_outage = None
        self._true_moa_interrupted = False
        if getattr(self, "_true_moa_cache_poisoned", False):
            self._ref_cache_key = None
            self._ref_cache_outputs = []
            self._true_moa_cache_poisoned = False
        try:
            prepared = original(self, messages)
        except InterruptedError:
            return {
                "messages": [dict(m) if isinstance(m, dict) else m for m in (messages or [])],
                "guidance": None,
                "_true_moa_interrupted": True,
            }
        except MoaReferenceOutage as exc:
            # Defer raising to create() so Hermes' retry-loop error handler finalises history cleanly.
            exc_ladder = exc.ladder
            outage = getattr(self, "_true_moa_outage", None) or {
                "labels": list(exc.labels),
                "attempts": exc.attempts,
                "waited_s": round(exc.waited, 1),
                "preset": str(getattr(self, "preset_name", "") or ""),
                "ladder": list(exc_ladder) if exc_ladder is not None else None,
                "budget_capped": bool(exc.budget_capped),
            }
            report.event(
                "turn_halted",
                preset=outage.get("preset", ""),
                failed=list(outage.get("labels") or []),
                attempts=outage.get("attempts", 0),
                waited_s=outage.get("waited_s", 0),
            )
            return {
                "messages": [dict(m) if isinstance(m, dict) else m for m in (messages or [])],
                "guidance": None,
                "_true_moa_outage": outage,
            }
        outage = getattr(self, "_true_moa_outage", None)
        if outage and isinstance(prepared, dict):
            prepared = dict(prepared)
            prepared["_true_moa_outage"] = outage
        return dict(prepared) if isinstance(prepared, dict) else {"messages": messages, "guidance": None}

    return _mark_wrapper(prepare, original, "prepare")


def _wrap_create(original: Callable[..., Any]) -> Callable[..., Any]:
    def create(self: Any, **api_kwargs: Any) -> Any:
        prepared = api_kwargs.get("_moa_prepared_request")
        if isinstance(prepared, dict):
            if prepared.get("_true_moa_interrupted"):
                raise InterruptedError("MoA reference retry interrupted by user")
            outage = prepared.get("_true_moa_outage")
            if isinstance(outage, dict) and outage:
                raw_ladder = outage.get("ladder")
                ladder = list(raw_ladder) if isinstance(raw_ladder, (list, tuple)) else None
                raise MoaReferenceOutage(
                    tuple(outage.get("labels") or ()),
                    int(outage.get("attempts") or 0),
                    float(outage.get("waited_s") or 0.0),
                    ladder=ladder,
                    budget_capped=bool(outage.get("budget_capped")),
                )
        return original(self, **api_kwargs)

    return _mark_wrapper(create, original, "create")


def _wrap_model_fallback(original: Callable[..., Any]) -> Callable[..., Any]:
    def _try_main_agent_model_fallback(
        failed_provider: str,
        task: str | None = None,
        reason: str = "error",
        failed_model: str | None = None,
        failed_base_url: str = "",
        failure_scope: Any = None,
    ) -> Any:
        config = state.load()
        if config["enabled"] and config["block_aggregator_fallback"] and task == "moa_reference":
            report.event(
                "aggregator_fallback_blocked",
                failed_provider=failed_provider,
                failed_model=failed_model or "",
                reason=reason,
            )
            logger.warning(
                "true-moa: refused main-agent fallback for moa_reference (%s/%s, %s) — "
                "the aggregator does not answer for a reference",
                failed_provider, failed_model or "?", reason,
            )
            return None, None, ""
        return original(
            failed_provider, task=task, reason=reason, failed_model=failed_model,
            failed_base_url=failed_base_url, failure_scope=failure_scope,
        )

    return _mark_wrapper(_try_main_agent_model_fallback, original, "_try_main_agent_model_fallback")


def install(force: bool = False) -> dict[str, Any]:
    """Verify the running core and apply the gate. Idempotent; never partial."""
    global _compat
    with _lock:
        if _native_mode_active:
            _compat = dict(
                _compat,
                ok=True,
                installed=True,
                mode="native_hooks",
                core_version=_core_version(),
                plugin_version=PLUGIN_VERSION,
            )
            return dict(_compat)
        if _patched and not force:
            return dict(_compat)
        if _patched and force:
            uninstall()

        compat: dict[str, Any] = {
            "ok": False,
            "installed": False,
            "mode": "in_process",
            "core_version": _core_version(),
            "plugin_version": PLUGIN_VERSION,
            "target_core": TARGET_CORE,
            "checked": [],
            "missing": [],
            "error": "",
        }

        try:
            moa_loop = importlib.import_module(_MOA_MODULE)
            aux_client = importlib.import_module(_AUX_MODULE)
            turn_api_req = importlib.import_module(_TURN_API_REQ_MODULE)
        except Exception as exc:
            compat["error"] = f"import failed: {exc}"
            _compat = compat
            report.event("gate_install_failed", error=compat["error"])
            logger.warning("true-moa: cannot import Hermes MoA internals (%s) — gate inactive", exc)
            return dict(compat)

        moa_class = getattr(moa_loop, "MoAChatCompletions", None)
        if moa_class is None:
            compat["missing"].append("agent.moa_loop.MoAChatCompletions")

        owners: dict[str, Any] = {
            "moa.prepare": moa_class,
            "moa.create": moa_class,
            "moa._run_fanout": moa_class,
            "moa._build_guidance": moa_class,
            "moa.aggregate_moa_context": moa_loop,
            "assembly.build_api_request": turn_api_req,
            "aux._try_main_agent_model_fallback": aux_client,
        }

        resolved: list[tuple[str, Any, str, Callable[..., Any]]] = []
        for key, (attribute, expected) in _TARGETS.items():
            owner = owners.get(key)
            compat["checked"].append(key)
            if owner is None:
                compat["missing"].append(key)
                continue
            target = getattr(owner, attribute, None)
            if target is None:
                compat["missing"].append(f"{key} (attribute absent)")
                continue
            problem = _signature_problem(target, expected)
            if problem:
                compat["missing"].append(f"{key} ({problem})")
                continue
            if getattr(target, "__true_moa_original__", None) is not None:
                compat["missing"].append(f"{key} (already wrapped)")
                continue
            resolved.append((key, owner, attribute, target))

        if compat["missing"]:
            compat["error"] = "core internals differ from the verified layout; nothing patched"
            _compat = compat
            report.event(
                "gate_install_failed",
                core_version=compat["core_version"],
                missing=compat["missing"],
            )
            logger.warning(
                "true-moa: incompatible core (%s) — gate stays off, MoA behaves as stock",
                "; ".join(compat["missing"]),
            )
            return dict(compat)

        wrappers = {
            "moa.prepare": _wrap_prepare,
            "moa.create": _wrap_create,
            "moa._run_fanout": _wrap_run_fanout,
            "moa._build_guidance": _wrap_build_guidance,
            "moa.aggregate_moa_context": _wrap_aggregate_moa_context,
            "assembly.build_api_request": _wrap_build_api_request,
            "aux._try_main_agent_model_fallback": _wrap_model_fallback,
        }

        applied: list[tuple[Any, str, Callable[..., Any]]] = []
        try:
            for key, owner, attribute, target in resolved:
                wrapper = wrappers[key](target)
                setattr(owner, attribute, wrapper)
                applied.append((owner, attribute, target))
        except BaseException as exc:
            for owner, attribute, target in reversed(applied):
                setattr(owner, attribute, target)
            compat["error"] = f"patch failed: {exc}"
            _compat = compat
            report.event("gate_install_failed", error=compat["error"])
            return dict(compat)

        _patched.extend(applied)
        compat["ok"] = True
        compat["installed"] = True
        compat["wrapped"] = [key for key, *_rest in resolved]
        _compat = compat
        report.event(
            "gate_installed",
            core_version=compat["core_version"],
            plugin_version=PLUGIN_VERSION,
            wrapped=compat["wrapped"],
            enabled=state.enabled(),
        )
        logger.info(
            "true-moa %s active on Hermes %s — %d seam(s) gated",
            PLUGIN_VERSION, compat["core_version"], len(applied),
        )
        return dict(compat)


def native_hooks_supported() -> bool:
    """True when the running Hermes core exposes PR #157214's official MoA hooks."""
    try:
        plugins_mod = importlib.import_module("hermes_cli.plugins")
        valid = set(getattr(plugins_mod, "VALID_HOOKS", ()))
        return {"moa_fanout_verdict", "auxiliary_fallback_policy"}.issubset(valid)
    except Exception:
        return False


def handle_auxiliary_fallback_policy(
    *,
    task: str | None = None,
    failed_provider: str = "",
    failed_model: str | None = None,
    reason: str = "error",
    **_kwargs: Any,
) -> bool | None:
    """Official ``auxiliary_fallback_policy`` hook handler (PR #157214)."""
    config = state.load()
    if config["enabled"] and config["block_aggregator_fallback"] and task == "moa_reference":
        report.event(
            "aggregator_fallback_blocked",
            failed_provider=failed_provider,
            failed_model=failed_model or "",
            reason=reason,
        )
        return False
    return None


def _verdict_session_key(preset_name: str | None, preset_config: dict[str, Any] | None, kwargs: dict[str, Any]) -> str:
    explicit = kwargs.get("turn_id") or kwargs.get("api_request_id") or kwargs.get("session_id")
    if explicit:
        return f"{preset_name or 'oneshot'}:{explicit}"
    tid = threading.get_ident()
    if preset_config is not None:
        return f"{preset_name or 'oneshot'}:t{tid}:cfg-{id(preset_config)}"
    return f"{preset_name or 'oneshot'}:t{tid}"


def handle_moa_fanout_verdict(
    *,
    preset_name: str | None = None,
    preset_config: dict[str, Any] | None = None,
    aggregator: dict[str, Any] | None = None,
    active_slots: list[dict[str, Any]] | None = None,
    reference_outputs: list[dict[str, Any]] | None = None,
    attempt: int = 1,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Official ``moa_fanout_verdict`` hook handler (PR #157214) backed by ``_FanoutDecisionState``."""
    config = state.load()
    if not config["enabled"]:
        return None
    outputs = list(reference_outputs or [])
    slots = [dict(s) for s in (active_slots or [])]
    if not outputs:
        return None

    slot_label_fn = lambda s: f"{s.get('provider')}:{s.get('model')}" if s.get("provider") else str(s.get("model"))  # noqa: E731
    preset_tag = str(preset_name or "oneshot")
    key = _verdict_session_key(preset_name, preset_config, kwargs)

    with _lock:
        if attempt <= 1 or key not in _verdict_turn_state:
            while len(_verdict_turn_state) >= _MAX_VERDICT_STATES:
                _verdict_turn_state.pop(next(iter(_verdict_turn_state)), None)
            seed_slots = slots if len(slots) >= len(outputs) else [
                slots[i] if i < len(slots) else {"provider": "unknown", "model": f"slot-{i}"}
                for i in range(len(outputs))
            ]
            _verdict_turn_state[key] = _FanoutDecisionState(
                config=config,
                initial_slots=seed_slots,
                aggregator=aggregator,
                preset_name=preset_tag,
                slot_label_fn=slot_label_fn,
            )
        decision: _FanoutDecisionState = _verdict_turn_state[key]

        # PR #157214 passes preserved_healthy + current batch_outputs; slice the current pending batch tail.
        pending_len = len(decision.pending_indices)
        current_slice = outputs[-pending_len:] if pending_len and len(outputs) >= pending_len else outputs
        batch_items = [
            (
                str(r.get("label") or "unknown"),
                str(r.get("text") or ""),
                r,
                bool(r.get("failed")) or str(r.get("text", "")).lstrip().lower().startswith(("[failed:", "[skipped:")),
            )
            for r in current_slice
        ]
        step = decision.evaluate_batch(batch_items, attempt_num=attempt)

        if step["action"] == "accept":
            _verdict_turn_state.pop(key, None)
            if step["quorum"]:
                report.event("quorum_accepted", preset=preset_tag, healthy=step["healthy"], total=decision.total_slots, failed=step["failed"])
            return {"action": "accept", "cache": bool(step["allow_cache"])}

        if step["action"] == "abort":
            _verdict_turn_state.pop(key, None)
            outage_exc = MoaReferenceOutage(
                tuple(step["failed"]),
                attempt,
                decision.waited_s,
                ladder=list(decision.ladder),
                budget_capped=bool(step["budget_capped"]),
            )
            report.event("reference_outage", preset=preset_tag, failed=step["failed"], attempts=attempt, waited_s=round(decision.waited_s, 1))
            return {"action": "abort", "message": str(outage_exc)}

        delay_s = float(step["delay_s"])
        decision.waited_s += delay_s
        report.event("reference_retry", preset=preset_tag, failed=step["failed"], attempt=attempt, delay_s=delay_s)
        return {"action": "retry", "slots": list(step["slots"]), "delay_s": delay_s, "merge_healthy": True}


def activate_native_hooks(ctx: Any) -> dict[str, Any]:
    """Bind official PR #157214 hooks without monkeypatching Hermes internals."""
    global _compat, _native_mode_active
    with _lock:
        if _patched:
            uninstall()
        ctx.register_hook("moa_fanout_verdict", handle_moa_fanout_verdict)
        ctx.register_hook("auxiliary_fallback_policy", handle_auxiliary_fallback_policy)
        _native_mode_active = True
        _compat = {
            "ok": True,
            "installed": True,
            "mode": "native_hooks",
            "core_version": _core_version(),
            "plugin_version": PLUGIN_VERSION,
            "target_core": TARGET_CORE,
            "checked": ["hook:moa_fanout_verdict", "hook:auxiliary_fallback_policy"],
            "wrapped": ["hook:moa_fanout_verdict", "hook:auxiliary_fallback_policy"],
            "missing": [],
            "error": "",
        }
        report.event(
            "gate_installed",
            core_version=_compat["core_version"],
            plugin_version=PLUGIN_VERSION,
            mode="native_hooks",
            wrapped=_compat["wrapped"],
            enabled=state.enabled(),
        )
        return dict(_compat)


def deactivate_native_hooks() -> None:
    global _native_mode_active, _compat
    with _lock:
        if _native_mode_active:
            _native_mode_active = False
            _compat = dict(_compat, installed=False)


def uninstall() -> dict[str, Any]:
    """Restore every wrapped attribute. Safe to call when nothing is patched."""
    global _compat
    with _lock:
        restored = 0
        for owner, attribute, target in reversed(_patched):
            try:
                current = getattr(owner, attribute, None)
                if getattr(current, "__true_moa_original__", None) is not None:
                    setattr(owner, attribute, target)
                    restored += 1
            except Exception as exc:
                logger.warning("true-moa: could not restore %s (%s)", attribute, exc)
        _patched.clear()
        _compat = dict(_compat, installed=bool(_native_mode_active))
        if restored:
            report.event("gate_removed", restored=restored)
        return {"restored": restored, "ok": True}


def status() -> dict[str, Any]:
    config = state.load()
    source, attempts = state.ladder_source(config)
    return {
        "installed": bool(_patched or _native_mode_active),
        "mode": "native_hooks" if _native_mode_active else "in_process",
        "enabled": bool(config["enabled"]),
        "ladder": list(state.effective_ladder(config)),
        "ladder_source": source,
        "ladder_attempts": attempts,
        "sync_hermes_retries": bool(config.get("sync_hermes_retries", False)),
        "custom_retries": int(config.get("custom_retries", 0)),
        "max_wait_budget_s": float(config.get("max_wait_budget_s") or 0.0),
        "budget_description": "Caps cumulative sleep between Reference retries only; does not include or cancel in-flight requests.",
        "enable_secondary_fallback": bool(config.get("enable_secondary_fallback", False)),
        "secondary_reference_model": str(config.get("secondary_reference_model", "")),
        "fallback_trigger_attempts": int(config.get("fallback_trigger_attempts", 2)),
        "min_healthy_references": int(config.get("min_healthy_references") or 0),
        "block_aggregator_fallback": bool(config["block_aggregator_fallback"]),
        "core_version": _compat.get("core_version") or _core_version(),
        "plugin_version": PLUGIN_VERSION,
        "target_core": TARGET_CORE,
        "compat_ok": bool(_compat.get("ok")),
        "compat_error": _compat.get("error", ""),
        "compat_missing": list(_compat.get("missing", [])),
        "wrapped": list(_compat.get("wrapped", [])),
    }
