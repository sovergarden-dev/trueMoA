---
name: Bug report
about: Create a report to help us improve True MoA
title: "[BUG] "
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what the bug is.

**Hermes Version & Environment**
- Hermes Core Version: (e.g. `2026.9.24` / `>=0.23.0`)
- True MoA Version: (e.g. `1.2.0`)
- Platform: [e.g. Windows 11, Ubuntu 24.04, macOS]

**Configuration**
Paste your True MoA configuration from `Settings > Plugins > true-moa` or `state.json`:
```json
{
  "enabled": true,
  "sync_hermes_retries": false,
  "custom_retries": 0,
  "ladder": [15, 30, 60, 60, 60],
  "block_aggregator_fallback": true,
  "max_wait_budget_s": 0
}
```

**Steps To Reproduce**
Steps to reproduce the behavior:
1. Go to '...'
2. Send prompt '....'
3. See error

**Expected behavior**
A clear and concise description of what you expected to happen.

**Screenshots / Logs**
If applicable, add screenshots or output from `/true-moa status` and `/true-moa report`.
