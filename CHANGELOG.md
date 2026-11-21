# Changelog

All notable changes to the **True MoA** plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.6.2] - 2026-11-21

### Fixed
- Rebind `dashboard/plugin_api.py` submodules (`doctor`, `gate`, `i18n`, `report`, `state`) by live module object identity (`live_gate is not gate`) in addition to package name so `PluginManager.discover_and_load(force=True)` hot-reloads never leave the Dashboard API holding stale unloaded `gate` references (`N10`).

---

## [1.6.1] - 2026-11-21

### Changed
- Consolidate language-locale validation into `state.is_supported_language()` (`state.py`, `i18n.py`) so `slash.configure_language()` supports full regional locale prefixes (such as `en-au`, `vi-vn`) from a single source of truth without duplicating alias sets.

### Fixed
- Bind `dashboard/plugin_api.py` dynamically to `PluginManager`'s active `hermes_plugins.true_moa.true_moa` module (and trigger `discover_and_load()` / `gate.install()` when enabled) so `web_server.py` import-before-discovery never splits runtime state or reports false `gate: inactive` health errors in Settings > Plugins > True MoA (`N10`).
- Localize `hermes true-moa on` failure output in `true_moa/cli.py` via `i18n.msg("armed_fail", ...)` and keep Desktop Command Palette labels in `desktop/plugin.js` synchronized between `English` and `Tiếng Việt`.

---

## [1.6.0] - 2026-11-21

### Added
- **Unified Bilingual Desktop Settings & Health Check UI (`desktop/plugin.js` + `dashboard/plugin_api.py` + `dashboard/manifest.json`)**: Ship an interactive `@hermes/plugin-sdk` Settings page under `Settings > Plugins > True MoA` with instant **English (default) / Tiếng Việt** language switching, progressive disclosure for retry modes (`Manual` / `Sync` / `Custom`) and backup failover, an inline **Doctor** health-check card, 90-day recovery counters, `Ctrl+K` palette commands, and a toggleable status-bar indicator (`N10`).
- **Full Runtime Localization (`true_moa/i18n.py`, `language`, `/true-moa lang`)**: Add `language` (`en` default, `vi` optional) across `state.DEFAULTS`, `plugin.yaml`, `/true-moa lang [en|vi]`, and `hermes true-moa lang [en|vi]`, localizing `slash.py`, `cli.py`, `doctor.py`, and `MoaReferenceOutage` with verified `en`/`vi` key-set parity (`N9`, expanding `selftest.py` to **64 guarantees**).

### Changed
- Rewrite `plugin.yaml` description and all 11 `config_schema` labels and descriptions into pure, plain-language English without mixed Vietnamese phrases or internal Hermes symbol jargon, and broadcast `plugin.true-moa.updated` from `state.save()` so Desktop UI and chat/CLI stay synchronized.
- Include the exact failing advisor model name(s) in live `moa.progress` retry and backup-switch status messages inside `true_moa/gate.py`, and align `SECURITY.md` (`1.6.x`/`1.5.x`) and `CONTRIBUTING.md` (`Python 3.11 & 3.14` CI matrix).

---

## [1.5.2] - 2026-11-18

### Changed
- Include `threading.get_ident()` in `_verdict_session_key` inside `true_moa/gate.py` so concurrent gateway threads sharing Hermes' cached `preset` object remain isolated under `PR #157214` native hooks, expanding `selftest.py` to **62 guarantees** (`N8`).

### Fixed
- Fast-halt immediately in `_FanoutDecisionState.evaluate_batch` whenever permanent `401`/`403`/`404` slot eliminations drop `healthy_count + len(pending_indices)` below `required_healthy` so doomed multi-reference runs never sleep or burn API retries on surviving sibling slots.
- Preserve `native_hooks` mode inside `gate.install()` (and report native hook status in `/true-moa retry`) when `_native_mode_active` is true so `/true-moa on` and `/true-moa retry` never stack 7 in-process monkeypatch wrappers onto active native hooks.

---

## [1.5.1] - 2026-11-18

