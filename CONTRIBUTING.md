# Contributing to True MoA

Thank you for your interest in contributing to **True MoA**! 

True MoA is designed with strict reliability standards to safeguard multi-agent architectures. We welcome contributions that improve reliability, broaden test coverage, or enhance user experience.

---

## Code of Conduct

Please maintain a collaborative, respectful, and constructive environment in all issues, pull requests, and discussions.

---

## Development & Testing Workflow

### Prerequisites
- Python 3.10+ (CI pins Python 3.11; tested locally on 3.11)
- Hermes Agent `>=0.23.0` installed locally (for integration tests)

### Running Offline Verification
True MoA ships with two test tiers, neither making live network calls:

```bash
# One-time dev setup (Linux/macOS: python3.11 + .venv/bin/; Windows: py -3.11 + .venv\Scripts\)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt "ruamel.yaml>=0.18,<0.19"

# Core-independent contract tests — the exact CI step
.venv/bin/python -m pytest tests/test_static_contract.py -q -rs

# Live 54-guarantee acceptance suite — needs hermes_cli + agent on PYTHONPATH
PYTHONPATH=<hermes-workspace> .venv/bin/python selftest.py
PYTHONPATH=<hermes-workspace> .venv/bin/python -m pytest tests/ -q -rs
```

`<hermes-workspace>` is the directory containing `hermes_cli/` and `agent/`. `ruamel.yaml` is required only by the live suite because Hermes core imports it at startup. CI runs `ruff check .` and `tests/test_static_contract.py` **only** — it never collects the live suite, so no test is skipped in CI. The 54-guarantee live suite must be run locally in a Hermes environment and reported in the PR; without `PYTHONPATH` its pytest wrapper is skipped with an explicit reason.

Every PR must pass all applicable architectural test guarantees with zero regressions.

---

## Guidelines for Changes

1. **Zero Core Modifications:** True MoA must operate strictly as an in-process plugin. Do not introduce requirements that modify Hermes core files.
2. **Fail-Closed Invariant:** The Aggregator model must never be allowed to answer alone when reference models fail, unless the user explicitly configures a degraded reference policy.
3. **Cache Protection:** Faulty reference results (such as `[failed: ...]`) must never be cached across turns or iterations.
4. **Bilingual Documentation:** If introducing new configuration schema fields, update both `README.md` (English) and `README.vi.md` (Vietnamese).

---

## Submitting Pull Requests

1. Fork the repository and create your feature branch:
   ```bash
   git checkout -b feature/my-enhancement
   ```
2. Ensure code passes linting and tests:
   ```bash
   ruff check .
   .venv/bin/python -m pytest tests/test_static_contract.py -q -rs
   PYTHONPATH=<hermes-workspace> .venv/bin/python selftest.py
   ```
3. Commit your changes with clear, descriptive commit messages.
4. Open a Pull Request against `main` describing the rationale and test evidence.
