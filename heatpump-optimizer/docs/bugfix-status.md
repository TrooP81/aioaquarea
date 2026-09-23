# Bugfix Status

Tracks fixes for the 2026-09-23 code audit of `aioaquarea/` and `heatpump-optimizer/`.

**Status values:** `TODO` → `IN PROGRESS` → `FIXED` (code + tests pass locally) → `VERIFIED` (phase gate passed) · `WONTFIX` · `NEEDS-INFO`

**Baseline (before fixes):** library `pytest tests/` 95 passed · optimizer `pytest --ignore=tests/e2e` 707 passed, 35 skipped.

## Summary

| Phase | Scope | Total | TODO | IN PROGRESS | FIXED | VERIFIED | WONTFIX |
|---|---|---|---|---|---|---|---|
| 1 | HIGH | 6 | 0 | 0 | 0 | 6 | 0 |
| 2 | MEDIUM | 7 | 0 | 0 | 0 | 7 | 0 |
| 3 | LOW | 13 | 0 | 0 | 0 | 12 | 1 |

## Phase 1 — HIGH

| ID | Area | Summary | Status | Files changed | Tests | Notes |
|---|---|---|---|---|---|---|
| OPT-1 | optimizer | MILP DHW deadline indexes `tank_state` with local hour-of-day instead of horizon offset | VERIFIED | `packages/optimizer/milp.py`, `packages/optimizer/rule_mixins.py` | `tests/test_milp.py` (CEST, CET, midnight crossing) | Shared `local_dhw_deadlines_in_horizon`; converted to horizon offsets |
| CORE-1 | core | `get_bool_setting` returns `True` for `"false"` (`smartthings_enabled`, `use_comfort_model`) | VERIFIED | `packages/core/settings_service.py` | `tests/test_settings_service.py::TestBoolSettings` | Strings parsed via `_parse_bool`; unknown → `False` |
| CORE-2 | core | Auth circuit breaker never opens (new instance per process) | VERIFIED | `packages/core/resilience.py`, `packages/core/services/aquarea.py` | `tests/test_resilience.py`, `tests/test_aquarea_service.py` | `RedisCircuitBreaker`; in-memory fallback if Redis fails. Known LOW: `INCR`+`EXPIRE` not atomic |
| CORE-3 | core | Redis client opened but never used | VERIFIED | `packages/core/services/aquarea.py`, `.github/copilot-instructions.md` | as CORE-2 | Redis now stores breaker state; docs corrected |
| LIB-1 | library | Zone temperature setters lack `@auth_required` | VERIFIED | `aioaquarea/core.py` | `tests/test_core_time.py` | Dead `_post_device_zone_temperature` removed |
| LIB-2 | library | `get_device_consumption` lacks `@auth_required`; auth errors swallowed | VERIFIED | `aioaquarea/core.py`, `aioaquarea/consumption_manager.py` | `tests/test_consumption_timezone.py` | Behaviour change: `AuthenticationError` now propagates instead of `None` |

## Phase 2 — MEDIUM

| ID | Area | Summary | Status | Files changed | Tests | Notes |
|---|---|---|---|---|---|---|
| LIB-3 | library | `set_special_status` raises `AttributeError` on mixed-sensor devices | VERIFIED | `aioaquarea/data.py` | `test_set_special_status_skips_external_sensor_zones` | External-sensor zones omitted from payload (not verified against live API) |
| LIB-4 | library | `deviceIdList` fallback treats GUID strings as dicts | VERIFIED | `aioaquarea/device_manager.py` | `tests/test_device_manager.py` (GUID strings + negative dict case) | Only bare GUID strings bypass `deviceType` check |
| LIB-5 | library | HTTP status codes never validated for non-JSON responses | VERIFIED | `aioaquarea/api_client.py`, `aioaquarea/weekly_timer_manager.py` | `tests/test_api_client.py`, `tests/test_weekly_timer.py` | Non-JSON ≥400 → `RequestFailedError`; weekly timer keeps `None` contract |
| LIB-10 | library | App Store version scraped on every login | VERIFIED | `aioaquarea/auth.py` | `tests/test_auth.py` | Cached after first success; failures retried |
| LIB-12 | library | `DeviceDirection`/`PumpDuty` built without defaults | VERIFIED | `aioaquarea/device_manager.py` | `tests/test_device_manager.py` | Missing/unknown → `IDLE` / `OFF` with warning |
| OPT-2 | optimizer | Shower-mode DHW-off action missing `device_id` | VERIFIED | `packages/optimizer/shower_mode.py` | `tests/test_shower_mode.py` | Recovered and timeout paths |
| POLL-1 | poller | SmartThings refresh lock is process-local | VERIFIED | `packages/poller/smartthings_oauth.py` | `tests/test_smartthings_oauth.py` | `pg_advisory_xact_lock` on refresh and `save_tokens`; lock held during refresh HTTP (15s timeout) by design |