### Changed
- Unify selective retry, secondary fallback selection, and quorum evaluation into `_FanoutDecisionState` in `true_moa/gate.py` so in-process seam wrapping and `handle_moa_fanout_verdict` share one single state machine with per-turn session isolation and bounded state retention (`_MAX_VERDICT_STATES = 64`), expanding `selftest.py` to **61 guarantees** (`N7`).

### Fixed
- Preserve sibling failing slots in `pending_indices` during multi-reference secondary switches and enforce `healthy_count >= required_healthy` across every exit path so a single backup slot cannot drop concurrent retries or bypass `min_healthy_references == 0` (`all`).
- Narrow `gate.is_non_retryable_failure()` on `HTTP 403` responses to explicit credential/permission rejections (`permission_denied`, `insufficient_permissions`, `access_denied`) and exclude Cloudflare/WAF/quota/capacity markers so transient proxy challenges still retry normally.

---

## [1.5.0] - 2026-11-18

### Added
- **Offline Health Diagnostics (`/true-moa doctor`)**: Add `true_moa/doctor.py` and `/true-moa doctor` (`hermes true-moa doctor`) to audit seam status, PM `pyproject.toml` workspace hygiene, `plugin-data` storage, MoA preset credentials, secondary fallback collisions, and retry-ladder vs `turn_liveness` alignment without network calls.
- **Multi-Reference Quorum Mode (`min_healthy_references` / `/true-moa quorum`)**: Add opt-in quorum threshold (`1..8`, default `0` = strict `all`) so multi-advisor presets can synthesize from `>= K` healthy advisors after full retry and fallback exhaustion without caching degraded runs or ever permitting `0` references.
- **Deterministic `401`/`403`/`404` Fast-Switch & Fast-Halt**: Classify non-recoverable authentication, permission, and missing-model errors via `gate.is_non_retryable_failure()` to switch immediately (`0s` sleep) to `secondary_reference_model` or halt fail-closed without burning backoff sleeps.
- **Official Upstream Hook Bridge (`PR #157214`)**: Add `handle_moa_fanout_verdict`, `handle_auxiliary_fallback_policy`, and automatic `native_hooks` activation when supported by Hermes core while preserving 7-seam in-process wrapping on `0.23.x`.
- **Cross-Platform CI Matrix & Static Typechecking**: Expand `.github/workflows/ci.yml` to `ubuntu-latest` and `windows-latest` across Python `3.11` and `3.14` with `mypy` static typechecking, and extend `selftest.py` to **60 guarantees** (`N1`–`N6`).

### Changed
- Extract one-shot `/moa` aggregator synthesis into `_synthesize_oneshot_guidance()` and standardize wrapper metadata via `_mark_wrapper()` so `hermes plugins validate` passes `no core override` without runtime attribute rebinds.
- Key 90-day report retention pruning per `(report_dir, day)` tuple in `true_moa/report.py` so multi-profile `HERMES_HOME` switches within the same day prune each profile directory independently without touching private state in `selftest.py`.
- Latch completed default-path legacy migration checks per process in `true_moa/state.py` and `true_moa/report.py`, reuse `state.PLUGIN_DIR` as the single plugin-root source of truth, and remove the fixed `time.sleep` thread in `selftest.py` (`L2`).

### Fixed
- Stamp `agent._touch_activity()` during `_sleep()` retry backoffs so Hermes' `agent.turn_liveness` watchdog (`600s` default) never force-aborts an active long retry ladder.
- Suppress premature `[failed: ...]` `moa.reference` cards and `moa.aggregating` phase transitions emitted by `MoAChatCompletions._run_fanout` during intermediate failed retry attempts, emitting the verified advisor set and aggregator transition once when the fan-out settles.

---

## [1.4.1] - 2026-11-18

### Changed
- Move developer lint and test dependencies (`ruff`, `pyyaml`, `pytest`) out of `pyproject.toml` into `requirements-dev.txt`, update `.github/workflows/ci.yml` and contributor setup docs accordingly, and expand `selftest.py` to **54 guarantees** (`M1`, `M2`, `Z4` plus `test_pyproject_is_tooling_only`).

