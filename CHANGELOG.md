# Changelog

All notable changes to the **True MoA** plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
