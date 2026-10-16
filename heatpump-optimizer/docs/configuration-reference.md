# Heat Pump Optimizer configuration reference

The backend image installs dependencies using `constraints.txt` and upgrades its
bundled package installer to the qualified `pip==26.2.0` before installation.
Release qualification audits the installed image graph, not the developer's shared
Python environment. The dashboard production graph is audited separately from its
development tooling; a clean production audit does not certify development tools.

The optimizer reads configuration from two layers:

1. **Environment variables** – read once at process start by
   `packages/core/config.py` (`pydantic-settings`, `.env` file supported).
2. **Runtime settings** – stored in the `settings` database table, defined in
   `SETTINGS_SCHEMA` in `packages/core/settings_service.py`, and edited through
   the dashboard Settings page or `GET`/`PUT /api/settings`.

For runtime settings, a stored value wins. When no value is stored, the
setting's *env fallback* (if any) is used, then its schema default.

Values are stored as strings. `secret` values are masked in `GET /api/settings`;
a `PUT` that echoes a masked value (containing `***`) does not overwrite the
stored secret. A value outside a setting's options, type, or range is rejected
with an error. An empty string clears a free-form value.

### Feed attribution during settings changes

Each price/weather request captures its provider, request-start time and applicable
area or coordinates before outbound HTTP I/O. Changing runtime settings while a
request is in flight applies to the next request; it does not relabel the response
already being fetched. Price persistence uses this captured context, including the
provider's currency, rather than rereading current settings.

Schema markers describe validated provider normalization, not merely a successful
HTTP response. Unannotated legacy price/weather lists are not automatically
certified. Tibber entries without currency metadata remain available to list-only
callers but do not receive an hourly schema marker. Missing or invalid currency
metadata leaves otherwise valid price-only values uncertified, independently of
entry order. Valid currency codes are normalized before checking for genuine
currency conflicts. Conflicting currencies, nonfinite values, duplicate timestamps
and unverifiable hourly timestamps reject the batch with a fixed validation reason;
the price poller logs failure rather than a successful empty fetch. Legitimately
empty provider results remain distinct from rejected batches. Price-fetch errors
log exception classification and HTTP status where available, never exception text
or provider URLs/query strings that may contain API credentials.
SMHI retains its timezone-aware `referenceTime` as forecast issuance; missing or
invalid issue clocks remain unknown and do not acquire a units marker.
Open-Meteo has no forecast issue clock in this response, so its captured request
start is used as the retrieval-based freshness clock (also used for manual weather).
Existing planning freshness, completeness and unit checks remain unchanged.
No historical rows are relabeled or rescaled.

## Environment variables

### Application (`packages/core/config.py`)

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://heatpump:changeme_in_production@db:5432/heatpump` | Async SQLAlchemy URL. Docker Compose builds it from `POSTGRES_*`. |
| `REDIS_URL` | `redis://redis:6379/0` | Redis; stores the Panasonic auth circuit-breaker state. |
| `APP_ENVIRONMENT` | `development` | `development`, `staging`, or `production`. Outside `development`, startup fails unless `SECRET_KEY` and `API_TOKEN` are set. |
| `SECRET_KEY` | `change-this-to-a-random-string` | HMAC key for persisted ML models. Changing it invalidates saved models. A warning is emitted while the default is used. |
| `API_TOKEN` | `disabled` | Bearer token for the API. `disabled` or empty turns authentication off. |
| `CORS_ORIGINS` | `http://localhost:4444` | Comma-separated allowed origins. |
| `MODEL_DIR` | `/app/models` | ML model directory; shared by `api`, `poller`, `optimizer` via the `modeldata` volume. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `POLL_INTERVAL_SECONDS` | `300` | Device status poll interval used by the poller scheduler. Also the env fallback for `poll_interval_seconds`. |
| `PANASONIC_DISTRIBUTED_READ_QUOTA_ENABLED` | `false` | Enables the Redis-backed, per-account Panasonic logical-read quota. Manual refresh reserves two reads and requires ten available reads before admission; background reads wait and fall back to the local limiter when Redis is unavailable. |
| `EXTERNAL_HEARTBEAT_ENABLED` | `false` | Environment fallback for the external reporting-heartbeat setting; disabled by default. |
| `EXTERNAL_HEARTBEAT_URL` | _(empty)_ | Healthchecks.io ping URL fallback. Treat as a secret; API responses fully redact it. |
| `EXTERNAL_HEARTBEAT_INTERVAL_SECONDS` | `60` | Attempt interval, constrained to 30–300 seconds. Failed attempts consume the interval. |
| `EXTERNAL_HEARTBEAT_ALLOWED_AUTHORITIES` | `hc-ping.com:443` | Deployment-only exact `host[:port]` allowlist; comma-separated, no wildcards. Change only in protected deployment configuration. |
| `EXTERNAL_HEARTBEAT_API_URL` | `http://api:8500` | Trusted internal API base used for the fixed `/health` check; never derived from the external URL. |
| `EXECUTOR_SAFETY_READ_RESERVE` | `2` | Startup-only count of the five hourly executor verification reads reserved for safety restores. Valid range: 1–4. |
| `PRICE_PROVIDER` | `entsoe` | Env fallback for `price_provider`. |
| `ENTSOE_API_TOKEN` | _(empty)_ | Env fallback for `entsoe_api_token`. |
| `ENTSOE_AREA` | `10YNL----------L` | Env fallback for `entsoe_area`. |
| `TIBBER_API_TOKEN` | _(empty)_ | Env fallback for `tibber_api_token`. |
| `TIBBER_HOME_ID` | _(empty)_ | Exact provider home ID; see installation binding below. |
| `LATITUDE` / `LONGITUDE` | `52.37` / `4.89` | Env fallbacks for `latitude` / `longitude`. |
| `SMARTTHINGS_CLIENT_ID` / `SMARTTHINGS_CLIENT_SECRET` | _(empty)_ | Env fallbacks for SmartThings OAuth. |
| `SMARTTHINGS_REDIRECT_URI` | _(empty)_ | Env fallback for `smartthings_redirect_uri`. |
| `SMARTTHINGS_PAT` | _(empty)_ | Env fallback for the legacy Personal Access Token. |
| `TANK_MIN_TEMP` | `45` | Env fallback for `tank_min_temp`. |
| `TANK_MIN_TEMP_OFFPEAK` | `41` | Env fallback for `tank_min_temp_offpeak`. |
| `TANK_MAX_TEMP` | `55` | Env fallback for `tank_max_temp`. |
| `COMFORT_TEMP_MIN` / `COMFORT_TEMP_MAX` | `20.0` / `22.0` | Env fallbacks for comfort bounds. |
| `TANK_VOLUME_LITERS` | `300` | Env fallback for `tank_volume_liters`. |
| `SH_MAX_POWER_KW` | `12.0` | Env fallback for `sh_max_power_kw`. |

Panasonic credentials are **not** read from the environment. Set
`aquarea_username` and `aquarea_password` on the Settings page.

### Panasonic distributed read-quota canary and rollback

