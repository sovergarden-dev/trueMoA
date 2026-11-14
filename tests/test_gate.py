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
  H. a disarmed gate restores stock behaviour exactly.

Run:
    <hermes-env>/venv/Scripts/python.exe true-moa/selftest.py
Exit code 0 = every guarantee held.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent.parent
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
    original_state = state.load()
    test_ctx = _DummyCtx(dict(original_state))
    state.bind_context(test_ctx)
    state.save({"enabled": True, "sync_hermes_retries": False, "custom_retries": 0,
                "ladder": [0.2, 0.2], "cache_success_only": False,
                "block_aggregator_fallback": True, "max_wait_budget_s": 0.0})

    orig_report_dir = report.REPORT_DIR
    temp_report_dir = Path(tempfile.mkdtemp(prefix="true-moa-test-reports-"))
    live_reports_before = {p.name: p.stat().st_size for p in orig_report_dir.glob("gate-*.jsonl")} if orig_report_dir.exists() else {}
    report.REPORT_DIR = temp_report_dir

    try:
        manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        schema = manifest.get("config_schema") or {}
        expected_fields = {"enabled", "block_aggregator_fallback", "max_wait_budget_s", "ladder", "sync_hermes_retries", "custom_retries"}
        check("J1 manifest version is 1.2.0", manifest.get("version") == "1.2.0", str(manifest.get("version")))
        check("J2 Settings schema exposes exactly six fields", set(schema) == expected_fields, str(sorted(schema)))
        check("J3 gate, fallback and sync settings are boolean toggles",
              all(schema.get(key, {}).get("type") == "boolean"
                  for key in ("enabled", "block_aggregator_fallback", "sync_hermes_retries")))
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

        preset_name = os.environ.get("TRUE_MOA_TEST_PRESET", "main")
        preset, _raw = moa_loop._resolve_preset_cached(preset_name)
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

        # The aggregator call is where the turn stops: create() raises inside the
        # retry loop, so Hermes finalises the turn through its own error handler.
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
              "cache_success_only=false probe still cleared failure cache")

        # A new prepare() represents the next user turn. It must get the full
        # configured ladder, not inherit a failure latch from the previous turn.
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
        check("H2 UI changes are visible to the gate without a reload", state.enabled())
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
        shutil.rmtree(temp_report_dir, ignore_errors=True)
        try:
            state.bind_context(test_ctx)
            state.save(original_state)
        except Exception as exc:
            CHECKS.append(f"FAIL  cleanup: state restoration failed — {exc}")
            FAILURES.append("cleanup: state restoration")

    print("\n".join(CHECKS))
    print("-" * 64)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: " + ", ".join(FAILURES))
        return 1
    print(f"all {len(CHECKS)} guarantees held — recovery validation passed")
    return 0


def test_all_guarantees():
    """Pytest entrypoint verifying all offline recovery guarantees."""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
