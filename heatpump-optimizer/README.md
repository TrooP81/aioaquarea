# Heat Pump Optimizer

Observe-only monitoring, diagnostic cost planning, and forecast evaluation for Panasonic Aquarea heat pumps.

**Observe-only build (C1).** Ordinary writes are prohibited in this build. Enabling optimization requires a new reviewed, approved build, not a setting, successful training, or reporting qualification. New plans are non-active diagnostics; legacy active rows may remain until superseded and are read-only references. Only existing linked safety restores may run outside positive observation, subject to their existing checks and embargoes.

**Restore ownership (S1).** Migration `034` adds a nullable `safety_claim_token` UUID without rewriting existing obligations. Each admitted restore holds a process-device fence and a dedicated PostgreSQL session advisory lock through its claim, SDK authentication retries, reserved verification, and token-checked completion/audit. Admission is bounded to 2 seconds; the whole attempt is bounded to 110 seconds, with at most 40 seconds for admission/preflight/quota/SDK work, at most 60 seconds for post-call verification, and 5 seconds for cancellation drainage. The first live admission checkpoint is 15 seconds after claim. Post-call checkpoints are absolute offsets from owned SDK restore-call entry, including quota waits and authentication recovery, not new cumulative waits after return. Verification still starts only after call settlement, and its deadline never resets or extends. Checkpoint 60 requires remaining time for evidence work; otherwise the obligation is deferred before a later reservation/read. Cleanup retains a newly acquired connection before any cancellable handoff, confirms worker drainage, and unlocks or invalidates/closes the dedicated connection before pool reuse or process-fence release. Never mix unfenced and fenced executor processes.

Known measurement/retrieval clocks must each be no older than the lesser of 60 seconds and the device freshness-policy maximum at captured UTC evaluation time. The maximum-age boundary is inclusive; observation must not follow retrieval, and neither may follow live read completion. A new wrapper call never makes known stale, future, or misordered evidence fresh. A source's nullable `executed_at` is not a physical-dispatch clock: a valid linked legacy restore can instead use reserved fresh post-claim live proof, completing without a write when already safe. Only absent clocks permit the live-read fallback with device identity, noncached status, and local UTC/monotonic read provenance; clocks are not invented. Executing/dispatched legacy rows without a UUID, unproven prior-owner drainage, or lost ownership remain deferred; age alone does not authorize reclaim. Ambiguous external effects are verified before any possible duplicate. This is not an exactly-once device guarantee and does not enable ordinary writes or maintenance.

**Local fresh start:** the [settings-preserving reset operator procedure](docs/settings-preserving-reset.md)
is separately guarded and requires exact runtime-manifest approval. Administrative
retirement is not physical OFF verification; it creates no pending carryover.

## Architecture

```
┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ poller  │──▶│ postgres │◀──│ api      │◀──│ web ui   │
│         │   │+timescale│   │(fastapi) │   │(next.js) │
└─────────┘   └──────────┘   └──────────┘   └──────────┘
     │              ▲              │
     ▼              │              ▼
 Panasonic     ┌──────────┐   ┌──────────┐
 Cloud API     │  redis   │   │optimizer │
(aioaquarea)   └──────────┘   └──────────┘
```

## Features

- **Real-time monitoring**: Device status, temperatures, compressor direction, device action, consumption
- **Hourly-accurate cost tracking**: Today's cost is computed as Σ(per-interval Δ kWh × that hour's spot price), not a flat-rate estimate
- **Fault detection**: Automatic detection and logging of device faults
- **Electricity price integration**: ENTSO-E day-ahead prices or Tibber subscription prices
- **Weather-aware**: Open-Meteo forecast for assumed heating efficiency, demand forecasting and pre-heating
- **SmartThings indoor temperature**: OAuth or PAT integration for real indoor sensor readings (multi-sensor averaging), with an in-app sensor selector to choose which discovered sensors to poll
- **Rules-based optimizer (`rules_v8`)**: Diagnostic DHW shifting, pre-heating, peak avoidance, schedule-driven eco/comfort and quiet-mode proposals; no ordinary dispatch
- **MILP optimizer (`milp_v2`)**: Diagnostic 24h scheduling via linear programming, with schedule-aware off-peak tank floor
- **Shower mode**: Retained settings; ordinary DHW boosts are disabled in this build
- **Seasonal calibration**: Opt-in cold-weather observation, persisting until explicit human exit; maintenance can queue demand/thermal candidates for reporting
- **Comfort schedule**: Weekday/weekend comfort hours with adaptive learning from actual usage
- **COP retirement**: Unverified legacy estimates are archived; no active COP training, loading, inference or live computation
- **Retained models**: Electrical heating-demand forecasting, indoor comfort and calibrated thermal response
- **Action verification**: Retained checks for existing linked safety restores, not authority for new ordinary commands
- **Manual overrides**: Always-wins pause button for the optimizer; survives optimizer reruns
- **Learning mode**: Positive observation suppresses both ordinary actions and linked restores. Turning learning mode off does not enable optimization, and does not end an active seasonal campaign
- **On-demand actions**: "Optimize now" queues a durable diagnostic re-plan; "Poll now" triggers a device refresh
- **Configurable settings UI**: Tank/comfort bounds, quiet mode hours, price sensitivity, learning thresholds — editable from the dashboard
- **Application log viewer**: Live, filterable view of structured logs from all services on the settings page
- **Operational alerts**: In-app (and optional webhook) alerts for stale data, adaptor outages, failed actions, and degraded forecasts
- **Automated backups**: Daily PostgreSQL archives with retention, isolated restore drills, and optional encrypted replica
- **Audit log**: Every executed action is recorded

## Quick Start