Keep `PANASONIC_DISTRIBUTED_READ_QUOTA_ENABLED=false` for the legacy local
limiter. During a canary, enable it on one isolated deployment with Redis
available, verify `/api/panasonic/read-quota` reports `enabled: true` and
`reliable: true`, and confirm manual refresh succeeds above the displayed
ten-read threshold. The dashboard disables manual refresh when the quota is
unreliable or below that threshold. To roll back, set the flag to `false` and
restart the API and poller; no migration or Redis cleanup is required.

### Docker Compose

| Variable | Default | Used by | Description |
| --- | --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `heatpump` / `changeme_in_production` / `heatpump` | `db`, app services, backups | Database credentials. |
| `DB_PORT` | `5434` | `db` | Loopback host port for PostgreSQL. |
| `API_PORT` | `8500` | `api` | Loopback host port for the API. |
| `WEB_PORT` | `4444` | `web` | Loopback host port for the dashboard. |
| `COMPOSE_ENV_FILE` | `.env` | all | Alternative env file for services. |
| `APP_VERSION` / `BUILD_REVISION` | `0.15.0` / `unknown` | image builds | Build metadata. |
| `TIMESCALE_IMAGE` / `REDIS_IMAGE` | pinned digests | `db`, backups / `redis` | Deployment image choices. Restore tooling is immutable and rejects a different database image or Timescale version. |

### Backups

| Variable | Default | Description |
| --- | --- | --- |
| `BACKUP_INTERVAL_SECONDS` | `2592000` | Env fallback for `backup_interval_seconds`: fixed 30-day UTC interval, not calendar months. |
| `BACKUP_RETENTION_DAYS` | `100` | Env fallback for `backup_retention_days`. Age eligibility never overrides protection or the two-snapshot floor. |
| `BACKUP_MAX_AGE_SECONDS` | `2764800` | Env fallback for `backup_max_age_seconds`: stale at 32 days. Failure degrades health immediately. |
| `BACKUP_TIMEOUT_SECONDS` | `3600` | Env fallback for `backup_timeout_seconds`; bounded attempt, not an interval-length sleep. |
| `BACKUP_HOST_DIR` | `./backups` in development only | Approved live value: `D:\backups\heatpump-optimizer\`. Deployment-only; never use the parent `D:\backups\`. The standalone backup definition requires an explicit existing protected bind mount. |
| `BACKUP_REPLICA_ENABLED` | `false` | Write an encrypted copy to the replica directory. |
| `BACKUP_REPLICA_HOST_DIR` | `./backups-replica` | Host directory mounted as the replica target. |
| `BACKUP_REPLICA_REQUIRED` | `false` | Treat a failed replica as a failed backup. |
| `BACKUP_REPLICA_ENCRYPTION_KEY` | _(empty)_ | Required when replicas are enabled. |

The four numeric backup settings are edited through the existing settings API,
not a dedicated settings page. Persisted values take precedence; empty backup
values fall back to the environment and then the defaults above. A bulk update
validates the complete merged policy inside the existing settings transaction lock;
single backup-key writes use the same boundary. Bounds:

| Setting | Minimum | Maximum |
| --- | ---: | ---: |
| `backup_interval_seconds` | 3600 | 7776000 |
| `backup_retention_days` | 2 | 365 |
| `backup_max_age_seconds` | 3600 | 10368000 |
| `backup_timeout_seconds` | 60 | 21600 |

Freshness must cover interval + timeout + 120 seconds; retention in seconds must
strictly exceed the interval and cover freshness. Invalid combinations are rejected
before any write. Defaults do not overwrite old persisted policies: explicitly set
the approved monthly policy during separately approved activation.

The worker checks policy/durable success at least every 60 seconds. Its next due
time is successful snapshot start + interval. Restart before that time does not
duplicate a backup; overdue restart attempts once, with a five-minute failed-attempt
retry delay. Unsupported/legacy history is unknown, not proof of first activation
or successful protection. No heartbeat-time backfill is performed.

Retention holds the same destination lock as archive selection/staging. It deletes
only validated direct managed sets (dump, manifest, checksum), never recursively.
Untrusted protection/inventory blocks all archive deletion. Latest successful,
newest eligible and latest verified archives are protected; at least two eligible
snapshots must remain after any deletion. Bootstrap may have fewer than two.
These rules can retain files beyond 100 days; capacity must not override them.

`docker-compose.backup.yml` is a standalone **staged, inactive** definition with
only a backup service and an explicit existing external source network. Source
host/database/role/credential and inspected SQL source ID are deployment inputs.
Do not use ordinary Compose wholesale against external active infrastructure.
No primary/scratch directory, secret or live deployment is provisioned by these edits.
Replica remains disabled/not required for the approved local-only deployment.

Readiness and operations alerts share a versioned evidence reader. Successful
backup, latest attempt and verified restore are separate histories; optional replica
failure is warning-only. Raw archive IDs/checksums/paths/source identities and
database errors do not enter public backup summaries or webhook backup details.
Disabled delivery, empty alerts and loading errors never establish backup health.

See [backup runbook](backup-runbook.md) for qualification/activation boundaries.

### Restore And Isolated Drills (Phase 1)

These are script/CI inputs, not runtime application settings. See the
[restore and rollback runbook](../README.md#local-evidence-and-restore-runbook).
The staged Phase 1 monthly worker publishes manifest/checksum sidecars and versioned
backup-health histories. Readiness uses a 2764800-second (32-day) successful-snapshot
freshness default, independently of attempt/verification timestamps. Restore
verification advances only after verified cleanup and persisted final reporting.
The implementation is not activated or Docker-qualified by offline unit checks.

| Variable | Default | Description |
| --- | --- | --- |
| `RESTORE_SOURCE_IMAGE` | required | Trusted source image: exact pinned `timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57`. No production override. |
| `RESTORE_SOURCE_TIMESCALE_VERSION` | required | Exactly `2.26.4`. Archive PostgreSQL major must be `16`; pinned server is `16.13`. |
| `RESTORE_EXPECTED_REVISION` | required | Trusted numeric pre-migration archive revision. Production upgrades only the verified candidate to repository head. |
| `RESTORE_REQUIRED_ROLES` | inspected roles | Additional space-separated source/job-owner identifiers. Production requires existing roles; disposable targets alone create `NOLOGIN` roles. |
| `RESTORE_PRODUCTION_TEST_COMPOSE_FILE` | unset | Must be the canonical disposable Compose file; any partial redirection fails closed. |
| `RESTORE_PRODUCTION_TEST_PROJECT` | unset | Must start with `restore-db-production-test-`. Live authorization never overrides redirection. |
| `RESTORE_PRODUCTION_TEST_BACKUP_DIR` | unset | Existing external disposable archive directory, read-only in containers; aliases/children of project backups are rejected. |
| `RESTORE_TEST_PROJECT` | unique disposable project | `--test` permits only `restore-db-production-test-*`. |
| `RESTORE_TEST_DATABASE` | unique disposable database | `--test` permits only `restore_production_test_*`; it never replaces an existing candidate. |
| `RESTORE_TEST_KEEP_STACK` | `false` | Test harness only: retain the isolated stack for assertions. Its owner must still clean up in `finally`. |
| `RESTORE_VERIFY_TMPFS_BYTES` | `2147483648` | Explicit legacy/manual capacity. For trusted source bytes `S`, use at least `ceil(1.5*S)+1 GiB`, rounded to 256 MiB. Compressed archive size is not `S`. |
| `RESTORE_VERIFY_MEMORY_BYTES` | derived | Always tmpfs + `2147483648` in entry points/tests. PostgreSQL: `shared_buffers=256MB`, `maintenance_work_mem=512MB`, `work_mem=16MB`, `max_connections=20`; single-job restore. |
| `RUN_DOCKER_RESTORE_TESTS` | off | Set to `1` to run the synthetic tmpfs Docker gate. No published ports or external networks. |
| `RUN_REAL_BACKUP_RESTORE_TESTS` | off | Additionally set to `1` only on an authorized trusted host. Restores newest complete dump without cutover. |
| `RESTORE_REAL_BACKUP_DIR` | project backups | Trusted-host read-only real archive directory. Never mount a live volume. |
| `RESTORE_REAL_EXPECTED_REVISION` | _(required for real drill)_ | Exact trusted revision of that archive, not automatically the application head. Reviewed head and synthetic archive marker are `037`; historical `030` restore evidence remains historical. |
| `BACKUP_VERIFY_ISOLATED` | off | Must be exactly `true`. No bypass flag. |
| `BACKUP_LIVE_DATABASE` | `heatpump` | Verifier rejects this name in both `PGDATABASE` and `BACKUP_VERIFY_DATABASE`; also always rejects `PGHOST=db`. |
| `BACKUP_VERIFY_DATABASE` | `heatpump_restore_verify` | Fresh isolated candidate name; cannot be live or an existing database. |
| `BACKUP_VERIFY_TIMEOUT_SECONDS` | `3600` | Single-job verification wall timeout. |

Legacy verifier errors distinguish `verification_resource_failed` (`container_oom`,
`tmpfs_exhausted`, `postgres_memory`, `timeout`) from `verification_failed` for
archive/content errors. That legacy verifier retains its input artifact and does
not own the current drill lifecycle or update versioned backup-health histories.
The staged Phase 1 `restore_drill.py` coordinator already performs metadata-based
capacity/headroom sizing, guarded target ownership and verified cleanup, and
persists verification evidence consumed by readiness. Unresolved cleanup/reporting
cannot establish a verified restore. See the [backup runbook](backup-runbook.md);
this implementation still requires owned Linux CI qualification before activation.

The weekly workflow requires a dedicated Linux runner labelled `restore-drill`
and repository variables `RESTORE_DRILL_BACKUP_DIR`,
`RESTORE_DRILL_EXPECTED_REVISION`, and `RESTORE_DRILL_TMPFS_BYTES`. Configure no
production DB credentials, networks, or volumes on that runner. Synthetic drills
run on every PR; real dumps stay off hosted PR runners and CI artifacts.

### Web (`web` service)

| Variable | Default | Description |
| --- | --- | --- |
| `INTERNAL_API_URL` | `http://api:8500` | Upstream for the server-side `/api/*` proxy route. |
| `INTERNAL_API_TOKEN` | `API_TOKEN` | Bearer token added by the proxy. Never exposed to the browser. |

