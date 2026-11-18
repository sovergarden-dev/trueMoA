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

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from true_moa import gate, report, state

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
    import hermes_cli.config as hermes_cfg
    default_home = Path.home() / (".hermes" if os.name != "nt" else Path("AppData") / "Local" / "hermes")
    hermes_home = Path(os.environ.get("HERMES_HOME", default_home))
    config_file_path = hermes_home / "config.yaml"
    live_state_file = hermes_home / "plugins" / "true-moa" / "state.json"
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
    original_state = state.load()
    test_ctx = _DummyCtx(dict(original_state))
    state.bind_context(test_ctx)

    orig_report_dir = report.REPORT_DIR
    temp_report_dir = Path(tempfile.mkdtemp(prefix="true-moa-test-reports-"))
    live_reports_before = {p.name: p.stat().st_size for p in orig_report_dir.glob("gate-*.jsonl")} if orig_report_dir.exists() else {}
    report.REPORT_DIR = temp_report_dir
    state.STATE_PATH = temp_report_dir / "state.json"

    state.save({"enabled": True, "sync_hermes_retries": False, "custom_retries": 0,
                "ladder": [0.2, 0.2],
                "block_aggregator_fallback": True, "max_wait_budget_s": 0.0})

    try:
        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest.get("config_schema") or {}
        expected_fields = {
            "enabled", "block_aggregator_fallback", "max_wait_budget_s", "ladder",
            "sync_hermes_retries", "custom_retries", "enable_secondary_fallback",
            "secondary_reference_model", "fallback_trigger_attempts"
        }
        check("J1 manifest version matches gate PLUGIN_VERSION",
              manifest.get("version") == gate.PLUGIN_VERSION,
              f"{manifest.get('version')} vs {gate.PLUGIN_VERSION}")
        check("J2 Settings schema exposes exactly nine fields", set(schema) == expected_fields, str(sorted(schema)))
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
        check("A2 every seam wrapped", len(result.get("wrapped", [])) == 5,
              str(result.get("wrapped")))

        import agent.auxiliary_client as aux_client
        import agent.moa_loop as moa_loop
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
        finally:
            moa_loop._run_references_parallel = orig_run_refs
            state.bind_context(None)

        # -- teardown ------------------------------------------------------------------
        restored = gate.uninstall()
        check("I1 gate removed cleanly", restored.get("ok") and restored.get("restored") == 5,
              str(restored))
        check("I2 core attributes are the stock ones again",
              getattr(moa_loop.MoAChatCompletions._run_fanout, "__true_moa_original__", None) is None)

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
