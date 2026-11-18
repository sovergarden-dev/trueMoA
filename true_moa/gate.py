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

from . import report, state

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "1.4.0"
TARGET_CORE = ">=0.23.0"

_MOA_MODULE = "agent.moa_loop"
_AUX_MODULE = "agent.auxiliary_client"
_TURN_API_REQ_MODULE = "agent.turn_api_request"
_SLICE = 0.1
_INTERRUPTED_SENTINEL = "[skipped: interrupted by user]"

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
_compat: dict[str, Any] = {
    "ok": False,
    "installed": False,
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
        names = ", ".join(labels) if labels else "unknown"
        ladder_desc = (
            f" (True MoA ladder={ladder} exhausted)"
            if ladder and not budget_capped
            else " (True MoA sleep budget reached)"
            if budget_capped
            else ""
        )
        super().__init__(
            f"MoA reference outage: {names} failed {attempts} attempt(s) over {waited:.0f}s{ladder_desc}. "
            "Turn halted before the aggregator acted; no reference result was cached."
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


def _is_interrupted(agent: Any, outputs: list[tuple[str, str, Any]] | None = None) -> bool:
    if agent is not None and bool(getattr(agent, "_interrupt_requested", False)):
        return True
    if outputs:
        return any(str(text).strip() == _INTERRUPTED_SENTINEL for _lbl, text, _acct in outputs)
    return False


def _sleep(seconds: float, agent: Any = None) -> str:
    """Wait *seconds*, waking immediately on gate disable or user interrupt."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if not state.enabled():
            return "disabled"
        if agent is not None and bool(getattr(agent, "_interrupt_requested", False)):
            return "interrupted"
        remaining = deadline - time.monotonic()
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
) -> None:
    """Keep cache, trace and reference metrics coherent after selective slot retries."""
    facade._ref_cache_key = cache_key
    facade._ref_cache_outputs = list(merged_outputs)
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
) -> tuple[list[tuple[str, str, Any]], int]:
    """Shared selective-retry, secondary-cascade and interruptible-sleep engine."""
    is_failed = getattr(moa_loop, "_is_failed_reference")
    slot_label_fn = getattr(
        moa_loop, "_slot_label",
        lambda s: f"{s.get('provider')}:{s.get('model')}" if s.get("provider") else str(s.get("model")),
    )
    ladder = [float(item) for item in state.effective_ladder(config)]
    max_wait_budget = float(config.get("max_wait_budget_s") or 0.0)
    waited_for_retries = 0.0
    attempt = 0
    started = time.monotonic()

    enable_fallback = bool(config.get("enable_secondary_fallback"))
    fallback_trigger = int(config.get("fallback_trigger_attempts") or 2)
    secondary_chain = (
        _valid_secondary_chain(config.get("secondary_reference_model"), aggregator)
        if enable_fallback
        else []
    )
    secondary_idx = 0
    next_switch_attempt = fallback_trigger
    fallback_activated = False
    switched_from: list[str] = []

    slots: list[dict[str, Any]] = [dict(m) for m in initial_slots]
    resolved: list[tuple[str, str, Any] | None] = [None] * len(slots)
    pending_indices: list[int] = list(range(len(slots)))
    failed_identities: set[tuple[str, str]] = set()

    while True:
        if _is_interrupted(agent):
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt + 1)
            raise InterruptedError("MoA reference fan-out interrupted by user")

        batch_slots = [slots[i] for i in pending_indices]
        batch_outputs = run_batch(batch_slots)

        if _is_interrupted(agent, batch_outputs):
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt + 1)
            raise InterruptedError("MoA reference fan-out interrupted by user")

        still_pending: list[int] = []
        failed_labels: list[str] = []
        for pos, slot_idx in enumerate(pending_indices):
            if pos >= len(batch_outputs):
                still_pending.append(slot_idx)
                failed_labels.append(slot_label_fn(slots[slot_idx]))
                failed_identities.add(_slot_identity(slots[slot_idx]))
                continue
            out = batch_outputs[pos]
            if is_failed(out[1]):
                still_pending.append(slot_idx)
                failed_labels.append(out[0])
                failed_identities.add(_slot_identity(slots[slot_idx]))
            else:
                resolved[slot_idx] = out

        if not still_pending:
            final_outputs = [r for r in resolved if r is not None]
            return final_outputs, attempt

        if on_failure_purge is not None:
            on_failure_purge()

        waited_so_far = waited_for_retries
        budget_exhausted = max_wait_budget > 0.0 and waited_so_far >= max_wait_budget
        has_next_attempt = (attempt < len(ladder)) and not budget_exhausted

        # Secondary cascade: only switch when at least one retry attempt remains to execute it.
        if (enable_fallback and secondary_idx < len(secondary_chain)
                and has_next_attempt and (attempt + 1) >= next_switch_attempt):
            chosen: dict[str, Any] | None = None
            while secondary_idx < len(secondary_chain):
                cand = secondary_chain[secondary_idx]
                secondary_idx += 1
                if _slot_identity(cand) in failed_identities:
                    logger.warning(
                        "true-moa: secondary reference %s:%s matches a failed slot — skipping candidate",
                        cand.get("provider"), cand.get("model"),
                    )
                    continue
                chosen = cand
                break

            if chosen is not None:
                fallback_activated = True
                if not switched_from:
                    switched_from = list(failed_labels)
                next_switch_attempt = (attempt + 1) + fallback_trigger
                healthy_keys = {
                    _slot_identity(slots[i])
                    for i in range(len(slots))
                    if resolved[i] is not None and i not in still_pending
                }
                chosen_key = _slot_identity(chosen)
                sec_label = slot_label_fn(chosen)
                report.event(
                    "reference_fallback_switched",
                    preset=preset_name,
                    primary=list(failed_labels),
                    secondary=sec_label,
                    attempt=attempt + 1,
                )
                logger.warning(
                    "true-moa: reference %s failed %d attempt(s) — switched to secondary reference %s",
                    ", ".join(failed_labels), attempt + 1, sec_label,
                )
                if chosen_key in healthy_keys:
                    still_pending = []
                else:
                    lead_idx = still_pending[0]
                    slots[lead_idx] = dict(chosen)
                    still_pending = [lead_idx]
            else:
                next_switch_attempt = (attempt + 1) + fallback_trigger

        pending_indices = still_pending
        if not pending_indices:
            return [r for r in resolved if r is not None], attempt

        if not has_next_attempt:
            waited = time.monotonic() - started
            outage = {
                "labels": list(failed_labels),
                "attempts": attempt + 1,
                "waited_s": round(waited, 1),
                "preset": preset_name,
                "budget_capped": bool(budget_exhausted and attempt < len(ladder)),
                "ladder": list(ladder),
                "fallback_activated": fallback_activated,
                "switched_from": switched_from,
            }
            if on_outage_record is not None:
                on_outage_record(outage)
            report.event(
                "reference_outage",
                preset=preset_name,
                failed=failed_labels,
                attempts=attempt + 1,
                waited_s=round(waited, 1),
                budget_capped=bool(budget_exhausted and attempt < len(ladder)),
            )
            logger.error(
                "true-moa: reference outage holds the turn — %s failed %d attempt(s) over %.0fs%s",
                ", ".join(failed_labels), attempt + 1, waited,
                " (retry-sleep budget reached)" if budget_exhausted and attempt < len(ladder) else "",
            )
            raise MoaReferenceOutage(
                tuple(failed_labels),
                attempt + 1,
                waited,
                ladder=list(ladder),
                budget_capped=bool(budget_exhausted and attempt < len(ladder)),
            )

        delay = ladder[attempt]
        if max_wait_budget > 0.0:
            delay = min(delay, max(0.0, max_wait_budget - waited_so_far))
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
            ", ".join(failed_labels), attempt, len(ladder), delay,
        )
        done_count = sum(1 for r in resolved if r is not None)
        _emit_progress(progress_owner, done_count, len(slots), f"retry {attempt}/{len(ladder)} in {delay:g}s")
        sleep_started = time.monotonic()
        sleep_state = _sleep(delay, agent=agent)
        waited_for_retries += max(0.0, time.monotonic() - sleep_started)
        if sleep_state == "disabled":
            logger.warning("true-moa: gate disabled during reference retry — standing down")
            report.event("gate_stood_down", preset=preset_name)
            fallback_by_pos = dict(zip(pending_indices, batch_outputs, strict=False))
            return [resolved[i] or fallback_by_pos.get(i) for i in range(len(slots)) if (resolved[i] or fallback_by_pos.get(i)) is not None], attempt
        if sleep_state == "interrupted":
            if on_failure_purge is not None:
                on_failure_purge()
            report.event("gate_interrupted", preset=preset_name, attempt=attempt)
            raise InterruptedError("MoA reference retry interrupted by user")


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

        moa_loop = importlib.import_module(_MOA_MODULE)

        def _purge_cache() -> None:
            self._ref_cache_key = None
            self._ref_cache_outputs = []
            self._true_moa_cache_poisoned = True

        def _record_outage(outage: dict[str, Any]) -> None:
            self._true_moa_outage = outage

        final_outputs, attempts_used = _run_gated_fanout(
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
        self._true_moa_outage = None
        if attempts_used > 0 or len(final_outputs) != len(getattr(self, "_ref_cache_outputs", []) or []):
            _sync_facade_merged_outputs(self, moa_loop, cache_key, final_outputs)
        return final_outputs

    _run_fanout.__name__ = getattr(original, "__name__", "_run_fanout")
    _run_fanout.__doc__ = getattr(original, "__doc__", None)
    _run_fanout.__true_moa_original__ = original
    return _run_fanout


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
            return original(
                user_prompt=user_prompt, api_messages=api_messages, reference_models=reference_models,
                aggregator=aggregator, temperature=temperature, aggregator_temperature=aggregator_temperature,
                reference_max_tokens=reference_max_tokens, reference_timeout=reference_timeout,
                degraded_reference_policy=degraded_reference_policy, agent=agent,
            )

        moa_loop = importlib.import_module(_MOA_MODULE)
        ref_msgs = moa_loop._reference_messages(api_messages)

        def _record_oneshot_outage(outage: dict[str, Any]) -> None:
            if agent is not None:
                agent._true_moa_oneshot_outage = outage

        try:
            final_outputs, _attempts = _run_gated_fanout(
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

        privacy_full = False
        try:
            from hermes_cli.config import load_config as _load_config
            privacy_full = moa_loop._moa_privacy_mode((_load_config() or {}).get("moa")) == "full"
        except Exception:
            pass
        agg_refs, degraded, all_failed = moa_loop._guidance_inputs(
            final_outputs, privacy_full, degraded_reference_policy
        )
        if all_failed:
            outage = {"labels": [lbl for lbl, _t, _a in final_outputs], "attempts": 1, "waited_s": 0.0, "preset": "oneshot"}
            _record_oneshot_outage(outage)
            raise MoaReferenceOutage(tuple(outage["labels"]), 1, 0.0)

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

    aggregate_moa_context.__name__ = getattr(original, "__name__", "aggregate_moa_context")
    aggregate_moa_context.__doc__ = getattr(original, "__doc__", None)
    aggregate_moa_context.__true_moa_original__ = original
    return aggregate_moa_context


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
                ladder = list(outage.get("ladder") or []) if outage.get("ladder") is not None else None
                raise MoaReferenceOutage(
                    tuple(outage.get("labels") or ()),
                    int(outage.get("attempts") or 0),
                    float(outage.get("waited_s") or 0.0),
                    ladder=ladder,
                    budget_capped=bool(outage.get("budget_capped")),
                )
        return original(agent, **kwargs)

    build_api_request.__name__ = getattr(original, "__name__", "build_api_request")
    build_api_request.__doc__ = getattr(original, "__doc__", None)
    build_api_request.__true_moa_original__ = original
    return build_api_request


def _wrap_build_guidance(original: Callable[..., Any]) -> Callable[..., Any]:
    def _build_guidance(
        self: Any,
        reference_outputs: list[tuple[str, str, Any]],
        aggregator: dict[str, Any],
        degraded_reference_policy: str,
        stale: bool = False,
    ) -> Any:
        if state.enabled() and reference_outputs:
            moa_loop = importlib.import_module(_MOA_MODULE)
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

    _build_guidance.__name__ = getattr(original, "__name__", "_build_guidance")
    _build_guidance.__doc__ = getattr(original, "__doc__", None)
    _build_guidance.__true_moa_original__ = original
    return _build_guidance


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
            outage = getattr(self, "_true_moa_outage", None) or {
                "labels": list(exc.labels),
                "attempts": exc.attempts,
                "waited_s": round(exc.waited, 1),
                "preset": str(getattr(self, "preset_name", "") or ""),
                "ladder": list(exc.ladder) if getattr(exc, "ladder", None) else None,
                "budget_capped": bool(getattr(exc, "budget_capped", False)),
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
        return prepared

    prepare.__name__ = getattr(original, "__name__", "prepare")
    prepare.__doc__ = getattr(original, "__doc__", None)
    prepare.__true_moa_original__ = original
    return prepare


def _wrap_create(original: Callable[..., Any]) -> Callable[..., Any]:
    def create(self: Any, **api_kwargs: Any) -> Any:
        prepared = api_kwargs.get("_moa_prepared_request")
        if isinstance(prepared, dict):
            if prepared.get("_true_moa_interrupted"):
                raise InterruptedError("MoA reference retry interrupted by user")
            outage = prepared.get("_true_moa_outage")
            if outage:
                ladder = list(outage.get("ladder") or []) if outage.get("ladder") is not None else None
                raise MoaReferenceOutage(
                    tuple(outage.get("labels") or ()),
                    int(outage.get("attempts") or 0),
                    float(outage.get("waited_s") or 0.0),
                    ladder=ladder,
                    budget_capped=bool(outage.get("budget_capped")),
                )
        return original(self, **api_kwargs)

    create.__name__ = getattr(original, "__name__", "create")
    create.__doc__ = getattr(original, "__doc__", None)
    create.__true_moa_original__ = original
    return create


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

    _try_main_agent_model_fallback.__name__ = getattr(
        original, "__name__", "_try_main_agent_model_fallback"
    )
    _try_main_agent_model_fallback.__doc__ = getattr(original, "__doc__", None)
    _try_main_agent_model_fallback.__true_moa_original__ = original
    return _try_main_agent_model_fallback


def install(force: bool = False) -> dict[str, Any]:
    """Verify the running core and apply the gate. Idempotent; never partial."""
    global _compat
    with _lock:
        if _patched and not force:
            return dict(_compat)
        if _patched and force:
            uninstall()

        compat: dict[str, Any] = {
            "ok": False,
            "installed": False,
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
        _compat = dict(_compat, installed=False)
        if restored:
            report.event("gate_removed", restored=restored)
        return {"restored": restored, "ok": True}


def status() -> dict[str, Any]:
    config = state.load()
    source, attempts = state.ladder_source(config)
    return {
        "installed": bool(_patched),
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
        "block_aggregator_fallback": bool(config["block_aggregator_fallback"]),
        "core_version": _compat.get("core_version") or _core_version(),
        "plugin_version": PLUGIN_VERSION,
        "target_core": TARGET_CORE,
        "compat_ok": bool(_compat.get("ok")),
        "compat_error": _compat.get("error", ""),
        "compat_missing": list(_compat.get("missing", [])),
        "wrapped": list(_compat.get("wrapped", [])),
    }