### Playwright live suite

| Variable | Default | Description |
| --- | --- | --- |
| `E2E_BASE_URL` | `http://localhost:4444` | Dashboard under test. |
| `E2E_API_URL` | `http://localhost:8500` | API under test. |

## Runtime settings

Types: `str`, `int`, `float`, `bool` (`true/1/yes/on`, `false/0/no/off`),
`secret` (masked), `json`. Keys starting with `_` are internal state and are
not meant to be edited.

### Optimizer control

**Observe-only build (C1).** Ordinary writes are prohibited in this build. Enabling optimization requires a new reviewed, approved build; no setting, trained flag or reporting qualification enables control. New plans are non-active diagnostics. Stored legacy active plans are references, not ordinary dispatch authority; existing linked restores retain their separate checks outside positive observation.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `optimizer_layer` | str | `rules_only` | Values unchanged. `rules_only` and `auto` use rules during COP retirement; `auto` never qualifies legacy history for MILP. `milp_preferred` explicitly selects MILP with optional trained Demand and unchanged solver fallback/input gates. |
| `ml_maintenance_enabled` | bool | `false` | Enable poller-owned model maintenance for reporting only. Read through `get_bool_setting`; never enables device writes. |
| `learning_mode_enabled` | bool | `false` | Positive manual observation suppresses both ordinary actions and linked restores. Turning learning mode off does not enable optimization or end active seasonal observation. |
| `learning_mode_since` | str | _(empty)_ | Internal: when learning mode was last enabled. |
| `shower_mode_enabled` | str | `false` | Retained `true`/`false` setting; ordinary DHW boosts remain disabled in this build. |
| `shower_drop_threshold` | int | `10` | Tank drop (°C) between polls that triggers shower mode. |
| `shower_max_duration_minutes` | int | `60` | Maximum shower boost duration. |
| `quiet_mode_start` | int | `22` | Night quiet-mode start hour (0–23). |
| `quiet_mode_end` | int | `6` | Night quiet-mode end hour (0–23). |
| `price_comfort_override_pct` | int | `90` | Skip comfort above this price percentile. |
| `price_eco_upgrade_pct` | int | `25` | Upgrade eco to normal below this price percentile. |
| `comfort_schedule` | json | weekday `7–9, 17–21`; weekend `8–21` | Comfort hours by day type: `{"weekday": [...], "weekend": [...]}`. |
| `learned_schedule_threshold` | float | `0.3` | Minimum heating activity score (0–1) to auto-add a comfort hour. |

COP retirement v1.0 adds no settings, default/value changes or migration. There is no COP-reactivation toggle. Status always reports the retired COP tombstone; `POST /api/ml/train` remains parameter-free and queues Demand-only maintenance. Demand still requires 168 usable intervals, and the former 50-sample COP threshold is retired rather than reduced. Planning uses the unchanged bounded assumed efficiency, not measured COP; the `+ml` label refers to Demand only.

Resetting `energy` preserves `cop_history`; resetting models preserves COP artifacts and all model baselines. The physical Panasonic controller's manually accessible **Totalt COP-värde**, marked **Ungefär**, is an approximate whole-system estimate with period/completeness limits, not the retired DHW model or an optimizer input. The separate manual ledger and default-off experimental forecaster do not reactivate COP or supply planning/training inputs. See [controller interpretation and manual monitoring contracts](../README.md#controller-total-cop).

### Model maintenance

Maintenance defaults off. Scheduled jobs, startup requests, planner reads and API reads do not write lifecycle tables while it is off. Explicit training requests remain durable and queued; request status returns reason `maintenance_disabled` and message `maintenance disabled`. `POST /api/ml/train`, `POST /api/comfort-model/train` and `POST /api/thermal/calibrate` return HTTP 202 and a `request_id` (inside `demand` for `/api/ml/train`). `GET /api/ml/requests/{request_id}` reads progress without creating authority.

