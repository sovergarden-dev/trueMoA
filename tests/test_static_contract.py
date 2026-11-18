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
    return ast.literal_eval(node.value)


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
    assert len(defaults) == 9
    assert _state().DEFAULTS == defaults, "runtime DEFAULTS drifted from source"
    for key, spec in schema.items():
        assert spec["default"] == defaults[key], key


def test_boolean_toggles() -> None:
    schema = _manifest()["config_schema"]
    for key in ("enabled", "block_aggregator_fallback",
                "sync_hermes_retries", "enable_secondary_fallback"):
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


def test_generate_ladder_is_deterministic() -> None:
    generate = _state().generate_ladder
    assert generate(1) == []
    assert generate(5) == [10.0, 15.0, 20.0, 30.0]
    ladder = generate(25)
    assert len(ladder) == 24
    assert ladder == generate(25)
    assert all(isinstance(step, float) for step in ladder)
