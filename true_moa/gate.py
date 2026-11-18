"""The strict gate itself.

Hermes' Mixture of Agents is fail-open by design: a reference model that raises
becomes a ``[failed: ...]`` string, the aggregator is told to "act on your own
judgment", the failure is written into the fan-out cache, and the auxiliary
client may even answer the reference call with the aggregator's own model. Every
one of those paths is closed here, at the three seams that actually decide the
outcome:

1. ``MoAChatCompletions._run_fanout`` — the only place reference output is
   produced, cached, accounted and emitted. Wrapped: retry on the same ladder the
   aggregator gets, never cache a failure, raise when the ladder is exhausted.
2. ``MoAChatCompletions._build_guidance`` — the only place the aggregator prompt
   is built. Guarded: an all-failed fan-out can never reach it, including from a
   cache written before this plugin loaded.
3. ``auxiliary_client._try_main_agent_model_fallback`` — where a failed
   ``moa_reference`` task is silently served by the aggregator's own model.
   Refused outright.

Nothing here edits a Hermes file. The wrappers live in this process only, are
signature-verified against the running core before they are applied, and are
removed again on disable — a core whose internals moved is reported as
incompatible and left untouched rather than patched half-way.
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

PLUGIN_VERSION = "1.3.1"
TARGET_CORE = ">=0.23.0"

_MOA_MODULE = "agent.moa_loop"
_AUX_MODULE = "agent.auxiliary_client"
_SLICE = 0.25

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
    """Identity of the running core, read from its own install stamp.

    Deliberately file-based: importing ``hermes_cli`` internals from a plugin is a
    private-import (catalog rule 9) and would break on any release that reshuffles
    them. The stamp is a documented artifact and needs no import at all.
    """
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


def _sleep(seconds: float) -> bool:
    """Wait *seconds*, returning False the moment the gate is switched off."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if not state.enabled():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(_SLICE, remaining))