The poller is the only fitting owner: one UTC job at 00:00, 06:00, 12:00 and 18:00, coalesced with one instance, backed by a short renewable lifecycle lease. Admission uses the same fixed UTC grid regardless of execution jitter. Missed native collection slots remain recorded gaps, with collected/expected slot counts; later complete slots can recover without backfilling prospective predictions. Demand attempts are persisted before work and remain at least 24 hours apart after failures or restarts. A frozen candidate is reviewed rather than replaced for insufficient prospective evidence. Damaged artifacts are durably invalidated and fenced, with `ml_component_invalidated` and `ml_maintenance_failed` events; healthy incumbents and other components are preserved, and repair can create a new candidate in a later admitted cycle. Disabling maintenance revokes an existing lease in the settings transaction. Cancellation drains its bounded worker before releasing ownership; only the owner may publish through fenced CAS. Demand preprocessing runs off-loop on detached inputs, reusing device status and weather indexes.

Artifact availability is distinct from integrity. A permission error, I/O error, timeout, storage-blip missing file or database connectivity failure reports `unavailable`, not damage, and preserves authority and prospective progress. The maintenance owner persists `availability:<hash>` checkpoints and emits `ml_artifact_unavailable`; retries are due after 6, then 12, then at most 24 hours, with the attempt counter capped at three. Only a missing-file failure on the third or later due attempt can escalate, after a fresh bounded directory read confirms storage availability and a separate file check confirms absence. Permission, I/O and timeout failures never escalate by count alone. Recovery verifies the same artifact and clears only its availability checkpoint, without replacing a candidate or adding qualification looks. Database outages emit `ml_maintenance_unavailable`; no failed database write is treated as artifact damage or presumed committed.

Demand diagnostics share one in-flight preparation before timezone/history materialization, including across model instances. Incompatible work returns `preparation_busy` with readiness false rather than queuing another history-sized job. Diagnostic and lifecycle reads use the same 90-day window and 50,001-row overflow sentinel per query; inputs above 50,000 rows are rejected before preprocessing. Cancelling a waiting diagnostic caller releases it immediately; the shared running worker retains ownership until exit. Lifecycle cancellation still drains its worker before releasing the lease.

Each admitted maintenance cycle runs bounded evidence retention (90 days) and reference-aware orphan cleanup (24-hour grace), under the same lease. A batch handles at most 250 issues, terminal requests or artifact directory entries, including any restart replay. Artifact inventory streams without whole-directory sorting, retaining one bounded stream between cycles and persisting its scan identity, high-water name and entries-seen count. After a process restart, the saved name is replayed across bounded pages; if that name disappeared, exhaustion starts a fresh pass. Files added during a pass may wait for a later pass. Current, frozen-comparator, rollback and retained references remain protected, with publication serialization and lease/ownership checks immediately before deletion. No automatic retention, artifact deletion or lifecycle DML runs while maintenance is off.

Reporting exposes five states, never control readiness:

| State | Meaning |
| --- | --- |
| `unqualified` | No currently qualified reporting incumbent; historical fit counts are insufficient. |
| `candidate` | A frozen fit awaits sufficient prospective evidence and a passing fixed decision checkpoint. |
| `qualified-for-reporting` | A compatible incumbent passed the reporting policy and its qualification is still fresh. |
| `expired` | Prior reporting qualification is stale or its scope is incompatible; retained history is not eligibility. |
| `unavailable` | Lifecycle/evidence cannot be reliably read; do not infer zero samples or readiness. |

Incumbent qualification and candidate progress are separate. Counts, missing counts/diversity, exclusions, pending outcomes, last valid outcome, maintenance cadence and next decision checkpoint are read-only reporting. Status reads do not consume decision looks, renew qualification, clear observation or grant ordinary-write authority.

OBS-3/OBS-4 provide exact-fit and prospective native-policy hooks. Fits use chronological fit/hold-out partitions with a maximum-label-horizon embargo: demand 4 hours, comfort 12 hours, thermal 6 hours. Demand needs at least 168 fit intervals, comfort at least 100 fit rows per declared head/horizon plus 20 active rows, four input buckets and 3 degrees of input span, and thermal at least 20 fit segments per declared rate. Transforms and outdoor support use fit rows only. No head is refitted on hold-out labels. Signed hash-addressed publication checks that restored predictions equal the evaluated fit, then commits through lifecycle CAS. Candidates are never planning incumbents. Legacy files and unqualified/expired/incompatible loaded artifacts fall back to untrained/default behavior; a rejected candidate preserves only a healthy qualified incumbent. The canonical demand MAE baseline key is `demand_weather_v3` for both reads and writes. Explicit admin reset may revoke existing lifecycle state but never recreates an epoch or adopts archived files.

Only immutable `prospective_native` issues under `native_unchanged_v1` can qualify. Each issue freezes one shared sensor/history basis, as-issued weather and price availability, native settings, timer program and provenance, dependency/schema fingerprints and comparator hashes. `native_persistence_15m_v1` persists the last confirmed native heating state, DHW state and supply temperature on 15-minute steps; it does not infer future heating from optimizer commands or retroactively insert actual actions. A known active timer program is currently unsupported and excluded. Missing/unsupported timer reads remain explicitly unknown, including on the older pinned library. Reads are cached for at least one hour, use the existing weekly-timer quota category, and occur automatically only while maintenance is enabled.

Reconciliation requires the same device and sensor scope, unchanged exact native settings, no heating-state changes, supply changes no greater than the 1-degree sensor resolution, telemetry gaps at most 15 minutes, outcome timestamps within 10 minutes, indoor sensor age at most 15 minutes, and timer provenance at most one hour old. Tank readings have 1-degree resolution and indoor readings 0.1-degree resolution; these do not relax setpoint/action checks or the qualification improvement threshold. Unknown execution, lookup failure/overflow, overrides, unresolved restores, timer effects, DHW diversion and stale/gapped data invalidate candidate, incumbent and baseline symmetrically. Only explicit never-dispatched proof exempts supersession. Equal electrical counters need exact source-day identity, fresh continuous per-device telemetry and confirmed inactivity before counting as zero. Incomplete coverage never qualifies and exclusions are retained by horizon and 2-degree outdoor bin. Issues are immutable in PostgreSQL; fixed-window ID high-water pages commit outcomes and progress atomically and revisit late targets. Fallback and diagnostic plans cannot block independent native collection. Plan forecasts remain counterfactual reporting, and later action-conditioned recomputation is `retrospective_actual_action`, never prospective evidence.

Prospective admission requires 14 elapsed days, 14 device-day clusters, 20 deduplicated issue cohorts, 60 valid pairs, at least 12 pairs per declared horizon, and three populated 2-degree outdoor bins with at least 12 pairs each. Missing heads cannot inherit another head's result. Bootstrap uses 10,000 deterministic paired resamples of moving 48-hour calendar blocks, including the full cohort calendar for horizon/bin subsets; execution is off-loop in the bounded owned worker with chunks of 128 resamples and at most 50,000 pairs. Sparse, non-finite, flat or incomplete evidence remains unqualified. Demand errors are MAE in kW: the absolute equivalent is at least 5% of each comparator's mean MAE, not 0.10 degrees or a kW conversion of that temperature limit. Comfort/indoor and thermal tank errors are MAE in degrees C and need at least 0.10 degrees improvement. All components also need at least 5% relative mean improvement and positive one-sided 95% lower improvement bounds against both the frozen incumbent (or frozen default predictor) and applicable baseline. Existing temperature MAE/R2/bias/tail limits are reporting gates, not a safety envelope.

