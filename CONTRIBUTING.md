# Contributing to True MoA

Thank you for your interest in contributing to **True MoA**! 

True MoA is designed with strict reliability standards to safeguard multi-agent architectures. We welcome contributions that improve reliability, broaden test coverage, or enhance user experience.

---

## Code of Conduct

Please maintain a collaborative, respectful, and constructive environment in all issues, pull requests, and discussions.

---

## Development & Testing Workflow

### Prerequisites
- Python 3.10+ (tested on Python 3.10 - 3.13)
- Hermes Agent `>=0.23.0` installed locally (for integration tests)

### Running Offline Verification
True MoA ships with an offline acceptance test suite that exercises all architectural failover guarantees without making live network calls:

```bash
# Run test suite directly
python selftest.py

# Or via pytest
pytest tests/
```

Every PR must pass all architectural test guarantees with zero regressions.

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
   python -m ruff check true_moa/ tests/
   python selftest.py
   ```
3. Commit your changes with clear, descriptive commit messages.
4. Open a Pull Request against `main` describing the rationale and test evidence.