### Fixed
- Strip `[project]` and `[project.optional-dependencies]` from `pyproject.toml` so `pm.plugin_declarations.read_python_declaration()` keeps `installs_packages == False` instead of enrolling `true-moa` as a `uv` workspace member and triggering repeated Hermes Python environment rebuilds.
- Relocate runtime `state.json` and `reports/gate-YYYYMMDD.jsonl` from `plugins/true-moa/` to `<HERMES_HOME>/plugin-data/true-moa/` with automatic one-time migration and legacy cleanup so `gate_installed` telemetry writes never invalidate `pm.venv_is_current()` or stall Hermes Desktop startup.

---

## [1.4.0] - 2026-11-18

### Added
- **Selective Failed-Slot Retry**: In multi-reference presets, `true_moa/gate.py` now preserves outputs from already-healthy advisors across retries and re-queries only failing slots, keeping cache, trace, and accounting coherent without duplicate advisor calls.
- **One-Shot `/moa` Fail-Closed Coverage**: Extend the gate to `moa_loop.aggregate_moa_context` and `turn_api_request.build_api_request` (7 total verified seams) so one-shot `/moa` turns receive the same retry ladder, secondary fallback, and fail-closed halt before the main model acts.
- **Interruptible Retry Sleep**: `_sleep()` polls `agent._interrupt_requested` every `0.1s` and raises `InterruptedError` into Hermes' native interrupt handler instead of holding a cancelled turn across long backoff sleeps or retrying `[skipped: interrupted by user]` notes.
- **Cascading Secondary Reference Chains**: `secondary_reference_model` now accepts comma-separated `provider:model` lists (`parse_reference_model_chain`), advancing sequentially across healthy non-aggregator backups without adding extra schema keys.
- **Interactive Slash & CLI Ladder/Fallback Controls**: Support `/true-moa ladder [sync | custom <N> | s1,s2,...]` and `/true-moa fallback [off | [<attempts>] <provider:model[,...]>]` identically in chat and `hermes true-moa`.
- **Rolling 90-Day Telemetry Retention**: `true_moa/report.py` automatically prunes `gate-YYYYMMDD.jsonl` files older than 90 days and bounds `summary()` to the active retention window.
- Expand the offline acceptance suite in `selftest.py` from 44 to **51 guarantees** (`K5–K6`, `L1–L4`, `J8`) and add CLI/chain contract tests in `tests/test_static_contract.py`.

### Changed
- Consolidate all manifest, `tomllib`, and CLI parser checks into `tests/test_static_contract.py` so `.github/workflows/ci.yml` executes a single source of truth, and switch `selftest.py` core imports to `importlib.import_module` so `hermes plugins validate` passes with zero private-import warnings.

### Fixed
- Fix `hermes true-moa ladder <steps>` failing with `argparse: unrecognized arguments` when arguments were passed as shown in `--help`.
- Guard secondary reference switching on remaining ladder/sleep budget (`has_next_attempt`) so `fallback_activated` and `reference_fallback_switched` never fire on the terminal attempt when zero retries remain.

---

## [1.3.1] - 2026-11-18

### Changed
- Canonicalize custom-provider slot comparison via `resolve_runtime_provider` endpoint identity in `true_moa/gate.py` so alias pairs (e.g. `be` vs `beat`) are recognized as the same endpoint in aggregator-impersonation and failed-primary checks.
- Track `reference_fallback_switched` in `true_moa.report.summary()` counters and surface fallback switch counts in `/true-moa status` and `/true-moa report`.
- Enforce single-source version parity across `plugin.yaml`, `pyproject.toml`, and `true_moa/gate.py` (`PLUGIN_VERSION`) in CI and `tests/test_static_contract.py`, remove write-only `_cache` state from `true_moa/state.py`, drop legacy `cache_success_only` test input from `selftest.py`, and align issue templates with the 9-key schema.

### Fixed
- Keep `_true_moa_outage["fallback_activated"]` `False` (and `switched_from` empty) when a configured `secondary_reference_model` matches a failed primary reference and fallback is skipped.

---

## [1.3.0] - 2026-11-18

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