Sequential policy `prospective_native_fixed_daily_v1` is fixed in code and the artifact manifest: 71 decision checkpoints, once daily at frozen-fit age 360 through 2040 hours inclusive (days 15 through 85). Each checkpoint closes its issue window 24 hours earlier so maximum-horizon targets can mature. The total one-sided alpha budget is 0.05, divided across the fixed looks, comparator tests and declared head/horizon/bin tests; a passing look needs both the 95% bound and the stronger allocated-alpha bound to be positive. Each look is durably consumed before worker scoring, including interrupted attempts. Six-hour reviews and retries do not add looks. Incumbent renewal uses the same remaining checkpoints; after the final look, a new fit is needed rather than silently starting another testing family.

The allocated-alpha bound is capped by the minimum 48-hour block improvement ratio, an exact lower support bound of the empirical block resampling distribution. This avoids extrapolating tiny allocated tails below the resolution of 10,000 resamples; an adverse supported block cannot be hidden by Monte Carlo resolution. These are empirical forecast-skill bounds, not guarantees about unseen disturbances or device safety.

Qualification is for reporting only and grants no control authority. It expires 72 hours after the last distinct valid outcome supporting a passing cohort, or immediately when sensor/policy/schema scope becomes incompatible. Fit time, file modification time and rescoring cannot renew it. Maintenance off automatically issues, fits, scores and publishes nothing, and ordinary device writes remain structurally denied. The first fixed checkpoint is frozen-fit time plus 15 days (October 24 for an October 9 freeze), not a two-week delivery promise. Sparse installations can need weeks or months for native-policy coverage and outdoor diversity, or remain unqualified indefinitely. Exclusions are never weakened to meet a date or count.

Thermal API, rules, MILP and seasonal readers refresh committed hash-only authority asynchronously, caching it for less than 60 seconds. Detected authority/integrity failures fall back to defaults, without directory or legacy-file adoption. Each thermal head reports `learned` only while that declared head has current prospective qualification and validated fit parameters; other heads report `default`, even when their historical fit was validated. The UI separates historical fit counts from head-specific confidence. Reconciled exclusion reasons remain unchanged in scoring totals and horizon/outdoor-bin distributions.

Migration `033` follows `032`. It adds five ordinary retained lifecycle/evidence tables (`ml_lifecycle_state`, `ml_artifacts`, `ml_maintenance_requests`, `ml_forecast_issues`, `ml_forecast_outcomes`), the non-cycling `ml_lifecycle_token_seq`, native-policy provenance on device status, and immutable issue enforcement. It does not adopt legacy artifacts, change COP archives or enable maintenance/control. Downgrade is allowed only for empty lifecycle storage with no issued tokens; otherwise it refuses. Operational rollback requires a separately reviewed compatible observe-only build retaining this storage, not a populated downgrade.

### Manual COP monitoring

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `manual_cop_monitoring_enabled` | bool | `false` | Experimental daily controller-estimate forecasting only. Manual ledger GET/PUT/DELETE and retention remain available while off. Read through `get_bool_setting`; the Diagnostics toggle is labelled Experimental forecasting. |

The existing `timezone` setting prefills entry only; the operator confirms the physical controller's IANA timezone. Each reading stores its local `period_date` and own timezone; UTC `period_start`/`period_end` are derived by the API (not stored since migration `036`), so they do not change when that setting later changes. The measurement scope is reported as `unknown`. Monthly readings are informational; they never enter forecasts. Predictions require 28 consecutive closed current daily revisions and seven matched-clock retrospective folds with at least 20 available training rows each. Late entries/corrections can keep the panel insufficient even with 28 readings because old revisions are never reconstructed. The empirical log-error band is experimental and uncalibrated, not confidence or physical-efficiency evidence. Missing optional ML dependencies return unavailable without a legacy fallback; no new dependency is needed or installed.

Migration `032` creates an ordinary retained table and non-cycling bigint sequence; forward-only migration `036` removes the stored UTC boundary columns and SQL calendar helper (API contract `2026-10-12.1`). The standalone reset scope `manual_cop` clears every device's readings, sets this setting to `false` and records one audit row without resetting the sequence; see `docs/manual-cop-schema-reset-runbook.md`. Revision/expected-revision JSON scalars are opaque decimal strings. Generic/model resets exclude manual readings and the sequence; delete/recreate and rollback never reuse tokens. Existing backup policy is unchanged and active deletion does not purge backups. Operational rollback disables this setting, uses a retirement-safe build, and retains both objects/data. No automatic downgrade/drop or sequence rewind is supported. Production migration, deployment, release and live enablement require separate authorization. Authentication remains conditional on the existing installed bearer token/perimeter and server proxy, installation-wide rather than per-device.

### Seasonal calibration

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `seasonal_calibration_enabled` | bool | `false` | Opt-in seasonal observation starts in detected cold weather and persists until explicit human exit. |
| `seasonal_calibration_max_outdoor_c` | float | `12` | Activate only when the recent average outdoor temperature is at or below this (°C). |
| `seasonal_calibration_window_days` | int | `7` | Days of outdoor data used for the season decision. |
| `seasonal_calibration_auto_train` | bool | `true` | Queue demand and thermal maintenance when enabled and evidence is sufficient; never fit in the seasonal cycle. |
| `seasonal_calibration_auto_exit` | bool | `false` | Read-only and always effectively false, even if a legacy stored value is true. Cannot end observation. |
| `_seasonal_calibration_campaign_since` | str | _(empty)_ | Internal persisted activation timestamp; cleared only by explicit human exit. |
| `_seasonal_calibration_safety_deferred_since` | str | _(empty)_ | Internal. |

Warming, missing/failed weather lookup, successful fitting, manual training and reporting qualification do not clear an active campaign. Explicitly disabling `seasonal_calibration_enabled` clears its campaign/deferral state atomically; manual observation, if enabled, stays active. Turning learning mode off leaves seasonal observation active, and exiting both still cannot enable ordinary writes in this build. Unresolved linked restores preserve the existing activation deferral/embargo rules.

Optimizer status, seasonal status/progress, and learning-details GETs only read: they do not initialize lifecycle state, latch a campaign, enqueue maintenance, fit, publish, refresh singleton models, or call Panasonic/timer providers. Only the explicit `run_seasonal_calibration_cycle()` performs activation/deferral writes. Its short transaction rechecks opt-in and genuine linked restore obligations under the existing settings lock and a bounded table lock; lock timeout leaves activation unchanged. Existing enqueue calls run after that transaction commits.

Season readiness reports live scoped collection separately from persisted fit/validation and native prospective reporting qualification. Thermal collection uses the fitting extractor and chronological 70/30 split with the existing six-hour embargo, requiring 20 TRAIN and 3 HOLDOUT intervals. Demand retains its 80/20 split, four-hour embargo, and 168 TRAIN/24 HOLDOUT minima. TOTAL includes embargo-excluded intervals, so it is not TRAIN + HOLDOUT. Collected intervals alone do not establish model quality, and neither collection nor fitting requires a qualified incumbent.

