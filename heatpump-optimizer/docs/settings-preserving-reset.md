# Local settings-preserving fresh start

**HIGH-risk operator procedure, not a web reset and not physical safety verification.**
The reviewed source implementation does not authorize running it. Obtain approval of
the literal runtime manifest separately. The approved terminal-retirement amendment
supersedes pending legacy carryover: no carryover counts or executable work are created.

The entry point is `python -m packages.core.reset_operator`. It has only
`fingerprint`, `plan`, and exact-digest `apply` interfaces. It never controls
services/devices, calls Panasonic, changes Redis, changes environment files, or creates
backups. It has no force option, generic retirement predicate, or physical-resolution API.
Ordinary writes remain prohibited. Positive manual **or** seasonal Learning Mode
continues to hold **all** dispatch, including restores, even if another lookup fails.
Expired sources remain excluded from dispatch.

## Passive discovery is not locked preparation (PF-T1)

An informational target-preflight artifact marked
`preliminary_read_only_target_preflight_NOT_EXECUTABLE` records observations only.
It may describe still-running owners and mixed Compose provenance. It is **not**
a scope approval, executable manifest, or permission to quiesce resources, provision
maintenance storage, acquire database locks, retire an obligation or reset data.
Use the existing informational preflight for passive discovery; do not feed it to
`plan` or `apply`. Both reject that artifact before resource inspection or DB work.

`plan` is **lock-taking preparation**, not passive discovery. Although its output
is secret-free approval material and it does not retire or wipe rows, its transaction
takes device/maintenance advisory locks, exclusive table locks and locked row reads
(`FOR UPDATE`). Run it only after **separate authorization** for that preparation,
with all required owners quiesced and the dedicated maintenance resources prepared.
`inspect_resources` also requires that quiescence; it is not a general running-stack
discovery interface. A successful plan does not authorize apply: review and approve
the newly generated exact manifest digest separately.

## Preserved and cleared boundaries (RESET1, RESET2, RESET4)

All rows, values and `updated_at` columns in `settings` survive unchanged, including
credentials, internal runtime keys, campaign timestamps, maintenance preferences and
`manual_cop_monitoring_enabled`. All `smartthings_oauth_token` rows/columns survive.
Equality is checked inside the transaction without logging/exporting values.
External environment/credentials, schema and Alembic **036** are untouched.
All sequences continue; no identity or manual-COP revision sequence is restarted.
Redis quota/auth state is untouched: never flush Redis.

The sole deletion statement is one explicit `TRUNCATE ... CONTINUE IDENTITY RESTRICT`
covering these application tables:

```
device_status consumption prices weather indoor_temp_reading faults
manual_cop_observations cop_history plans plan_actions executor_verification_reads
optimization_requests overrides shower_events space_heating_gate service_heartbeats
ml_forecast_outcomes ml_forecast_issues ml_maintenance_requests ml_artifacts
ml_lifecycle_state app_logs audit_log
```

Unknown/missing public application tables, external referencing foreign keys, unexpected
sessions or lock/dependency failures abort; no CASCADE, database/schema drop, forced session
termination, sequence restart or broad fallback is available. Timescale internal storage
is handled only by Timescale's own truncate implementation, not enumerated or deleted
separately. The retained Alembic table must contain exactly `036`.

Bootstrap exceptions are **one new lifecycle UUID**, with new monotonic tokens, empty
checkpoints, references and leases, and **two minimal audit records** for terminal
retirement and this reset. Ordinary log reset cannot remove these audit actions.
The maintenance gate follows the retained preference (absent means its existing false
default), without writing that preference. Existing manual-COP observations are cleared
without disabling forecasting: **enabled with no data**, not ready or disabled.

No full-data backup is made under this design. After reset commit history is
irrecoverable. Normal ingestion can subsequently retrieve prior-period cloud data.
The empty-table assertion is before restart, not an ongoing collection embargo.

## Preconditions and exact approval workflow (RESET3, RESET5)

These are instructions for a separately authorized operator, **not actions performed
by the implementation agent**:

1. Bind the reviewed build/source fingerprint, `desktop-linux` Docker context, exact
   Compose project and per-container literal config-file lists, database name/OID/server address/port, full project container
   ID inventory and both model-volume names. Do not use unrelated heatpump, QRMS or
   Immich resources for a test. Keep backup stopped throughout.
2. Gracefully quiesce every application owner: web, API, poller, optimizer, one-shot
   jobs, maintenance/log writers and device actors. Use the existing owned scheduler,
   wrapper, task and pool shutdown paths. Verify shutdown completes before proceeding.
   A dead/restarting/paused container, database client session, claim token, executing/
   dispatched action, lifecycle lease or running job is not settled; do not force it.
   Prevent supervisors/operators from restarting writers during this procedure.
3. Prepare a **fresh, empty, explicitly project-labelled model volume**. Do not copy
   artifacts. Keep the old volume detached from all running consumers, quarantined,
   ineligible as a fallback. Do not delete it in this procedure.
