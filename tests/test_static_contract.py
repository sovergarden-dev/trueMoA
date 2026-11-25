"""Core-independent contract tests.

These run in plain CI (no Hermes install). They validate declarative
configuration and pure functions from ``true_moa.state``; the live-seam
acceptance suite remains in ``selftest.py`` / ``tests/test_gate.py``.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
from pathlib import Path

import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "plugin.yaml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
GATE_PY = REPO_ROOT / "true_moa" / "gate.py"
STATE_PY = REPO_ROOT / "true_moa" / "state.py"
CLI_PY = REPO_ROOT / "true_moa" / "cli.py"


def _state():
    """Load state.py standalone (stdlib only); never import the package or Hermes."""
    spec = importlib.util.spec_from_file_location("true_moa_state_contract", STATE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _defaults() -> dict:
    """Parse DEFAULTS statically; importing state is safe but keep CI pure."""
    tree = ast.parse(STATE_PY.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "DEFAULTS"
    )
    assert node.value is not None
    return dict(ast.literal_eval(node.value))


def test_manifest_identity() -> None:
    m = _manifest()
    assert m["name"] == "true-moa"
    version = str(m["version"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    gate_tree = ast.parse(GATE_PY.read_text(encoding="utf-8"))
    gate_version = next(
        ast.literal_eval(n.value)
        for n in gate_tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PLUGIN_VERSION" for t in n.targets)
    )
    assert gate_version == version
    assert m.get("provides_hooks") == ["transform_api_error_classification"]
    assert m.get("provides_commands") == ["true-moa"]


def test_pyproject_is_tooling_only() -> None:
    """Keep pyproject.toml tooling-only so Hermes PM never treats true-moa as a uv workspace member."""
    pyproj = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    for forbidden_table in ("project", "build-system", "dependency-groups"):
        assert forbidden_table not in pyproj, f"pyproject.toml must not declare [{forbidden_table}]"
    assert "tool" in pyproj and "ruff" in pyproj["tool"]


def test_schema_matches_runtime_defaults() -> None:
    """The single-source-of-truth invariant behind the previous CI failure."""
    schema = _manifest()["config_schema"]
    defaults = _defaults()
    assert set(schema) == set(defaults)
    assert len(defaults) == 18
    assert _state().DEFAULTS == defaults, "runtime DEFAULTS drifted from source"
    assert defaults["language"] == "en"
    assert schema["language"].get("choices") == ["en", "vi"]
    assert defaults["min_healthy_references"] == 0
    assert defaults["adaptive_fanout"] is False
    assert defaults["chunked_context_trim"] is True
    assert defaults["concise_advisory"] is False
    assert defaults["guard_empty_responses"] is True
    assert defaults["fallback_sticky_s"] == 300
    for key, spec in schema.items():
        assert spec["default"] == defaults[key], key
    raw_manifest = MANIFEST.read_text(encoding="utf-8")
    for mixed_vi_token in ("Khi bật", "Số lần", "Đặt 0"):
        assert mixed_vi_token not in raw_manifest, f"plugin.yaml should be pure English, found {mixed_vi_token!r}"


def test_boolean_toggles() -> None:
    schema = _manifest()["config_schema"]
    for key in (
        "enabled",
        "block_aggregator_fallback",
        "sync_hermes_retries",
        "enable_secondary_fallback",
        "adaptive_fanout",
        "chunked_context_trim",
        "concise_advisory",
        "guard_empty_responses",
    ):
        assert schema[key]["type"] == "boolean", key


def test_secondary_fallback_defaults_are_safe() -> None:
    defaults = _defaults()
    assert defaults["enable_secondary_fallback"] is False
    assert defaults["secondary_reference_model"] == ""
    assert 1 <= defaults["fallback_trigger_attempts"] <= 10


def test_reference_model_parser_rejects_unsafe_shapes() -> None:
    parse = _state().parse_reference_model_string
    for raw in ("", "   ", "no-colon", ":model", "provider:", ":", 123, None,
                {}, {"provider": "p"}, {"model": "m"}):
        assert parse(raw) is None, repr(raw)
    assert parse("provider:model") == {
        "provider": "provider", "model": "model", "enabled": True,
    }
    assert parse({"provider": "p", "model": "m"}) == {
        "provider": "p", "model": "m", "enabled": True,
    }
    parse_chain = _state().parse_reference_model_chain
    assert parse_chain("bdd:gemini-3.8-flash, beat:deepseek-v4.1-flash-free, bdd:gemini-3.8-flash") == [
        {"provider": "bdd", "model": "gemini-3.8-flash", "enabled": True},
        {"provider": "beat", "model": "deepseek-v4.1-flash-free", "enabled": True},
    ]
    assert parse_chain("") == []


def test_cli_parser_accepts_ladder_and_fallback_arguments() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import cli

    parser = argparse.ArgumentParser(prog="hermes true-moa")
    cli.setup_parser(parser)
    ns_ladder = parser.parse_args(["ladder", "10,20,40"])
    assert ns_ladder.true_moa_command == "ladder"
    assert ns_ladder.values == ["10,20,40"]
    ns_fb = parser.parse_args(["fallback", "2", "bdd:gemini-3.8-flash"])
    assert ns_fb.true_moa_command == "fallback"
    assert ns_fb.values == ["2", "bdd:gemini-3.8-flash"]
    ns_quorum = parser.parse_args(["quorum", "2"])
    assert ns_quorum.true_moa_command == "quorum"
    assert ns_quorum.values == ["2"]
    ns_doc = parser.parse_args(["doctor"])
    assert ns_doc.true_moa_command == "doctor"
    ns_lang = parser.parse_args(["lang", "vi"])
    assert ns_lang.true_moa_command == "lang"
    assert ns_lang.values == ["vi"]
    ns_smart = parser.parse_args(["smart", "interval", "6"])
    assert ns_smart.true_moa_command == "smart"
    assert ns_smart.values == ["interval", "6"]
    ns_cache = parser.parse_args(["cache"])
    assert ns_cache.true_moa_command == "cache"


def test_bilingual_catalog_and_unified_desktop_surface() -> None:
    import json
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import i18n

    assert i18n.SUPPORTED_LANGUAGES == ("en", "vi")
    assert set(i18n.MESSAGES["en"]) == set(i18n.MESSAGES["vi"])
    assert len(i18n.MESSAGES["en"]) >= 25
    assert callable(getattr(_state(), "is_supported_language", None))
    assert _state().is_supported_language("en") is True
    assert _state().is_supported_language("en-au") is True
    assert _state().is_supported_language("vi") is True
    assert _state().is_supported_language("Tiếng Việt") is True
    assert _state().is_supported_language("fr") is False
    assert _state().is_supported_language("") is False
    assert i18n.normalize_language(None) == "en"
    assert i18n.normalize_language("English") == "en"
    assert i18n.normalize_language("vi") == "vi"
    assert i18n.normalize_language("Tiếng Việt") == "vi"

    desktop_js = REPO_ROOT / "desktop" / "plugin.js"
    dash_manifest = REPO_ROOT / "dashboard" / "manifest.json"
    dash_api = REPO_ROOT / "dashboard" / "plugin_api.py"
    assert desktop_js.is_file()
    assert dash_manifest.is_file()
    assert dash_api.is_file()
    dm = json.loads(dash_manifest.read_text(encoding="utf-8"))
    assert dm["name"] == "true-moa" and dm["api"] == "plugin_api.py"


def test_non_retryable_classifier_distinguishes_config_errors_from_transient() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import gate

    classify = getattr(gate, "is_non_retryable_failure", None)
    assert callable(classify), "gate.is_non_retryable_failure missing"
    assert classify("[failed: Error code: 401 - {'error': {'message': 'Invalid API key'}}]") is True
    assert classify("[failed: Error code: 404 - model 'ghost-v9' does not exist]") is True
    assert classify("[failed: HTTP 403 Forbidden: permission_denied]") is True
    assert classify("[failed: HTTP 403 Forbidden: Cloudflare Ray ID 8abc - Just a moment]") is False
    assert classify("[failed: Error code: 403 - daily quota exceeded]") is False
    assert classify("[failed: HTTP 403 Forbidden]") is False
    assert classify("[failed: Error code: 429 - Rate limit reached]") is False
    assert classify("[failed: Connection timed out after 60s]") is False
    assert classify("[failed: HTTP 503 Service Unavailable]") is False


def test_generate_ladder_is_deterministic() -> None:
    generate = _state().generate_ladder
    assert generate(1) == []
    assert generate(5) == [10.0, 15.0, 20.0, 30.0]
    ladder = generate(25)
    assert len(ladder) == 24
    assert ladder == generate(25)
    assert all(isinstance(step, float) for step in ladder)


def test_prefix_tracker_diagnoses_all_causes() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import optimize

    tracker = optimize.PrefixTracker(max_entries=16)
    base_msgs = [
        {"role": "system", "content": "advisor system"},
        {"role": "user", "content": "check module A " * 100},
    ]
    d1 = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=base_msgs, usage={"input_tokens": 2000, "output_tokens": 200}, latency_ms=800, now=100.0,
    )
    assert d1["diagnosis"] == "first_call"

    d2 = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=[*base_msgs, {"role": "assistant", "content": "step 1"}],
        usage={"input_tokens": 200, "cache_read_tokens": 1800, "output_tokens": 150}, latency_ms=500, now=120.0,
    )
    assert d2["diagnosis"] == "hit" and d2["cache_hit_pct"] == 90.0 and d2["prefix_match_pct"] == 100.0

    d_partial = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=[*base_msgs, {"role": "assistant", "content": "step 1"}],
        usage={"input_tokens": 1400, "cache_read_tokens": 600}, latency_ms=500, now=140.0,
    )
    assert d_partial["diagnosis"] == "partial_hit"

    d_ttl = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=[*base_msgs, {"role": "assistant", "content": "step 1"}, {"role": "user", "content": "next"}],
        usage={"input_tokens": 2200, "cache_read_tokens": 0}, latency_ms=900, now=500.0,
    )
    assert d_ttl["diagnosis"] == "miss_ttl_expired"

    d_relay = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=[*base_msgs, {"role": "assistant", "content": "step 1"}, {"role": "user", "content": "next"}],
        usage={"input_tokens": 2200, "cache_read_tokens": 0}, latency_ms=700, now=530.0,
    )
    assert d_relay["diagnosis"] == "miss_provider_relay"

    d_shift = tracker.observe(
        preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
        messages=[{"role": "system", "content": "advisor system"}, {"role": "user", "content": "different head"}],
        usage={"input_tokens": 1800, "cache_read_tokens": 0}, latency_ms=600, now=540.0,
    )
    assert d_shift["diagnosis"] == "miss_prefix_shifted"

    d_switch = tracker.observe(
        preset="1", label="bdd:gemini", model_key="bdd:gemini", slot_index=0,
        messages=[{"role": "system", "content": "advisor system"}, {"role": "user", "content": "different head"}],
        usage={"input_tokens": 1800, "cache_read_tokens": 0}, latency_ms=600, now=550.0,
    )
    assert d_switch["diagnosis"] == "miss_model_switched"

    d_short = tracker.observe(
        preset="1", label="bdd:gemini", model_key="bdd:gemini", slot_index=0,
        messages=[{"role": "system", "content": "advisor system"}, {"role": "user", "content": "different head"}],
        usage={"input_tokens": 400, "cache_read_tokens": 0}, latency_ms=300, now=560.0,
    )
    assert d_short["diagnosis"] == "too_short"


def test_adaptive_fanout_tracker_skip_and_refresh_signals() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import optimize

    tracker = optimize.AdaptiveFanoutTracker()
    instr = "[The conversation above is the current state...]"
    slots = ("bp:deepseek",)

    iter1 = [{"role": "user", "content": "debug test suite"}]
    act, reason, snap = tracker.evaluate(
        facade_id=1, ref_messages=iter1, slots_sig=slots, advisory_instruction=instr,
        has_cached_outputs=False, max_interval=4, keepalive_s=240, now=100.0,
    )
    assert (act, reason) == ("refresh", "no_cache")
    tracker.mark_refreshed(1, snap)

    # Benign read result containing error_handler code snippet skips as mechanical step
    iter2 = [
        {"role": "user", "content": "debug test suite"},
        {"role": "assistant", "content": "[called tool: read_file('x.py')]\n[tool result: def error_handler(is_failed=False):\n    return 0]"},
        {"role": "user", "content": instr},
    ]
    act2, reason2, _ = tracker.evaluate(
        facade_id=1, ref_messages=iter2, slots_sig=slots, advisory_instruction=instr,
        has_cached_outputs=True, max_interval=4, keepalive_s=240, now=110.0,
    )
    assert (act2, reason2) == ("reuse", "mechanical_step")

    # Identical retry does NOT advance steps_since_refresh or emit skip
    act2_retry, reason2_retry, _ = tracker.evaluate(
        facade_id=1, ref_messages=iter2, slots_sig=slots, advisory_instruction=instr,
        has_cached_outputs=True, max_interval=4, keepalive_s=240, now=112.0,
    )
    assert (act2_retry, reason2_retry) == ("reuse", "identical_retry")

    # Short tool error appended BEFORE trailing synthetic instruction triggers refresh
    iter3_err = [
        {"role": "user", "content": "debug test suite"},
        {"role": "assistant", "content": iter2[1]["content"] + "\n[called tool: pytest]\n[tool result: Command exited with code 1\nFAILED tests/a.py]"},
        {"role": "user", "content": instr},
    ]
    act3, reason3, snap3 = tracker.evaluate(
        facade_id=1, ref_messages=iter3_err, slots_sig=slots, advisory_instruction=instr,
        has_cached_outputs=True, max_interval=4, keepalive_s=240, now=120.0,
    )
    assert (act3, reason3) == ("refresh", "tool_error")
    tracker.mark_refreshed(1, snap3)

    # Keepalive TTL refresh when idle time exceeds keepalive_s
    iter4_ok = [
        {"role": "user", "content": "debug test suite"},
        {"role": "assistant", "content": iter3_err[1]["content"] + "\n[called tool: read_file('y.py')]\n[tool result: ok]"},
        {"role": "user", "content": instr},
    ]
    act4, reason4, snap4 = tracker.evaluate(
        facade_id=1, ref_messages=iter4_ok, slots_sig=slots, advisory_instruction=instr,
        has_cached_outputs=True, max_interval=4, keepalive_s=240, now=400.0,
    )
    assert (act4, reason4) == ("refresh", "ttl_keepalive")
    tracker.mark_refreshed(1, snap4)

    # Shifted/shrunk context triggers refresh
    act_shrink, reason_shrink, _ = tracker.evaluate(
        facade_id=1, ref_messages=[{"role": "user", "content": "debug test suite"}], slots_sig=slots,
        advisory_instruction=instr, has_cached_outputs=True, max_interval=4, keepalive_s=240, now=410.0,
    )
    assert (act_shrink, reason_shrink) == ("refresh", "context_shrunk")


def test_chunked_trim_preserves_prefix_and_isolates_sessions() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import optimize

    trimmer = optimize.ChunkedTrimTracker(max_slots=8, max_anchors_per_slot=8)
    sys_msg = {"role": "system", "content": "system prompt"}
    pairs = []
    for i in range(10):
        pairs.append({"role": "user", "content": f"user turn {i}"})
        pairs.append({"role": "assistant", "content": f"assistant turn {i}"})
    msgs_t1 = [sys_msg, *pairs]
    weights_t1 = [100] * len(msgs_t1)  # total = 2100, budget = 1400 -> target = 1050

    trimmed_1, dropped_1, pin_1 = trimmer.trim(
        msgs_t1, weights_t1, 1400, slot_key=("https://api.b.ai/v1", "deepseek-v4.1-flash"),
    )
    assert pin_1 is False and dropped_1 > 0 and trimmed_1[0]["role"] == "system" and trimmed_1[1]["role"] == "user"

    # Next turn appends one pair -> 1200 <= 1400 if reusing pinned boundary!
    msgs_t2 = [
        *msgs_t1,
        {"role": "user", "content": "user turn 10"},
        {"role": "assistant", "content": "assistant turn 10"},
    ]
    weights_t2 = [100] * len(msgs_t2)
    trimmed_2, _dropped_2, pin_2 = trimmer.trim(
        msgs_t2, weights_t2, 1400, slot_key=("https://api.b.ai/v1", "deepseek-v4.1-flash"),
    )
    assert pin_2 is True and trimmed_2[:-2] == trimmed_1

    # Second concurrent session on the SAME model must not evict session 1's pin
    msgs_other = [sys_msg, *[{"role": ("user" if j % 2 == 0 else "assistant"), "content": f"other {j}"} for j in range(18)]]
    _, _, pin_other = trimmer.trim(msgs_other, [100] * len(msgs_other), 1400, slot_key=("https://api.b.ai/v1", "deepseek-v4.1-flash"))
    assert pin_other is False
    trimmed_3, _, pin_3 = trimmer.trim(msgs_t2, weights_t2, 1400, slot_key=("https://api.b.ai/v1", "deepseek-v4.1-flash"))
    assert pin_3 is True and trimmed_3 == trimmed_2


def test_concise_prompt_empty_guard_and_sticky_failover() -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import optimize

    orig = [{"role": "system", "content": "Base prompt"}, {"role": "user", "content": "hi"}]
    concise_1 = optimize.apply_concise_system_prompt(orig)
    concise_2 = optimize.apply_concise_system_prompt(concise_1)
    assert optimize.CONCISE_ADVISORY_MARKER not in orig[0]["content"]
    assert optimize.CONCISE_ADVISORY_MARKER in concise_1[0]["content"]
    assert concise_2[0]["content"] == concise_1[0]["content"]

    assert optimize.is_empty_or_refusal_response("") is True
    assert optimize.is_empty_or_refusal_response("  \n ") is True
    assert optimize.is_empty_or_refusal_response("(empty response)") is True
    assert optimize.is_empty_or_refusal_response("I cannot access the repository or run commands.") is True
    assert optimize.is_empty_or_refusal_response("ok") is False
    assert optimize.is_empty_or_refusal_response("Check `gate.py` first and run `pytest`.") is False

    sticky = optimize.StickyFallbackTracker()
    prim = ("url:https://api.b.ai/v1", "deepseek-v4.1-flash")
    sec = {"provider": "bdd", "model": "gemini-3.8-flash", "enabled": True}
    sticky.pin(prim, sec, ttl_s=60.0, now=100.0)
    assert sticky.get(prim, now=150.0) == sec
    assert sticky.active_count(now=150.0) == 1
    assert sticky.get(prim, now=161.0) is None
    sticky.pin(prim, sec, ttl_s=60.0, now=200.0)
    sticky.invalidate(prim)
    assert sticky.get(prim, now=210.0) is None


def test_report_reference_telemetry_aggregates_cleanly(tmp_path: Path) -> None:
    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from true_moa import report

    orig_dir = report.REPORT_DIR
    report.REPORT_DIR = tmp_path
    try:
        report.event(
            "reference_call", preset="1", label="bp:deepseek", model_key="bp:deepseek", slot_index=0,
            input_tokens=200, cache_read_tokens=1800, cache_write_tokens=0,
            output_tokens=300, reasoning_tokens=150, cache_hit_pct=90.0,
            prefix_match_pct=100.0, latency_ms=420, diagnosis="hit",
        )
        report.event("adaptive_fanout_skipped", preset="1", reason="mechanical_step")
        report.event("sticky_fallback_reused", preset="1", primary="p", secondary="s")
        report.event("chunked_trim_applied", provider="bp", model="deepseek", dropped=4, reused_pin=True)
        report.event("empty_response_rejected", preset="1", label="bp:deepseek")
        s = report.summary()
        assert s["counters"]["adaptive_fanout_skipped"] == 1
        assert s["counters"]["sticky_fallback_reused"] == 1
        assert s["counters"]["chunked_trim_applied"] == 1
        assert s["counters"]["empty_response_rejected"] == 1
        tel = s["reference_telemetry"]
        assert tel["calls"] == 1
        assert tel["cache_hit_pct"] == 90.0
        assert tel["avg_output_tokens"] == 300
        assert tel["avg_reasoning_pct"] == 50.0
        assert tel["avg_prefix_match_pct"] == 100.0
        assert tel["by_model"][0]["top_diagnosis"] == "hit"
    finally:
        report.REPORT_DIR = orig_dir
