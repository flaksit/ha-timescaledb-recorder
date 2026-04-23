---
status: partial
phase: 03-hardening-and-observability
source: [03-VERIFICATION.md]
started: 2026-04-23T00:00:00Z
updated: 2026-04-23T00:00:00Z
---

## Current Test

[awaiting human testing]

## Tests

### 1. Worker crash → notification + respawn
expected: Kill/crash a worker thread; HA shows a persistent notification within WATCHDOG_INTERVAL_S (10s); a new thread is spawned and resumes work
result: [pending]

### 2. DB outage → stall issue → auto-clear on recovery
expected: Take DB offline; after STALL_THRESHOLD (5) retries the stall repair issue appears; bring DB back online; issue auto-clears without HA restart
result: [pending]

### 3. Recorder disabled at setup → repair issue → auto-clears when recorder loads
expected: Start HA with recorder component disabled; `recorder_disabled` repair issue appears; enable recorder and reload; issue auto-clears
result: [pending]

## Summary

total: 3
passed: 0
issues: 0
pending: 3
skipped: 0
blocked: 0

## Gaps
