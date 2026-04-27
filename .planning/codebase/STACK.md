---
generated: 2026-04-18
focus: tech
---

# Technology Stack

**Analysis Date:** 2026-04-18

## Languages

**Primary:**
- Python 3.14.2+ — all integration and test code

## Runtime

**Environment:**
- CPython 3.14.2 (minimum, enforced in `pyproject.toml`)
- Runs embedded inside Home Assistant's asyncio event loop; no standalone server

**Package Manager:**
- `uv` — dependency resolution and venv management
- Lockfile: `uv.lock` present and committed (revision 3, format version 1)
- venv auto-activated via `direnv` + `.envrc`

## Frameworks

**Core:**
- `homeassistant` 2026.3.4 — HA core library; provides `ConfigEntry`, `HomeAssistant`, event bus, entity/device/area/label registries, `EntityFilter`, `async_track_time_interval`
- `asyncpg` 0.31.0 — async PostgreSQL driver; used for all DB operations (pool, connection, `executemany`, `execute`, `fetchrow`)

**Schema Validation:**
- `voluptuous` 0.15.2 — config/options form schema validation in `config_flow.py`

**Data Serialization:**
- `attrs` 25.4.0 — used via `attrs.asdict()` for serializing HA `EntityEntry`/`DeviceEntry` (which use `@attr.s`) to JSONB; falls back to `dataclasses.fields()` or `vars()` for other entry types

**Testing:**
- `pytest` 9.0.0
- `pytest-asyncio` 1.3.0 — async test support
- `pytest-homeassistant-custom-component` 0.13.320 — HA test harness (pulls in `homeassistant`, `freezegun`, `coverage`)

## Key Dependencies

**Critical (runtime):**
- `asyncpg==0.31.0` — declared in both `manifest.json` (HA requirement) and `pyproject.toml` dev group; pinned exactly

**Infrastructure (transitive via homeassistant):**
- `aiohttp` 3.13.3 — HTTP client used by HA core
- `aiodns` 4.0.0 — async DNS
- `orjson` — JSON serialization (via HA / aiohasupervisor)

## Configuration

**Environment:**
- No `.env` file used by the integration itself
- Connection to TimescaleDB configured via DSN string stored in HA config entry data (`CONF_DSN`)
- All tunable parameters stored in HA options (not environment): `write_batch_size_records`, `flush_interval_seconds`, `compress_after_hours`, `chunk_interval_days`

**Build:**
- `pyproject.toml` — project metadata, Python version constraint, dev dependency groups
- `uv.lock` — full dependency lock
- `hacs.json` — HACS integration metadata
- `custom_components/timescaledb_recorder/manifest.json` — HA integration manifest (version, requirements, config_flow, integration_type)

## Dev Tooling

**Formatter/Linter:**
- Ruff — formatter configured as default in `.vscode/settings.json` (`charliermarsh.ruff`); no `ruff.toml` present at project root (uses VS Code extension defaults)

**Type Checking:**
- Type checking disabled in VS Code (`"python.analysis.typeCheckingMode": "off"`)

**LSP:**
- Pylance / basedpyright configured via VS Code Python extension

**Direnv:**
- `.envrc` activates `.venv` automatically; runs `uv sync` if venv is absent

## Platform Requirements

**Development:**
- Python 3.14.2+
- `uv` for dependency management
- `direnv` for automatic venv activation (optional but configured)
- A running TimescaleDB instance for integration testing (integration tests not present; unit tests use mocks)

**Production:**
- Installed as a HACS custom component inside Home Assistant
- Requires a reachable TimescaleDB (PostgreSQL + TimescaleDB extension) instance
- HA validates `requirements: ["asyncpg==0.31.0"]` at load time and installs via pip if absent

---

*Stack analysis: 2026-04-18*