1. **Copy environment config:**
   ```bash
   cp .env.example .env
   # Edit .env for location, price area, and ports if needed. Panasonic credentials
   # and API tokens (ENTSO-E, Tibber, SmartThings) can be entered on the Settings page.
   # The checked-in contract stays local-only: development mode, API_TOKEN=disabled,
   # loopback database/API/dashboard ports, and CORS at http://localhost:4444.
   ```

2. **Start all services:**
   ```bash
   docker compose up -d
   ```
   Cold starts are ordered automatically; for a running-stack upgrade run `docker compose stop optimizer poller` before `docker compose up -d`, and on a migrate lock timeout stop writers and retry.

3. **Access the dashboard:**
   - Web UI: http://localhost:4444
   - API docs: http://localhost:8500/docs

## Configuration

Settings can be supplied via environment variables (typically through `.env`) and most are also editable at runtime from the **Settings** page in the dashboard. UI changes take precedence over the env defaults. The tables below cover the common variables; see [docs/configuration-reference.md](docs/configuration-reference.md) for every environment variable and runtime setting.

### Credentials & data sources

Panasonic Comfort Cloud username and password are entered in the dashboard **Settings** tab only; they are not read from `.env`.

| Variable | Description |
|----------|-------------|
| `PRICE_PROVIDER` | Price source: `entsoe` (default) or `tibber` |
| `ENTSOE_API_TOKEN` | Free token from [ENTSO-E](https://transparency.entsoe.eu/) |
| `ENTSOE_AREA` | Your bidding zone (e.g., `10Y1001A1001A46L` for SE3, `10YNL----------L` for NL) |
| `TIBBER_API_TOKEN` | Token from [developer.tibber.com](https://developer.tibber.com/) |
| `LATITUDE` / `LONGITUDE` | Your location for weather forecasts |
| `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET` | SmartThings OAuth app credentials (preferred) |
| `SMARTTHINGS_REDIRECT_URI` | OAuth callback URL, e.g. `http://localhost:8500/api/smartthings/oauth/callback` |
| `SMARTTHINGS_PAT` | Personal Access Token (legacy fallback if OAuth isn't configured) |

### Optimizer & system

| Variable | Default | Description |
|----------|---------|-------------|
| `TANK_MIN_TEMP` | `45` | Hot water tank minimum (°C) during comfort hours |
| `TANK_MIN_TEMP_OFFPEAK` | `41` | Lower tank floor allowed during sleep/away hours from the comfort schedule |
| `TANK_MAX_TEMP` | `55` | Hot water tank maximum (°C) |
| `COMFORT_TEMP_MIN` / `COMFORT_TEMP_MAX` | `20.0` / `22.0` | Room temperature bounds |
| `TANK_VOLUME_LITERS` | `300` | DHW tank volume — used to derive thermal capacity |
| `SH_MAX_POWER_KW` | `12.0` | Heat pump max electrical input for space heating |
| `POLL_INTERVAL_SECONDS` | `300` | Device polling interval |

### App & deployment

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | _(insecure default)_ | Required: set to a random string in production (used for HMAC-signed model files) |
| `API_TOKEN` | `disabled` | Set to a strong token to enable bearer-token auth on `/api/*` |
| `CORS_ORIGINS` | `http://localhost:4444` | Comma-separated allowed origins |
| `MODEL_DIR` | `/app/models` | Where ML models are persisted |
| `LOG_LEVEL` | `INFO` | Standard log level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | — | Database credentials (used by `docker-compose`) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `DB_PORT` / `API_PORT` / `WEB_PORT` | `5434` / `8500` / `4444` | Loopback-only host-side port mappings |

## Services

| Service | Default host port | Description |
|---------|-------------------|-------------|
| `web` | `WEB_PORT` (4444) | Dashboard UI |
| `api` | `API_PORT` (8500) | REST API + Swagger docs (`/docs`) |
| `poller` | — | Data collection, reporting-only model maintenance (default off), seasonal observation, alert delivery |
| `optimizer` | — | Diagnostic planning and existing linked safety restores; ordinary dispatch disabled |
| `migrate` | — | One-shot `alembic upgrade head`; `poller`, `optimizer`, and `api` start after it succeeds |
| `db` | `DB_PORT` (5434 → container 5432) | TimescaleDB |
| `redis` | (internal only) | Panasonic auth circuit-breaker state |
| `backup` | — | Scheduled `pg_dump` archives into `./backups` |
| `restore-tool` | — | `maintenance` profile only; pinned tools for the guarded restore entry point, not a live verifier |

## Backups And Calibration

The `backup` service creates PostgreSQL custom-format archives every 24 hours by default and retains completed archives for 14 days. Set `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION_DAYS` to change those bounds. Optional encrypted replicas require `BACKUP_REPLICA_ENABLED=true` and `BACKUP_REPLICA_ENCRYPTION_KEY`; set `BACKUP_REPLICA_REQUIRED=true` only when a failed replica should make backup health fail.

The legacy live-pointed verifier has been removed. `BACKUP_VERIFY_AFTER_DUMP` is
forced to `false` in Compose. Never verify on `PGHOST=db`, against the live
database, or on the live Compose network. `verify_backup.sh` rejects these targets
and a missing `BACKUP_VERIFY_ISOLATED=true` marker before inspecting any archive.

Run the synthetic Timescale drill with Docker and a POSIX shell:

```bash
RUN_DOCKER_RESTORE_TESTS=1 python -m pytest tests/test_restore_db_integration.py --noconftest -q -s
```

The dedicated `Backup Restorability Phase 1` workflow runs the synthetic drill on
every PR. It proves the missing-`heatpump` role failure at `bgw_job` COPY, successful
full restore with a disposable `heatpump NOLOGIN` role, four hypertables/chunks/
validated parent keys, and actual scheduled job execution after database rename.
The execution probe uses the existing disposable login role: TimescaleDB will not
run jobs owned by a `NOLOGIN` compatibility role. Real production roles are checked,
never created or altered automatically.

Weekly real-backup drills run Sunday at 04:17 UTC on a dedicated Linux self-hosted
runner labelled `restore-drill`. Configure repository variables
`RESTORE_DRILL_BACKUP_DIR`, `RESTORE_DRILL_EXPECTED_REVISION`, and
`RESTORE_DRILL_TMPFS_BYTES`. Give that runner read-only archive access, no live
database credentials or live volumes, and enough Docker-host memory. Real dumps
are never used on hosted PR runners or uploaded as CI artifacts.

Each drill uses a unique `restore-db-production-test-*` project, an internal-only
network with no published ports, the immutable image, read-only backups, tmpfs
storage, explicit PostgreSQL settings, and unconditional project/volume cleanup.
The legacy manual budget is 2 GiB tmpfs plus 2 GiB process headroom. For a known
source size `S`, provision at least `ceil(1.5*S) + 1 GiB`, rounded up to 256 MiB,
for tmpfs; container memory is always tmpfs plus 2 GiB. Do not infer `S` from the
compressed archive size. Resource failures report `verification_resource_failed`,
not archive corruption. Phase 2 will add metadata-driven automatic sizing,
container-state/OOM inspection, the isolated scheduler launcher, and backup health
state reporting; those are not shipped here.

### Local Evidence And Restore Runbook

The checked-in compose configuration is intentionally a local-only, no-token
contract: `APP_ENVIRONMENT=development`, `API_TOKEN=disabled`, database and API
on loopback port `5434`/`8500`, dashboard on loopback port `4444`, and CORS limited
to `http://localhost:4444`. It is not a production deployment recipe.

Render objective, zero-write evidence with the guarded wrapper only:

```bash
python scripts/render_compose_evidence.py --output compose-evidence.json
```

The wrapper creates its own empty environment file, supplies it to both Compose
environment selectors, runs Compose with a sanitized child environment, validates
the rendered JSON, and writes only the validated evidence projection. It never
contacts Panasonic or writes to the database. It fails closed when Docker/Compose
is unavailable or the render contains secret-bearing fields, values, locations, or
credential-bearing URLs. Share `compose-evidence.json` only after the wrapper
reports `PASS`.

To recover a local database, first stop the application writers, choose a named
completed archive directly under `backups/`, and run:

```bash
export RESTORE_SOURCE_IMAGE=timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57
export RESTORE_SOURCE_TIMESCALE_VERSION=2.26.4
export RESTORE_EXPECTED_REVISION=030  # Use the trusted source revision, not repository head.
export RESTORE_REQUIRED_ROLES=heatpump
scripts/restore_db.sh --production --allow-live-restore heatpump-YYYYMMDDTHHMMSSZ.dump
```

These legacy-archive inputs must come from trusted backup records. PostgreSQL
major 16, TimescaleDB exactly 2.26.4, and the exact pinned image are mandatory;
there is no version override. Recorded isolated spikes restored
`heatpump-20261003T071914Z.dump` and `heatpump-20261003T145920Z.dump` at source
Alembic revision `030`; repository migration head was `031`. Never assume an
archive has already been migrated to the current head.

The script validates the archive and exact `RESTORE <archive-name>` confirmation,
refuses active API/poller/optimizer/backup/migration writers, and obtains an external
restore lock. It restores a fresh `heatpump_restore_*` candidate using pre/post
Timescale hooks and `pg_restore --exit-on-error --no-owner --no-privileges`. Role
owners are inspected without filtering the restored archive. Missing roles abort
production before candidate creation; only canonical disposable targets create
compatibility roles, always `NOLOGIN`. Candidate checks precede migration to the
repository head, credential-presence checks, and cutover. Credentials are not printed.

Before renaming, both databases get database-scoped `timescaledb.restoring=on` and
explicit `_timescaledb_functions.stop_background_workers()` calls. Both stop/start
functions must resolve through `to_regprocedure` before any rename. The original is
retained as `heatpump_old_*`; only the renamed live database resets restoring mode
and calls `_timescaledb_functions.start_background_workers()`. A pre-cutover
failure drops only an owned candidate. A failed second rename attempts to restore
the original name. No application service is started automatically, and failures
keep writers stopped without shutting down PostgreSQL.

Before manually restarting writers, require BK-7A/BK-7B operational evidence: a
scheduled job's `last_successful_finish` and its probe timestamp must advance past
`CUTOVER_TIME` in the new live database, while retained-old statistics/probe counts
stay unchanged for at least two schedule intervals. Keep the old database in
restoring mode for 24 hours or until explicit operator sign-off; never restart its
workers while the new live database is active.

For rollback, stop writers, record the printed retained database name, and use the
same worker-gated rename helper through `restore-tool` with explicit authorization:

```bash
docker compose --profile maintenance run --rm --no-deps -T \
   -e RESTORE_ALLOW_LIVE_RESTORE=true -e RESTORE_SOURCE_IMAGE \
   -e RESTORE_SOURCE_TIMESCALE_VERSION restore-tool \
   sh /scripts/restore_archive.sh rollback heatpump heatpump_old_TIMESTAMP_PID heatpump_failed_TIMESTAMP
```

The helper suppresses both databases, terminates their connections, renames current
live to the failed name, renames retained-old back to live, and resumes only the
original. Validate again before manually starting writers. If even partial rename
rollback failed, connect to maintenance database `postgres`, inspect actual names,
and perform that same rename sequence; do not blindly repeat the recovery command.

`--test` uses only `docker-compose.restore-production.test.yml`, a unique
`restore-db-production-test-*` project, and `restore_production_test_*` databases.
It rejects `--allow-live-restore`. Canonical `RESTORE_PRODUCTION_TEST_*` redirection
is validated before the live gate; adding the live flag never escapes redirection.
Disposable production tests require an external archive directory. Test restores
mount archives read-only rather than copying into the live container.

An authorized trusted-host drill of the latest real archive, without cutover:

```bash
RUN_DOCKER_RESTORE_TESTS=1 RUN_REAL_BACKUP_RESTORE_TESTS=1 \
   python -m pytest tests/test_restore_db_integration.py::test_BK_13_latest_real_backup_restores_without_filters_or_errors --noconftest -q -s
```

On Windows, add Git's POSIX shell to `PATH`, set the two flags in PowerShell, and
pass `--basetemp` and `-o cache_dir` paths outside the repository. These standalone
drills intentionally use `--noconftest` to avoid app fixtures and repo-local scratch.

Use a restore for data recovery, not to downgrade schema or application code. A
downgrade needs an explicit, reviewed Alembic/image compatibility plan; restore a
backup only when its schema and the selected application image are known compatible.

Thermal calibration is explicit: use `POST /api/thermal/calibrate` after enough representative operating data exists. It updates model parameters only; it does not send a heat-pump command.

## API Endpoints

The full, always-current OpenAPI spec is available at `http://localhost:8500/docs`. When `API_TOKEN` is set, every route requires `Authorization: Bearer <token>` except `/health`, `/health/ready`, and `/api/smartthings/oauth/callback`. The dashboard's server-side proxy adds the token for you. Highlights:

**Overview & history**
- `GET /api/dashboard` - Overview (status, prices, today's energy/cost, any stored legacy active plan, override flag)
- `GET /api/control-state` — Persisted optimizer control state (no device I/O)
- `GET /api/status/history?hours=24` — Device status history
- `GET /api/device/settings` — Last-known device-reported settings
- `GET /api/consumption/history?hours=24` — Per-interval energy deltas
- `GET /api/prices?hours=48` — Electricity prices (past + future)
- `GET /api/weather?hours=48` — Weather forecast
- `GET /api/stats?period=day|week|month` — Aggregated stats
- `GET /api/audit` — Audit log
- `GET /api/logs?minutes=30&level=&service=` — Application log entries (last up to 24h)
- `GET /api/currency` / `GET /api/time-format` — UI display preferences
- `GET /api/version` — Running API version
- `GET /health` — Liveness probe
- `GET /health/ready` — Readiness probe, including poller/optimizer heartbeat and backup freshness
- `GET /metrics` — Prometheus-format operational metrics

**Optimizer & overrides**
- `GET /api/plans` / `GET /api/plans/{id}` — Plans and plan details with actions
- `GET /api/optimizer/status` - Diagnostic planner, five-state model reporting lifecycle, and observation state; never control readiness
- `GET /api/learning-mode` / `POST /api/learning-mode` - Read or toggle manual observation (`{"enabled": true|false}`); off does not permit ordinary writes
- `POST /api/optimize-now` - Queue a durable diagnostic re-plan; poll `GET /api/optimize-now/{request_id}` for its status and non-active resulting plan
- `GET /api/plan-activity` / `GET /api/outcomes/summary` / `GET /api/operations/alerts` — Dashboard lifecycle, measured outcome, and operational-health data
- `POST /api/operations/safety-reverts/{action_id}/resolve` — Resolve a deferred safety revert once fresh evidence proves it safe
- `POST /api/poll-now` — Force an immediate device poll
- `POST /api/overrides` / `DELETE /api/overrides/{id}` — Create or cancel a manual override

**Panasonic**
- `GET /api/panasonic/capabilities` — Mapped command surface, observed zones/tank, safety policy, and adaptor state (no cloud call; see [../docs/panasonic-aquarea-api.md](../docs/panasonic-aquarea-api.md))

**Comfort schedule & indoor temp**
- `GET /api/comfort-schedule` / `PUT /api/comfort-schedule` — Read/write the schedule
- `GET /api/comfort-schedule/learned` — Auto-detected usage patterns
- `POST /api/comfort-schedule/apply-learned` — Merge learned patterns into the schedule
- `GET /api/indoor-temp?hours=24` / `GET /api/indoor-temp/latest` — Indoor sensor history/latest
- `GET /api/sensors/diagnostics?hours=168` — Observation-only SmartThings sensor diagnostics

**SmartThings integration**
- `GET /api/smartthings/devices` — List discoverable temperature sensors (powers the Settings sensor selector)
- `GET /api/smartthings/oauth/authorize` / `GET /api/smartthings/oauth/callback` — OAuth flow
- `GET /api/smartthings/oauth/status` — Current connection status
- `DELETE /api/smartthings/oauth/disconnect` — Revoke stored OAuth tokens

The Settings page → **SmartThings Integration** section includes a sensor selector: it discovers your temperature sensors and lets you tick which ones to poll for indoor temperature. Selecting none falls back to polling all discovered sensors. The choice is saved as `smartthings_device_ids`.

**Faults, COP & compressor**
- `GET /api/faults` / `POST /api/faults/{id}/resolve`
- `GET /api/cop/history` / `GET /api/cop/stats` — Read-only legacy archives, labelled `legacy_unverified_estimate`, `confidence=estimated`, `measured=false`, with `X-COP-Status: retired` even when empty
- `POST /api/cop/compute` — Retired: HTTP 410 with detail code `cop_retired`; no COP-history writes
- `GET /api/cop/manual` - Local device choices and manual ledger; DAY by default, MONTH explicitly selected
- `PUT /api/cop/manual/{device_id}/{granularity}/{period_date}` - Create/correct a manual controller estimate with an opaque decimal-string `expected_revision` (`"0"` creates only when absent)
- `DELETE /api/cop/manual/{device_id}/{granularity}/{period_date}` - Explicit deletion with the current decimal-string `expected_revision`
- `GET /api/compressor/activity`

**Models, thermal & calibration**
- `POST /api/ml/train` - HTTP 202, parameter-free Demand maintenance request; `demand.request_id` plus the unchanged COP retirement result
- `POST /api/comfort-model/train` / `POST /api/thermal/calibrate` - HTTP 202 queued maintenance, with `request_id`; no fitting in the API worker
- `GET /api/ml/requests/{request_id}` - Read queued/running/succeeded/failed/cancelled progress and `maintenance_disabled` when applicable; unknown IDs return 404
- `GET /api/comfort-model/status` / `GET /api/comfort-model/predict` - Reporting lifecycle and available qualified-model prediction
- `GET /api/thermal/status` / `GET /api/thermal/curve` / `GET /api/thermal/indoor-forecast` - Reporting lifecycle and diagnostic forecasts
- `GET /api/thermal/forecast-scorecard` — Scores past indoor forecasts against later sensor readings
- `GET /api/thermal/heat-curve-advice` — Suggests a small manual Panasonic heat-curve adjustment (never sends a command)

**Settings & connectivity**
- `GET /api/settings` / `PUT /api/settings`
- `POST /api/test-connection` — Probe Panasonic/price/weather/SmartThings connectivity

**Admin / data reset**
- `POST /api/admin/reset` — Permanently delete selected resettable data scopes (`{"scopes": ["indoor_temp", "energy", ...], "reset_models": true}`). Available scopes: `indoor_temp`, `energy`, `device_status`, `weather`, `prices`, `plans`, `logs`. `energy` clears consumption only, never `cop_history`. Clearing any model-feeding scope (`indoor_temp`, `energy`, `device_status`, `weather`) also resets Demand, comfort and thermal models. COP artifacts, COP history, all model baselines, settings, credentials and the SmartThings connection are preserved.

The Settings page → **Danger Zone — Reset Data** card exposes this: tick categories (or *Select all resettable data*), confirm, and only those data scopes and affected retained models are cleared. COP archives, model baselines, manual controller-COP readings and their revision sequence remain intact. Use it to recover after training on bad data (e.g. a misconfigured indoor-temperature sensor).

## How the Optimizer Works

The `optimizer` service re-plans diagnostics every hour. Its minute dispatch check cannot send ordinary actions in this build. Existing linked restores retain their separate safety checks and wait while observation is positive. The planner logic below describes proposals, not executable commands.

### Layer selection

The `optimizer_layer` setting chooses the planner:

| Value | Behaviour |
|-------|-----------|
| `rules_only` (default) | Always the rules engine. |
| `milp_preferred` | Selects diagnostic MILP, optionally using reporting-qualified Demand. Existing input gates and solver fallback to rules are unchanged. Heating efficiency is assumed, never learned COP. |
| `auto` | Always rules during COP retirement, with reason `auto_milp_disabled_cop_retired`. No legacy-history qualification query or automatic MILP promotion. |

Planning retains the bounded assumption `max(1.5, min(6.0, 3.5 + 0.1 * outdoor_temperature_c))`. This is not measured pump performance. DHW thermal kWh are divided by this ratio exactly once; space-heating demand is already electrical kWh and is not divided again. Plans carry `cop_source=assumed` and `cop_measured=false`. The `+ml` version suffix means retained Demand only; `rules_v8` and `milp_v2` identify the planner ORM-lifetime repair.

Planning captures device status (including isolated native-policy provenance), persisted gate evidence, and baseline consumption counters before read-only rollback expires their ORM rows. Missing or mismatched gate evidence remains UNKNOWN; consumption snapshots preserve source-day/timezone reset accounting and genuine unavailable/row-budget reasons. This changes diagnostic planning/accounting only: ordinary writes stay disabled and persisted diagnostics contain no executable actions.

### Rules Engine (`rules_v8`)
1. **DHW Shifting**: Uses the thermal model to find the cheapest slot before each deadline, with direction-aware heating time estimation
2. **Pre-heating**: Boosts zone temperature during cheap hours before forecast cold spells
3. **Peak avoidance**: Quiet mode level 1 for up to 1/6 of the horizon's hours, choosing the most expensive hours that are at least 1.3× the median price; skipped when outdoor temperature is below 0 °C
4. **Comfort schedule**: Switches between eco and comfort modes based on weekday/weekend schedule with configurable price overrides
5. **Quiet mode**: Reduces compressor speed during configurable night hours (default 22:00–06:00)
6. **Adaptive learning**: Automatically detects regular heating patterns and merges them into the comfort schedule
7. **Holiday mode**: Suspends all optimization when device is in holiday mode
8. **Action verification**: Polls device after each command to confirm it took effect

### MILP Optimizer (`milp_v2`)
Solves a 24h cost-minimization problem with:
- Decision: when to run DHW, how much space heating per hour
- Objective: minimize Σ(price × kWh_electrical)
- Constraints: per-hour tank floor (uses `tank_min_temp_offpeak` during sleep/away hours, normal `tank_min_temp` during comfort hours), tank max, comfort bounds, assumed heating-efficiency curve, hardware rate limits, and the comfort model's predicted indoor response when available

### Shower mode
No ordinary Force DHW boost starts in this build, even if the retained `shower_mode_enabled` setting is true. A previously linked Force DHW off obligation follows the existing restore checks; positive observation also suppresses that lane.

### Poller schedule

| Job | Interval |
|-----|----------|
| Device status (+ shower detection) | `POLL_INTERVAL_SECONDS` (300 s) |
| Consumption | 15 min |
| Prices | 15 min |
| Weather | 30 min |
| SmartThings indoor temperature | `smartthings_poll_interval` (300 s) |
| Model maintenance (only when `ml_maintenance_enabled=true`) | 6 h UTC; Demand attempts at least 24 h apart |
| Seasonal calibration check | 6 h |
| Operational alert delivery | 5 min |
| Heartbeat | 1 min |

### ML Models
- **COP Model**: Retired inert compatibility API. `train()` returns an explicit unsupported/error result, `load_latest()` returns false without artifact access, and prediction refuses. Status is always the uncached retirement tombstone (`trained=false`, zero samples/source records, null metrics/date, reason `unverified_thermal_output`), even with legacy artifacts or a previously trained cache entry. The former 50-sample COP gate is retired, not lowered.
- **Demand Model**: Electrical heating-demand forecasting; at least 168 usable fit intervals. Raw counts and historical CV alone cannot qualify a reporting incumbent.
- **Comfort Model**: Indoor-response forecasting with separate declared heads/horizons, persistence comparators and prospective skill gates; no control authority.
- **Thermal model**: Tank, zone and indoor heating/standby rates from supported whole segments; unsupported, unqualified or expired rates use defaults.
- Exact-fit artifacts are signed, hash-addressed and `SECRET_KEY`-bound in `MODEL_DIR`. Frozen candidates are not planning incumbents; only compatible, fresh reporting-qualified incumbents can support diagnostic forecasts.

`ml_maintenance_enabled` defaults to `false`. Explicit training routes queue durable HTTP 202 requests even while off; request status then says `maintenance_disabled`. Only the poller owns fitting/publication: six-hour UTC maintenance, at least 24 hours between Demand attempts (including failures/restarts), renewable leases and fenced compare-and-swap. API/planner/status reads do not fit or adopt models. The optional weekly entry point also queues requests; it is not a default Compose service.

The five reporting states are `unqualified`, `candidate`, `qualified-for-reporting`, `expired`, and `unavailable`. A fit is not qualification. Status separates incumbent and candidate progress, evidence counts/diversity, exclusions, last valid outcome, cadence and next decision checkpoint. None of these states permits ordinary writes. Qualification expires at most 72 hours after its last distinct supporting valid outcome, or immediately on incompatible scope.

Only prospective unchanged-native-policy evidence qualifies: at least 14 elapsed days, 14 device-day clusters, 20 issue cohorts and 60 valid pairs, with declared horizon/outdoor-bin coverage and improvement against both frozen comparators. Sequential policy `prospective_native_fixed_daily_v1` fixes 71 daily looks at frozen-fit ages 15 through 85 days; six-hour reviews/retries add no looks. The first possible checkpoint is fit time plus 15 days, not a two-week promise. Sparse/excluded evidence can require weeks or months, or postpone qualification indefinitely; maintenance off makes no automatic progress. See [qualification and sequential-decision details](docs/configuration-reference.md#model-maintenance).

Migration `033` follows `032`: retained lifecycle authority, artifacts, maintenance requests, immutable prospective issues with retained outcomes, native-policy provenance and a non-cycling fencing-token sequence. It neither adopts legacy files nor enables control. Downgrade refuses populated lifecycle storage or any issued token; a separately reviewed compatible observe-only rollback must retain these records.

### Controller Total COP

Total COP is manually accessible by going to the heat pump's physical Panasonic controller. The supplied controller screens show **Totalt COP-värde**, marked **Ungefär** (approximate), in day, week and year views. The year view shows monthly bins; a selected month is not an annual COP. Current-day/month values may cover partial periods, and the same day shown in a week view is not an independent sample.

This is a controller-estimated whole-system total COP, not the retired DHW-specific model, measured thermal-output evidence, or the optimizer's planning assumption. Cloud/API access to it has not been established. The manual ledger implemented under `COP-MANUAL-MON-2026-10-04 v1.1` records only deliberately entered readings. It does not ingest images, scrape/OCR screens, discover cloud COP, or backfill legacy archives.

**Under the hood > Diagnostics > Manual controller COP** has explicit local-device selection, day/month entry, correction and deletion. A timezone setting only prefills the form; the operator must identify and confirm the physical controller's IANA timezone. Reading provenance is immutable: `source=controller_estimate`, `acquisition=manual_entry`, `scope=total`, `measured=false`, `accuracy=approximate`, `energy_boundary=unknown`. Energy boundaries, physical device identity and actual accuracy are not certified by this UI.

The supplied October 4 fixtures are entry examples only: yesterday October 3 `3.7`, today October 4 `9.7`, that same selected day repeated in a weekly view, and selected October 2026 month `3.9` in a year view. They are not annual/weekly COP, independent samples, training evidence or ground truth. No averaging of ratios, synthetic zeroes, interpolation or week/year entries is supported. `9.7` is accepted without the planner's separate efficiency cap.

Each `(device_id, day|month, period_date)` stores its frozen timezone and UTC calendar boundaries. Month keys are first-of-month. Boundaries use actual local calendar days, including DST; ambiguous midnight selects the earliest valid instant and nonexistent midnight is rejected. `observed_at` is timezone-aware, no earlier than period start and no later than server now. `partial`, `closed`, and `unknown` are explicit operator choices. `closed` additionally requires observation at or after period end; it is attestation, not certification or automatic promotion. Correcting timezone recomputes boundaries; correcting period identity requires deliberate delete/re-entry.

Revision tokens are opaque decimal JSON strings, never JavaScript numbers. A retained bigint `NO CYCLE` sequence supplies a fresh token for each successful create/correction. Concurrent/stale writes, duplicate `"0"` creation and missing keys for known devices return 409; unknown local devices return 404, invalid entries 422, storage failures 503. Create/correction return the committed row (201/200); deletion returns 204. Rollback or delete/recreate never rewinds/reuses tokens, and exhaustion fails. There is no revision-history table or reconstruction of superseded values.

Read pages contain at most 90 rows, descending by date with exclusive `before`; `next_before` exists only when more rows remain. DAY is the default and MONTH requires `granularity=month`. Device/series cursors are independent. Daily forecast candidates are selected independently of the displayed page in the same read-only snapshot; the DB session closes before bounded thread fitting. The routes inherit installation-wide API bearer/proxy authentication, conditional on configured `API_TOKEN` and/or the existing perimeter, not new per-device authorization. No Panasonic session, Redis or quota call is made. Structured mutation logs contain keys/tokens/outcomes, not reading values.

**Experimental forecasting** controls `manual_cop_monitoring_enabled` (default `false`) only; entry, correction, deletion and retention remain available while off. The forecaster requires 28 consecutive closed current daily revisions through yesterday for one device/timezone, all available at server UTC `issue_at`. Availability is `max(observed_at, created_at, updated_at)`. Existing NumPy/LinearRegression fit local-date ordinal to log controller COP and project tomorrow, two calendar steps beyond yesterday; monthly readings never train it.

Seven matched-clock retrospective folds target the final seven days. Each historical issue is the day before its target at the live request's local hour/minute/second/microsecond. Fold training uses dates no later than target minus two and only current revisions available at that historical issue, with at least 20 rows in every fold. Later corrections/bulk entries are excluded without substituting an unavailable old value. Held-out targets may arrive later for retrospective scoring but must exist by the actual issue. Ambiguous historical clocks follow the live fold; nonexistent clocks refuse rather than shift. These simulations are not previously issued forecasts.

Only `ready` includes a projection, log-linear daily trend and **experimental empirical log-error band, uncalibrated**, based on the linearly interpolated 90th percentile of seven absolute log errors. This is neither a confidence interval nor an efficiency diagnosis. Other statuses (`disabled`, `no_data`, `insufficient_data`, `stale`, `unavailable`) have null forecasts and explicit reasons/counts. Missing optional ML support and failed/nonfinite/nonpositive fits affect this panel only. Forecasts recompute on every GET; no model/cache/artifact, scheduler, alert or publication is created. Fingerprints bind daily keys/revisions, timezone/boundaries and issue time. The UI clears numbers during mutation, disablement and failure and discards obsolete response generations.

Manual data and forecasting have no rules, MILP, auto-selection, executor, Demand/thermal training or device-control influence. COP retirement, assumed planning efficiency, Demand's 168-interval minimum, existing retraining cadence and write guards remain unchanged.

The ordinary additive migration `032_manual_cop_observations` follows `031`, creates only `manual_cop_observations` and `manual_cop_revision_seq`, and does not change archives, add a status-table FK, create a hypertable or backfill readings. Generic/model resets exclude both retained objects.

**Current reviewed Alembic head: `037`** (Tibber price-scope registry). `036` below is the historical local-period shape transition, not the current database marker. Qualification receipts, reset guards and synthetic restore expectations use `037`. Real backup restores must verify the exact revision recorded for that archive, not silently stamp or upgrade it.

**Local calendar periods (migration `036`, API contract `2026-10-12.1`).** A reading is keyed by device, granularity, local `period_date` and IANA `timezone`; actual event timestamps (`observed_at`, `created_at`, `updated_at`) stay UTC. Migration `036_manual_cop_local_periods` drops the stored `period_start`/`period_end` columns, their SQL midnight/order checks and the `manual_cop_calendar_midnight` helper, keeping every row, revision token and the sequence unchanged. The API still returns `period_start`/`period_end`, now derived in Python from date, granularity and timezone; validation (closed periods, `observed_at` not before the period end, DST-aware midnights) is unchanged. The measurement `scope` is `unknown` (the controller estimate's energy boundary is not asserted). `036` is forward-only: its downgrade refuses with `manual_cop_local_periods_forward_only`; restore a pre-migration backup instead. It runs with `lock_timeout` 5 s and `statement_timeout` 20 s and leaves the database at `035` when it cannot lock. See [docs/manual-cop-schema-reset-runbook.md](docs/manual-cop-schema-reset-runbook.md).

**Manual ledger reset.** `POST /api/admin/reset` with `{"scopes": ["manual_cop"]}` (the scope must be requested alone; the web Reset Data card offers it separately) deletes every device's manual readings, sets `manual_cop_monitoring_enabled` to `false` and writes exactly one `reset_data` audit row in one transaction. It never touches other tables, settings, models, artifacts, Redis or safety claims, and it never resets `manual_cop_revision_seq`, so earlier revision tokens keep conflicting. Failure returns 503 `manual_cop_reset_unavailable` with nothing committed (an allocated audit id may be skipped). Readings remain until explicit deletion; active deletion does not purge backups and the existing backup policy is unchanged. Operational rollback disables forecasting and restores a retirement-safe build while retaining table, sequence and data; sequence rewind and automatic downgrade/drop are refused. Implementation/isolated-test approval is not release, production-migration, deployment or live-enablement approval.

### Retirement Activation And Rollback

Implementation/test approval does not authorize deployment, activation, real-data training or device operations. Before a separately approved activation, stop/drain every old COP-capable trainer and process, including API workers and optional weekly/manual jobs; verify no in-flight legacy fitting/publication remains before starting retirement-aware workers. Preserve COP artifacts, all baselines and `cop_history` without deletion, re-signing or migration changes.

After that separately approved activation, check the once-at-startup `cop_retired` event, the auto-selection reason, assumed plan provenance and read-only archive/410 contracts. This change does not invalidate pending plans or alter executor/safety-restore semantics. A rollback must use a separately approved retirement-safe build in rules-only operation and retain the archives; do not downgrade migrations. Returning to a COP-capable binary is a new activation decision requiring independent review and human approval, not a routine rollback.

## Development

```bash
# Install the locked development graph (see ../docs/dependency-management.md)
python -m pip install -c constraints.txt -e ".[all,dev]"

# Unit tests (no database needed)
python -m pytest --ignore=tests/e2e -q

# Lint / format
ruff check packages/ tests/
ruff format packages/ tests/

# Run a single service locally
python -m packages.poller.main
```

### Backend end-to-end tests

Backend qualification requires newly created, coordinator-owned disposable PostgreSQL and Redis services. CI uses the pinned images in the workflow, published only on `127.0.0.1:5433` and `127.0.0.1:6380`. Local Linux container tests use a private, owner-labelled shared namespace with PostgreSQL listening on 5433 and Redis on 6380; they never share the host network or an existing test stack.

Set the complete profile **before application imports**: `APP_ENVIRONMENT=test`, `API_TOKEN=test-token`, public test-only `SECRET_KEY=deployment-verify-public-test-secret`, dummy ENTSO-E/Panasonic configuration, both checkout roots in `PYTHONPATH`, and an exclusively created empty TEMP `MODEL_DIR`. The only accepted endpoints are:

```bash
DATABASE_URL=postgresql+asyncpg://heatpump:heatpump_test@127.0.0.1:5433/heatpump_test
REDIS_URL=redis://127.0.0.1:6380/0
```

The harness requires `COP_MANUAL_TEST_OWNED` and `COP_MANUAL_TEST_RECEIPT`, not merely a token prefix or a test setting. `tests/e2e/test_manual_cop_postgresql.py prepare` creates the exclusive TEMP/model roots and receipt. GitHub supplies the actual `job.services` IDs/network/ports through `COP_MANUAL_TEST_SERVICE_CONTEXT`; local coordinators supply a newly created TEMP lease and actual platform creation records. Docker GET inspection verifies service IDs, pinned images, creation times, namespace, loopback publications or owner labels, and PostgreSQL identity before mutation. Unknown, expired, non-test or populated initial migration targets are refused without cleanup. Do not hand-forge receipts or reuse a previously running stack. Legacy `run-tests.bat` and `scripts/run-e2e.ps1` do not establish this proof and are not qualification entry points.

Run these phases serially from the optimizer directory. The migration class owns a fresh empty database, derives 032's predecessor, exercises legacy reversibility through 020, and verifies both populated downgrade refusals. Later reversible migrations use the discovered unique head; another retention boundary fails for review rather than being skipped.

```bash
python -m alembic heads
python -m pytest --noconftest tests/e2e/test_manual_cop_postgresql.py::TestManualCOPPostgreSQLMigration -q
python -m alembic upgrade head
python -m alembic check
python -m pytest --noconftest tests/e2e/test_manual_cop_postgresql.py -k "not TestManualCOPPostgreSQLMigration and not test_sequence_exhaustion_is_503_and_never_wraps" -q
python -m alembic check
python -m pytest --noconftest tests/e2e/test_manual_cop_postgresql.py::TestManualCOPPostgreSQLAPI::test_sequence_exhaustion_is_503_and_never_wraps -q
python -m alembic check
python -m pytest tests/e2e --ignore=tests/e2e/test_manual_cop_postgresql.py -q
python -m alembic check
```

CI executes each command through `tests/e2e/test_manual_cop_postgresql.py phase NAME -m ...`, with a 600-second child limit, 10-second TERM/KILL grace and an external 660-second parent limit. Backend jobs are capped at 30 minutes; service readiness is bounded at 90 seconds for PostgreSQL and 60 seconds for Redis. A constraint-pinned `tzdata==2026.4` supplement and executable internal Linux TMP are qualification prerequisites. The phase helper retains UTF-8 stdout/stderr, PID/PGID/deadline/exit receipts, source hashes and exact collected/selected node IDs. `seal` requires disjoint complete manual phases and complete ordinary E2E coverage with no skip, xfail or allow-failure exemption. Exhaustion is the last manual mutation; never rewind the sequence to reuse the database.

Ordinary E2E fixtures clear only legacy synthetic rows with quoted `TRUNCATE ... RESTART IDENTITY`, without `CASCADE`. They create missing legacy tables individually and drop only those fixture-created tables in reverse dependency order. Migrated tables, Timescale structures, the retained ledger/sequence/helper and Alembic marker remain intact and are compared across the session. Engines are disposed even on failure; only the resource coordinator destroys the owned services after receipts are sealed and child processes have ended. Unit tests retain their normal global model isolation and the backup contract's six original classifications/assertions in a dedicated scratch child.

These are disposable verification procedures, not release, deployment, production-database, backup-restore, model-training or live-device authorization. Full unit/E2E and dependency qualification, independent review and separate final HIGH release approval remain mandatory.

### Frontend end-to-end tests

The dashboard has two Playwright suites under `web/`:

```bash
cd web

# Mocked UI tests — every API call is stubbed and Playwright starts its own
# dev server. Fast, deterministic, good for CI. (web/e2e/)
npm run test:e2e

# Live-stack tests — drive the real running system (web :4444 + API :8500 +
# DB + optimizer) with NO mocking, asserting real data and physical invariants
# (e.g. the indoor forecast must drift gradually toward outdoor, never snap).
# Requires the Docker stack to be up first (`docker compose up -d`). (web/e2e-live/)
npm run test:e2e:live

# Override targets when not on the default ports:
#   E2E_BASE_URL=http://host:4444 E2E_API_URL=http://host:8500 npm run test:e2e:live
```


## Safety

- **C1**: Ordinary writes are prohibited in this build; optimization requires a new reviewed, approved build. No setting, model fit or reporting qualification overrides this contract.
- Manual override **always wins** over the optimizer
- **Learning mode** suppresses both dispatch lanes while positive. Turning it off never enables ordinary writes; an active seasonal campaign must also be explicitly exited. Normal observation activation waits for unresolved linked restores; forced activation is exceptional and audited.
- Rate limiter prevents API abuse (30 reads/h, 20 writes/h)
- Every Panasonic write first requires live (not cloud-cached) adaptor status no older than 60 seconds
- Circuit breaker disables auth for 15 min after 3 failures (state shared across services via Redis)
- All actions are audit-logged
- Existing overrides and restore embargoes keep their gate precedence; clearing an override does not enable ordinary control or bypass restore checks.