4. Use a dedicated project-labelled **Linux maintenance container**, running this
   source, with an explicitly supplied DSN environment variable and the new volume
   mounted at `--model-dir`. All other non-DB project containers must be exited.
   Docker CLI context `desktop-linux` in this container must explicitly reference
   the local `unix:///var/run/docker.sock`, not a remote daemon. This tool issues only
   bounded read-only Docker `ps`/`inspect` queries. Its actual HOSTNAME/container ID,
   mount destination, project labels and DB container address must match the manifest.
   Provisioning this container/context/mount is an operator task, not tool automation.
5. Provide an operator-owned receipt directory mode **0700** on durable Linux storage.
   Receipt files use exclusive creation, mode **0600**, fsync, directory fsync and
   readback. Unsupported Windows receipt durability is refused before DB mutation.
   Custody is with the operator/Delivery Lead; for this operation archive receipts in
   `.agent-runs/20261016-fresh-start-safety/` after transferring them securely.
6. With locked preparation separately authorized, produce a plan while writers stay
   stopped. The scope JSON has exactly these keys
   (replace every placeholder with fresh inspected identities, never credentials):

```json
{
  "context": "desktop-linux",
  "project": "<exact-project>",
  "compose_files": {
    "<full-DB-container-ID>": ["<literal-original-compose-path>"],
    "<full-application-container-ID>": ["<literal-generated-deployment-compose-path>"],
    "<full-backup-container-ID>": ["<literal-original-compose-path>", "<literal-backup-override-path>"],
    "<full-maintenance-container-ID>": ["<literal-maintenance-compose-path>"]
  },
  "database": "<database>",
  "database_oid": 0,
  "server_address": "<DB-container-IP>",
  "server_port": 5432,
  "old_model_volume": "<old-detached-volume>",
  "new_model_volume": "<new-empty-volume>",
  "source_revision": "<fingerprint-command-output>",
  "owners": ["<full-DB-container-ID>", "<full-application-container-ID>", "<full-backup-container-ID>"],
  "maintenance_container": "<full-maintenance-container-ID>"
}
```

`owners` includes the running DB and stopped backup; it is an exact inventory,
not a list of containers to stop. Add **every** other project owner to both `owners`
and the mapping individually. `compose_files` keys must be exactly the full-ID set
`owners` union the maintenance ID. Every value is that container's **ordered literal**
`com.docker.compose.project.config_files` label split on commas. The maintenance
container needs its own approved entry too.

Infrastructure created from original Compose files and applications created from a
generated deployment file may coexist, but each inspected container must match
**its own** approved list exactly. No global union, cross-container substitution,
basename comparison, subset match, sorting, case/slash conversion, path resolution
or other path normalization is permitted. List order is approval material.

Executable manifests are **version 2**. Version 1/global-list manifests are
incompatible and rejected, even with a recomputed digest; there is no automatic
upgrade. Regenerate the source fingerprint, scope and lock-taking plan under the
new contract, then obtain fresh exact-digest approval. Never relabel an old manifest,
reuse a stale approval, or treat an old receipt as approval of changed provenance.

Operator interfaces, shown for documentation only:

```text
python -m packages.core.reset_operator fingerprint
python -m packages.core.reset_operator plan --input scope.json --dsn-env LOCAL_RESET_DATABASE_URL --model-dir /fresh-models --receipts /private-receipts --operation-uuid <UUID> --operator <identity> --statement "<exact-invalidity-attestation>" --statement-at <timezone-aware-time> --device-id <selected-device>
python -m packages.core.reset_operator apply --input approved-manifest.json --dsn-env LOCAL_RESET_DATABASE_URL --model-dir /fresh-models --receipts /private-receipts --approved-digest <approved-exact-digest>
```

Capture the secret-free **locked-preparation** output as the version-2 approval
manifest, not as a passive observation. The DSN is read only
from the explicitly named environment variable, never a CLI argument or an inferred
application `.env` target. Normal `packages.core` package initialization still reads
application configuration and creates its unused lazy engine; the reset entry point
never borrows that engine or opens a connection through it. Do not print DSNs or
configuration values. Have the operator approve the full exact plan
and digest, including the attestation/time and tuple **11763 / 11762 / event 26**.
The tool is intentionally limited to this tuple and to pending restore / expired
source / matching open event. Different current state, new obligations or a changed
fingerprint require a new reviewed decision, not editing a target predicate or
reusing the digest. Approval of implementation is not approval to execute a reset.

## Settlement, receipt and failure stages (REPAIR1, REPAIR2)

The prepared receipt says **approved intention**, not completed retirement.
The first bounded transaction obtains the same device ownership key as execution,
the maintenance key, exclusive application table locks and action/event row locks.
It inventories all unresolved restores, open shower events, inflight actions and
unpaired possibly-effectful activations. It revalidates the exact fingerprint,
linkage, device and epoch and refuses any new/unapproved work or active claim.

