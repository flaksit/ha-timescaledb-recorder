# Milestones: ha-timescaledb-recorder

## v1.1 — Robust Ingestion

**Shipped:** 2026-04-23
**Phases:** 1–3 | **Plans:** 29 | **Timeline:** 2026-04-22 → 2026-04-23 (2 days, 205 commits)
**Scale:** 147 files changed · 3,669 LOC source Python · 5,565 LOC tests

**Delivered:** Full rewrite of the write path — async event-loop-resident asyncpg design replaced by dedicated OS worker thread with synchronous psycopg3 connection; complete durability story with bounded buffer, file-backed persistent queue, HA sqlite backfill, and watchdog-driven self-healing observability.

**Key Accomplishments:**
1. Thread-worker isolation — all DB writes in dedicated OS thread; HA event loop cannot be blocked or crashed by DB errors
2. psycopg3 replaces asyncpg — sync driver, native JSONB/TEXT[], no async-in-thread complexity
3. Bounded RAM buffer (10k, drop-oldest) + file-backed persistent queue — zero data loss up to 10+ days DB outage
4. HA sqlite backfill — on startup and DB recovery via recorder.history API; per-entity, chunked, interleaved with live writes
5. Watchdog async task — detects thread death, auto-respawns, fires structured persistent_notification with captured traceback
6. Full observability — 5 repair issues + persistent_notification hooks; all auto-clear when conditions resolve

**Requirements:** 27/29 Shipped · 2 Dropped (SQLERR-03/04 unified into retry+stall path per D-01)

**Final git tag:** `v1.1.5` (semver releases: v1.1.0 → v1.1.5)

**Archive:** `.planning/milestones/v1.1-ROADMAP.md` · `.planning/milestones/v1.1-REQUIREMENTS.md` · `.planning/milestones/v1.1-phases/`