Each status response captures one read-only repeatable-read view, then closes its session before calculation. Its cutoff, generation, context digest, and source watermarks describe that view, not later edits; a subsequent completed flight reads a new view. Completed empty extraction is zero, while unavailable counts/context are null. Maintenance enabled/default state, automatic/manual preference, queue/activity, last attempt, and next due time are independent facts; unknown activity or scheduling never implies automatic operation. Existing defaults, fitting/admission rules, qualification gates, cadence, and ordinary-write containment are unchanged.

### Equipment and comfort bounds

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `tank_min_temp` | int | env `TANK_MIN_TEMP` | Tank minimum during comfort hours (°C). |
| `tank_min_temp_offpeak` | int | env `TANK_MIN_TEMP_OFFPEAK` | Tank minimum outside comfort hours (°C). |
| `tank_max_temp` | int | env `TANK_MAX_TEMP` | Tank maximum (°C). |
| `tank_volume_liters` | int | env `TANK_VOLUME_LITERS` | DHW tank volume (L). |
| `sh_max_power_kw` | float | env `SH_MAX_POWER_KW` | Max electrical input for space heating (kW). |
| `comfort_temp_min` | float | env `COMFORT_TEMP_MIN` | Comfort minimum (°C). |
| `comfort_temp_max` | float | env `COMFORT_TEMP_MAX` | Comfort maximum (°C). |
| `comfort_temp_target` | float | `20.5` | Indoor target for the comfort model (°C). |

### Controller heat curve and room-heating gate

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `heat_curve_outdoor_cold_c` | float | `5` | Curve cold outdoor point (°C). |
| `heat_curve_supply_cold_c` | float | `47` | Supply water at the cold point (°C). |
| `heat_curve_outdoor_warm_c` | float | `15` | Curve warm outdoor point (°C). |
| `heat_curve_supply_warm_c` | float | `23` | Supply water at the warm point (°C). |
| `heat_curve_heating_off_outdoor_c` | float | `12` | Controller heating-off outdoor temperature (°C). |
| `heat_curve_delta_t_c` | float | `4` | Controller heating ΔT (°C). |
| `space_heating_behavior_profile` | str | `WH_MXC12J9E8_J_DEFAULT` | Room-heating eligibility profile (only option). |
| `space_heating_baseline_mode` | str | `shadow` | `off`, `shadow`, or `on`. |
| `space_heating_default_fraction` | float | `0.35` | Fallback room-heating duty fraction (0–1). |
| `space_heating_gate_on_offset_c` | float | `1` | Gate ON offset above heating-off temperature (°C). |
| `space_heating_gate_off_offset_c` | float | `3` | Gate OFF offset above heating-off temperature (°C). |
| `_heat_curve_verification_state` | json | `{}` | Internal. |

### Indoor forecast and comfort model

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `use_comfort_model` | str | `false` | `true`/`false`. Use the ML comfort model for indoor prediction. |
| `thermal_lag_minutes` | str | _(empty)_ | Thermal lag override; empty = auto-detect. |
| `indoor_forecast_max_passive_change_c_per_hour` | float | `0.5` | Maximum learned passive indoor change (°C/h). |
| `indoor_forecast_min_r2` | float | `0.15` | Minimum R² for learned indoor control. |
| `indoor_forecast_min_persistence_improvement_c` | float | `0.1` | Minimum improvement over persistence (°C). |
| `indoor_forecast_max_abs_bias_c` | float | `0.5` | Maximum absolute bias (°C). |
| `indoor_forecast_gate_passes_required` | int | `2` | Consecutive passing scorecards to enable learned control. |
| `indoor_forecast_gate_failures_required` | int | `1` | Consecutive failed scorecards to disable learned control. |

### Data sources

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `price_provider` | str | env `PRICE_PROVIDER` | `entsoe`, `tibber`, or `manual`. |
| `entsoe_api_token` | secret | env `ENTSOE_API_TOKEN` | |
| `entsoe_area` | str | env `ENTSOE_AREA` | ENTSO-E bidding zone. |
| `tibber_api_token` | secret | env `TIBBER_API_TOKEN` | |
| `tibber_home_id` | str | env `TIBBER_HOME_ID` | Exact selected home. Empty auto-binds only one active subscription. |
| `tibber_feed_diagnostic` | str, read-only | _(empty)_ | Home-qualified failure code and binding provenance. |
| `manual_price_eur_per_kwh` | float | `0.25` | Static price per kWh for `manual` (in `manual_price_currency`). |
| `manual_price_currency` | str | `EUR` | `EUR`, `SEK`, `NOK`, `DKK`, `GBP`, `USD`, `CHF`, `PLN`, `CZK`, `HUF`. |
| `weather_provider` | str | `open-meteo` | `open-meteo`, `smhi`, or `manual`. |
| `manual_outdoor_temp` | float | `10.0` | °C, for `manual` weather. |
| `manual_wind_speed` | float | `5.0` | m/s. |
| `manual_humidity` | float | `60.0` | %. |
| `manual_irradiance` | float | `200.0` | W/m². |
| `manual_precipitation` | float | `0.0` | mm/h. |
| `outdoor_temperature_source` | str | `weather` | `weather` or `heat_pump`. `weather` falls back to the heat-pump sensor when the report is too old. |
| `outdoor_temperature_weather_offset_c` | float | `0.0` | Added to the weather temperature (°C). |
| `outdoor_temperature_weather_max_age_minutes` | int | `180` | Weather age before falling back to the heat-pump sensor. |
| `latitude` / `longitude` | float | env `LATITUDE` / `LONGITUDE` | Weather location. |
| `timezone` | str | `Europe/Amsterdam` | IANA timezone for schedules and local hours. |

New feed records carry nullable provenance introduced by migration 035:
consumption source timezone, normalized weather units, and hourly price interval
schema. ENTSO-E intervals are duration-weighted only when an hour is fully covered;
provider currency and negative prices are preserved. Weather wind speed is stored
in m/s, including conversion of responses explicitly declaring km/h. Historical
records are not rescaled or backfilled. Planning readiness checks the selected
provider's frozen, coherent input batch, not a newer unrelated feed timestamp;
legacy rows without the required provenance cannot certify a planning horizon.

Rules plans do not claim command-specific electricity costs without supported
serialized-strategy replay: `cost_estimate` is nullable, and `cost_accounting`
states that limitation. Its separately labelled unshifted baseline uses confirmed
per-device counter deltas and their elapsed duration, not cumulative-counter
averages or a default daily consumption. Tariff allocation in measured outcomes
is explicitly estimated uniformly across each confirmed interval's duration;
missing tariffs remain unpriced, and negative tariffs are retained.

Manual heat-curve advice requires fresh selected-room observations and fresh
device status within the configured status-age limit. Future, stale, missing,
or nonfinite evidence cannot yield a suggested curve. Advice remains manual-only
and does not authorize ordinary device writes.
Viewing advice derives verification progress and eligible audit recovery without
persisting settings or completing a durable transition. The GET is read-only;
existing observation thresholds and recommendation gates still apply.
The manual outcome-experiment card uses the same configured device freshness
policy and requires admissible status and persisted gate clocks. Missing, future,
stale or nonfinite evidence cannot produce `manual_review_ready`; this card
creates no device action.

