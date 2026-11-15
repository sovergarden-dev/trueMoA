"""Pytest entrypoint for the True MoA live acceptance suite.

The canonical suite lives in ``selftest.py`` at the repository root. This
module is deliberately a thin wrapper: ``python selftest.py`` and
``pytest tests/`` exercise the exact same guarantees instead of maintaining a
second copy of the checks (which is how the six-field schema expectation
drifted away from the runtime's nine fields).

``selftest.py`` wraps live Hermes seams, so it requires ``hermes_cli`` and
``agent`` on the import path. When they are absent this test is skipped with an
explicit reason; the core-independent guarantees live in
``tests/test_static_contract.py``. CI runs only that file and never collects
this wrapper, so no test is skipped in CI.

Run locally:
    PYTHONPATH=<hermes-workspace> .venv/bin/python -m pytest tests/ -q -rs
Exit code 0 = every applicable guarantee held.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CORE_AVAILABLE = all(
    importlib.util.find_spec(module) is not None
    for module in ("hermes_cli", "agent")
)


@pytest.mark.skipif(
    not _CORE_AVAILABLE,
    reason="live Hermes core (hermes_cli + agent) not available; "
           "run selftest.py inside a Hermes environment",
)
def test_all_guarantees() -> None:
    """All offline recovery guarantees in ``selftest.py`` must hold."""
    from selftest import main
    assert main() == 0
