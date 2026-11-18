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
import hashlib
import importlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from true_moa import cli, gate, report, slash, state

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
            "enabled", "block_aggregator_fallback", "max_wait_budget_s", "ladder",
            "sync_hermes_retries", "custom_retries", "enable_secondary_fallback",
            "secondary_reference_model", "fallback_trigger_attempts", "min_healthy_references",
        }
        check("J1 manifest version matches gate PLUGIN_VERSION",
              manifest.get("version") == gate.PLUGIN_VERSION,
              f"{manifest.get('version')} vs {gate.PLUGIN_VERSION}")
        check("J2 Settings schema exposes exactly ten fields", set(schema) == expected_fields, str(sorted(schema)))
        check("J3 gate, fallback and sync settings are boolean toggles",
              all(schema.get(key, {}).get("type") == "boolean"
                  for key in ("enabled", "block_aggregator_fallback", "sync_hermes_retries", "enable_secondary_fallback")))
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
              and "0" in budget_description and "request reference" in budget_description
              and "sleep" in budget_label and "rto" not in budget_label)

        # -- A: compatibility + patch -------------------------------------------------
        result = gate.install(force=True)
        check("A1 gate verified against live core", bool(result.get("ok")),
              result.get("error", "") or f"core {result.get('core_version')}")
        check("A2 every seam wrapped", len(result.get("wrapped", [])) == 7,
              str(result.get("wrapped")))

        aux_client = importlib.import_module("agent.auxiliary_client")
        moa_loop = importlib.import_module("agent.moa_loop")
        turn_assembly = importlib.import_module("agent.turn_request_assembly")
        turn_api_req = importlib.import_module("agent.turn_api_request")
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
        finally:
            moa_loop._run_references_parallel = orig_run_refs
            state.bind_context(None)

        # -- teardown ------------------------------------------------------------------
        restored = gate.uninstall()
        check("I1 gate removed cleanly", restored.get("ok") and restored.get("restored") == 7,
              str(restored))
        check("I2 core attributes are the stock ones again",
              getattr(moa_loop.MoAChatCompletions._run_fanout, "__true_moa_original__", None) is None)

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
