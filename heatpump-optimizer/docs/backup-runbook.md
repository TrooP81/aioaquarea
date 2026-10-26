# Database backups and isolated restore qualification

**Staged implementation, not activation evidence.** No worker start, live dump,
Docker drill, production restore, device access or notification delivery is
authorized by this document.

## Approved policy and recovery boundary

Owner approval recorded 2026-10-16 16:52 covers implementing reviewed Repair 1 and
the Repair 2 monthly amendment. Monthly means 2,592,000 seconds (30 days UTC).
Freshness is 32 days, retention 100 days, with a non-relaxable two-eligible-snapshot
floor and latest-success/verified protections. Nominal RPO is approximately
30 days; failures can extend it. The owner accepts local-only loss, excluded
external encryption keys/Compose secrets/configuration/`modeldata`, and one initial
source/schema restore qualification rather than recurring automatic drills.

The deployment-only primary root is `D:\backups\heatpump-optimizer\`, never
`D:\backups\`. Replica is disabled and not required. These paths must not be
created or written during implementation/testing.

## Explicit legacy transition and archive-preserving one-shot

The owner’s **2026-10-26T12:59:01.369+01:00 “go ahead”** approves implementation
and testing of the independently reviewed HIGH design (including F1), **not live
execution or archive deletion**. New-candidate independent tests, CODE review,
CI and release gates remain required. Historical `3cd0ba6` CI is not acceptance
of these source changes. API-up / poller-and-backup-held remains the operational
boundary; this document does not authorize changing the local stack.

The worker has three distinct invocation contracts:

| Invocation | Contract |
|---|---|
| No arguments | Existing normal scheduler and retention policy. |
| `--once` | Existing single due evaluation; may do nothing and still exit zero; retention remains enabled. |
| `--once-preserve-archives` | One due evaluation only; never enters retention or its diagnostic operation, never sleeps into scheduling. Zero means a completed, durably reported backup success, **not restore qualification**. Busy, not due, preflight/CAS rejection, failure, or unresolved reporting is nonzero. |
| `--once-preserve-archives --transition-legacy` | Same preservation contract, with narrowly authorized atomic conversion of one exact legacy row into a genuine running worker attempt. |

Unknown/duplicate options, mixed one-shot modes, and transition without
preservation mode fail before locks, tools or destination mutation. These modes
do not change persisted settings, Compose restart policy, or service state.
Any separately approved operational launch must explicitly disable restart;
the worker cannot enforce an external orchestrator's restart policy.

Preservation invocations require independently obtained
`BACKUP_EXPECTED_SOURCE_ID` and `BACKUP_EXPECTED_SCHEMA_REVISION` (the reviewed
operation is `039`). The explicit transition additionally requires
`BACKUP_LEGACY_EXPECTED_SHA256` and `BACKUP_LEGACY_EXPECTED_UPDATED_AT`. These are
invocation fences, not defaults or stored settings. Under separately approved
execution, prepare the SHA-256 from PostgreSQL's original UTF-8 **text**, not
reserialized JSON, and the original `timestamptz`. Keep the raw row and effective
configuration private; never pass raw legacy text through shell arguments or
publish it in logs. A changed timestamp, text digest, source, schema, observed
state, unsupported shape, duplicate JSON member, or competing owner rejects
the transaction without creating output or replacing the row.

Only an existing JSON object with exactly one `status: "ok"` member qualifies.
Ordinary version1 activation remains supported; no implicit legacy acceptance
is added. The status transaction saves original `service`, timestamp, exact text
and digest atomically with the genuine running attempt in the private
`legacy_transition` envelope. It records the real first terminal outcome when
available. A failed first attempt remains failed after a later successful retry;
unsettled first-attempt reporting remains incomplete, never fabricated.

Retry using **only** `--once-preserve-archives`, with the same independently
checked source/schema fences. Retry spacing, success-anchored due time,
stale-running deadline, destination lock, server-side settlement and locked SQL
CAS still apply. Never clear/reset the row or restore legacy state over actual
history. A source-bound per-attempt application name lets cancellation settle
the worker's actual server-side activity; inability to prove settlement leaves
incomplete evidence rather than claiming completion.

Terminal outcomes and their bindings are immutable. Exact terminal replay is a
no-write operation, including unchanged heartbeat time. Lost acknowledgements
are reconciled using the same attempt/event; committed success is never
relabeled failed. In normal mode only, retention refusal uses a separate,
ownership/binding-fenced `backup_retention_diagnostic` operation. It may add only
`last_attempt.reason_code=backup_retention_blocked`; replay is no-write and does
not clear that overlay. It never rewrites `last_success`, verification or the
original/first-attempt audit. Preservation mode skips both operations and cannot
manufacture a retention warning or clear historical diagnostics.

Output paths are collision/symlink checked before ownership; publication refuses
overwrite. Cleanup is restricted to invocation-owned scratch, never archives.
Rollback means hold execution and preserve all original/new history and
artifacts, not deletion, live restore, migration, downgrade or automatic restart.

Focused real-SQL qualification is in `tests/helpers/qualify_backup_status_postgresql.py`,
required by canonical `backend-e2e` through the existing owned runner's
`backup-status-inventory` and `backup-status` phases. The separate full
collection inventory is reconciled with every executed setup/call/teardown
before the full seal; absent phases, omitted/deselected cases, skips, failed
teardown, source drift or out-of-manifest modules refuse the seal. Ordinary
unit/E2E discovery and its existing partitions remain unchanged.

The parent retains its prepared-profile SQL/platform and exact-source guards;
only these two children's environments omit `DATABASE_URL`, retaining the
matrix's refusal to consume any configured database. The helper can also be
run explicitly with `--noconftest` and no `DATABASE_URL` for focused disposable
checks; that alone cannot satisfy the canonical seal. It uses only an
already-present immutable PG16/Timescale2.26.4 image, a creation-owned
networkless/read-only container with disposable tmpfs, and synthetic schema039
data. It covers actual SQL/CAS, concurrency, replay, lost acknowledgements,
server-side cancellation, collision preservation and a checksum-bound synthetic
dump/restore. That focused database round-trip is **not** the owned coordinator
qualification, new-candidate CI, or BAK2 household acceptance. The existing
supplementary `tests/e2e/test_backup_roundtrip.py` now exercises the explicit
legacy/current039 preservation path within its existing proof/receipt boundary.
Neither test authorizes any household operation.

## Required qualification before activation

1. Run offline unit checks with the complete disposable environment set before
   imports. Exercise health histories, merged-policy validation, API/webhook
   allowlists, retention guards and lifecycle failures.
2. Execute the existing **owned Linux CI `backend-e2e`** preparation, inventories
   and all ordinary/manual/lifecycle phases. The required `backup-status` matrix
   follows `check-ordinary` and precedes supplementary `backup-roundtrip`, then
   `check-backup` and the full seal. The SQL phase keeps the same 600-second
   child / 660-second external-parent bounds. Adding its literal source binding
   increases the exact required-source counts from 43/48 to 44/49, without
   relaxing uniqueness or completeness. These phases cannot
   substitute for existing phases, skip/xfail nodes, or fabricate ownership.
3. Use the pinned coordinator base below. Dockerfile and CI have the same checked-in
   default; no `HPO_RESTORE_COORDINATOR_BASE_IMAGE` repository variable is required.
   Its derived image and Unix-socket entry path must qualify on synthetic data
   before documenting Windows-host support.
4. Confirm worker tools (`flock`, `timeout` with `-k`, `setsid`, `psql`, `pg_dump`, `pg_restore`,
   `sha256sum`, and OpenSSL if replica-enabled) on the pinned image. No missing-tool
   workaround, dependency installation, relaxed restore guard or Timescale catalog
   filtering is an acceptable passing result. Tool/dependency changes need approval.
5. Keep only synthetic metadata/receipts in CI evidence. Never upload a household
   dump, source credential or raw database error.

Native Windows shell/Docker qualification is not established. The Windows-host
route is a separately approved short-lived Linux coordinator container with no
network and access to Docker Desktop's local Unix socket. Socket access is
privileged and must never be mounted into the scheduled worker.

### Coordinator base and rotation

The checked-in default is the official Docker CLI stable release, resolved and
verified by read-only registry HTTP queries on **2026-10-16**:

```text
docker:29.9.0-cli@sha256:1a4c7cb63513f349bdad01fcc6e0f3f2f67d37b9da86f14dc0d4a0942eecda00
```

This is the multi-platform **index** digest, not an image-config digest. The
inspected Linux/amd64 child is
`sha256:4caa52207e95c68c3087006eefb3a5f6ca48ad1c0ba3aa3e6843549a0720b7bb`;
its annotations identify Alpine 3.24. The derived image adds only the authorized
`python3=3.14.8-r0` package, confirmed in the official Alpine 3.24 main indexes
for x86_64 and aarch64. The coordinator uses only the standard library and requires
Python 3.11 or newer; it does not import the application.

The official CLI base avoids separately downloading and maintaining a static
Docker binary/checksum installation path. The base digest pins its existing tools;
the exact Python package version fails closed if unavailable. Transitive APK
dependencies and repository availability are not a fully locked package closure.

To rotate, under a separately reviewed dependency change:

1. Identify the current stable `docker:<version>-cli` official tag. Resolve its
   registry index using `docker buildx imagetools inspect` or registry HTTP GETs.
   With HTTP, verify the returned bytes' SHA-256 against `Docker-Content-Digest`.
   Inspect the Linux child annotations/config to identify its Alpine release;
   do not substitute an unverified digest or a floating tag.
2. Read the official matching Alpine main package indexes and select an exact
   Python version available for the supported architectures. Review compatibility,
   provenance and security changes; do not silently fall back to an unpinned APK.
3. Update the Dockerfile ARG, pinned Python APK, the `backend-e2e` environment
   default in `optimizer-ci.yml`, and this record together. The offline parity
   test rejects different CI/Dockerfile defaults or an unversioned Python package.
4. Rerun offline unit/lint/LF checks. After the owner's approval to push/run CI,
   qualify the derived image and complete owned round-trip/seal. A successful
   registry query is not build, runtime or restore qualification.

### Executable offline worker tests

`tests/test_backup_worker_execution.py` launches the **actual** `backup_db.sh`
and `stage_backup.sh` under bash, using disposable directories and PATH doubles
for PostgreSQL commands, date, sleep, publication/copy/checksum commands and
optional replication. Real `flock` provides lock semantics; no database,
container, provider, credential file or household archive is accessed.

The matrix covers first activation, pre-due/due/overdue restarts, persisted-success
suppression, failure retry spacing, phase/status failures, required/optional replica
outcomes, retention's two-snapshot floor/protected archives, untrusted inventory
refusal, policy reload on a bounded wake, and staging-versus-retention contention.
The PostgreSQL adapter models SQL responses: it does not qualify the SQL engine.
That remains the owned round-trip's responsibility.

The executable matrix also contains TERM-resistant dump and staging-copy cases:
deadline and cancellation must leave no live child holding the backup lock, preserve
the prior successful backup, and record failure/interruption. Staging cleanup must
leave the synthetic source worker running.

Only a missing/nonfunctional bash permits skipping this matrix locally.
`backend-unit` includes it in the ordinary full unit command; on Linux with
`CI=true`, missing bash is a hard failure, not a skip. Missing `flock` fails too.
The byte-level shell guard and `.gitattributes` `*.sh text eol=lf` check run even
without bash. A Windows pass with shell skips is not executable-shell evidence.

### Bounded staging, creation proof and cleanup

The coordinator's private (0700) lease directory and its audit receipt are, when
the pinned image runs as root, owned by the unprivileged owner of
`--lease-root` so the requesting runner/operator can read them. The coordinator
writes its planned stage identity to its private audit receipt **before** source exec, then exclusively reserves `.stage-<lease-token>` with a
matching owner receipt. Explicit `run` never creates a missing reservation, so a
queued exec cannot recreate a stage after cleanup. The source supervisor holds
a separate control lock inherited by all copy descendants. Copying has an
in-container 45-second timeout plus two-second kill-after; the monitored copy shell
ignores TERM. A separate watchdog in that isolated process group sends group KILL
at the hard deadline even if its caller dies or BusyBox timeout kills only its
monitored PID. Cleanup cannot acquire the control lock while that group survives.

Lock waits use `timeout -k 2 <seconds> flock <fd>`: BusyBox `flock`
does not implement util-linux's `-w` option. The coordinator extracts the staged
archive and manifest through bounded `docker exec ... cat` into exclusive binary
files; `docker cp` cannot read the worker's live `/backups` tmpfs. Checksums and
manifest bindings are validated before any target restore.

Source cleanup waits (bounded to 55 seconds plus kill-after) for the control lock.
It verifies the owner token, removes only the known files and requires successful
`rmdir` before acknowledging settlement. It never stops the source worker.
Missing ownership proof, remaining process/control lock, unexpected contents,
failed removal or daemon loss records an unresolved source stage and makes
`cleanup_verified=false`. Local `lease/archive.dump` and `archive.json` are removed
on both success and failure; only the audit receipt is retained after clean completion.

Target containers are inspected twice: pre-start image/labels/mounts/HostConfig
network configuration (an empty runtime NetworkID is expected), then post-start
actual image/network membership. They start with an idle shell, not a database
initializer or restore. Database launch follows runtime inspection; SQL database
identity is checked before any restore command. The scratch volume uses a fresh
random name plus lease/nonce labels; before mounting, its returned name, local
driver, absence of driver options and post-reservation CreatedAt must all match.
An adopted/raced or uninspectable volume is never mounted, written or deleted.

The worker image remains the pinned Alpine-based Timescale image. It uses
BusyBox-compatible `timeout -k 2` and `setsid`, not GNU-only long options.
The Dockerfile fails its build if kill-after or setsid is unavailable; no new
package is installed for these controls. Each worker phase/scheduler sleeper has
its own process group. Settlement sends TERM, waits at most two seconds, then
sends KILL to that group before reporting interruption. Image/runtime qualification
is still required; this build assertion is not local Docker execution evidence.

#### Captured-output ownership audit

The staged Compose command is `/bin/sh /scripts/backup_db.sh`; on its pinned
Alpine-based image this is BusyBox ash, not bash. The worker therefore uses POSIX
functions/read/case/arithmetic only. No worker command substitution, backtick
capture or external pipeline remains. SQL policy/due/identity/manifest/protection
queries, archive/retention/replica checksums, UUID generation and clock reads all
run through main-shell-owned process groups. Each owned process's stdin is
saved on fd 7 in the main shell before backgrounding, then
explicitly forwarded and closed in both parent and child. This preserves SQL
heredocs under dash/ash, whose asynchronous-list stdin otherwise becomes
`/dev/null`; bash alone does not expose that defect. Group signals use POSIX
`kill -s TERM -- -PGID` / `kill -s KILL -- -PGID`, including the staging watchdog.
The abbreviated `kill -KILL -- -PGID` form is not portable to dash.

Captured results use an exclusively
created private `.work-<uuid>` directory and builtin reads, not subshells. Each
invocation reads a fresh UUID from `/proc/sys/kernel/random/uuid`, the same Linux
kernel interface already required for archive IDs, using POSIX builtin `read`.
No new image utility or command substitution is needed. `umask 077` and exclusive
`mkdir` protect the workspace; an existing random name is never adopted and a new
UUID is tried (at most ten attempts). Other storage failures fail closed.

Cleanup removes only this invocation's known files/directory. SIGKILL/OOM can
leave an abandoned workspace, but neither a legacy PID-style directory nor a
UUID-style directory blocks a later invocation. They are deliberately ignored
by archive retention and staging, never broadly deleted, and do not establish
untrusted archive inventory or fail health by their mere presence. No workspace
count/status schema change is introduced by this targeted repair. Abandoned
workspaces can accumulate; capacity monitoring and any separately authorized
manual cleanup remain operational responsibilities.

The staging audit differs: captured SQL, manifest reads and archive checksums
already execute inside its `bounded-copy` group and inherit the control/backup
locks. They do not launch detached per-capture groups, so the existing independent
group watchdog covers their descendants. Its owner/state captures read only the
fixed-size private control files; there is no worker-style hidden `bounded` PGID.
Staging and the closed coordinator/volume lifecycle fixes are unchanged.

Cancellation regressions use TERM-resistant checksum and captured identity-SQL
tools as well as `pg_dump`. Each cancellation checks that the effective tool
timeout exceeds the eight-second shutdown grace (SQL: 20 seconds; checksum/dump:
60 seconds in the test), then requires group death, lock release, preserved prior
success and persisted interruption before that grace ends. Both bash and `/bin/sh`
routes cover captured commands on Linux; this does not establish local ash/runtime
qualification on Windows.

## Separate deployment/first-backup gate

The separate `restore-drill.yml` synthetic workflow also uses the guarded
`restore_db.sh --test` entry. Its test creates an anonymous, nonce-labelled worker
on the disposable source's inspected internal network, runs the actual backup
worker, and produces a fresh source receipt from immutable Docker inspection and
independent SQL identity checks. It does not use a prefix or `KEEP_STACK` as
authority. Detailed content assertions independently restore the exact
checksum-bound synthetic dump; the guarded entry must additionally produce its
own verified content/cleanup receipt and leave the source unchanged. No target is
kept for inspection after that entry: verified teardown remains mandatory.

The synthetic source role is LOGIN, without a superuser grant: Timescale's
`add_retention_policy` registers a background process as its current role.
The independent content restore uses a separate fresh cluster so it proves
restored job roles are created NOLOGIN rather than adopting the source's LOGIN
role. Synthetic qualification command diagnostics retain stderr and identify
empty/malformed JSON commands; public backup-health output remains sanitized.

Before any command capable of starting the worker, obtain approval explicitly
covering **activation and the immediate first live dump**. Independently inspect
the active API/poller source, project, exact container/network, database name and
server identity, PostgreSQL major, Timescale version and Alembic revision.
The standalone backup-only configuration must name that exact existing network
and source, with `BACKUP_EXPECTED_SOURCE_ID` matching inspected SQL identity.
Do not replace/recreate DB or Redis or bring up ordinary Compose wholesale.

Record protected primary and scratch storage, Windows ACLs, at-rest encryption,
free capacity/headroom and the accepted recovery boundary. `umask 077` is not
Windows ACL/encryption evidence. The mount refuses implicit host-path creation.
Explicitly apply the monthly settings policy: new defaults do not overwrite old
persisted settings. Stage/render images/configuration first without creating a
running worker. Implementation approval is not deployment authorization.

## Separately approved initial drill

Use only a fresh independently inspected source receipt and exclusively reserved
private lease root. `verify_backup_isolated.sh` is an explicit launcher;
`restore_db.sh --test <managed-dump>` delegates the disposable lifecycle to
`restore_drill.py`. `--test` now requires `RESTORE_SOURCE_RECEIPT` and
`RESTORE_LEASE_ROOT`; a prefix, open port, `RESTORE_TEST_KEEP_STACK` or test flag
cannot authorize resource adoption or mutation. Legacy archives without trusted
managed manifests are not automatically eligible for this qualification route.
The production/cutover branch remains unchanged.

Archive selection, checksum/manifest validation and copying happen under the
destination's retention lock. Restore consumes only the staged read-only copy.
The coordinator reserves a private UUID lease, records exact returned creation
IDs, verifies image/labels/network/mount/database identity, refuses unverifiable
capacity and reserves cleanup time. Default lifecycle is 4,500 seconds, startup
90, restore at most 3,600, final reporting 20, with 120 seconds reserved for cleanup.
Synthetic CI uses smaller budgets and at least 60 seconds reserved for cleanup.

Success means content verification **and inspected resource absence** followed by
persisted final source reporting. Killing a client or losing Docker connectivity
does not prove container work stopped. Never suppress cleanup errors or clean
unknown objects. Preserve private residual-resource receipts for separately
approved operator recovery. A failed drill stays degraded; prior success and
verified history are not erased.

## Observation and rollback

After separately authorized activation/drill, compare readiness and the UI's three
histories, then observe a second naturally scheduled backup before claiming the
scheduler operational. UI refresh remains 30 seconds; notifications reuse the
existing five-minute evaluation cycle and require their existing authorization.
Whole-host failure can silence that delivery; independent monitoring is separate.

Rollback stops only the approved backup service, retaining archives and corrected
fail-closed health. It is not a database restore, cutover, schema downgrade or
license to delete backup artifacts. Age/count protections may retain archives
past 100 days; disk pressure must fail/alert, never bypass retention protection.