## Phase 3 — LOW

| ID | Area | Summary | Status | Files changed | Tests | Notes |
|---|---|---|---|---|---|---|
| LIB-6 | library | `heatSet: null` sent unguarded in special-status payload | VERIFIED | `aioaquarea/device_control.py` | `test_post_device_set_special_status_omits_unset_temperatures` | |
| LIB-7 | library | Unused `referer` parameter in `AquareaAPIClient.request` | VERIFIED | `aioaquarea/api_client.py`, `aioaquarea/core.py` | existing suite | Parameter and call-site kwarg removed |
| LIB-8 | library | Dead `_unknown_devices` state | VERIFIED | `aioaquarea/device_manager.py` | existing suite | `get_devices()` still returns a new list |
| LIB-9 | library | Stale `self.hass` comment in `entities.py` | VERIFIED | `aioaquarea/entities.py` | n/a | Comment only. The `__build_zones__` change in `refresh_data` is earlier uncommitted work, not part of this fix |
| LIB-11 | library | Unused constants in `const.py` | WONTFIX | | | Kept for external consumers |
| LIB-13 | docs | Docs claim Python 3.9+, code requires 3.10+ | VERIFIED | `.github/copilot-instructions.md` | n/a | |
| OPT-3 | optimizer | Executor wrapper monkey-patches module globals | VERIFIED | `packages/optimizer/executor_core.py`, `packages/optimizer/executor.py` | `test_core_executors_keep_injected_session_factories_isolated` | Injected `session_factory` / `sleep` / `learning_check` |
| OPT-4 | optimizer | `_estimate_cost` swallows DB errors silently | VERIFIED | `packages/optimizer/rules_engine.py` | new fallback-warning test | |
| ML-1 | optimizer | Dead `iscoroutine(session.add())` shims | VERIFIED | `executor_core.py`, `optimizer/main.py`, executor tests | executor suites | All sites removed; mocks use sync `add` |
| CORE-4 | core | `log_sink` flush loop swallows exceptions silently | VERIFIED | `packages/core/log_sink.py` | `tests/test_logging.py` | Writes to stderr; loop continues |
| CORE-5 | api | Admin reset swallows `OSError` on model deletion | VERIFIED | `packages/api/routers/admin.py` | `tests/test_admin.py` | Failed files logged and not reported as deleted |
| API-1 | api | OAuth `state` compared with `!=` | VERIFIED | `packages/api/routers/smartthings.py` | `test_rejects_mismatched_oauth_state` | `secrets.compare_digest`; empty-state case not separately tested |
| CORE-6 | core | `CORS_ORIGINS` default port mismatch | VERIFIED | `.github/copilot-instructions.md` | n/a | Not a code bug: compose publishes web on `4444`; instructions corrected from `3500` |

## Changelog

- 2026-09-23 — Status document created; baseline recorded.
- 2026-09-23 — Phase 1 VERIFIED. Library 100 passed; optimizer 721 passed, 35 skipped; black/ruff clean. Independent review: PASS (added garbage-string bool test from review).
- 2026-09-23 — Phase 2 VERIFIED. Review found a LIB-5 regression in weekly timer and a POLL-1 callback race; both fixed. Library 109 passed; optimizer 727 passed, 35 skipped; black/ruff clean.
- 2026-09-23 — Phase 3 VERIFIED. Library 110 passed; optimizer 733 passed, 34 skipped (skip delta is an environment-dependent symlink test); black/ruff clean. Independent review: PASS.

## Remaining / not done

- E2E backend suite (`run-tests.bat`, needs Docker test stack) not run.
- Nothing committed or pushed. The optimizer installs `aioaquarea` from `git+...@main`, so library fixes reach it only after they are pushed.
- Before enabling `auto` / `milp_preferred`, run MILP in shadow mode for a day to confirm the OPT-1 DHW scheduling.
- Library behaviour changes for external consumers (home-assistant-aquarea): LIB-2 (`AuthenticationError` propagates from consumption) and LIB-5 (non-JSON ≥400 raises `RequestFailedError`).
- Accepted LOW: `RedisCircuitBreaker.record_failure` uses non-atomic `INCR` + `EXPIRE`.
