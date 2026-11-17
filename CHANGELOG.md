# Changelog

All notable changes to the **True MoA** plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [1.3.0] - 2026-11-16

### Added
- **Secondary Reference Fallback** (opt-in, default off): `enable_secondary_fallback`, `secondary_reference_model`, and `fallback_trigger_attempts` let the gate switch to a secondary reference after repeated primary failures. A secondary matching the aggregator or using the `moa` provider is refused, and a dual outage still halts the turn — the strict fail-closed policy is unchanged.
- The canonical `selftest.py` carries the **44 guarantees** (J/K/Z groups) inherited from commit `dc302cc`, including a hard `save_config` trap and SHA-256 checks proving live `config.yaml` / `state.json` stay untouched. No guarantee is added or removed. Relative to `dc302cc` the only assertion change is `J1`, which tracks 1.3.0; the second duplicate `H2` check (`UI changes are visible ...`) is relabeled `H2b` so all 44 labels are distinct, with its assertion unchanged. The test tiers (CI static vs. local live) and the docs are aligned with that suite here.

### Fixed
- CI manifest validation derives the settings schema solely from `plugin.yaml` / `state.DEFAULTS` (PR #1 run `43656528162` failed on a stale hard-coded six-key set). Both key sets are compared for exact equality, not subset, so drift in either source fails the step.
- Split the test tiers: CI runs `ruff check .`, the manifest check and `tests/test_static_contract.py` **only** — it never collects the live suite, so no test is skipped there. The 44 live guarantees remain a local Hermes-environment check to report in the PR.
- `tests/test_gate.py` is now a thin wrapper around `selftest.py` instead of a verbatim copy of its 36-check predecessor. Measured with AST over both trees: `v1.2.0` had 36 `check()` calls (35 distinct labels, `H2` twice) and the suite now has 44 calls / 44 distinct labels. 33 assertion bodies are unchanged; `J1` tracks 1.3.0 and `J3` adds `enable_secondary_fallback` to the boolean set. The second duplicate `H2` check is relabeled `H2b` with an unchanged body, and 8 new labels are added (J4e, K1-K4, Z1-Z3). `J2`'s message was renamed from "six" to "nine fields" with an unchanged body. No recovery guarantee was dropped.
- Pinned `ruff==0.17.5`, `pyyaml==6.0.3`, `pytest==8.3.4` in CI and `[project.optional-dependencies].dev`; CI and CONTRIBUTING both use `ruff check .`.
- `selftest.py` resolves the MoA preset from `moa.default_preset`, falling back to the first preset with an enabled reference, instead of the removed hard-coded `main` preset. The live suite now runs on a stock config without `TRUE_MOA_TEST_PRESET`, and exits with an explicit error when no preset carries an enabled reference.
- Bilingual docs list all nine settings, report 44 guarantees, and give a verified Python 3.11 dev-venv recipe (Windows `py -3.11`, Linux/macOS `python3.11`, plus `ruamel.yaml` for the Hermes-core live suite).

---

## [1.2.0] - 2026-11-14

### Added
- **Sync with Hermes API Retries (`sync_hermes_retries`)**: Optional toggle in Settings > Plugins > true-moa that automatically inherits the retry count configured in Hermes Agent limits (`agent.api_max_retries`) and dynamically generates an adaptive backoff ladder.
- **Custom Reference Retries (`custom_retries`)**: Direct numerical attempt counter (e.g. 5, 13) that takes highest precedence over manual ladders and sync mode.
- **Adaptive Ladder Generator**: Progressive backoff schedule (`10s → 15s → 20s → 30s → 45s → 60s...`) supporting up to 25 attempts.
- **Source Attribution Telemetry**: `/true-moa status` and CLI output now clearly attribute the active ladder source (`[custom: N attempts]`, `[sync: N attempts]`, or `[manual: N attempts]`).
- **Telemetry Sandbox Isolation**: Selftest suite now runs in an isolated ephemeral directory, ensuring offline verification never pollutes operational telemetry files.
- Extended architectural recovery test suite to **36 guarantees**.

### Changed
- Clarified error message when the recovery ladder is exhausted: now displays the active ladder schedule to prevent user confusion with global agent settings.
- Raised internal ladder truncation ceiling from 10 to 24 steps in state normalizer.

---

## [1.1.0] - 2026-11-10

### Added
- Full Hermes Plugin manifest (`plugin.yaml`) with four declarative `config_schema` fields rendered natively in Hermes Desktop (**Settings > Plugins > true-moa**).
- Two-way settings synchronization: In-process `PluginContext` configuration binds live to `state.json` and CLI without drift.
- Unconditional failure cache clearing: Poisoned advisories containing `[failed: ...]` are purged immediately regardless of user configuration.
- Offline acceptance test suite (`selftest.py`) verifying 30 recovery guarantees.

### Changed
- Clarified `max_wait_budget_s` semantics: acts strictly as a cumulative sleep-budget cap between retries, rather than a hard total-turn RTO.

---

## [1.0.0] - 2026-11-09

### Added
- Initial release of **True MoA** plugin for Hermes Agent `>=0.23.0`.
- In-process monkeypatching of 5 critical Hermes seams (`moa.prepare`, `moa.create`, `moa._run_fanout`, `moa._build_guidance`, `aux._try_main_agent_model_fallback`).
- Strict fail-closed policy: completely prevents the Aggregator model from generating standalone answers when reference models fail.
- Blocked Aggregator fallback: prevents auxiliary client from silently answering reference prompts with the aggregator model.
- Append-only gate telemetry logging (`reports/gate-YYYYMMDD.jsonl`).
- Interactive slash command `/true-moa` for on-the-fly toggling.