Learned-usage hours follow the configured IANA timezone, including DST gaps and
repeated local hours. Scheduled diagnostic cooldown starts only after a successful
publication commit, never at process startup. Action normalization compares UTC
instants while preserving distinct source/restore ownership keys.

Represented diagnostic planning uses `native_sh_trace_v1`: unchanged hourly
native space-heating demand with explicitly **assumed** provenance, plus integer
DHW timing. ECO, quiet and target-to-compressor effects remain `unproven`.
The trace binds the cached device/status identity and aware observation/capture
clocks (using the existing configured status/polling freshness policy), observed tank target, and
known inactive forced-DHW state. Unknown context refuses certification. Pulses
at or crossing the observed target-minus-0.5 °C command threshold remain
unproven: the replay does not invent heating after command suppression or target
termination. No tank target writes are generated. Rules build and replay this
trace without importing or constructing the optional PuLP solver.
Saved price/weather/target fields retain their public snapshot contract;
`forecast_no_heating` explicitly represents a fully passive, no-SH/no-DHW
counterfactual, not an inferred native-controller trajectory.
Unknown/incomplete controller gates or hardware tank ceilings cannot certify
a horizon. Tank/room initial states are observed, not clamped; any initial
violations are disclosed. Hard modeled floors, ceilings, deadlines and pulse
extrema are checked by a separate serialized-event replay, including SH diversion
while DHW owns the shared plant. Replay prices electrical kW × elapsed hours
exactly once. Feasibility/cost are conditional on the declared model, not physical
comfort guarantees or globally optimal household costs. Rules fallback is accepted
only after its actual DHW commands pass the same independent replay. Both invalid
strategies refuse planning. Published diagnostics remain non-active with no
executable actions; modeled commands are retained only as diagnostic evidence.

Service entrypoints own startup cleanup from their first await and track all
scheduled coroutine jobs until their cleanup ends. A cancelled solver await waits
for its owned thread to finish. Scheduled jobs drain before maintenance, then
ordinary logs drain within their five-second budget, then wrapper resources close.
The API, poller, optimizer and standalone ML entrypoint dispose their database
engine last, after these owners settle, including on startup failure or cancellation.
An unsettled log writer blocks disposal and retains ownership rather than claiming
successful shutdown. Cancellation-resistant teardown does not relax restore
attempt deadlines or grant ordinary-write permission.

Ordinary DB logging reserves capacity **before** encoding. Its 1,000 total slots
include rows still being encoded and in-flight batches; batches contain at most
100 rows, and each encoded row is limited to 8,192 bytes. Traversal is bounded to
4,096 nodes and 16 levels without copying the input tree or invoking arbitrary
string-conversion hooks. Oversized, unsupported and unencodable rows release
their reservation and increment the oversize counter. Admission never waits for
capacity. Failed-write diagnostics use at most one bounded-message daemon writer;
a stalled stderr drops subsequent diagnostics instead of blocking the event loop.
Diagnostic losses are reported separately from row losses. Ordinary logs retain
the existing retry/age limits and five-second shutdown budget, may duplicate
after ambiguous commits, and may be lost on abrupt death. Transactional safety
audit records remain outside this dropping queue.

### Panasonic

Capability `availability.commands_allowed` is false in this observe-only build.
`observation_available` separately describes fresh observed hardware/adapter
availability; observed support does not authorize ordinary automation.

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `aquarea_username` | secret | _(none)_ | Panasonic ID username. |
| `aquarea_password` | secret | _(none)_ | Panasonic ID password. |
| `poll_interval_seconds` | int | env `POLL_INTERVAL_SECONDS` | Expected poll interval used by data-freshness checks and alerts. The poller schedule itself uses the `POLL_INTERVAL_SECONDS` environment variable. |
| `device_status_max_age_minutes` | int | `15` | Maximum device-status age for control readiness (5–60). |

### SmartThings

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `smartthings_enabled` | str | `false` | `true`/`false`. |
| `smartthings_client_id` | secret | env `SMARTTHINGS_CLIENT_ID` | OAuth client ID. |
| `smartthings_client_secret` | secret | env `SMARTTHINGS_CLIENT_SECRET` | OAuth client secret. |
| `smartthings_redirect_uri` | str | env `SMARTTHINGS_REDIRECT_URI` | Must match the SmartApp registration exactly. |
| `smartthings_pat` | secret | env `SMARTTHINGS_PAT` | Legacy Personal Access Token. |
| `smartthings_device_ids` | str | _(empty)_ | Sensors to poll; empty = all discovered. |
| `comfort_reference_sensor_id` | str | _(empty)_ | Reference sensor; empty = robust median of selected sensors. |
| `smartthings_poll_interval` | int | `300` | Seconds. Read by the poller at start-up. |
| `smartthings_device_max_age_minutes` | int | `180` | Readings older than this are excluded (30–1440). |
| `_smartthings_oauth_state` | secret | _(empty)_ | Internal OAuth CSRF state. |

### Alerts, experiments, display

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `operational_alerts_enabled` | bool | `true` | In-app alerts for stale data, failed actions, degraded quality. |
| `operational_alert_webhook_url` | secret | _(empty)_ | Optional HTTPS webhook for alerts. |
| `_operational_alert_delivery_state` | json | `{}` | Internal throttle state. |
| `outcome_experiments_enabled` | bool | `false` | Manual heat-curve trial suggestions; never sends commands. |
| `outcome_experiment_max_curve_step_c` | float | `0.5` | Largest suggested curve step (°C). |
| `currency` | str | `EUR` | Display currency: `EUR`, `GBP`, `USD`, `SEK`, `NOK`, `DKK`, `CHF`, `PLN`, `CZK`, `HUF`. |
| `time_format` | str | `24h` | `24h` or `12h`. |

## Adding a runtime setting

Register the key in `SETTINGS_SCHEMA` with `type`, `description`, and either
`default` or `default_env` (the `Settings` attribute name). Add `options` or
`min_value`/`max_value` to have the API validate input. Then add a row to this
reference.


### Reporting windows and source-qualified costs

`GET /api/stats?period=day|week|month` measures the rolling 24-hour, 7-day or
30-day window `[window_start, window_end)`. `price_area`, `price_source` and
`price_currency` identify the active feed; unrelated sources, currencies and
future/end-hour tariffs cannot contribute. `/api/prices` is unchanged.

Stats, dashboard daily costs and measured plan outcomes use uniform-duration
allocation within confirmed, per-device/per-channel counter intervals. Reads
retain two hours before and after the window for interval inference and change
confirmation; energy allocation alone is half-open. Daily resets use recorded
source-day/timezone provenance, not sums of daily cumulative maxima. Allocation
is estimated and is not command attribution. Reporting baseline prices are
weighted by hourly tariff overlap duration, not by all supplied prices.