def _emit_progress(owner: Any, done: int, total: int, label: str) -> None:
    emit = getattr(owner, "_emit", None)
    if not callable(emit):
        return
    try:
        emit("moa.progress", refs_done=done, refs_total=total, label=label)
    except Exception:  # never let a progress event break recovery
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
        if not config["enabled"]:
            return original(self, preset, ref_messages, reference_models, aggregator,
                            aggregator_temperature, cache_key)
        if not reference_models:
            # A preset with no enabled references is legitimately aggregator-only.
            return original(self, preset, ref_messages, reference_models, aggregator,
                            aggregator_temperature, cache_key)

        moa_loop = importlib.import_module(_MOA_MODULE)
        is_failed = getattr(moa_loop, "_is_failed_reference")
        ladder = [float(item) for item in state.effective_ladder(config)]
        max_wait_budget = float(config.get("max_wait_budget_s") or 0.0)
        waited_for_retries = 0.0
        attempt = 0
        started = time.monotonic()

        enable_fallback = bool(config.get("enable_secondary_fallback"))
        secondary_spec = str(config.get("secondary_reference_model") or "").strip()
        fallback_trigger = int(config.get("fallback_trigger_attempts") or 2)
        secondary_model_entry = state.parse_reference_model_string(secondary_spec) if enable_fallback else None

        # Anti-loophole: secondary reference model must not match aggregator or recurse on moa
        if secondary_model_entry:
            sec_p = str(secondary_model_entry.get("provider") or "").lower()
            sec_m = str(secondary_model_entry.get("model") or "").lower()
            if sec_p == "moa":
                logger.warning("true-moa: secondary reference %s cannot use 'moa' provider — fallback disabled", secondary_spec)
                secondary_model_entry = None
            elif aggregator:
                agg_p = str(aggregator.get("provider") or "").lower()
                agg_m = str(aggregator.get("model") or "").lower()
                same_slot = (
                    _slot_identity(secondary_model_entry) == _slot_identity(aggregator)
                    if (sec_p and agg_p)
                    else sec_m == agg_m
                )
                if same_slot:
                    logger.warning(
                        "true-moa: secondary reference %s matches aggregator %s — fallback disabled to prevent aggregator impersonation",
                        secondary_spec, aggregator,
                    )
                    secondary_model_entry = None

        active_references = [dict(m) for m in reference_models]
        fallback_checked = False
        fallback_activated = False
        switched_from: list[str] = []

        slot_label_fn = getattr(moa_loop, "_slot_label", lambda s: f"{s.get('provider')}:{s.get('model')}" if s.get("provider") else str(s.get("model")))

        while True:
            outputs = original(self, preset, ref_messages, active_references, aggregator,
                               aggregator_temperature, cache_key)
            failed = [label for label, text, _acct in outputs if is_failed(text)]
            if not failed:
                self._true_moa_outage = None
                return outputs

            # Unconditional invariant: never cache a failed advisory across iterations.
            self._ref_cache_key = None
            self._ref_cache_outputs = []
            self._true_moa_cache_poisoned = True

            # Check if secondary reference fallback should activate for the next attempt
            if (enable_fallback and secondary_model_entry and not fallback_checked
                    and (attempt + 1) >= fallback_trigger):
                fallback_checked = True
                sec_key = _slot_identity(secondary_model_entry)
                failed_keys = {
                    _slot_identity(m)
                    for m in active_references if slot_label_fn(m) in failed
                }

                if sec_key in failed_keys:
                    logger.warning(
                        "true-moa: secondary reference %s matches failed primary — fallback skipped",
                        secondary_spec,
                    )
                else:
                    fallback_activated = True
                    switched_from = list(failed)
                    new_refs = []
                    seen_keys: set[tuple[str, str]] = set()

                    # Retain healthy references first
                    for m in active_references:
                        is_enabled = bool(m.get("enabled", True))
                        s_label = slot_label_fn(m)
                        if not (is_enabled and s_label in failed):
                            new_refs.append(dict(m))
                            if is_enabled:
                                seen_keys.add(_slot_identity(m))

                    # Add secondary reference model once if at least one reference failed
                    if sec_key not in seen_keys:
                        new_refs.append(dict(secondary_model_entry))

                    active_references = new_refs
                    sec_label = slot_label_fn(secondary_model_entry)
                    report.event(
                        "reference_fallback_switched",
                        preset=getattr(self, "preset_name", ""),
                        primary=list(failed),
                        secondary=sec_label,
                        attempt=attempt + 1,
                    )
                    logger.warning(
                        "true-moa: reference %s failed %d attempt(s) — switched to secondary reference %s",
                        ", ".join(failed), attempt + 1, sec_label,
                    )

            waited_so_far = waited_for_retries
            budget_exhausted = max_wait_budget > 0.0 and waited_so_far >= max_wait_budget
            if attempt >= len(ladder) or budget_exhausted:
                waited = time.monotonic() - started
                self._true_moa_outage = {
                    "labels": list(failed),
                    "attempts": attempt + 1,
                    "waited_s": round(waited, 1),
                    "preset": str(getattr(self, "preset_name", "") or ""),
                    "budget_capped": bool(budget_exhausted and attempt < len(ladder)),
                    "ladder": list(ladder),
                    "fallback_activated": fallback_activated,
                    "switched_from": switched_from,
                }
                report.event(
                    "reference_outage",
                    preset=getattr(self, "preset_name", ""),
                    failed=failed,
                    attempts=attempt + 1,
                    waited_s=round(waited, 1),
                    budget_capped=bool(budget_exhausted and attempt < len(ladder)),
                )
                logger.error(
                    "true-moa: reference outage holds the turn — %s failed %d attempt(s) over %.0fs%s",
                    ", ".join(failed), attempt + 1, waited,
                    " (retry-sleep budget reached)" if budget_exhausted and attempt < len(ladder) else "",
                )
                raise MoaReferenceOutage(
                    tuple(failed),
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
                preset=getattr(self, "preset_name", ""),
                failed=failed,
                attempt=attempt,
                delay_s=delay,
            )
            logger.warning(
                "true-moa: reference %s failed — retry %d/%d in %.0fs (aggregator held)",
                ", ".join(failed), attempt, len(ladder), delay,
            )
            _emit_progress(self, len(outputs) - len(failed), len(outputs),
                           f"retry {attempt}/{len(ladder)} in {delay:g}s")
            sleep_started = time.monotonic()
            slept = _sleep(delay)
            waited_for_retries += max(0.0, time.monotonic() - sleep_started)
            if not slept:
                # Switched off mid-ladder: stop enforcing, hand back what core produced.
                logger.warning("true-moa: gate disabled during reference retry — standing down")
                report.event("gate_stood_down", preset=getattr(self, "preset_name", ""))
                return outputs

    _run_fanout.__name__ = getattr(original, "__name__", "_run_fanout")
    _run_fanout.__doc__ = getattr(original, "__doc__", None)
    _run_fanout.__true_moa_original__ = original
    return _run_fanout


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
        # Scope outage state to one turn so fan-out cache cannot replay across turns.
        self._true_moa_outage = None
        if getattr(self, "_true_moa_cache_poisoned", False):
            self._ref_cache_key = None
            self._ref_cache_outputs = []
            self._true_moa_cache_poisoned = False
        try:
            prepared = original(self, messages)
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
