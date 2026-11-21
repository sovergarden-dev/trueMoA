# True MoA (Mixture of Agents Strict Failover)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hermes Plugin](https://img.shields.io/badge/Hermes-Plugin-blueviolet.svg)](https://hermes-agent.nousresearch.com)
[![Version](https://img.shields.io/badge/version-1.6.2-green.svg)](CHANGELOG.md)
[![Architecture: Fail-Closed](https://img.shields.io/badge/Architecture-Fail--Closed-red.svg)](#architecture)

[ **English** ](README.md) | [ **Tiếng Việt** ](README.vi.md)

---

**True MoA** is an in-process, zero-core-modification reliability plugin for [Hermes Agent](https://github.com/NousResearch/Hermes-Agent) (`>=0.23.0`). 

It enforces a **strict fail-closed policy** for Mixture of Agents (MoA): if reference models fail, the aggregator is strictly blocked from acting alone without reference guidance, failed advisories are never cached, and auxiliary clients are barred from impersonating references.

---

## The Problem: Stock MoA is Fail-Open

In stock Hermes MoA implementations, reference model failures are designed to be fail-open:
- If a reference model fails or times out, its response is replaced with a `[failed: ...]` string.
- The Aggregator is instructed to *"act on your own judgment"*, silently answering user requests as if it were a solo model.
- Broken responses are cached in the fan-out cache, polluting subsequent turns.
- If auxiliary fallback is enabled, the system may even silently answer reference tasks using the aggregator model itself.

```mermaid
flowchart TD
    subgraph Stock_Hermes_MoA [Stock Hermes MoA: Fail-Open]
        A1[User Prompt] --> B1[Fan-out to Reference Models]
        B1 -->|Reference Fails / Times out| C1[Advisory: '[failed: timeout]']
        C1 --> D1[Aggregator Model Acts Alone]
        D1 --> E1[User receives unverified answer]
        C1 -.-> F1[Fan-out Cache Poisoned]
    end

    subgraph True_MoA_Gate [True MoA: Fail-Closed & Verified]
        A2[User Prompt] --> B2[Fan-out to Reference Models]
        B2 -->|Reference Fails| C2{Retry Ladder / Sync Active?}
        C2 -->|Retries Remaining| D2[Progressive Sleep & Auto Retry]
        D2 --> B2
        C2 -->|Ladder Exhausted| E2[Halt Turn Gracefully]
        E2 --> F2[Fail-Closed: Prevent Aggregator From Acting]
        E2 --> G2[Purge Cache Unconditionally]
        B2 -->|Reference Succeeds| H2[Aggregator Synthesizes Verified Guidance]
    end

    style Stock_Hermes_MoA fill:#ffebee,stroke:#c62828,stroke-width:2px
    style True_MoA_Gate fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
```

---

## Key Features

1. **Strict Fail-Closed Enforcement (`gate.py`):**
   - When reference models fail across the ladder, the turn halts cleanly through Hermes' error handler before the Aggregator can generate an unguided response.
2. **Aggregator Fallback Blocking:**
   - Auxiliary fallbacks for `moa_reference` tasks are intercepted and refused (`(None, None, "")`). The aggregator model never answers for its own references.
3. **Adaptive Retry Ladder & Hermes Synchronization (v1.2.0):**
   - **Sync Mode:** Optionally inherits `agent.api_max_retries` from Hermes configuration (e.g. 13 attempts) and generates a progressive backoff ladder (`10s → 15s → 20s → 30s → 45s → 60s...`).
   - **Custom Counter:** Enter a direct attempt count (e.g. `5` or `13`) without manually entering arrays.
   - **Manual Ladder:** Configure exact per-step sleep durations.
4. **Unconditional Cache Hygiene:**
   - Any advisory containing `[failed: ...]` immediately purges `_ref_cache_outputs` and `_ref_cache_key`, preventing poisoned results from bleeding across turns.
5. **Zero Core Modifications:**
   - 100% in-process plugin. Operates without altering any core Hermes codebase files, ensuring clean upgrades when Hermes updates.
6. **Bilingual Desktop UI, Health Check & Live Control (`en` / `vi`):**
   - `/true-moa status`, `/true-moa doctor`, `/true-moa lang [en|vi]`, `/true-moa on`, `/true-moa off`, `/true-moa report`.
   - Interactive bilingual panel (`English` default + `Tiếng Việt`) in **Settings > Plugins > True MoA** with progressive retry-mode controls, live Doctor diagnostics, `Ctrl+K` actions, and an optional status-bar indicator.

---

## Installation

### Method 1: Using Hermes Plugin CLI (Recommended)

```bash
hermes plugins install https://github.com/sovergarden-dev/trueMoA
```

Enable the plugin:
```bash
hermes plugins enable true-moa
```

### Method 2: Manual Installation

Clone or copy this repository into your local Hermes plugins directory:

**Windows:**
```powershell
git clone https://github.com/sovergarden-dev/trueMoA.git "$env:LOCALAPPDATA\hermes\plugins\true-moa"
hermes plugins enable true-moa
```

**Linux / macOS:**
```bash
git clone https://github.com/sovergarden-dev/trueMoA.git ~/.hermes/plugins/true-moa
hermes plugins enable true-moa
```

Verify installation:
```bash
hermes true-moa status
```

---

## Configuration

True MoA provides seamless two-way configuration through the desktop UI (**Settings > Plugins > true-moa**), in-chat slash commands, or `config.yaml`:

| Setting | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| **`language`** | `string` | `"en"` | Display language across Settings UI, `/true-moa` chat commands, CLI output, health checks, and outage notices (`"en"` = English default, `"vi"` = Vietnamese). |
| **`enabled`** | `boolean` | `true` | Toggle the strict fail-closed gate. When off, Hermes reverts to default fail-open behavior. |
| **`sync_hermes_retries`** | `boolean` | `false` | When enabled, automatically adopts the attempt count from `Settings > Advanced > Agent limits > API Retries` and generates an adaptive ladder. |
| **`custom_retries`** | `number` | `0` | Direct numerical attempt count (e.g. `5` or `13`). Takes highest precedence when `> 0`. `0` uses sync or manual ladder. |
| **`block_aggregator_fallback`** | `boolean` | `true` | Prevents the aggregator model from answering reference calls on failure or rate-limit. |
| **`max_wait_budget_s`** | `number` | `0` | Caps cumulative sleep time between retries (seconds). `0` uses the full ladder. (Does not abort in-flight HTTP requests). |
| **`enable_secondary_fallback`** | `boolean` | `false` | **Opt-in.** After repeated primary-reference failures, switch to a secondary reference for the remaining retries. Off by default; the gate stays fail-closed. |
| **`secondary_reference_model`** | `string` | `""` | Secondary reference in `provider:model` form, or a comma-separated cascade (`p1:m1, p2:m2`). Ignored when the toggle is off or empty. |
| **`fallback_trigger_attempts`** | `number` | `2` | Failed primary attempts before switching (clamped to `1..10`). |
| **`min_healthy_references`** | `number` | `0` | `0` (default) requires **all** enabled references to succeed. `1..8` enables Quorum mode: after retries/fallback exhaust, accepts `>= K` healthy advisors without caching degraded runs (`0` advisors never allowed). |
| **`ladder`** | `list` | `[15, 30, 60, 60, 60]` | Manual array of sleep intervals (seconds) between retries (used when sync and custom are inactive). |

### Precedence Hierarchy:
$$\text{custom\_retries} > 0 \;\longrightarrow\; \text{sync\_hermes\_retries: true} \;\longrightarrow\; \text{manual ladder}$$

### Secondary Reference Fallback (opt-in)

Disabled by default. When `enable_secondary_fallback` is on and a reference has failed `fallback_trigger_attempts` times, the gate swaps in `secondary_reference_model` for the remaining ladder steps and emits a `reference_fallback_switched` telemetry event. Healthy references are retained.

The switch **never relaxes the fail-closed guarantee**:

- If the secondary also fails, the turn still halts — no aggregator-alone answer.
- A secondary matching the aggregator (or using the `moa` provider) is refused, preventing aggregator impersonation.
- Failed advisories are still never cached.

---

## CLI & Slash Commands

In any active Hermes session:
- `/true-moa status` — Inspect gate state, active ladder, source attribution, and outage telemetry.
- `/true-moa lang [en | vi]` — Show or switch True MoA display language between English (`en`, default) and Vietnamese (`vi`).
- `/true-moa on` / `/true-moa off` — Arm or temporarily disarm the gate.
- `/true-moa ladder [sync | custom <N> | 10,20,40]` — Show or update the retry ladder in place.
- `/true-moa fallback [off | [<attempts>] <provider:model[,...]>]` — Show, arm, or disable secondary reference fallback.
- `/true-moa quorum [all | <1..8>]` — Require every reference (`all`) or at least `K` healthy advisors after retries.
- `/true-moa doctor` — Run offline health diagnostics across seams, PM workspace hygiene, presets, and credentials.
- `/true-moa report` — Display recent gate failure and retry telemetry (90-day rolling retention).
- `/true-moa retry` — Force re-probe and re-wrap all 7 seams.

From terminal:
```bash
hermes true-moa status
hermes true-moa doctor
hermes true-moa lang vi
hermes true-moa lang en
hermes true-moa on
hermes true-moa off
hermes true-moa ladder 10,20,40
hermes true-moa fallback 2 bdd:gemini-3.8-flash
hermes true-moa quorum 2
```

---

<a id="architecture"></a>
## Architectural Seams & Guarantees

True MoA intercepts Hermes execution at exactly 7 verified seams:

| Seam | Rule & Guarantee |
| :--- | :--- |
| `MoAChatCompletions.prepare` | Catches outage or user interrupt during request assembly, returning a tagged stub for clean loop handling. |
| `MoAChatCompletions.create` | Raises `MoaReferenceOutage` or `InterruptedError` inside Hermes' retry loop so turns finalize cleanly. |
| `MoAChatCompletions._run_fanout` | Retries **only failed slots**, cascades secondary backups, wakes immediately on user Stop, and purges failed caches. |
| `MoAChatCompletions._build_guidance` | Ensures an all-failed advisory list never converts to unguided prompt text. |
| `moa_loop.aggregate_moa_context` | Extends selective retry, secondary fallback, and fail-closed gating to one-shot `/moa` turns. |
| `turn_api_request.build_api_request` | Surfaces one-shot `/moa` outages inside Hermes' API error handler before the main model is called. |
| `auxiliary_client._try_main_agent_model_fallback` | Refuses fallback for `moa_reference` tasks (`(None, None, "")`). |

### Running the Acceptance Test Suite

The repository includes a comprehensive 64-guarantee test suite that validates all offline behaviors without live API calls:

```bash
# One-time dev setup (Linux/macOS: python3.11 + .venv/bin/; Windows: py -3.11 + .venv\Scripts\)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt "ruamel.yaml>=0.18,<0.19"

# Core-independent lint, typecheck and contract tests — the exact CI steps
.venv/bin/ruff check .
.venv/bin/mypy true_moa tests/test_static_contract.py
.venv/bin/python -m pytest tests/test_static_contract.py -q -rs

# Live 64-guarantee acceptance suite — needs the Hermes core on PYTHONPATH
PYTHONPATH=<hermes-workspace> .venv/bin/python selftest.py
PYTHONPATH=<hermes-workspace> .venv/bin/python -m pytest tests/ -q -rs
```

`<hermes-workspace>` is the directory containing `hermes_cli/` and `agent/`. The live suite also needs `ruamel.yaml`, which Hermes core imports at startup; it is installed above because it is a core dependency, not a plugin dependency.

GitHub Actions runs `ruff check .`, `mypy`, and `tests/test_static_contract.py` across Linux and Windows (`Python 3.11` & `3.14`) — it never collects the live suite, so no test is skipped in CI. Locally, `pytest tests/` without `PYTHONPATH` skips the live-acceptance test with an explicit reason while the contract tests still run.

Expected output for the live suite:
```text
...
all 64 guarantees held — recovery validation passed
```

---

## Telemetry & Privacy

True MoA maintains an append-only, privacy-preserving operational log in `<HERMES_HOME>/plugin-data/true-moa/reports/gate-YYYYMMDD.jsonl` (outside the plugin code directory so Hermes PM environment stamps remain immutable).
- Logs record timestamps, event names, slot labels, attempt counts, and wait durations.
- **Never logs:** Prompts, user message bodies, completions, or authentication credentials.
- Telemetry files are ignored by git to protect deployment privacy.

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before submitting Pull Requests.

---

## License

This project is licensed under the [MIT License](LICENSE) © 2026 Syrin (sovergarden-dev).