`total_kwh` is confirmed overlapping energy (a partial quantity when telemetry is
incomplete), or null without any confirmed evidence. `telemetry_status` and
`complete` distinguish meter completeness from tariff coverage. `priced_kwh`,
`unpriced_kwh`, `coverage_pct` and `partial_priced_amount` describe only confirmed
energy, not unknown use. Coverage is null, never 100%, without confirmed energy.
`total_cost_eur` is null unless telemetry and pricing are complete; zero and
negative prices remain valid. Legacy `*_eur` monetary fields carry the explicit
source currency, **not** automatically converted EUR.

Completeness conservatively requires confirmed interval coverage of all three
counters for each observed device throughout the window. Flat, missing, stale,
unresolved-reset and unconfirmed-tail counters do not certify zero use. The
existing affirmative-zero contract covers space heating only, not whole-device
heat/cool/tank energy, so this reporting API cannot certify complete zero total
use. It returns unavailable/incomplete rather than manufacturing zero evidence.
Dashboard exposes `today_telemetry_status` and `today_cost_allocation_basis` with
the same rules. Measured outcomes retain `actual_cost` as a **partial priced
amount** for compatibility, with `total_cost` nullable and `complete` explicit.
Energy/cost are rounded to 3 decimals and tariff averages to 5 decimals.

These are reporting-only corrections. Feed persistence, frozen rules/MILP price
inputs, confirmed-interval extraction and the rules observed-rate baseline are
unchanged. Revert reporting API and web contracts together; no migration is
needed. Tibber home identity/tariff changes are documented below and remain subject to their separate HIGH-risk release approval.


Reporting `incomplete_reason` (dashboard: `today_cost_incomplete_reason`) is null
for complete results, `telemetry_unavailable` without confirmed intervals,
`telemetry_incomplete` when whole-window/channel coverage cannot be certified,
or `tariff_incomplete` when telemetry is complete but confirmed energy lacks
prices. Meter incompleteness takes precedence if both kinds are missing;
`unpriced_kwh` still separately describes tariff gaps. Fully priced **confirmed**
energy can have `unpriced_kwh=0` and `coverage_pct=100` while the whole-window cost
is null: flat channels, missing leading observations or unconfirmed tails remain
unknown energy, not invented unpriced kWh. The six-hour E2E fixture with heat
counters 1.2 through 1.7, flat cooling 0 and tank 0.5 has exactly this status.

### Tibber installation binding (P1.6b)

Set `tibber_home_id` through the settings API (or `TIBBER_HOME_ID` before first binding).
IDs are exact, case-sensitive provider strings; surrounding whitespace is rejected.
With an empty ID, polling automatically persists the binding **only** when exactly
one returned home has an active subscription. The read-only diagnostic records
`single_active_subscription` provenance. This automatic binding is revalidated on
**every poll** and is valid only while exactly one eligible home exists. If a
second eligible home appears (or eligibility becomes ambiguous), no prices are
accepted and `auto_binding_requires_explicit_selection` suspends Tibber planning.
This suspension is latched, even if only one home is returned again. The owner must
explicitly write `tibber_home_id` through the settings API, including re-saving the
same ID, to clear the suspension and record `explicit` provenance atomically.
Explicit selection remains valid with multiple homes, provided the selected home
is returned with an active subscription and usable prices. Zero or multiple eligible homes, an
explicit ID absent from the response, no active subscription, or no prices fail
closed: polling logs `price_fetch_failed` with a fixed reason code, the diagnostic
is visible through settings, and current planning reads an unavailable scope.
Never select the first home or reuse another home's prices. After changing homes,
planning resumes only when the new installation satisfies normal feed-quality admission.

A poll captures provider/token/home in one settings snapshot before provider I/O.
A concurrent setting change does not relabel its results: they remain under the
captured home's scope. Auto-binding uses a transactional compare-and-set on the
empty setting and cannot overwrite a concurrent explicit selection. Diagnostic
updates lock/check the captured home and cannot suspend another home's feed.

Migration **037 must run on deployment before the new poller starts**. It adds
`price_scopes` only; `prices.area` stays `VARCHAR(32)` and no hypertable is altered.
The canonical identity is UTF-8 compact JSON `["tibber", "<exact home ID>"]`
(`ensure_ascii=False`, no Unicode/case/whitespace normalization). Its scope is
`tb:` plus the first 28 lower-case SHA-256 hex characters (31 characters total).
Registry scope and `(provider, home_id)` keys are unique. In the price-write
transaction, insert-on-conflict-do-nothing waits for concurrent inserts, then
verifies the full immutable mapping and tariff metadata; any mismatch rejects
and rolls back the write. Database triggers reject updates, deletes and truncation.
Existing `tibber` rows remain untouched and never become a newly bound home's data.

Plans retain scope/source/currency in their immutable input provenance. Historical
measurement uses that persisted scope even after settings change. Ambiguous legacy
Tibber plans expose cost `unavailable` with `legacy_tibber_scope_ambiguous`, while
retaining non-price outcomes; they are never guessed or backfilled.

Amounts remain provider totals, including finite zero and negative values. The
immutable tariff contract records `provider_total`, source currency,
`hourly_duration_weighted_v1`, `tibber_provider_total_v1` and application extra fee
`0`. This owner's Tibber total already includes grid fees; this is **not** a claim
about all provider contracts. No fee is added here. Grid/extra-fee arithmetic and
new fee settings belong exclusively to **P2.4**.

Rollback: disable Tibber planning admission before rolling application code back;
retain the registry, qualified prices and plan provenance. Do not restore first-home
fallback. Migration downgrade removes only the registry/function, never prices;
that destructive metadata downgrade is for owned migration qualification, not the
production rollback procedure. Owned PostgreSQL/Timescale CI must verify 036 to 037
and downgrade on populated prices; local offline collection is not execution evidence.


Native ML forecast materialization resolves the active price scope from its captured
settings in the caller's database session, before ordering or limiting price rows.
Its immutable `pricing.area` records that scope; suspended or latched Tibber bindings
produce `pricing.available=false` and no price points, without legacy/other-home fallback.
Stats and dashboard costs retain P1.6a's confirmed-interval allocation, nullable/partial-cost semantics and tariff-window methodology, with P1.6b's registry-verified active-scope filtering layered onto every price read. Suspended or latched bindings yield no tariff rows and therefore no fabricated price or cost; finite zero and negative prices remain valid.
Compatibility poll-now paths persist Tibber prices under the captured fetch identity,
registering that identity in the same transaction; an in-flight setting change cannot
relabel those rows. Suspended fetches write no prices.

### External reporting heartbeat

The optional Healthchecks.io sender remains disabled unless explicitly enabled. Its
runtime URL is a full-redaction secret setting. An empty database value clears
the override and falls back to `EXTERNAL_HEARTBEAT_URL`; use
`external_heartbeat_enabled=false` to disable rather than relying on an empty URL. Create a separate provider check per
deployment and never use a real ping URL in source control or tests. The sender
requires fresh poller and optimizer heartbeats (no older than 180 seconds), Redis
`PONG`, API `/health` healthy payload, and ready persisted device observations.
Unhealthy-but-running containers are alert-only; a hung process that continues to
satisfy these signals may not be detected. See `docs/heartbeat-runbook.md` for setup.