It sets only the matching restore and event to **operator_invalidated**, with
**reset_superseded** reason in the administrative metadata/audit. The source stays
expired until history is wiped. Previous result context survives retirement.
No `executed_at`, `recovered_at`, dispatch provenance, observed OFF, success or
physical verification is fabricated. Every minimal receipt explicitly records
`device_write: false` and `physical_state_verified: false`.

Committed DB audit readback and a durable finalized retirement receipt are required
**before** the second transaction can wipe anything. That transaction reacquires
locks, checks resources/sessions/inventory/fingerprint/epoch again, retains exact
configuration rows in memory, truncates only the allowlist and verifies bootstrap,
configuration equality and empty targets before commit. DB transactions have a
45-second outer bound, 2-second lock timeout and 30-second statement timeout.

Failures report the last known stage, without driver SQL or credential values:

| Stage | Meaning / operator action |
|---|---|
| preparation | No confirmed retirement; inspect audit by UUID before any retry |
| retirement_commit_pending_readback | Commit response may be ambiguous; read back by UUID |
| retirement_committed_receipt_pending | Retirement committed, but readback/receipt incomplete; **no wipe** |
| retirement_receipt_durable | Retirement is durable; reset transaction did not complete; do not undo legitimate invalidation |
| reset_commit_pending_readback | Reset commit response may be ambiguous; inspect by UUID before retry |
| reset_committed_receipt_pending | Reset committed; final readback/receipt must be repaired before restart |

On every failure **leave workers stopped**. No automatic rollback to stale models
or automatic service restart exists. Replay the **same UUID and identical manifest**
only after resolving custody/ownership; it returns the committed result, never
duplicates retirement or repeats an already committed wipe. Changed attestation,
scope, targets or fingerprint with the same UUID conflicts. Never invent timestamps
or claim physical recovery to make a replay pass.

## Model transition and read-only verification (RESET4, RESET6, REPAIR3)

After committed receipt readback, the operator must recreate all API/poller/
optimizer model consumers against the **same new volume**, verify mount identities,
fresh UUID/monotonic tokens, no old artifact references/files, empty model status,
no leases/jobs/qualifications/outcomes, and truthful GET responses before normal
collection resumes. A clean startup clears process-local model/status caches.
Never reconnect the old volume or restore old mutation tokens. Verify current
unresolved counts independently; retired audit history is **not** pending authority.
Only after successful verification may cleanup of the exact detached former volume
be separately approved. This tool neither deletes it nor claims that cleanup is done.

GET/UI reporting distinguishes retained campaign timing from `data_reset_at` in the
reset audit; neither elapsed campaign time nor retained enablement proves evidence
survived. Administrative retirement is displayed separately from verified physical
OFF. Current/future safety blockers take priority over training guidance, including
during a retained active campaign. Unknown current state is unavailable, never clear.
A retired-only state does not recreate a restore, embargo or seasonal blocker.

Normal future verified OFF completion closes only its matching open shower event
inside the fenced action/audit transaction. Recovery/expiry takes the existing
device ownership key and re-reads the row under lock, so stale work cannot reopen
either a recovered or administratively retired event.

## Disposable validation

Unit coverage is in `test_data_reset.py`, `test_reset_operator.py`,
`test_reset_safety_repair.py` and `test_reset_learning_guard.py`; existing learning,
shower and safety tests retain their fixtures and network/source guards.
Mocked UI CG-T8 cases are in `web/e2e/learning-mode.spec.ts`.

Real storage qualification is explicit-only:
`python -m pytest tests/isolated_reset_postgresql.py -q` on Linux with normal fixtures.
It is **required evidence**, not replaced by unit fakes. The Test Engineer must first
provision a brand-new, isolated, migrated schema-036 Timescale stack using the pinned
image digest in that test. No shared database/network/bind mounts are accepted.
Supply `HPO_RESET_TEST_PROOF` pointing to:

```json
{"namespace":"<fresh-32-hex-UUID>", "context":"desktop-linux",
 "project":"reset-disposable-<same-UUID>", "container":"<full-DB-container-ID>"}
```

Set both `HPO_RESET_TEST_DATABASE_URL` and `DATABASE_URL` to that disposable database
only, with user `reset_test`, database `reset_test_<same-UUID>`, host `127.0.0.1`,
and a unique published port >=20000. Never use existing heatpump test ports/stacks.
All storage and networks must carry that exact project label. The test verifies
ownership before connecting; missing proof is a failure, not a skip. It exercises
real hypertable/FK truncate, atomic audit-failure rollback, device-lock refusal,
retained-column equality, monotonic lifecycle/manual-COP sequences and replay.
It leaves its owned evidence/resources for inspection; teardown is separately scoped.
Additional competing-writer/race and failure-injection qualification remains required
before operational use. No test authorizes production execution.
