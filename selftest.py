"""Offline acceptance probe for the True MoA gate.

Runs against the *live* Hermes core with the reference model replaced by a
controlled outage — no network, no provider calls, no real turn. Every assertion
below is a recovery guarantee the gate is supposed to provide:

  A. the gate verifies and wraps the exact seams it depends on;
  B. a failing reference is retried on the ladder before anything else happens;
  C. when the ladder is exhausted the fan-out raises instead of returning
     `[failed: ...]`, so the aggregator never acts on a dead turn;
  D. the failure is never written into `_ref_cache_outputs`;
  E. a later turn receives a fresh ladder and cannot inherit an outage flag;
  F. `moa_reference` can no longer be answered by the main/aggregator model;
  G. a healthy fan-out passes through untouched and *is* cached;
  H. a disarmed gate restores stock behaviour exactly;
  I. uninstall restores every original Hermes attribute cleanly;
  J. plugin manifest, dynamic retry ladders, and telemetry sandbox isolation;
  K. secondary reference fallback activates on failure threshold and preserves fail-closed;
  Z. live config and state files are strictly unmodified (hard trap + SHA-256 verification).

Run:
    <hermes-env>/venv/Scripts/python.exe true-moa/selftest.py
Exit code 0 = every guarantee held.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    import hermes_yaml as yaml  # type: ignore[import-untyped,no-redef]

PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from true_moa import cli, doctor, gate, optimize, report, slash, state

FAILURES: list[str] = []
CHECKS: list[str] = []


class _DummyCtx:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get_config(self, key, default=None):
        return self.store.get(key, default)

    def set_config(self, key, value):
        self.store[key] = value


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append(f"{'PASS' if condition else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(name)


def main() -> int:
    hermes_cfg = importlib.import_module("hermes_cli.config")
    default_home = state.resolve_hermes_home() if hasattr(state, "resolve_hermes_home") else (
        Path.home() / (".hermes" if os.name != "nt" else Path("AppData") / "Local" / "hermes")
    )
    hermes_home = Path(os.environ.get("HERMES_HOME", default_home))
    config_file_path = hermes_home / "config.yaml"
    live_state_file = hermes_home / "plugin-data" / "true-moa" / "state.json"
    raw_cfg_before = config_file_path.read_bytes() if config_file_path.exists() else None
    raw_state_before = live_state_file.read_bytes() if live_state_file.exists() else None
    sha_cfg_before = hashlib.sha256(raw_cfg_before).hexdigest() if raw_cfg_before is not None else None
    sha_state_before = hashlib.sha256(raw_state_before).hexdigest() if raw_state_before is not None else None

    # Hard isolation: prevent any accidental call to save_config during testing
    orig_save_config = getattr(hermes_cfg, "save_config", None)
    if orig_save_config is None:
        raise RuntimeError("hermes_cli.config.save_config must be resolvable to trap it")
    trap_hits = {"n": 0}
    def _trapped_save_config(*args, **kwargs):
        trap_hits["n"] += 1
        raise RuntimeError("SECURITY VIOLATION: hermes_cli.config.save_config called during selftest!")
    hermes_cfg.save_config = _trapped_save_config

    hermes_plugins = importlib.import_module("hermes_cli.plugins")
    orig_get_verdict = getattr(hermes_plugins, "get_moa_fanout_verdict", None)
    orig_allow_aux = getattr(hermes_plugins, "allow_auxiliary_main_agent_fallback", None)
    if callable(orig_get_verdict):
        hermes_plugins.get_moa_fanout_verdict = lambda *_a, **_kw: None
    if callable(orig_allow_aux):
        hermes_plugins.allow_auxiliary_main_agent_fallback = lambda *_a, **_kw: True

    orig_state_path = state.STATE_PATH
    orig_report_dir = report.REPORT_DIR
    temp_report_dir = Path(tempfile.mkdtemp(prefix="true-moa-test-reports-"))
    report.REPORT_DIR = temp_report_dir
    state.STATE_PATH = temp_report_dir / "state.json"
    original_state = state.load()
    test_ctx = _DummyCtx(dict(original_state))
    state.bind_context(test_ctx)

    state.save({"enabled": True, "sync_hermes_retries": False, "custom_retries": 0,
                "ladder": [0.2, 0.2],
                "block_aggregator_fallback": True, "max_wait_budget_s": 0.0})

    try:
        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest.get("config_schema") or {}
        expected_fields = {
            "language", "enabled", "block_aggregator_fallback", "max_wait_budget_s", "ladder",
            "sync_hermes_retries", "custom_retries", "enable_secondary_fallback",
            "secondary_reference_model", "fallback_trigger_attempts", "min_healthy_references",
            "adaptive_fanout", "adaptive_max_interval", "adaptive_cache_keepalive_s",
            "chunked_context_trim", "concise_advisory", "guard_empty_responses", "fallback_sticky_s",
        }
        check("J1 manifest version matches gate PLUGIN_VERSION",
              manifest.get("version") == gate.PLUGIN_VERSION,
              f"{manifest.get('version')} vs {gate.PLUGIN_VERSION}")
        check("J2 Settings schema exposes all eighteen fields including smart optimization",
              set(schema) == expected_fields and schema.get("language", {}).get("choices") == ["en", "vi"],
              str(sorted(schema)))
        check("J3 gate, fallback, sync and optimization settings are boolean toggles",
              all(schema.get(key, {}).get("type") == "boolean"
                  for key in ("enabled", "block_aggregator_fallback", "sync_hermes_retries",
                              "enable_secondary_fallback", "adaptive_fanout", "chunked_context_trim",
                              "concise_advisory", "guard_empty_responses")))
        check("J4 retry ladder schema preserves six total attempts default",
              schema.get("ladder", {}).get("type") == "list"
              and schema.get("ladder", {}).get("default") == [15, 30, 60, 60, 60])
        check("J4b custom retries schema is non-negative number defaulting to 0",
              schema.get("custom_retries", {}).get("type") == "number"
              and schema.get("custom_retries", {}).get("default") == 0)
        check("J4c effective_ladder respects custom attempts",
              len(state.effective_ladder({"custom_retries": 5})) == 4
              and state.effective_ladder({"custom_retries": 5}) == [10.0, 15.0, 20.0, 30.0])
        check("J4d effective_ladder respects sync_hermes_retries",
              len(state.effective_ladder({"sync_hermes_retries": True})) == 12)
        check("J4e secondary fallback schema is defined with valid defaults",
              schema.get("secondary_reference_model", {}).get("type") == "string"
              and schema.get("fallback_trigger_attempts", {}).get("default") == 2)
        budget = schema.get("max_wait_budget_s", {})
        budget_description = str(budget.get("description", "")).lower()
        budget_label = str(budget.get("label", "")).lower()
        check("J5 retry sleep budget defaults to uncapped sleep-only semantics",
              budget.get("type") == "number" and budget.get("default") == 0
              and "0" in budget_description and "in-flight" in budget_description
              and "sleep" in budget_label and "rto" not in budget_label)

        # -- A: compatibility + patch -------------------------------------------------
        result = gate.install(force=True)
        check("A1 gate verified against live core", bool(result.get("ok")),
              result.get("error", "") or f"core {result.get('core_version')}")
        check("A2 every seam wrapped", len(result.get("wrapped", [])) == 9,
              str(result.get("wrapped")))

        aux_client = importlib.import_module("agent.auxiliary_client")
        moa_loop = importlib.import_module("agent.moa_loop")
        turn_assembly = importlib.import_module("agent.turn_request_assembly")
        turn_api_req = importlib.import_module("agent.turn_api_request")
        conv_loop = importlib.import_module("agent.conversation_loop")
        seam_targets = [
            ("moa.prepare", moa_loop.MoAChatCompletions.prepare),
            ("moa.create", moa_loop.MoAChatCompletions.create),
            ("moa._run_fanout", moa_loop.MoAChatCompletions._run_fanout),
            ("moa._build_guidance", moa_loop.MoAChatCompletions._build_guidance),
            ("moa._fanout_cache_key", moa_loop.MoAChatCompletions._fanout_cache_key),
            ("moa._trim_messages_for_reference", moa_loop._trim_messages_for_reference),
            ("moa.aggregate_moa_context", moa_loop.aggregate_moa_context),
            ("assembly.build_api_request", turn_api_req.build_api_request),
            ("aux._try_main_agent_model_fallback", aux_client._try_main_agent_model_fallback),
        ]
        sig_mismatches = []
        for seam_name, seam_fn in seam_targets:
            orig_fn = getattr(seam_fn, "__true_moa_original__", None)
            if getattr(seam_fn, "__wrapped__", None) is not orig_fn or orig_fn is None or inspect.signature(seam_fn) != inspect.signature(orig_fn):
                sig_mismatches.append(seam_name)
        check("A2b every wrapped seam preserves __wrapped__ and exact original signature",
              not sig_mismatches,
              f"mismatches={sig_mismatches}")
        orig_run_references_parallel = moa_loop._run_references_parallel

        preset_name = (os.environ.get("TRUE_MOA_TEST_PRESET") or "").strip()
        preset = None
        _raw = None
        if preset_name:
            preset, _raw = moa_loop._resolve_preset_cached(preset_name)
        else:
            moa_cfg = (hermes_cfg.load_config() or {}).get("moa") or {}
            candidates = []
            configured_default = str(moa_cfg.get("default_preset") or "").strip()
            if configured_default:
                candidates.append(configured_default)
            candidates.extend(
                name for name in (moa_cfg.get("presets") or {}) if name != configured_default
            )
            for candidate in candidates:
                try:
                    candidate_preset, candidate_raw = moa_loop._resolve_preset_cached(candidate)
                except Exception:
                    continue
                if any(s.get("enabled", True) for s in (candidate_preset.get("reference_models") or [])):
                    preset_name, preset, _raw = candidate, candidate_preset, candidate_raw
                    break
            if preset is None:
                raise SystemExit(
                    "selftest: no MoA preset carrying an enabled reference model was found; "
                    "set TRUE_MOA_TEST_PRESET or configure moa.presets."
                )
        refs = [s for s in (preset.get("reference_models") or []) if s.get("enabled", True)]
        check(f"A3 preset '{preset_name}' carries a reference model", bool(refs),
              ", ".join(moa_loop._slot_label(s) for s in refs))
        first_slot_model = str((refs[0].get("model") if refs else "") or "")

        # -- B/C/D: controlled outage, full ladder -------------------------------------
        calls = {"n": 0}

        def _boom(**_kwargs):
            calls["n"] += 1
            raise ConnectionError("controlled reference outage")

        moa_loop.call_llm = _boom
        client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
        messages = [{"role": "user", "content": "recovery probe"}]

        started = time.monotonic()
        prepared = client.prepare(messages)
        elapsed = time.monotonic() - started
        flag = prepared.get("_true_moa_outage") if isinstance(prepared, dict) else None

        check("B1 outage carried out of the fan-out", bool(flag), str(flag))
        check("B2 ladder retried before halting", calls["n"] == 3, f"{calls['n']} reference call(s)")
        check("B3 ladder waited between attempts", elapsed >= 0.35, f"{elapsed:.2f}s elapsed")
        named = " ".join(flag.get("labels") or ()) if flag else ""
        check("C1 outage names the failed slot",
              bool(named) and (not first_slot_model or first_slot_model.split("/")[-1] in named),
              str(flag.get("labels") if flag else None))

        # create() raises inside the retry loop so Hermes finalises the turn cleanly.
        halt = None
        try:
            client.create(**{"_moa_prepared_request": prepared, "messages": messages})
        except gate.MoaReferenceOutage as exc:
            halt = exc
        check("C2 aggregator call refused on outage", halt is not None,
              str(halt) if halt else "create() proceeded to the aggregator")
        check("C3 outage message explains True MoA retry ladder",
              halt is not None and "True MoA ladder=" in str(halt),
              str(halt) if halt else "no outage raised")
        cached = getattr(client, "_ref_cache_outputs", None)
        check("D1 failed advisory never cached", not cached, repr(cached)[:80])
        check("D2 cache key cleared", getattr(client, "_ref_cache_key", None) is None)
        check("D3 failure cache protection is unconditional",
              not getattr(client, "_ref_cache_outputs", None),
              "unconditional failure-cache purge held")

        # Next user turn must get a fresh ladder, never inherit a prior failure latch.
        before = calls["n"]
        started = time.monotonic()
        next_turn = client.prepare(messages)
        next_elapsed = time.monotonic() - started
        next_flag = next_turn.get("_true_moa_outage") if isinstance(next_turn, dict) else None
        check("E1 next turn gets a fresh recovery ladder",
              bool(next_flag) and calls["n"] - before == 3 and next_elapsed >= 0.35,
              f"{calls['n'] - before} calls, {next_elapsed:.2f}s")

        # A healthy turn on the SAME facade clears the previous turn's outage flag.
        moa_loop._run_references_parallel = lambda *a, **k: [("probe/reference", "advisor ok", None)]
        recovered = client.prepare(messages)
        check("E2 next healthy turn clears stale outage state",
              isinstance(recovered, dict) and not recovered.get("_true_moa_outage"))

        # -- F: no aggregator impersonation -------------------------------------------
        blocked = aux_client._try_main_agent_model_fallback("beat", task="moa_reference", reason="rate_limit")
        check("F1 moa_reference fallback refused", blocked == (None, None, ""), repr(blocked))
        passthrough = aux_client._try_main_agent_model_fallback("beat", task="titling", reason="rate_limit")
        check("F2 unrelated auxiliary fallback untouched",
              isinstance(passthrough, tuple) and len(passthrough) == 3)

        # -- G: healthy fan-out is untouched and cached -------------------------------
        healthy = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
        prepared = healthy.prepare(messages)
        check("G1 healthy fan-out returns an aggregator request",
              isinstance(prepared, dict) and bool(prepared.get("messages")))
        check("G2 healthy fan-out is cached for the next iteration",
              bool(getattr(healthy, "_ref_cache_outputs", None)))

        # -- H: max_wait_budget_s caps long ladder waits ------------------------------
        moa_loop._run_references_parallel = orig_run_references_parallel
        state.save({"enabled": True, "ladder": [1.0, 1.0, 1.0], "max_wait_budget_s": 0.25})
        calls["n"] = 0
        budget_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
        budget_prepared = budget_client.prepare(messages)
        budget_flag = budget_prepared.get("_true_moa_outage") if isinstance(budget_prepared, dict) else None
        check("H0 max_wait_budget_s stops ladder early when budget exhausted",
              bool(budget_flag) and budget_flag.get("budget_capped") is True and calls["n"] < 4,
              str(budget_flag))

        # -- H1: Settings > Plugins (PluginContext) synchronization -------------------
        seed_ctx = _DummyCtx({"enabled": original_state["enabled"]})
        state.bind_context(seed_ctx)
        seeded_value = state.load()
        check("H1 first bind seeds existing state into Settings > Plugins",
              seed_ctx.store.get("enabled") is seeded_value["enabled"] and set(seed_ctx.store) == set(state.DEFAULTS),
              str(seed_ctx.store))
        state.bind_context(None)

        dummy_ctx = _DummyCtx()
        dummy_ctx.store["enabled"] = False
        state.bind_context(dummy_ctx)
        settings_readback = state.load()
        check("H2 Settings > Plugins wins over stale state.json",
              settings_readback["enabled"] is False,
              f"ctx={dummy_ctx.store!r}, resolved={settings_readback!r}")
        dummy_ctx.store["enabled"] = True
        check("H2b UI changes are visible to the gate without a reload", state.enabled())
        state.save({"enabled": False, "max_wait_budget_s": 0.0})
        check("H3 state.save mirrors changes to Settings > Plugins config",
              dummy_ctx.store.get("enabled") is False, str(dummy_ctx.store))

        # -- H4: disarmed gate restores stock behaviour -------------------------------
        calls["n"] = 0
        stock = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
        try:
            stock.prepare(messages)
            stock_raised = False
        except gate.MoaReferenceOutage:
            stock_raised = True
        check("H4 disarmed gate is fail-open again", not stock_raised or calls["n"] == 0)
        state.bind_context(None)

        # -- K: Secondary Reference Fallback -------------------------------------------
        gate.install(force=True)
        fb_calls = []

        def _mock_run_refs(models, *args, **kwargs):
            called_labels = [moa_loop._slot_label(m) for m in models]
            fb_calls.append(called_labels)
            res = []
            for m in models:
                lbl = moa_loop._slot_label(m)
                if "fallback-target" in lbl:
                    res.append((lbl, "secondary reference guidance ok", None))
                else:
                    res.append((lbl, "[failed: primary down]", None))
            return res

        orig_run_refs = moa_loop._run_references_parallel
        moa_loop._run_references_parallel = _mock_run_refs
        state.bind_context(test_ctx)
        try:
            state.save({
                "enabled": True,
                "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:fallback-target",
                "fallback_trigger_attempts": 2,
                "ladder": [0.1, 0.1, 0.1],
            })
            fb_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            fb_prep = fb_client.prepare(messages)
            k1_condition = (
                len(fb_calls) == 3
                and not any("fallback-target" in lbl for lbl in fb_calls[0])
                and not any("fallback-target" in lbl for lbl in fb_calls[1])
                and any("fallback-target" in lbl for lbl in fb_calls[2])
            )
            check("K1 secondary fallback activates on threshold",
                  k1_condition,
                  f"{len(fb_calls)} call batch(es): {fb_calls}")
            switched_count = report.summary()["counters"].get("reference_fallback_switched", 0)
            check("K2 secondary fallback delivers guidance without outage",
                  isinstance(fb_prep, dict) and not fb_prep.get("_true_moa_outage")
                  and "secondary reference guidance ok" in str(fb_prep.get("guidance", ""))
                  and switched_count >= 1,
                  f"switched={switched_count}, guidance={fb_prep.get('guidance', '')}")

            # K3: Dual outage: when both primary and secondary fail, gate still halts turn (Fail-Closed)
            def _mock_all_fail(models, *args, **kwargs):
                return [(moa_loop._slot_label(m), "[failed: all down]", None) for m in models]

            moa_loop._run_references_parallel = _mock_all_fail
            dual_prep = fb_client.prepare(messages)
            dual_outage = dual_prep.get("_true_moa_outage") if isinstance(dual_prep, dict) else None
            check("K3 dual outage halts turn with fail-closed",
                  bool(dual_outage) and dual_outage.get("fallback_activated") is True,
                  str(dual_outage))

            # K4: Anti-loophole: secondary matching aggregator or failed primary stays unactivated
            agg_prov = str(preset.get("aggregator", {}).get("provider") or "")
            agg_mod = str(preset.get("aggregator", {}).get("model") or "")
            prim_prov = str(refs[0].get("provider") or "")
            prim_mod = str(refs[0].get("model") or "")
            state.save({
                "enabled": True,
                "enable_secondary_fallback": True,
                "secondary_reference_model": f"{agg_prov}:{agg_mod}",
                "fallback_trigger_attempts": 1,
                "ladder": [0.1],
            })
            loophole_prep = fb_client.prepare(messages)
            loophole_outage = loophole_prep.get("_true_moa_outage") if isinstance(loophole_prep, dict) else None
            state.save({
                "enabled": True,
                "enable_secondary_fallback": True,
                "secondary_reference_model": f"{prim_prov}:{prim_mod}",
                "fallback_trigger_attempts": 1,
                "ladder": [0.1],
            })
            same_prim_prep = fb_client.prepare(messages)
            same_prim_outage = same_prim_prep.get("_true_moa_outage") if isinstance(same_prim_prep, dict) else None
            check("K4 aggregator as secondary is rejected",
                  bool(loophole_outage) and not loophole_outage.get("fallback_activated")
                  and bool(same_prim_outage) and not same_prim_outage.get("fallback_activated"),
                  f"agg={loophole_outage}, same_prim={same_prim_outage}")

            # K5: Short ladder / exhausted budget does not record a false secondary switch
            switches_before_k5 = report.summary()["counters"].get("reference_fallback_switched", 0)
            state.save({
                "enabled": True,
                "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:fallback-target",
                "fallback_trigger_attempts": 2,
                "ladder": [0.05],
            })
            short_prep = fb_client.prepare(messages)
            short_outage = short_prep.get("_true_moa_outage") if isinstance(short_prep, dict) else None
            switches_after_k5 = report.summary()["counters"].get("reference_fallback_switched", 0)
            check("K5 exhausted ladder at trigger threshold does not log false switch",
                  bool(short_outage) and short_outage.get("fallback_activated") is False
                  and switches_after_k5 == switches_before_k5,
                  f"outage={short_outage}, delta_switches={switches_after_k5 - switches_before_k5}")

            # K6: Comma-separated secondary chain cascades past a failing first secondary
            def _mock_chain_refs(models, *args, **kwargs):
                out = []
                for m in models:
                    lbl = moa_loop._slot_label(m)
                    if "sec-two" in lbl:
                        out.append((lbl, "second secondary rescued turn", None))
                    else:
                        out.append((lbl, f"[failed: {lbl} down]", None))
                return out

            moa_loop._run_references_parallel = _mock_chain_refs
            state.save({
                "enabled": True,
                "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:sec-one, apo:sec-two",
                "fallback_trigger_attempts": 1,
                "ladder": [0.05, 0.05, 0.05],
            })
            chain_prep = fb_client.prepare(messages)
            check("K6 comma-separated secondary chain cascades to healthy backup",
                  isinstance(chain_prep, dict) and not chain_prep.get("_true_moa_outage")
                  and "second secondary rescued turn" in str(chain_prep.get("guidance", "")),
                  str(chain_prep.get("guidance", ""))[:120] if isinstance(chain_prep, dict) else str(chain_prep))

            # L1: Selective slot retry does not re-query already-succeeded advisors
            slot_hits: dict[str, int] = {}

            def _mock_selective(models, *args, **kwargs):
                out = []
                for m in models:
                    lbl = moa_loop._slot_label(m)
                    slot_hits[lbl] = slot_hits.get(lbl, 0) + 1
                    if "slot-good" in lbl:
                        out.append((lbl, "good advisor output", None))
                    elif slot_hits[lbl] == 1:
                        out.append((lbl, "[failed: transient 429]", None))
                    else:
                        out.append((lbl, "recovered flaky output", None))
                return out

            moa_loop._run_references_parallel = _mock_selective
            state.save({
                "enabled": True,
                "enable_secondary_fallback": False,
                "ladder": [0.05, 0.05],
            })
            two_slots = [
                {"provider": "apo", "model": "slot-good", "enabled": True},
                {"provider": "apo", "model": "slot-flaky", "enabled": True},
            ]
            sel_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            sel_outputs = sel_client._run_fanout(preset, messages, two_slots, preset.get("aggregator") or {}, None, ("sel", 1))
            good_calls = sum(v for k, v in slot_hits.items() if "slot-good" in k)
            flaky_calls = sum(v for k, v in slot_hits.items() if "slot-flaky" in k)
            sel_texts = " | ".join(t for _l, t, _a in sel_outputs)
            check("L1 selective retry preserves healthy advisor without re-calling it",
                  good_calls == 1 and flaky_calls == 2
                  and "good advisor output" in sel_texts and "recovered flaky output" in sel_texts,
                  f"hits={slot_hits}, texts={sel_texts}")

            # L2: User interrupt aborts retry sleep promptly and raises InterruptedError on create()
            class _InterruptOnRetrySleep:
                def __init__(self) -> None:
                    self.batch_done_at: float | None = None

                @property
                def _interrupt_requested(self) -> bool:
                    return self.batch_done_at is not None and (time.monotonic() - self.batch_done_at) >= 0.05

            fake_agent = _InterruptOnRetrySleep()

            def _fail_and_arm_interrupt(slots, *a, **kw):
                res = _mock_all_fail(slots, *a, **kw)
                if fake_agent.batch_done_at is None:
                    fake_agent.batch_done_at = time.monotonic()
                return res

            moa_loop._run_references_parallel = _fail_and_arm_interrupt
            state.save({"enabled": True, "ladder": [5.0, 5.0], "max_wait_budget_s": 0.0})
            int_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=fake_agent)
            int_started = time.monotonic()
            int_prep = int_client.prepare(messages)
            int_elapsed = time.monotonic() - int_started
            int_raised = False
            try:
                int_client.create(**{"_moa_prepared_request": int_prep, "messages": messages})
            except InterruptedError:
                int_raised = True
            except gate.MoaReferenceOutage:
                int_raised = False
            check("L2 user interrupt wakes retry sleep promptly and raises InterruptedError",
                  0.05 <= int_elapsed < 1.5 and int_raised and not getattr(int_client, "_ref_cache_outputs", None),
                  f"elapsed={int_elapsed:.2f}s, raised={int_raised}")

            # L3: One-shot /moa (_append_moa_context -> build_api_request) enforces fail-closed gate
            state.save({"enabled": True, "ladder": [0.05], "enable_secondary_fallback": False})
            oneshot_agent = SimpleNamespace(
                _interrupt_requested=False,
                provider="custom",
                model="agg-main",
                base_url="http://127.0.0.1:9999/v1",
                api_mode="chat_completions",
                tools=[],
                max_tokens=1024,
                session_id="selftest-oneshot",
                platform="cli",
                _empty_content_retries=0,
                _reset_stream_delivery_tracking=lambda: None,
                _reapply_reasoning_echo_for_provider=lambda _m: None,
                _build_api_kwargs=lambda m, **kw: {"messages": m},
                _is_openrouter_url=lambda: False,
                _api_request_payload_for_hook=lambda kw: kw,
            )
            oneshot_msgs = [{"role": "user", "content": "oneshot probe"}]
            oneshot_cfg = {
                "reference_models": [{"provider": "apo", "model": "oneshot-ref", "enabled": True}],
                "aggregator": {"provider": "apo", "model": "oneshot-agg"},
            }
            turn_assembly._append_moa_context(oneshot_agent, oneshot_msgs, oneshot_cfg, "oneshot probe")
            oneshot_halted = False
            try:
                turn_api_req.build_api_request(
                    oneshot_agent, api_messages=oneshot_msgs, _moa_prepared_request=None,
                    tools_for_api=[], system_message="", messages=oneshot_msgs,
                    original_user_message="oneshot probe", approx_tokens=10, total_chars=40,
                    retry_count=0, api_call_count=0, api_request_id="req-1",
                    api_start_time=time.time(), effective_task_id="t-1", turn_id="turn-1",
                )
            except gate.MoaReferenceOutage:
                oneshot_halted = True
            check("L3 one-shot /moa path halts fail-closed before main API call",
                  oneshot_halted and "Proceeding without aggregated guidance" not in oneshot_msgs[0]["content"],
                  f"halted={oneshot_halted}, msg={oneshot_msgs[0]['content']!r}")

            # L3b: conversation_loop._run_phase resolves real parameters from _LoopState (never 'kwargs') even when re-installed post-import
            reinstalled = gate.install(force=True)
            aliases_synced = getattr(conv_loop.build_api_request, "__true_moa_original__", None) is not None
            loop_init_kwargs: dict[str, Any] = {
                f.name: None
                for f in dataclasses.fields(conv_loop._LoopState)
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            }
            loop_init_kwargs.update({
                "api_messages": oneshot_msgs,
                "_moa_prepared_request": None,
                "tools_for_api": [],
                "system_message": "",
                "messages": oneshot_msgs,
                "original_user_message": "oneshot probe",
                "approx_tokens": 10,
                "total_chars": 40,
                "retry_count": 0,
                "api_call_count": 0,
                "api_request_id": "req-1",
                "api_start_time": time.time(),
                "effective_task_id": "t-1",
                "turn_id": "turn-1",
            })
            loop_state = conv_loop._LoopState(**loop_init_kwargs)
            oneshot_agent._true_moa_oneshot_outage = {
                "labels": ["apo:oneshot-ref"],
                "attempts": 1,
                "waited_s": 0.1,
            }
            phase_outage = False
            try:
                conv_loop._run_phase(conv_loop.build_api_request, oneshot_agent, loop_state)
            except gate.MoaReferenceOutage:
                phase_outage = True
            phase_params = conv_loop._PHASE_PARAMS.get(conv_loop.build_api_request, ())
            expected_phase_params = tuple(
                p for p in inspect.signature(conv_loop.build_api_request.__true_moa_original__).parameters if p != "agent"
            )
            check("L3b conversation_loop._run_phase preserves exact build_api_request signature and post-import alias sync",
                  bool(reinstalled.get("ok")) and aliases_synced and phase_outage and "kwargs" not in phase_params and set(phase_params) == set(expected_phase_params),
                  f"synced={aliases_synced}, outage={phase_outage}, params={phase_params}")

            # L4: CLI and slash quick controls for ladder and fallback
            cli_parser = argparse.ArgumentParser(prog="hermes true-moa")
            cli.setup_parser(cli_parser)
            rc_lad = cli.handle(cli_parser.parse_args(["ladder", "12,24,48"]))
            slash_fb_out = slash.handle("fallback 2 bdd:gemini-3.8-flash,beat:deepseek-v4.1-flash-free")
            loaded_after_cmd = state.load()
            check("L4 CLI ladder and slash fallback commands persist validated settings",
                  rc_lad == 0 and loaded_after_cmd["ladder"] == [12.0, 24.0, 48.0]
                  and loaded_after_cmd["enable_secondary_fallback"] is True
                  and "bdd:gemini-3.8-flash" in loaded_after_cmd["secondary_reference_model"]
                  and "fallback" in slash_fb_out.lower(),
                  f"rc={rc_lad}, state={loaded_after_cmd}")

            # N1: Turn-liveness clock is touched during retry sleep so watchdog does not kill long backoffs
            touches: list[str] = []
            live_agent = SimpleNamespace(_interrupt_requested=False, _touch_activity=lambda desc: touches.append(str(desc)))
            moa_loop._run_references_parallel = _mock_all_fail
            state.save({"enabled": True, "ladder": [0.15], "enable_secondary_fallback": False, "min_healthy_references": 0})
            live_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=live_agent)
            _ = live_client.prepare(messages)
            check("N1 retry sleep stamps agent._touch_activity for turn-liveness watchdog",
                  len(touches) >= 1 and any("retry" in t.lower() or "true-moa" in t.lower() for t in touches),
                  f"touches={touches}")

            # N2: Premature [failed: ...] moa.reference and moa.aggregating UI events are suppressed during retries
            ui_events: list[tuple[str, dict]] = []
            flaky_ui_attempts = {"n": 0}

            def _flaky_for_ui(slots, *a, **kw):
                flaky_ui_attempts["n"] += 1
                out = []
                for s in slots:
                    lbl = f"{s.get('provider')}:{s.get('model')}"
                    txt = "[failed: HTTP 503 overloaded]" if flaky_ui_attempts["n"] == 1 else "verified clean answer"
                    out.append((lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            moa_loop._run_references_parallel = _flaky_for_ui
            state.save({"enabled": True, "ladder": [0.05], "enable_secondary_fallback": False})
            ui_client = moa_loop.MoAChatCompletions(
                preset_name, reference_callback=lambda ev, **kw: ui_events.append((ev, kw)), agent=None,
            )
            _ = ui_client._run_fanout(
                preset, messages, [{"provider": "apo", "model": "ui-slot", "enabled": True}],
                preset.get("aggregator") or {}, None, ("ui", 1),
            )
            ref_events = [kw for ev, kw in ui_events if ev == "moa.reference"]
            agg_events = [kw for ev, kw in ui_events if ev == "moa.aggregating"]
            check("N2 UI callback suppresses failed intermediate cards and emits final clean output once",
                  len(ref_events) == 1 and "[failed:" not in str(ref_events[0].get("text", "")) and len(agg_events) == 1,
                  f"ref_events={len(ref_events)}, agg_events={len(agg_events)}, events={[e for e, _ in ui_events]}")

            # N3: Deterministic 401/404 errors fast-switch to secondary (0s sleep) or fast-halt without sleeping ladder
            def _auth_401_or_sec_ok(slots, *a, **kw):
                out = []
                for s in slots:
                    lbl = f"{s.get('provider')}:{s.get('model')}"
                    if s.get("model") == "sec-fast":
                        out.append((lbl, "fast secondary rescue", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    else:
                        out.append((lbl, "[failed: Error code: 401 - Invalid API key]", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            moa_loop._run_references_parallel = _auth_401_or_sec_ok
            state.save({
                "enabled": True, "ladder": [4.0, 4.0], "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:sec-fast", "fallback_trigger_attempts": 3,
            })
            fast_t0 = time.monotonic()
            fast_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            fast_out = fast_client._run_fanout(
                preset, messages, [{"provider": "apo", "model": "dead-401", "enabled": True}],
                preset.get("aggregator") or {}, None, ("fast", 1),
            )
            fast_sw_elapsed = time.monotonic() - fast_t0

            state.save({"enabled": True, "ladder": [4.0, 4.0], "enable_secondary_fallback": False})
            halt_t0 = time.monotonic()
            halt_prep = fast_client.prepare(messages)
            halt_elapsed = time.monotonic() - halt_t0
            check("N3 deterministic 401/404 fast-switches to secondary and fast-halts without ladder sleep",
                  fast_sw_elapsed < 1.0 and any("fast secondary rescue" in t for _, t, _ in fast_out)
                  and halt_elapsed < 1.0 and bool(halt_prep.get("_true_moa_outage")),
                  f"sw_elapsed={fast_sw_elapsed:.2f}s, halt_elapsed={halt_elapsed:.2f}s")

            # N4: Multi-reference quorum accepts >= min_healthy_references without caching degraded batch, never 0
            def _two_of_three_ok(slots, *a, **kw):
                out = []
                for s in slots:
                    lbl = f"{s.get('provider')}:{s.get('model')}"
                    txt = "[failed: HTTP 503 down]" if s.get("model") == "ref-dead" else f"ok from {lbl}"
                    out.append((lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            three_slots = [
                {"provider": "apo", "model": "ref-1", "enabled": True},
                {"provider": "apo", "model": "ref-2", "enabled": True},
                {"provider": "apo", "model": "ref-dead", "enabled": True},
            ]
            moa_loop._run_references_parallel = _two_of_three_ok
            state.save({"enabled": True, "ladder": [0.02], "enable_secondary_fallback": False, "min_healthy_references": 2})
            q_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            q_outputs = q_client._run_fanout(preset, messages, three_slots, preset.get("aggregator") or {}, None, ("q", 1))
            q_unpoisoned_cache = not getattr(q_client, "_ref_cache_outputs", None)

            moa_loop._run_references_parallel = _mock_all_fail
            zero_halted = False
            try:
                q_client._run_fanout(preset, messages, three_slots, preset.get("aggregator") or {}, None, ("q0", 1))
            except gate.MoaReferenceOutage:
                zero_halted = True
            state.save({"min_healthy_references": 0})
            check("N4 quorum mode accepts >= K healthy advisors, skips degraded cache, and still halts on 0 healthy",
                  len([t for _, t, _ in q_outputs if not t.startswith("[failed:")]) == 2
                  and q_unpoisoned_cache and zero_halted,
                  f"outputs={len(q_outputs)}, cache_clean={q_unpoisoned_cache}, zero_halted={zero_halted}")

            # N5: Static /true-moa doctor & /true-moa quorum commands validate config without network calls
            q_msg = slash.handle("quorum 2")
            q_saved = state.load().get("min_healthy_references")
            _ = slash.handle("quorum all")
            q_reset = state.load().get("min_healthy_references")
            doc_out = slash.handle("doctor")
            check("N5 slash quorum and doctor commands validate settings and report health offline",
                  q_saved == 2 and q_reset == 0 and "doctor" in doc_out.lower() and "pyproject" in doc_out.lower(),
                  f"q_saved={q_saved}, q_reset={q_reset}, doc_head={doc_out.splitlines()[0] if doc_out else ''!r}")

            # N6: Official PR #157214 hook handlers (moa_fanout_verdict + auxiliary_fallback_policy)
            verdict_fn = getattr(gate, "handle_moa_fanout_verdict", None)
            policy_fn = getattr(gate, "handle_auxiliary_fallback_policy", None)
            hook_ok = False
            if callable(verdict_fn) and callable(policy_fn):
                state.save({"enabled": True, "ladder": [0.05], "enable_secondary_fallback": False, "block_aggregator_fallback": True})
                v_retry = verdict_fn(
                    preset_name="hook-test", preset_config={}, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-a", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-a", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=1,
                )
                v_abort = verdict_fn(
                    preset_name="hook-test", preset_config={}, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-a", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-a", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=2,
                )
                v_accept = verdict_fn(
                    preset_name="hook-test", preset_config={}, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-a", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-a", "text": "healthy advice", "failed": False}],
                    attempt=2,
                )
                pol_ref = policy_fn(task="moa_reference", failed_provider="apo", failed_model="ref-a", fallback_provider="apo", fallback_model="agg", reason="error")
                pol_vis = policy_fn(task="vision", failed_provider="apo", failed_model="v1", fallback_provider="apo", fallback_model="agg", reason="error")
                hook_ok = (
                    isinstance(v_retry, dict) and v_retry.get("action") == "retry"
                    and isinstance(v_abort, dict) and v_abort.get("action") == "abort"
                    and isinstance(v_accept, dict) and v_accept.get("action") == "accept"
                    and pol_ref is False and pol_vis is None
                )
            check("N6 official PR #157214 verdict and fallback-policy hook handlers honor strict gate contract",
                  hook_ok,
                  f"callable={callable(verdict_fn) and callable(policy_fn)}")

            # N7: Multi-slot secondary switch preserves sibling pending retries & enforces strict all=N quorum
            flaky_503_calls = {"n": 0}

            def _mixed_401_and_503(slots, *a, **kw):
                out = []
                for s in slots:
                    m = s.get("model")
                    lbl = f"{s.get('provider')}:{m}"
                    if m == "sec-backup":
                        out.append((lbl, "backup advisor ok", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    elif m == "bad-401":
                        out.append((lbl, "[failed: Error code: 401 - Invalid API key]", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    else:
                        flaky_503_calls["n"] += 1
                        txt = "flaky advisor recovered" if flaky_503_calls["n"] >= 2 else "[failed: HTTP 503 down]"
                        out.append((lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            mixed_slots = [
                {"provider": "apo", "model": "bad-401", "enabled": True},
                {"provider": "apo", "model": "flaky-503", "enabled": True},
            ]
            moa_loop._run_references_parallel = _mixed_401_and_503
            state.save({
                "enabled": True, "ladder": [0.02, 0.02], "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:sec-backup", "fallback_trigger_attempts": 2,
                "min_healthy_references": 0,
            })
            mix_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            mix_out = mix_client._run_fanout(preset, messages, mixed_slots, preset.get("aggregator") or {}, None, ("mix", 1))

            def _mixed_401_and_permanent_503(slots, *a, **kw):
                out = []
                for s in slots:
                    m = s.get("model")
                    lbl = f"{s.get('provider')}:{m}"
                    if m == "sec-backup":
                        out.append((lbl, "backup advisor ok", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    elif m == "bad-401":
                        out.append((lbl, "[failed: Error code: 401 - Invalid API key]", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    else:
                        out.append((lbl, "[failed: HTTP 503 permanently down]", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            moa_loop._run_references_parallel = _mixed_401_and_permanent_503
            strict_all_halted = False
            try:
                mix_client._run_fanout(preset, messages, mixed_slots, preset.get("aggregator") or {}, None, ("mix2", 1))
            except gate.MoaReferenceOutage:
                strict_all_halted = True

            # Concurrent verdict hook sessions on same preset_name do not overwrite each other's budget/chain state
            iso_ok = False
            if callable(verdict_fn):
                state.save({
                    "enabled": True, "ladder": [0.04, 0.04, 0.04], "max_wait_budget_s": 0.05,
                    "enable_secondary_fallback": False, "min_healthy_references": 0,
                })
                cfg_session_a = {"session": "a"}
                cfg_session_b = {"session": "b"}
                _ = verdict_fn(
                    preset_name="shared", preset_config=cfg_session_a, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=1,
                )
                _ = verdict_fn(
                    preset_name="shared", preset_config=cfg_session_a, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=2,
                )
                # Session B starts attempt 1 on same preset_name="shared"; must NOT reset Session A's waited_s (0.05 >= 0.05)!
                _ = verdict_fn(
                    preset_name="shared", preset_config=cfg_session_b, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "other-503", "enabled": True}],
                    reference_outputs=[{"label": "apo:other-503", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=1,
                )
                v_a3 = verdict_fn(
                    preset_name="shared", preset_config=cfg_session_a, aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                    reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=3,
                )
                state.save({"max_wait_budget_s": 0.0})
                iso_ok = isinstance(v_a3, dict) and v_a3.get("action") == "abort"

            check("N7 multi-slot secondary switch preserves sibling retries, enforces strict all=N, and isolates concurrent hook turns",
                  len(mix_out) == 2 and flaky_503_calls["n"] == 2 and strict_all_halted and iso_ok,
                  f"mix_len={len(mix_out)}, flaky_calls={flaky_503_calls['n']}, strict_halted={strict_all_halted}, iso_ok={iso_ok}")

            # N8: Doomed quorum fast-halt, native-hooks install idempotency, and multi-thread shared-preset isolation
            doomed_503_calls = {"n": 0}

            def _doomed_401_and_503(slots, *a, **kw):
                out = []
                for s in slots:
                    m = s.get("model")
                    lbl = f"{s.get('provider')}:{m}"
                    if m == "dead-401":
                        out.append((lbl, "[failed: Error code: 401 - Invalid API key]", moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                    else:
                        doomed_503_calls["n"] += 1
                        txt = "flaky advisor recovered" if doomed_503_calls["n"] >= 2 else "[failed: HTTP 503 down]"
                        out.append((lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage())))
                return out

            doomed_slots = [
                {"provider": "apo", "model": "dead-401", "enabled": True},
                {"provider": "apo", "model": "flaky-503", "enabled": True},
            ]
            moa_loop._run_references_parallel = _doomed_401_and_503
            state.save({
                "enabled": True, "ladder": [0.02, 0.02], "enable_secondary_fallback": False,
                "min_healthy_references": 0,
            })
            doomed_halted = False
            try:
                mix_client._run_fanout(preset, messages, doomed_slots, preset.get("aggregator") or {}, None, ("doomed", 1))
            except gate.MoaReferenceOutage:
                doomed_halted = True
            calls_under_strict_all = doomed_503_calls["n"]

            # Under quorum=1, sibling 503 slot IS still retried and rescues the turn
            doomed_503_calls["n"] = 0
            state.save({"min_healthy_references": 1})
            quorum_rescued = mix_client._run_fanout(preset, messages, doomed_slots, preset.get("aggregator") or {}, None, ("qrescue", 1))
            calls_under_quorum_1 = doomed_503_calls["n"]
            state.save({"min_healthy_references": 0})

            # Native hooks mode stays un-monkeypatched across `/true-moa on` and `/true-moa retry`
            class _HookProbeCtx:
                def __init__(self) -> None:
                    self.hooks: list[str] = []

                def register_hook(self, name: str, _fn) -> None:
                    self.hooks.append(name)

            no_double_wrap = False
            try:
                gate.activate_native_hooks(_HookProbeCtx())
                _ = slash.handle("on")
                retry_msg = slash.handle("retry")
                st_after = gate.status()
                expected_native = {
                    "hook:moa_fanout_verdict",
                    "hook:auxiliary_fallback_policy",
                    "hook:pre_auxiliary_call",
                    "hook:post_auxiliary_call",
                    "moa._fanout_cache_key",
                    "moa._trim_messages_for_reference",
                }
                no_double_wrap = (
                    st_after.get("mode") == "native_hooks"
                    and expected_native.issubset(set(st_after.get("wrapped") or []))
                    and getattr(moa_loop.MoAChatCompletions._run_fanout, "__true_moa_original__", None) is None
                    and getattr(moa_loop.MoAChatCompletions.prepare, "__true_moa_original__", None) is None
                    and getattr(moa_loop.MoAChatCompletions.create, "__true_moa_original__", None) is None
                    and "native hooks" in retry_msg.lower()
                )
            finally:
                gate.deactivate_native_hooks()
                gate.install()

            # Multi-thread concurrent verdict sessions sharing the exact same cached preset dict
            mt_iso_ok = False
            if callable(verdict_fn):
                state.save({
                    "enabled": True, "ladder": [0.04, 0.04, 0.04], "max_wait_budget_s": 0.05,
                    "enable_secondary_fallback": False, "min_healthy_references": 0,
                })
                shared_cached_preset = {"reference_models": [{"provider": "apo", "model": "ref-503", "enabled": True}]}
                ev_a_ready = threading.Event()
                ev_b_done = threading.Event()
                mt_results: dict[str, object] = {}

                def _thread_a() -> None:
                    _ = verdict_fn(
                        preset_name="cached-preset", preset_config=shared_cached_preset, aggregator={"provider": "apo", "model": "agg"},
                        active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                        reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                        attempt=1,
                    )
                    _ = verdict_fn(
                        preset_name="cached-preset", preset_config=shared_cached_preset, aggregator={"provider": "apo", "model": "agg"},
                        active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                        reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                        attempt=2,
                    )
                    ev_a_ready.set()
                    ev_b_done.wait(timeout=5.0)
                    mt_results["a3"] = verdict_fn(
                        preset_name="cached-preset", preset_config=shared_cached_preset, aggregator={"provider": "apo", "model": "agg"},
                        active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                        reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                        attempt=3,
                    )

                def _thread_b() -> None:
                    ev_a_ready.wait(timeout=5.0)
                    mt_results["b1"] = verdict_fn(
                        preset_name="cached-preset", preset_config=shared_cached_preset, aggregator={"provider": "apo", "model": "agg"},
                        active_slots=[{"provider": "apo", "model": "ref-503", "enabled": True}],
                        reference_outputs=[{"label": "apo:ref-503", "text": "[failed: HTTP 503]", "failed": True}],
                        attempt=1,
                    )
                    ev_b_done.set()

                ta = threading.Thread(target=_thread_a)
                tb = threading.Thread(target=_thread_b)
                ta.start()
                tb.start()
                ta.join(timeout=5.0)
                tb.join(timeout=5.0)
                state.save({"max_wait_budget_s": 0.0})
                res_a3 = mt_results.get("a3")
                mt_iso_ok = isinstance(res_a3, dict) and res_a3.get("action") == "abort"

            check("N8 doomed quorum fast-halts on attempt 1, native hooks resist double-wrapping, and multi-thread hooks isolate shared presets",
                  doomed_halted and calls_under_strict_all == 1 and calls_under_quorum_1 == 2 and len(quorum_rescued) == 2 and no_double_wrap and mt_iso_ok,
                  f"strict_calls={calls_under_strict_all}, q1_calls={calls_under_quorum_1}, no_double_wrap={no_double_wrap}, mt_iso_ok={mt_iso_ok}")

            # N9: Bilingual English (default) + Vietnamese switching across slash, doctor, retry progress, and outage banners
            prog_labels: list[str] = []

            def _flaky_for_prog(slots, *a, **kw):
                lbl = f"{slots[0].get('provider')}:{slots[0].get('model')}"
                txt = "ok" if len(prog_labels) >= 1 else "[failed: HTTP 503 busy]"
                return [(lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]

            moa_loop._run_references_parallel = _flaky_for_prog
            lang_switch_vi = slash.handle("lang vi")
            status_vi = slash.handle("status")
            doctor_vi = slash.handle("doctor")
            state.save({"enabled": True, "ladder": [0.02], "enable_secondary_fallback": False})
            prog_client = moa_loop.MoAChatCompletions(
                preset_name,
                reference_callback=lambda ev, **kw: prog_labels.append(str(kw.get("label", ""))) if ev == "moa.progress" else None,
                agent=None,
            )
            _ = prog_client._run_fanout(
                preset, messages, [{"provider": "apo", "model": "advisor-vi", "enabled": True}],
                preset.get("aggregator") or {}, None, ("prog_vi", 1),
            )
            outage_vi = str(gate.MoaReferenceOutage(("apo:advisor-vi",), 2, 10.0, ladder=[10.0]))

            lang_switch_en = slash.handle("lang en-au")
            status_en = slash.handle("status")
            doctor_en = slash.handle("doctor")
            outage_en = str(gate.MoaReferenceOutage(("apo:advisor-en",), 2, 10.0, ladder=[10.0]))
            bilingual_ok = (
                "tiếng việt" in lang_switch_vi.lower()
                and "đang bật" in status_vi.lower()
                and "chẩn đoán" in doctor_vi.lower()
                and any("apo:advisor-vi" in p and "thử lại" in p.lower() for p in prog_labels)
                and "cố vấn moa" in outage_vi.lower()
                and "english" in lang_switch_en.lower()
                and "armed" in status_en.lower()
                and "offline health check" in doctor_en.lower()
                and "moa reference outage:" in outage_en.lower()
            )
            check("N9 bilingual English/Vietnamese switch localizes slash, doctor, retry progress, and outage messages",
                  bilingual_ok,
                  f"prog={prog_labels}, vi_status={status_vi.splitlines()[0] if status_vi else ''!r}")

            # N10: Unified Desktop surface (desktop/plugin.js + dashboard/plugin_api.py) passes SDK lint and syncs settings
            from hermes_cli.plugin_validate_desktop import desktop_review_findings, desktop_surface_findings
            desktop_js_path = PLUGIN_DIR / "desktop" / "plugin.js"
            api_py_path = PLUGIN_DIR / "dashboard" / "plugin_api.py"
            unified_ok = False
            if desktop_js_path.is_file() and api_py_path.is_file():
                js_src = desktop_js_path.read_text(encoding="utf-8")
                surf_err = desktop_surface_findings(js_src)
                surf_warn = desktop_review_findings(js_src)
                spec_api = importlib.util.spec_from_file_location("_true_moa_selftest_api", api_py_path)
                if spec_api and spec_api.loader:
                    api_mod = importlib.util.module_from_spec(spec_api)
                    spec_api.loader.exec_module(api_mod)
                    snap_en = api_mod.build_dashboard_snapshot()
                    patched_vi = api_mod.apply_settings_patch({"language": "vi", "min_healthy_references": 2})
                    snap_back = api_mod.apply_settings_patch({"language": "en", "min_healthy_references": 0})
                    # Snapshot GET remains strictly read-only when gate is uninstalled; reverify re-arms explicitly.
                    gate.uninstall()
                    readonly_snap = api_mod.build_dashboard_snapshot()
                    rearmed_snap = api_mod.api_reverify_gate()
                    orig_gate_mod = sys.modules["true_moa.gate"]
                    reloaded_gate_proxy = SimpleNamespace(**orig_gate_mod.__dict__)
                    hot_reload_rebound = False
                    try:
                        sys.modules["true_moa.gate"] = reloaded_gate_proxy  # type: ignore[assignment]
                        _d_m, resolved_gate_m, _i_m, _r_m, _s_m = api_mod._live_modules()
                        hot_reload_rebound = resolved_gate_m is reloaded_gate_proxy and api_mod.gate is reloaded_gate_proxy
                    finally:
                        sys.modules["true_moa.gate"] = orig_gate_mod
                        api_mod._live_modules()
                    unified_ok = (
                        surf_err == []
                        and surf_warn == []
                        and snap_en["settings"]["language"] == "en"
                        and patched_vi["settings"]["language"] == "vi"
                        and patched_vi["settings"]["min_healthy_references"] == 2
                        and "chẩn đoán" in patched_vi["doctor"]["title"].lower()
                        and snap_back["settings"]["language"] == "en"
                        and api_mod.gate is gate
                        and readonly_snap["status"]["installed"] is False
                        and rearmed_snap["status"]["installed"] is True
                        and rearmed_snap["doctor"]["ok"] is True
                        and hot_reload_rebound
                    )
            check("N10 unified Desktop UI and dashboard API stay inside SDK surface and synchronize bilingual state",
                  unified_ok,
                  f"js={desktop_js_path.is_file()}, api={api_py_path.is_file()}")

            # N11: Per-reference telemetry & miss-cause diagnosis recorded through real _run_fanout + /true-moa cache
            optimize.reset_trackers()
            n11_seq = {"n": 0}
            n11_slots = [{"provider": "apo", "model": "tel-ref", "enabled": True}]

            def _telemetry_run(slots, ref_msgs, *a, **kw):
                n11_seq["n"] += 1
                idx = n11_seq["n"]
                if idx == 1:
                    usage = moa_loop.CanonicalUsage(input_tokens=2000, cache_read_tokens=0, output_tokens=250)
                elif idx == 2:
                    usage = moa_loop.CanonicalUsage(input_tokens=150, cache_read_tokens=2000, output_tokens=180)
                else:
                    usage = moa_loop.CanonicalUsage(input_tokens=2300, cache_read_tokens=0, output_tokens=200)
                return [(
                    "apo:tel-ref",
                    f"advice iteration {idx}",
                    moa_loop._RefAccounting(usage, model="tel-ref", provider="apo", messages=[{"role": "system", "content": "s"}, *ref_msgs]),
                )]

            moa_loop._run_references_parallel = _telemetry_run
            state.save({"enabled": True, "language": "en", "enable_secondary_fallback": False, "adaptive_fanout": False})
            tel_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            long_prompt = "analyze cache behavior " * 120
            _ = tel_client._run_fanout(preset, [{"role": "user", "content": long_prompt}], n11_slots, preset.get("aggregator") or {}, None, ("n11", 1))
            _ = tel_client._run_fanout(preset, [{"role": "user", "content": long_prompt}, {"role": "assistant", "content": "step 1"}], n11_slots, preset.get("aggregator") or {}, None, ("n11", 2))
            _ = tel_client._run_fanout(preset, [{"role": "user", "content": long_prompt}, {"role": "assistant", "content": "step 1"}], n11_slots, preset.get("aggregator") or {}, None, ("n11", 3))
            gate.handle_pre_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-a1", session_id="sess-n11", provider="apo", model="tel-aux",
                request_messages=[{"role": "system", "content": "s"}, {"role": "user", "content": long_prompt}],
            )
            gate.handle_post_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-a1", session_id="sess-n11", provider="apo", model="tel-aux",
                api_duration=0.42, usage={"input_tokens": 2000, "cache_read_tokens": 0, "output_tokens": 120, "reasoning_tokens": 0},
            )
            gate.handle_pre_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-b1", session_id="sess-n11", provider="apo", model="tel-aux-b",
                request_messages=[{"role": "system", "content": "s"}, {"role": "user", "content": long_prompt}],
            )
            gate.handle_post_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-b1", session_id="sess-n11", provider="apo", model="tel-aux-b",
                api_duration=0.35, usage={"input_tokens": 2000, "cache_read_tokens": 0, "output_tokens": 90, "reasoning_tokens": 0},
            )
            gate.handle_pre_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-a2", session_id="sess-n11", provider="apo", model="tel-aux",
                request_messages=[{"role": "system", "content": "s"}, {"role": "user", "content": long_prompt}, {"role": "assistant", "content": "step 1"}],
            )
            gate.handle_post_auxiliary_call(
                aux_task="moa_reference", api_request_id="req-n11-a2", session_id="sess-n11", provider="apo", model="tel-aux",
                api_duration=0.25, usage={"input_tokens": 150, "cache_read_tokens": 1950, "output_tokens": 100, "reasoning_tokens": 0},
            )
            cache_en = slash.handle("cache")
            _ = slash.handle("lang vi")
            cache_vi = slash.handle("cache")
            _ = slash.handle("lang en")
            n11_summary = report.summary(window_days=1)
            n11_reasons = n11_summary["reference_telemetry"]["miss_reasons"]
            n11_tail_aux = [
                r for r in report.tail(10)
                if isinstance(r, dict) and r.get("event") == "reference_call" and r.get("label") in {"apo:tel-aux", "apo:tel-aux-b"}
            ]
            aux_a_hit = any(r.get("label") == "apo:tel-aux" and r.get("diagnosis") == "hit" for r in n11_tail_aux)
            aux_b_first = any(r.get("label") == "apo:tel-aux-b" and r.get("diagnosis") == "first_call" for r in n11_tail_aux)
            check("N11 per-reference telemetry logs hit and miss diagnoses cleanly and renders bilingual /true-moa cache",
                  n11_reasons.get("first_call", 0) >= 2 and n11_reasons.get("hit", 0) >= 2
                  and n11_reasons.get("miss_provider_relay", 0) >= 1
                  and aux_a_hit and aux_b_first
                  and "apo:tel-ref" in cache_en and "thống kê cache" in cache_vi.lower()
                  and "lượt" in cache_vi.lower() and "call(s)" not in cache_vi.lower(),
                  f"reasons={n11_reasons}, aux_hit={aux_a_hit}, aux_b={aux_b_first}")

            # N12: Smart Adaptive Fan-out via MoAChatCompletions.create() skips mechanical reads and refreshes on error / interval
            optimize.reset_trackers()
            adapt_calls = {"n": 0}
            orig_call_llm = moa_loop.call_llm
            orig_agg_fn = moa_loop.MoAChatCompletions._call_prepared_aggregator

            def _adapt_refs(slots, ref_msgs, *a, **kw):
                adapt_calls["n"] += 1
                lbl = f"{slots[0].get('provider')}:{slots[0].get('model')}"
                return [(lbl, f"adaptive advice #{adapt_calls['n']}", moa_loop._RefAccounting(moa_loop.CanonicalUsage(input_tokens=500, output_tokens=80)))]

            moa_loop._run_references_parallel = _adapt_refs
            moa_loop.MoAChatCompletions._call_prepared_aggregator = lambda self_agg, prepared_req, _kw: SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="agg ok", tool_calls=None), finish_reason="stop")],
                usage=moa_loop.CanonicalUsage(),
            )
            try:
                state.save({"enabled": True, "adaptive_fanout": True, "adaptive_max_interval": 3, "adaptive_cache_keepalive_s": 0})
                adapt_preset_override = {
                    "enabled": True,
                    "reference_models": [{"provider": "apo", "model": "adapt-ref", "enabled": True}],
                    "aggregator": {"provider": "apo", "model": "adapt-agg"},
                    "fanout": "per_iteration",
                    "degraded_reference_policy": "loud",
                }
                orig_resolve_cached = moa_loop._resolve_preset_cached
                moa_loop._resolve_preset_cached = lambda _name: (adapt_preset_override, {"moa": {"privacy_filter": ""}})
                try:
                    adapt_client = moa_loop.MoAChatCompletions("adapt-test", reference_callback=None, agent=None)
                    m1 = [{"role": "user", "content": "fix bug in repo"}]
                    adapt_client.create(messages=m1)
                    calls_after_t1 = adapt_calls["n"]

                    m2 = [*m1, {"role": "assistant", "content": "reading", "tool_calls": [{"function": {"name": "read_file", "arguments": "{}"}}]}, {"role": "tool", "content": "def error_handler(is_failed=False): return 0"}]
                    adapt_client.create(messages=m2)
                    calls_after_mech1 = adapt_calls["n"]

                    # Identical retry does not call advisors again or break skip count
                    adapt_client.create(messages=m2)
                    calls_after_retry = adapt_calls["n"]

                    # Tool error forces immediate refresh
                    m3_err = [*m2, {"role": "assistant", "content": "testing", "tool_calls": [{"function": {"name": "run_tests", "arguments": "{}"}}]}, {"role": "tool", "content": "Command exited with code 1\nAssertionError: broken"}]
                    adapt_client.create(messages=m3_err)
                    calls_after_err = adapt_calls["n"]

                    # After 3 consecutive mechanical steps, max_interval=3 triggers refresh
                    m4_ok = [*m3_err, {"role": "assistant", "content": "r1", "tool_calls": [{"function": {"name": "ls", "arguments": "{}"}}]}, {"role": "tool", "content": "ok1"}]
                    adapt_client.create(messages=m4_ok)
                    calls_after_step1 = adapt_calls["n"]
                    m5_ok = [*m4_ok, {"role": "assistant", "content": "r2", "tool_calls": [{"function": {"name": "ls", "arguments": "{}"}}]}, {"role": "tool", "content": "ok2"}]
                    adapt_client.create(messages=m5_ok)
                    calls_after_step2 = adapt_calls["n"]
                    m6_ok = [*m5_ok, {"role": "assistant", "content": "r3", "tool_calls": [{"function": {"name": "ls", "arguments": "{}"}}]}, {"role": "tool", "content": "ok3"}]
                    adapt_client.create(messages=m6_ok)
                    calls_after_interval = adapt_calls["n"]
                finally:
                    moa_loop._resolve_preset_cached = orig_resolve_cached
                    state.save({"adaptive_fanout": False, "adaptive_max_interval": 5, "adaptive_cache_keepalive_s": 240})
            finally:
                moa_loop.MoAChatCompletions._call_prepared_aggregator = orig_agg_fn
                moa_loop.call_llm = orig_call_llm

            check("N12 smart adaptive fan-out skips mechanical tool reads, preserves retries, and refreshes on tool errors and max interval",
                  calls_after_t1 == 1 and calls_after_mech1 == 1 and calls_after_retry == 1
                  and calls_after_err == 2 and calls_after_step1 == 2 and calls_after_step2 == 2 and calls_after_interval == 3,
                  f"seq={(calls_after_t1, calls_after_mech1, calls_after_retry, calls_after_err, calls_after_step1, calls_after_step2, calls_after_interval)}")

            # N13: Chunked prefix-safe trim pins boundary across turns + Concise Advisory injects static prompt without touching core
            optimize.reset_trackers()
            state.save({"enabled": True, "chunked_context_trim": True, "concise_advisory": True})
            base_sys_len = len(moa_loop._REFERENCE_SYSTEM_PROMPT)
            trim_slot = {"provider": "apo", "model": "trim-10k"}
            trim_runtime = {"provider": "apo", "model": "trim-10k", "base_url": "http://localhost:9999/v1"}
            ctx_cache = {("apo", "trim-10k"): 10500}
            trim_hist = [{"role": "system", "content": moa_loop._REFERENCE_SYSTEM_PROMPT}]
            for idx in range(18):
                trim_hist.append({"role": "user", "content": f"Turn {idx} prompt " + ("x" * 600)})
                trim_hist.append({"role": "assistant", "content": f"Turn {idx} reply " + ("y" * 600)})
            trimmed_a = moa_loop._trim_messages_for_reference(trim_hist, trim_slot, trim_runtime, reserve_output_tokens=1000, context_length_cache=ctx_cache)
            trimmed_b = moa_loop._trim_messages_for_reference(
                [*trim_hist, {"role": "user", "content": "Turn 18 prompt " + ("z" * 400)}, {"role": "assistant", "content": "Turn 18 reply " + ("w" * 400)}],
                trim_slot, trim_runtime, reserve_output_tokens=1000, context_length_cache=ctx_cache,
            )
            state.save({"concise_advisory": False})
            check("N13 chunked trim pins boundary prefix across consecutive turns and concise advisory preserves immutable core prompt",
                  len(moa_loop._REFERENCE_SYSTEM_PROMPT) == base_sys_len
                  and optimize.CONCISE_ADVISORY_MARKER in str(trimmed_a[0].get("content", ""))
                  and len(trimmed_a) >= 4
                  and trimmed_a[1] == trimmed_b[1]
                  and trimmed_b[:-2] == trimmed_a,
                  f"len_a={len(trimmed_a)}, len_b={len(trimmed_b)}, pin_match={trimmed_a[1] == trimmed_b[1] if len(trimmed_a) > 1 and len(trimmed_b) > 1 else False}")

            # N14: Empty/refusal guard rejects blank answers and Sticky Secondary Failover reuses warm backup then invalidates on death
            optimize.reset_trackers()
            empty_seq = {"n": 0}

            def _empty_then_ok(slots, *a, **kw):
                empty_seq["n"] += 1
                lbl = f"{slots[0].get('provider')}:{slots[0].get('model')}"
                txt = "(empty response)" if empty_seq["n"] == 1 else "I cannot access files." if empty_seq["n"] == 2 else "Concrete diagnosis and step."
                return [(lbl, txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]

            moa_loop._run_references_parallel = _empty_then_ok
            state.save({
                "enabled": True, "ladder": [0.02, 0.02], "guard_empty_responses": True,
                "enable_secondary_fallback": False,
            })
            emp_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            emp_out = emp_client._run_fanout(preset, messages, [{"provider": "apo", "model": "emp-ref", "enabled": True}], preset.get("aggregator") or {}, None, ("emp", 1))

            sticky_seq: list[str] = []

            def _sticky_scenario(slots, *a, **kw):
                lbl = f"{slots[0].get('provider')}:{slots[0].get('model')}"
                sticky_seq.append(lbl)
                # Turn 1 fails primary -> pins backup; Turn 2 reuses backup; Turn 3 backup dies -> recovers primary.
                if "prim-sticky" in lbl:
                    if len(sticky_seq) <= 2:
                        return [(lbl, "[failed: HTTP 503 down]", moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]
                    return [(lbl, "primary recovered after sticky invalidated", moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]
                if len(sticky_seq) <= 3:
                    return [(lbl, "warm sticky backup ok", moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]
                return [(lbl, "[failed: HTTP 503 backup down]", moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]

            moa_loop._run_references_parallel = _sticky_scenario
            state.save({
                "enabled": True, "ladder": [0.02, 0.02], "enable_secondary_fallback": True,
                "secondary_reference_model": "apo:sticky-sec", "fallback_trigger_attempts": 1,
                "fallback_sticky_s": 300,
            })
            st_client = moa_loop.MoAChatCompletions(preset_name, reference_callback=None, agent=None)
            st_slots = [{"provider": "apo", "model": "prim-sticky", "enabled": True}]
            t1_out = st_client._run_fanout(preset, messages, st_slots, preset.get("aggregator") or {}, None, ("st1", 1))
            t2_out = st_client._run_fanout(preset, messages, st_slots, preset.get("aggregator") or {}, None, ("st2", 1))
            t3_out = st_client._run_fanout(preset, messages, st_slots, preset.get("aggregator") or {}, None, ("st3", 1))

            verdict_n14_ok = True
            verdict_e2e_ok = True
            if callable(verdict_fn):
                optimize.reset_trackers()
                state.save({
                    "enabled": True, "ladder": [5.0], "guard_empty_responses": True,
                    "enable_secondary_fallback": False, "secondary_reference_model": "",
                })
                verdict_empty = verdict_fn(
                    preset_name="n14-empty",
                    preset_config={"reference_models": [{"provider": "apo", "model": "v-emp", "enabled": True}]},
                    aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "v-emp", "enabled": True}],
                    reference_outputs=[{"label": "apo:v-emp", "text": "(empty response)", "failed": False}],
                    attempt=1,
                )
                optimize.STICKY_FALLBACK_TRACKER.pin(
                    ("apo", "v-prim"),
                    {"provider": "apo", "model": "v-warm", "enabled": True},
                    ttl_s=300.0,
                )
                state.save({
                    "enabled": True, "ladder": [5.0], "enable_secondary_fallback": True,
                    "secondary_reference_model": "apo:v-warm", "fallback_trigger_attempts": 3,
                    "fallback_sticky_s": 300,
                })
                verdict_sticky = verdict_fn(
                    preset_name="n14-sticky",
                    preset_config={"reference_models": [{"provider": "apo", "model": "v-prim", "enabled": True}]},
                    aggregator={"provider": "apo", "model": "agg"},
                    active_slots=[{"provider": "apo", "model": "v-prim", "enabled": True}],
                    reference_outputs=[{"label": "apo:v-prim", "text": "[failed: HTTP 503]", "failed": True}],
                    attempt=1,
                )
                verdict_n14_ok = (
                    isinstance(verdict_empty, dict) and verdict_empty.get("action") == "retry"
                    and isinstance(verdict_sticky, dict) and verdict_sticky.get("action") == "retry"
                    and float(verdict_sticky.get("delay_s", -1.0)) == 0.0
                    and (verdict_sticky.get("slots") or [{}])[0].get("model") == "v-warm"
                )
                optimize.reset_trackers()
                verdict_e2e_ok = False
                state.save({
                    "enabled": True, "ladder": [0.01, 0.01], "guard_empty_responses": True,
                    "enable_secondary_fallback": False, "secondary_reference_model": "",
                })
                prev_hook_verdict = hermes_plugins.get_moa_fanout_verdict
                try:
                    hermes_plugins.get_moa_fanout_verdict = verdict_fn
                    e2e_1 = {"n": 0}

                    def _e2e_empty_1(_slots):
                        e2e_1["n"] += 1
                        txt = "(empty response)" if e2e_1["n"] == 1 else "Clean retry answer"
                        return [("apo:v-e2e1", txt, moa_loop._RefAccounting(moa_loop.CanonicalUsage()))]

                    out_1, _c1 = moa_loop._run_fanout_with_verdict(
                        preset_name="n14-e2e1", preset_config={"reference_models": [{"provider": "apo", "model": "v-e2e1", "enabled": True}]},
                        aggregator={"provider": "apo", "model": "agg"}, initial_slots=[{"provider": "apo", "model": "v-e2e1", "enabled": True}],
                        agent=None, run_batch=_e2e_empty_1,
                    )
                    e2e_2 = {"n": 0}

                    def _e2e_empty_2(_slots):
                        e2e_2["n"] += 1
                        if e2e_2["n"] == 1:
                            return [
                                ("apo:v-a", "Good A", moa_loop._RefAccounting(moa_loop.CanonicalUsage())),
                                ("apo:v-b", "(empty response)", moa_loop._RefAccounting(moa_loop.CanonicalUsage())),
                            ]
                        return [
                            ("apo:v-a", "Good A2", moa_loop._RefAccounting(moa_loop.CanonicalUsage())),
                            ("apo:v-b", "Good B2", moa_loop._RefAccounting(moa_loop.CanonicalUsage())),
                        ]

                    two_slots = [
                        {"provider": "apo", "model": "v-a", "enabled": True},
                        {"provider": "apo", "model": "v-b", "enabled": True},
                    ]
                    out_2, _c2 = moa_loop._run_fanout_with_verdict(
                        preset_name="n14-e2e2", preset_config={"reference_models": two_slots},
                        aggregator={"provider": "apo", "model": "agg"}, initial_slots=two_slots,
                        agent=None, run_batch=_e2e_empty_2,
                    )
                    verdict_e2e_ok = (
                        len(out_1) == 1 and out_1[0][1] == "Clean retry answer"
                        and len(out_2) == 2 and [t[1] for t in out_2] == ["Good A2", "Good B2"]
                    )
                finally:
                    hermes_plugins.get_moa_fanout_verdict = prev_hook_verdict
                    optimize.reset_trackers()

            state.save({"enable_secondary_fallback": False, "secondary_reference_model": ""})
            check("N14 empty/refusal guard triggers retry and sticky secondary failover pins warm backup and invalidates cleanly on death",
                  empty_seq["n"] == 3 and "Concrete diagnosis" in emp_out[0][1]
                  and "warm sticky backup ok" in t1_out[0][1]
                  and sticky_seq == ["apo:prim-sticky", "apo:sticky-sec", "apo:sticky-sec", "apo:sticky-sec", "apo:prim-sticky"]
                  and "warm sticky backup ok" in t2_out[0][1]
                  and "primary recovered" in t3_out[0][1]
                  and verdict_n14_ok and verdict_e2e_ok,
                  f"empty_calls={empty_seq['n']}, sticky_seq={sticky_seq}, verdict_n14={verdict_n14_ok}, verdict_e2e={verdict_e2e_ok}")

            # N15: /true-moa smart CLI/slash updates, doctor cache_efficiency check, and Desktop UI 1.7.0 surface round-trip
            smart_res = slash.handle("smart concise on")
            rc_smart = cli.handle(argparse.Namespace(true_moa_command="smart", values=["interval", "7"]))
            smart_cfg = state.load()
            doc_struct = doctor.run_structured(lang="en")
            api_snap_170 = False
            if desktop_js_path.is_file() and api_py_path.is_file():
                js_src_170 = desktop_js_path.read_text(encoding="utf-8")
                s_err_170 = desktop_surface_findings(js_src_170)
                s_warn_170 = desktop_review_findings(js_src_170)
                patched_170 = api_mod.apply_settings_patch({"adaptive_fanout": True, "fallback_sticky_s": 180})
                _ = api_mod.apply_settings_patch({"adaptive_fanout": False, "concise_advisory": False, "adaptive_max_interval": 5, "fallback_sticky_s": 300})
                api_snap_170 = (s_err_170 == [] and s_warn_170 == [] and patched_170["settings"]["adaptive_fanout"] is True
                                and patched_170["settings"]["fallback_sticky_s"] == 180
                                and "reference_telemetry" in patched_170["telemetry"])
            doc_ids = {c["id"] for c in (doc_struct.get("checks") or [])}
            check("N15 slash/CLI smart commands, doctor cache_efficiency check, and Desktop 1.7.0 telemetry API roundtrip cleanly",
                  "on" in smart_res.lower() and rc_smart == 0 and smart_cfg["concise_advisory"] is True
                  and smart_cfg["adaptive_max_interval"] == 7 and "cache_efficiency" in doc_ids and bool(api_snap_170),
                  f"rc={rc_smart}, doc_ids={sorted(doc_ids)}, api_snap={api_snap_170}")
        finally:
            moa_loop._run_references_parallel = orig_run_refs
            state.bind_context(None)

        # -- teardown ------------------------------------------------------------------
        restored = gate.uninstall()
        check("I1 gate removed cleanly", restored.get("ok") and restored.get("restored") == 9,
              str(restored))
        check("I2 core attributes are the stock ones again",
              getattr(moa_loop.MoAChatCompletions._run_fanout, "__true_moa_original__", None) is None
              and getattr(moa_loop.MoAChatCompletions._fanout_cache_key, "__true_moa_original__", None) is None
              and getattr(moa_loop._trim_messages_for_reference, "__true_moa_original__", None) is None
              and getattr(conv_loop.build_api_request, "__true_moa_original__", None) is None)

        # J8: Telemetry retention prunes stale files > 90 days per report directory without private-field resets
        prune_probe_dir = temp_report_dir / "prune-probe"
        prune_probe_dir.mkdir(parents=True, exist_ok=True)
        stale_log = prune_probe_dir / "gate-20200101.jsonl"
        stale_log.write_text('{"event":"reference_retry"}\n', encoding="utf-8")
        try:
            report.REPORT_DIR = prune_probe_dir
            report.event("gate_RetentionProbe")
        finally:
            report.REPORT_DIR = temp_report_dir
        check("J8 report retention prunes logs older than 90 days across profile directories",
              not stale_log.exists(),
              f"stale_exists={stale_log.exists()}")

        # M1/M2: PM tooling-only declaration + plugin-data runtime storage & legacy migration
        pm_decl_mod = importlib.import_module("pm.plugin_declarations")
        decl = pm_decl_mod.read_python_declaration(PLUGIN_DIR)
        check("M1 pyproject.toml stays tooling-only so PM installs_packages is False",
              decl.installs_packages is False and decl.pyproject_installs is False,
              f"installs_packages={decl.installs_packages}, pyproject_installs={decl.pyproject_installs}")

        mig_sandbox = temp_report_dir / "migration-probe"
        fake_plugin_dir = mig_sandbox / "plugins" / "true-moa"
        fake_data_dir = mig_sandbox / "plugin-data" / "true-moa"
        (fake_plugin_dir / "reports").mkdir(parents=True, exist_ok=True)
        (fake_plugin_dir / "state.json").write_text('{"enabled": false, "custom_retries": 7}\n', encoding="utf-8")
        (fake_plugin_dir / "reports" / "gate-20261118.jsonl").write_text('{"event":"reference_retry"}\n', encoding="utf-8")

        migrated_ok = False
        if hasattr(state, "migrate_legacy_state") and hasattr(report, "migrate_legacy_reports"):
            state.migrate_legacy_state(fake_plugin_dir / "state.json", fake_data_dir / "state.json")
            report.migrate_legacy_reports(fake_plugin_dir / "reports", fake_data_dir / "reports")
            default_state_path = state.resolve_state_path(ignore_override=True)
            default_report_path = report.resolve_report_dir(ignore_override=True)
            migrated_ok = (
                not (fake_plugin_dir / "state.json").exists()
                and not (fake_plugin_dir / "reports").exists()
                and (fake_data_dir / "state.json").is_file()
                and '"custom_retries": 7' in (fake_data_dir / "state.json").read_text(encoding="utf-8")
                and (fake_data_dir / "reports" / "gate-20261118.jsonl").is_file()
                and "plugin-data" in default_state_path.parts
                and "plugins" not in default_state_path.parts
                and "plugin-data" in default_report_path.parts
                and "plugins" not in default_report_path.parts
            )
        check("M2 runtime storage resolves to plugin-data/true-moa and migrates legacy files out of plugin dir",
              migrated_ok,
              f"legacy_left={(fake_plugin_dir / 'state.json').exists() or (fake_plugin_dir / 'reports').exists()}")

        test_log_files = list(temp_report_dir.glob("gate-*.jsonl"))
        check("J6 test events written to isolated directory",
              len(test_log_files) > 0,
              f"{len(test_log_files)} test log file(s)")

        total_test_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in test_log_files)
        check("J7 selftest events isolated in temporary sandbox",
              total_test_lines >= 5 and report.REPORT_DIR == temp_report_dir,
              f"{total_test_lines} test event(s) recorded in sandbox")

    finally:
        try:
            gate.uninstall()
            optimize.reset_trackers()
        except Exception as exc:
            CHECKS.append(f"FAIL  cleanup: gate.uninstall failed — {exc}")
            FAILURES.append("cleanup: gate.uninstall")
        try:
            report.REPORT_DIR = orig_report_dir
        except Exception:
            pass

        # Unbind test context while sandbox paths and traps are still active
        try:
            state.bind_context(None)
        except Exception as exc:
            CHECKS.append(f"FAIL  cleanup: context unbind failed — {exc}")
            FAILURES.append("cleanup: context unbind")

        # Check safety: ensure real live files were NEVER modified by tests
        def short_h(h):
            return h[:8] if h else "absent"

        try:
            sha_cfg_after = hashlib.sha256(config_file_path.read_bytes()).hexdigest() if config_file_path.exists() else None
            sha_state_after = hashlib.sha256(live_state_file.read_bytes()).hexdigest() if live_state_file.exists() else None
            cfg_dirty = (sha_cfg_after != sha_cfg_before)
            state_dirty = (sha_state_after != sha_state_before)

            detail_cfg = f"{short_h(sha_cfg_before)}... vs {short_h(sha_cfg_after)}..."
            if cfg_dirty:
                detail_cfg += " [MODIFIED during run — review manually; concurrent Hermes write possible]"
            detail_state = f"{short_h(sha_state_before)}... vs {short_h(sha_state_after)}..."
            if state_dirty:
                detail_state += " [MODIFIED during run — review manually; concurrent Hermes write possible]"

            check("Z1 live config.yaml unmodified by selftest", not cfg_dirty, detail_cfg)
            check("Z2 live state.json unmodified by selftest", not state_dirty, detail_state)
            check("Z3 save_config trap never triggered", trap_hits["n"] == 0, f"{trap_hits['n']} hit(s)")
            plugin_tree_clean = not (PLUGIN_DIR / "state.json").exists() and not (PLUGIN_DIR / "reports").exists()
            check("Z4 plugin source directory stays free of state.json and reports/",
                  plugin_tree_clean,
                  f"state={(PLUGIN_DIR / 'state.json').exists()}, reports={(PLUGIN_DIR / 'reports').exists()}")
        except Exception as exc:
            CHECKS.append(f"FAIL  cleanup: hash verification failed — {exc}")
            FAILURES.append("cleanup: hash verification")

        # Restore original paths and untrap save_config last
        try:
            state.STATE_PATH = orig_state_path
            if orig_save_config is not None:
                hermes_cfg.save_config = orig_save_config
            if orig_get_verdict is not None:
                hermes_plugins.get_moa_fanout_verdict = orig_get_verdict
            if orig_allow_aux is not None:
                hermes_plugins.allow_auxiliary_main_agent_fallback = orig_allow_aux
        except Exception:
            pass

        shutil.rmtree(temp_report_dir, ignore_errors=True)

    print("\n".join(CHECKS))
    print("-" * 64)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print(f"all {len(CHECKS)} guarantees held — recovery validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
