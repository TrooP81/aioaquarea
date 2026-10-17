# ML and Planning Session Handoff

Prepared: 2026-10-12, using the conversation date.

Audience: the next developer or coding agent continuing this workspace.

## Final Observe-Only Release Acceptance

**PRODUCTION_READY for the approved, tested observe-only candidate only.**
This final update supersedes the historical pending dispositions below.

The Release Gate accepted immutable snapshot
`ae5e3e4ac63ea12905c5eb122b2baa76a6cf1ec0`, created in a separate local test
repository with explicit user approval. The original repository remains
uncommitted at base `9286a1993e01b89aa8c1152978396c56405a5d57`.
Before this documentation-only amendment, the final source bridge verified all
389 selected files and 257 canonical binding files against that snapshot.
This amendment is post-qualification documentation, not part of its tested bytes.
Any subsequent implementation change requires an appropriate new evidence bridge
and requalification; the snapshot verdict is not blanket approval of later edits.

| Final gate | Supported result |
| --- | --- |
| Backend Linux units | 3,752 passed; five documented opt-in restore skips |
| Guarded PostgreSQL/Redis E2E | All 727 cases passed, no skips; complete inventory sealed |
| Library Linux regression | 114 passed; unchanged-source carry-forward verified |
| Frontend browser matrix | 672 passed, no failures, skips or retries |
| Synthetic restore qualification | 68 passed; unauthorized household-backup case skipped |
| Production artifacts | Actual backend/web Dockerfiles built; metadata, nonroot execution and smoke checks passed |
| Production supply chain | Runtime Python, CI Python and production npm audits clean; Compose and SPDX checks passed |
| Independent reviews | S3-S5, R03, season-readiness and integrated repair findings closed |
| Final human evidence approval | Explicitly approved for the snapshot above |

These are separate suites and receipts, not an additive unique-test total.
The earlier S2/R03 and readiness evidence gaps, Windows physical-symlink/fsync
limitations, and updated database capability E2E gap are closed by the retained
independent reviews and final Linux/PostgreSQL qualification.

**Accepted exception SC-DEV-01:** development-only `braces@3.0.3`,
GHSA-vfj7-8cjw-p6xm, remains temporarily user-accepted, not fixed. The full
development audit remains non-green. The repository maintainer owns reassessment
before the next release or immediately when a compatible fix is published.
Mitigations are trusted repository inputs, disposable CI, no deployed development
dependencies, and continued visibility of the audit failure.

**Limits remain:** ordinary automatic pump writes are disabled; normal collector
inputs lack the explicit declarations required for represented diagnostics.
Readiness does not certify autonomous control, real-household disaster recovery,
live provider/device behavior, or broader OS/runtime lifecycle. The earlier
intermittent browser startup timeout remains recorded despite subsequent stress
and complete-matrix passes.

Evidence is retained under the ignored workspace directory
`.agent-runs/production-readiness-20261015/`: `final-gate.json`,
`final-release-evidence-approval.json`, `development-risk-disposition.json`,
`integration/artifacts-r3/VERIFICATION_REPORT.md`, and the source-bound suite
receipts they reference. The verified source ZIP remains under
`integration/snapshot-qualified-r3/`.

All qualification-owned resources were cleaned up. **No deployment, original
repository commit/push, device command or household-data operation is authorized
by this verdict or the final evidence approval.**

## Source Acceptance Update: 2026-10-15

This update supersedes the historical S3-S5 pending dispositions below. The
remaining sections retain their original handoff context; their old revision,
deployment, approval and test statements are not current-state certification.

**S3-S5 passed bounded source acceptance. Overall readiness remains NOT_READY.**

Accepted identity: HEAD `9286a1993e01b89aa8c1152978396c56405a5d57` plus the
verified working tree, with manifest
`ef04dea9bb1d074b9965508b6e79b07cefa60853f49c649d933c5dc64d3eae6d`.

| Scope | Current disposition | Evidence and limits |
| --- | --- | --- |
| S3 feeds/accounting | Independent tests, review and source gate passed | Final current-source feed/accounting rerun: 121 passed; earlier broader provenance/accounting receipts retained |
| S4 conditional diagnostics | Independent tests, review and source gate passed; S5 bridge verified | All 328 S4 cases pass in the combined run; endpoint deadlines and their independent comfort floors, passive recovery, replay and cost checks covered |
| S5 remaining contracts/runtime | Independent tests and review passed; integrated source gate passed | Combined run: 1,322 passed and one inherited Windows physical-symlink skip; 20 additional independent adversity probes passed; lint, format and scoped diff checks passed |
| S2/R03 | Acceptance still unproven | A later bounded extension was approved, but its claimed verification lacked retained executable evidence; do not treat the historical prohibition on repair 5 as the current approval state |
| Season readiness repair 2 | Independent acceptance incomplete | Later review passed, but the remaining independent test gates are not closed by S3-S5 |
| Whole-system readiness | NOT_READY | Physical-symlink assertion, updated database-backed capability E2E, S2/R03 and readiness acceptance remain open |

Counts are separate receipts, not an additive unique-test total. The focused
30-case S5 rerun is already included in the 1,322 combined cases. Evidence is
indexed in the ignored workspace directory
`.agent-runs/s5-source/te-repair1/index.json`; its logs, XML, manifests and source
bridge identify the tested bytes. The final feed/accounting and diff receipts
are retained alongside that index.

Normal collector inputs currently contain no diagnostic declarations, so they
cannot produce admissible represented diagnostics under this accepted conditional
contract. See [Diagnostic planning](./diagnostic-planning.md). Source acceptance
does not establish live availability, physical guarantees, or permission for
ordinary automatic device writes.

These S3-S5 source repairs remain uncommitted and undeployed. The previously
approved local development deployment at `9286a199` is separate and does not
include these repairs. This update authorizes no deployment, database operation,
device command, commit or push.

## 1. Executive Status

**The overall task is not complete and is not ready for release.**

The user's goal is a controller whose ML models update and recover automatically
from available sensor and weather data, producing cost-aware plans that respect
indoor comfort boundaries and domestic-hot-water requirements. The user explicitly
asked to fix the underlying pipeline, not just labels or readiness progress bars.

There are two distinct work streams to keep separate:

1. The full, independently reviewed audit repair packet, R01-R35, organized into
   slices S1-S5.
2. A later Season readiness repair, UR1-UR5 / SR-1-SR-4, addressing live evidence
   calculation, status purity, consistent snapshots, maintenance reporting, and UI.

| Work | Latest supported disposition | Remaining gate or blocker |
| --- | --- | --- |
| S1: restore safety, R01-R02 | Independent tests and code review passed at its recorded revision | Preserve it; revalidate affected boundaries after subsequent shared-helper edits |
| S2: ML/data recovery | Most fixes implemented and tested; overall S2 remains blocked | Forecast evidence model-role identity and valid shadow-serializer compatibility; no repair 5 approval |
| S3: feeds and accounting | Pending | R14-R18, including planned migration 035 |
| S4: diagnostic planning | Pending | R27 and R28a-f; comfort, physical-state, serialized-action, and cost correctness |
| S5: remaining contracts/runtime | Pending, except R21 was pulled forward into readiness work | R21-R26, R29-R32, R35; final integration acceptance still required |
| Season readiness | Repair 2 implemented and implementer checks passed | Independent Test Engineer and Quality Reviewer gates still pending |
| Final whole-worktree tests/review/Release Gate | Not completed for the combined current changes | Do not infer a release pass from historical slice checks |
| Commit/push/deployment of current repairs | Not performed | Requires later explicit authorization and satisfied gates |


## 2. Current Workspace and Evidence Identity

Verified during handoff preparation:

| Item | Value |
| --- | --- |
| Workspace | `D:\apps\panasonic\aioaquarea` |
| Optimizer project | `heatpump-optimizer` |
| Branch | `fix/phase2-safety-reverts` |
| HEAD | `fa2c1e0e9703ba4d3909362d6a0e9def4f4c1bde` |
| Last committed application version | `0.15.0` |
| Last committed API contract | `2026-10-10.1` |
| Tracked modified files before this document | 57 |
| Staged changes before this document | None |
| Untracked repair files | Migration 034 and the safety-attempt owner helper |
| Unrelated untracked user file | [Scratch patch](../../diff_b2.patch#L1) |

Do not stage, delete, rewrite, or treat the scratch patch as a product source file.
Do not reset or revert existing changes. User/formatter edits have occurred
throughout the session, including in source files, tests, and TEMP evidence tools.

### Manifest Caveat

The tracker contains a recorded readiness identity whose full fingerprint is:

```text
726e1d424a2faf2d975845a1eb412fab7c0630435d18d344ed4c4a4af9fec0ed
```

Its referenced `post-manifest.json` contains 301 file entries. A read-only
comparison during handoff preparation found six current files different from
that saved manifest:

- [Model reporting status](../packages/ml/model_status.py#L1)
- [Lifecycle PostgreSQL tests](../tests/e2e/test_ml_lifecycle_postgresql.py#L1)
- [Optimizer API contract tests](../tests/test_optimizer_api_contracts.py#L1)
- [Browser fixtures](../web/e2e/fixtures.ts#L1)
- [Optimizer-status browser tests](../web/e2e/optimizer-status.spec.ts#L1)
- [UI status labels](../web/lib/constants.ts#L1)

These include later readiness repair/test paths. **This comparison does not prove
unauthorized edits or failed implementation. It proves that the tracker manifest
is not sufficient to certify the current worktree.** Inspect the latest report's
embedded manifests and current bytes before reusing results. Do not overwrite the
files to make them match an older manifest.

The latest readiness repair 2 implementation report records a newer identity:

```text
Backend:   d463504608e770f08b8019d30ae8f184e9f03b058af648a61e807fa97614132c
Web:       8f1f735ce24171844a91cb0543193139a645339b5de3cbf52a42f76e86b28492
Readiness: ed8d4cd94702c2a366aa952de2c14061ad23251e07e054eca5cbedb239b49bab
Full:      77b458c5f58ed651e781bb25f3c7d82a51a1046103d52fd98d7e501cea2bfb59
```

Those are recorded implementation identities, not independently recertified
whole-worktree fingerprints from this handoff.

### Tracker Precedence

The tracker retains historical failed gates as well as final passing gates.
For S1, an older `quality_reviewer_gate: FAIL` is superseded by the final review
PASS. Likewise, readiness's initial `CHANGES_REQUIRED` does not mean its later
label repair failed; that label repair had an independent PASS, followed by new
code-review findings and readiness repair 2.

Read the chronological reports and their source identities. Some generic `next`
and `latest_request.current_identity` fields are stale. Do not automatically rerun
the old unavailable-label repair merely because an older tracker field names it.

## 3. Safety, Product, and Authorization Boundaries

These are intentional behavior, not bugs to remove:

- This is an **observe-only build**: ordinary automatic pump writes are prohibited.
  A runtime learning/maintenance toggle must not enable optimization.
- `ACTIVE` observation suppresses ordinary actions and safety restores.
  `UNKNOWN`/`INACTIVE` may admit only genuine existing linked restores under the
  approved safety checks. Do not manufacture a restore link to gain write access.
- Learned COP is retired. `auto` selects rules, not automatic MILP promotion.
- Manual controller-COP entries and optional forecasting are observational and
  independent of optimization/training/device control.
- Model maintenance defaults off. Do not enable it as a debugging workaround.
- Model fitting or status reporting must not cause Panasonic reads, writes,
  authentication, or quota reservations. Unknown timer provenance is unavailable,
  not evidence that no timer exists.
- Preserve model signing, committed-reference authority, lifecycle CAS/fencing,
  retention boundaries, device/sensor isolation, and restore obligations.
- The historical pending restore example is action 11763 / shower event 26.
  Its current production status was not checked for this handoff. Do not clear it
  blindly or infer completion from a successful deployment.

Approval history:

| Date | Approval | Limit |
| --- | --- | --- |
| 2026-10-08 | Push/deploy both COP features using the qualified candidate | Historical deployment, not authorization for later repairs |
| 2026-10-09 | Baseline checkpoint and observe-only increment implementation/isolated tests | Deployment/maintenance activation remained separate |
| 2026-10-10 | Reviewed all-audit-bugs repair packet S1-S5, migrations 034/035, bounded restore deferral, conditional DHW/native-SH diagnostics, bounded best-effort logs, isolated tests | No deployment, activation, library/dependency changes, commits, or version bump |
| 2026-10-11 | One additional S2 deduplication round, then one provenance round | Raised S2 approved repair budget to four; did not authorize repair 5 |
| Later readiness request | Fix underlying Season readiness issues, not only the panel | Output-identical extraction/reporting repair; R21 pure GET/explicit transitions within the existing approved repair scope |
| Latest operational constraint | Continue without time-consuming background services | Must constrain any resumed verification |
| 2026-10-12 | Create this thorough handoff | Documentation only; did not authorize deployment or additional bug repairs |

The all-bugs design originally allowed two repair rounds per slice. S1 used two
and passed. S2 used two plus two separately approved additional rounds and remains
blocked. Readiness has its own two-round record; independent acceptance of its
second round is still pending. Do not silently reset budgets when changing roles.

## 4. What Happened Earlier in This Session

### COP Release and Deployment

The original working tree was based on commit `46d8793`, version 0.14.0, with
uncommitted COP-retirement and manual-controller-COP work. The first rollout
attempt stopped at candidate/rollback preflight.

The approved candidate identifier was a **content SHA-256**, not a Git commit:

```text
40efb92079ed95e66392927bd3d3c448e350958c9b2ef0ea0d0b672bd9a44cb1
```

It represented a 326-member source manifest. After that identity was verified,
both features were committed and pushed as:

```text
d4c8952495f6ed5c5923de87ebc3ab1bba0fc608
```

Migration 032 was deployed with matching backend/web/optimizer/poller images.
A fresh backup was isolated-restore validated, services were drained, retained
data/sequences were checked, health/contracts passed, and manual-COP forecasting
remained off. The features included COP retirement, a retained manual ledger,
opaque revision tokens, and default-off observational forecasting.

### Thermal Label Fix Became an ML Audit

The user reported `Indoor heat: undefined (undefined samples)`. The optimizer
status route omitted fields consumed by the UI. That response defect was fixed,
but the user asked to ensure the actual model worked, not merely its label.

Offline replay identified real calibration problems: room sensors were interleaved,
and positive/negative sample selection manufactured heating/cooling rates. Initial
repairs added consistent sensor series, baseline comparisons, and chronological
validation. Later independent audits found more integration and planning defects;
those initial passing suites were not sufficient to certify the entire system.

The initial offline one-hour replay was approximately 0.96 C MAE for the old
forecast versus 0.14 C for persistence. This was retrospective evidence against
the old model, not a current measured performance claim.

### Observe-Only Increment and Version 0.15.0

The reviewed OBS-1 through OBS-6 increment implemented:

- Build-scoped ordinary-write denial and retained genuine safety-restore handling.
- Non-active, action-free diagnostic plans.
- One poller-owned maintenance lifecycle, default off, with queue-only training
  entrypoints and HTTP 202 request responses.
- Prospective native-policy forecast issues/outcomes and qualification rules.
- Exact evaluated-fit publication, signed hash-addressed files, and committed CAS
  references rather than newest-file adoption.
- Migration 033: five lifecycle/request/artifact/forecast-issue/outcome tables.
- Observation never automatically ending and explicit reporting-only model states.

The requested separate early baseline commit was **not created separately**.
Earlier ML baseline changes and the observe-only increment were combined into:

```text
828a342fde330081d209dc04dc6b200a02832b0f
```

The version/release commit was:

```text
fa2c1e0e9703ba4d3909362d6a0e9def4f4c1bde
```

The branch was pushed and deployment was reported on 2026-10-10 as version 0.15.0,
API contract `2026-10-10.1`, migration 033, all four services at that SHA, health
passing, ordinary writes blocked, and maintenance/forecasting off. Known code-review
issues were still present; do not retrospectively label that release fully qualified.

Historical deployment backups:

| Archive | SHA-256 | Recorded validation |
| --- | --- | --- |
| `heatpump-20261008T053411Z.dump` | `6cad1b57fdc7d0f8f261b0c7434ba950ffac678410bf346b9cdb54eaf75df4ef` | Isolated restore passed at revision 031 |
| `heatpump-20261010T082749Z.dump` | `10039c496c8778c1eaa1bf60f4fcf4ba61024b421d648551e3223ec3f703d73e` | Isolated restore passed at revision 032 |

These are historical records, not fresh backups for the current uncommitted tree.
Earlier images were tagged `rollback-pre-d4c8952` and `rollback-pre-fa2c1e0`;
they lack later containment/fencing. Do not blindly roll back to those images or
downgrade retained data. Migrations 032 and 033 protect retained ledger/lifecycle
data; a tested guarded 034 reversal is not permission for a production downgrade.

**No live container, database, backup, device, or current remote branch was
rechecked while preparing this handoff.** The last reported deployment is not proof
of today's runtime state. Current repair files have not been committed/deployed.

### Pump Status Freshness API Contract

The user also reported:

```text
Live pump status: Unavailable - restore preconditions may be unavailable.
```

The UI's fallback was triggered because the status route omitted the additive
`data_freshness` object. The fix restored `latest_device_status`, `age_seconds`,
`stale_after_seconds`, and `fresh` using the existing freshness owner rather than
inventing a second policy in the component. Optional helper failures omit the
additional data; cancellation propagates normally.

The localized API/test/browser change passed 20 API unit tests, four Chromium
cases, Ruff/format, ESLint and typecheck, with independent code review and Release
Gate acceptance for that patch only. It was **not separately committed/deployed**.
Later readiness edits touch the same API and tests; its older hashes are not
certification of the current combined status route. Preserve both fixes.

### Intended Model Contracts

| Component | Owning source | Contract to preserve |
| --- | --- | --- |
| Demand | [Demand model](../packages/ml/demand_model_core.py#L1) | Electrical heating-demand kW, not thermal output; minimum 168 fit intervals, chronological baseline comparison, bounded off-loop preparation and compatible single-flight admission |
| Thermal | [Thermal model](../packages/ml/thermal.py#L1) | Six tank/zone/indoor heating/cooling heads, whole-run causal calibration, selected/reference sensor consistency, committed schema-v5 loading and per-head prospective qualification |
| Comfort | [Comfort model](../packages/ml/comfort_model.py#L1) | Controlled/passive/inverse prediction uses the trained lift feature schema; quality compares against same-period persistence and invalid inputs are unavailable |
| Forecast quality | [Forecast reconciliation](../packages/ml/forecast_quality.py#L1) | Frozen origin/context, actual-execution matching, scoped native evidence, independence, expiry and descriptive legacy fallback scoring |
| Lifecycle/recovery | [Lifecycle owner](../packages/ml/lifecycle.py#L1) | Default-off poller owner, durable family-scoped requests, fences/leases, evaluated-fit immutable publication and committed CAS authority |
| Persistence/status | [Signed persistence](../packages/ml/safe_persistence.py#L1) / [Reporting status](../packages/ml/model_status.py#L1) | Signature verification before unpickle, path confinement, file/directory durability before publication, selected-reference reporting and failure isolation |
| COP | Manual observational ledger/forecast | Learned COP remains retired; no promotion or optimization/device-control input from that feature |

The Comfort bug's concrete distinction was trained lift features such as
`[15, 20]`, not absolute supply values `[35, 40]`; passive heat-lift features must
remain `[0, 0]`, not absolute offsets `[8, 8]`. Correct units and actual serializer/
DTO boundaries matter as much as fit scores.

Reporting states are unqualified, candidate, qualified-for-reporting, expired,
and unavailable. A sufficient training count, accepted fit, loaded artifact and
prospectively qualified head are four different facts. Changing `SECRET_KEY`
invalidates existing signed artifacts and must not be used as an accidental
recovery mechanism.

## 5. Complete Audit and Repair Design

The full audit covered 88 modules: 13 ML, 14 optimizer, 41 core, five poller, and
15 API files, with 195 computational groups. Static exemptions were explicit;
the remaining named runtime contracts were eventually executed. Failed oracles
counted as coverage, not correctness.

Audit acceptance areas:

- `AUD-1`: exhaustive module/computation coverage.
- `AUD-2`: automatic updates, recovery, authority, expiry, backpressure, cleanup.
- `AUD-3`: sensors/devices/clocks, units, causal data, prices/weather, statistical validity.
- `AUD-4`: rules/MILP constraints, serialized physical behavior, deadlines and cost.
- `AUD-5`: ordinary-write containment, genuine restores, truthful observability.
- `AUD-6`: source-bound test evidence and owned-resource cleanup.

The audit result was `CHANGES_REQUIRED`, not functionality certification. Findings
were deduplicated into R01-R35, with R28a-f separating coupled planning invariants.

### Repair Ledger

Status wording below deliberately distinguishes implementation from final acceptance.

| ID | Root cause / required repair | Slice and status |
| --- | --- | --- |
| R01 | Future/stale known clocks falsely authorize readiness or restore completion | S1 accepted at recorded revision; current shared-helper changes need final regression checks |
| R02 | Restore claim ownership/fencing, cancellation and cleanup across awaited calls | S1 accepted; migration 034 created locally |
| R03 | Nonfinite/malformed horizon evidence and duplicate forecast quorum | S2 partly repaired; model-role identity remains blocked |
| R04 | Numeric settings admit NaN/Infinity, including unbounded floats | S2 implemented; prior focused verification passed |
| R05 | Controlled/passive/inverse Comfort serving feature units differ from training | S2 implemented; actual lift-parity checks passed |
| R06 | Indoor Thermal automatic family admission/routing is lost or tank-only | S2 implemented; real DTO and positive prospective qualification verified |
| R07 | Deselected/reference sensors or unequal cadence contaminate model inputs | S2 implemented; selected scope/query-limit/weighting checks passed |
| R08 | Warm controlled fallback jumps instantly to outdoor temperature | S2 implemented; gradual dynamics checks passed |
| R09 | Invalid trajectory inputs/outputs and saved curves reach usable/serialization paths | S2 implemented; finite/refusal checks passed |
| R10 | Valid zero outdoor temperature replaced by a truthiness fallback | S2 implemented; zero-value controls passed |
| R11 | Native ML materialization triggers remote timer reads | S2 implemented; cache/persisted-only behavior verified |
| R12 | New Demand callers inherit another preparation flight's cancellation | S2 implemented; admission/cleanup checks passed |
| R13 | Confirmed malformed artifacts confused with transient failures; restoration-to-CAS race | S2 implemented; locked revalidation and incumbent/progress preservation verified |
| R14 | Source-day resets, local/DST boundaries and mixed-device meter sequences | S3 pending |
| R15 | Rules cost uses cumulative-sample averages and unsupported action-cost assumptions | S3 pending; supported replay integration also depends on S4 |
| R16 | ENTSO-E quarter-hour resolution and later XML periods ignored | S3 pending |
| R17 | Weather units/nonfinite provider values mishandled | S3 pending; do not confuse partial finite-input edits with complete acceptance |
| R18 | Readiness certifies a different/incoherent provider, currency or weather/price horizon | S3 pending; readiness amendment does not close this |
| R19 | Fallback-family forecast history excluded before descriptive scoring | S2 implemented; retain separation from learned/prospective permission |
| R20 | Default forecast quality baseline mode silently treated as shadow | S2 implemented; default/injected mode controls passed |
| R21 | Status/learning GET paths persist seasonal campaign transitions | Pulled into readiness amendment; implemented/tested, final readiness review pending |
| R22 | Learned-usage schedule uses default Amsterdam rather than configured timezone | S5 pending |
| R23 | Acquisition/startup failure or cancellation bypasses resource cleanup | S5 pending; S1 fixed its own dedicated-connection subset only |
| R24 | Empty-installation indoor forecast returns an invalid response schema/500 | S5 pending |
| R25 | Capabilities advertise ordinary automation despite build prohibition | S5 pending |
| R26 | Typed Redis NoScriptError fails text-only script recovery | S5 pending; a genuine failed PostgreSQL/Redis gate |
| R27 | Rules DHW runtime rounding and final deadline/priced-window checks | S4 pending |
| R28a | MILP invents initial tank energy by clamping actual state | S4 pending |
| R28b | Missing modeled upper indoor comfort constraints | S4 pending |
| R28c | Solved compressor duty does not match serialized command semantics | S4 pending |
| R28d | Quantized DHW pulse can exceed tank ceiling between hourly endpoints | S4 pending |
| R28e | Missing/unknown enabled-gate entries default to allowed | S4 pending |
| R28f | SH/DHW shared-plant overlap and omitted diversion in replay/snapshots | S4 pending |
| R29 | Bounded-orphan pruning fixture assumes filesystem enumeration order | S5 pending; fix the fixture, not valid production behavior |
| R30 | Scheduled optimization cooldown/concurrency boundary before successful commit | S5 pending |
| R31 | Signatures and action ordering compare timestamp strings, not UTC instants | S5 pending |
| R32 | Manual advice reports readiness from stale/missing evidence | S5 pending; remains non-actuating |
| R33 | Passive forecast returns NaN with ready=true | S2 implemented; input/output refusal controls passed |
| R34 | Foreign sensor rows consume frozen-basis query limit before filtering | S2 implemented; filter before LIMIT verified |
| R35 | Unbounded best-effort log buffer and loss before commit | S5 pending; keep transactional safety audit separate |

Not every audit observation was established as a product bug: tariff proration is
an explicitly estimated allocation without fine-grained measurements; a binary-first
solver's cost difference is an objective limitation, not proof of violating an
unpromised global optimum. Preserve those qualifications instead of weakening tests
or making stronger physical claims.

## 6. S1: Accepted Restore-Safety Work

Primary owners:

- [Safety attempt ownership](../packages/core/safety_attempts.py#L1)
- [Executor and restore completion](../packages/optimizer/executor_core.py#L1)
- [Wrapper write permits](../packages/core/services/aquarea.py#L1)
- [Clock/readiness policy](../packages/core/device_data_quality.py#L1)
- [Migration 034](../migrations/versions/034_safety_attempt_claims.py#L1)
- [Safety tests](../tests/test_safety_reverts.py#L1)
- [Shutdown tests](../tests/test_executor_shutdown.py#L1)
- [Owned PostgreSQL safety tests](../tests/e2e/test_phase2_safety_reverts.py#L1)

Implemented behavior:

- Future and known-stale observation/retrieval clocks cannot be made fresh by a
  recent database insertion timestamp or local fetch time.
- Persisted, already-safe, LIVE verification and readiness paths share clock
  admissibility. False completion must not insert a success audit record.
- UUID claim tokens fence transitions, verification reservations, progress,
  requeue, completion, and audit insertion.
- Shared process-device ownership and a dedicated PG session advisory fence span
  quota/preflight, the unchanged SDK call including authentication retry,
  verification, and completion.
- Approved limits remain 110 seconds whole attempt, 40 seconds admission/SDK,
  60 seconds verification, and five seconds cancellation drainage.
- Absolute checkpoints 15/60 are tested with clocks that actually advance.
- Acquired dedicated connections are closed/invalidated even after aborted
  cleanup queries or cancellation handoff races.
- Elapsed time, boot IDs, or a released advisory lock are not stopped-owner proof.
  Unproven ownership/drainage defers recovery rather than admitting another actor.
- Legacy missing source-dispatch clocks can recover through reserved genuinely
  LIVE post-claim proof, with identity/baseline and prior-owner settlement checks.
- Ambiguous external effects are not claimed exactly-once. Verification precedes
  any possible duplicate restore.

Final S1 recorded tested fingerprint:

```text
44d762c5cd8312979c485b0f404045da00588ab12c3e505333a818873b98a59b
```

Final independent evidence: 306 unit/containment cases and 71 owned-PG cases passed;
the code reviewer ran 26 additional focused offline checks and found no remaining
HIGH/MEDIUM S1 finding. Migration checks covered 033-to-034, legacy data/sequence
preservation and guarded reversal. These results bind to that revision, not every
later modification of shared files.

## 7. S2: Implemented Fixes and the Current Blocker

S2 contains exactly 15 criteria: R03-R13, R19, R20, R33, R34. A historical
coordinator reference to 17 was a counting error, not an extra requirement.

Four repair rounds were used. Early reviews caught gaps not covered by initially
passing tests: boolean horizons in history readers, lost thermal family identity
in the real request DTO, artifact restoration before invalidation CAS, and duplicate
saved evidence. Those repairs and positive controls must be preserved.

**Latest S2 independent verification remains CHANGES_REQUIRED.**

| Finding | Exact last reproduced behavior | Required next action |
| --- | --- | --- |
| TE4-F02 | Six genuine serializer origins replayed with changed plan IDs and model-source labels count as 60 samples / 12 per horizon and pass the gate | Derive independence from validated immutable origin/model-role semantics; arbitrary source labels must not manufacture another independent observation |
| TE4-F01 | Twelve genuine shadow serializer origins score zero learned samples instead of 60 | Accept the actual valid shadow serializer contract while retaining role/provenance validation; do not fix inflation by rejecting all evidence |
| Test formatting | Newly added failing tests do not pass formatter | Formatting-only correction with assertions/AST preserved; this is not a reason to suppress the product failures |

Latest S2 TE result: **535 passed, eight failed**, zero skips in the scoped run;
Ruff lint passed. The eight failures are case variants of the two medium product
findings, not eight independent root causes.

Four rounds are exhausted. **No S2 repair 5 is authorized.** A new explicit bounded
approval is required before further S2 product repair. The later request to fix
readiness did not authorize reworking this blocked legacy-reader identity problem.

Historical S2 repair 4 TE fingerprint:

```text
1ee551e7cb0ea0858027ed96bb3e1734115c1c3d91df39489a6148d6c7e1f4a1
```

Read current [Forecast-quality code](../packages/ml/forecast_quality.py#L1) and
[its regressions](../tests/test_forecast_quality.py#L1) before any edit; both were
subsequently changed by user/formatter and readiness work.

## 8. Season Readiness: Underlying Pipeline, Not Just the Panel

### Original Symptom

```text
Demand evidence: 390/168 (100%)
Indoor-heating evidence: 0/20 (0%)
Automatic maintenance is selected when evidence is ready.
```

The old panel mixed incompatible quantities:

- Demand was currently usable training intervals.
- Indoor samples came from the loaded qualified thermal artifact. Without a
  qualified loaded reference, default parameters reset the count to zero even
  when candidate/collected evidence existed.
- Automatic-preference wording appeared when enabled status was unknown, and did
  not prove that a maintenance owner was active or work was running.

The user then said, "do not fix the panel only.. also fix all the underlying issues".

### Implemented Readiness Contract

The independently reviewed amendment covers UR1-UR5 / SR-1-SR-4:

| ID | Contract |
| --- | --- |
| UR1 / SR-1 | Output-identical pure extraction shared with the fitter; selected-device/reference/context filters; real collected train/holdout evidence, not qualified-parameter defaults |
| UR2 | One bounded consistent read-only snapshot, captured clock/context/watermarks, controlled concurrency and cancellation; not merely several reads sharing a timestamp |
| UR3 / SR-2 | Collection sufficiency, fit/validation, prospective pairs/diversity/checkpoints/expiry and reporting qualification are separate stages; no-incumbent route works |
| UR4 / SR-3 / R21 | GET reporting performs no campaign/queue/settings/lease DML or fitting/device I/O; explicit seasonal-cycle writes are atomic and authority-checked |
| UR5 / SR-4 | Backend and UI distinguish real zero from unavailable evidence, enabled state from preference, and each stage's current blockers |

Key source locations:

- [Seasonal readiness producer](../packages/ml/seasonal_learning.py#L1)
- [Thermal extraction and fitter](../packages/ml/thermal.py#L1)
- [Demand extraction and quality](../packages/ml/demand_model_core.py#L1)
- [Read-only database snapshot support](../packages/core/database.py#L1)
- [Reporting authority and head projection](../packages/ml/model_status.py#L1)
- [Lifecycle owner and queue](../packages/ml/lifecycle.py#L1)
- [Optimizer status integration](../packages/api/routers/optimizer.py#L1)
- [Season readiness UI](../web/components/OptimizerStatus.tsx#L1)
- [Shared UI contracts](../web/lib/api-types.ts#L1)
- [Readiness labels](../web/lib/constants.ts#L1)

No thresholds were lowered and no source samples were invented. Candidate/fit
counts are diagnostics, not automatic prospective qualification or control
permission. Unknown maintenance remains explicitly unknown; disabled remains off.

### Evidence Already Obtained

Independent initial readiness verification passed its backend boundaries:

- 228 focused unit cases, 12 fresh-PG cases, and 17 independent extraction-parity
  cases were recorded.
- Authenticated cold/off ASGI status returned real live collection counts while
  performing zero DML and zero forbidden calls.
- An explicit enabled-cycle owner queued/claimed thermal work using real DTOs.
- A real no-incumbent case reconciled 168 eligible native outcomes and qualified
  only the declared heads for reporting, with ordinary writes/control still false.
- Zero evidence and unknown evidence were tested separately.

The following were **test fixtures, not measurements of this user's installation**:

- Demand total 390, train 308, holdout 78, four embargo-removed observations.
- Indoor total 64, train 28, holdout 20.
- A separate qualified fixture with 528 source observations and 168 prospective
  eligible outcomes.

Totals need not equal train plus holdout because causal filtering, embargoes and
other rejection rules remove observations. Do not display raw totals as proof the
minimum fitting or qualification contract is satisfied.

### Readiness Repair History and Current Gate

1. Initial TE found the older summary rendered
   `Unrecognized status (readiness_unavailable)` despite the new panel knowing
   the status. This was a label-mapper defect, not a backend collection failure.
2. Readiness repair 1 fixed that known status label. Independent verification
   passed three affected browser cases and reused unchanged backend evidence.
3. Code review then found two underlying reporting defects:
   - `SR-QR-F01`: family-level thermal qualification was reused for undeclared or
     unqualified heads.
   - `SR-QR-F02`: malformed artifact metrics could throw after successful live
     collection, turning the whole generation unavailable and losing good counts.
4. Readiness repair 2 is implemented:
   - Projects the existing selected reference onto actually declared, currently
     qualified head capabilities using the captured clock.
   - Keeps unrelated/expired/malformed heads unqualified without discarding their
     valid collection and prospective blockers.
   - Validates nested metrics explicitly, marks only affected reporting sections
     unavailable, and sanitizes copied malformed legacy metrics to null.
   - Preserves live collection/watermarks and valid sibling metrics; does not
     change raw references, training policy, qualification policy or thresholds.

Implementer repair 2 final checks: **99 unit cases, two fresh-PG cases, nine mocked
browser cases passed**, plus touched Ruff/format, ESLint and TypeScript checks.
The current report explicitly means "ready for independent Test Engineer".

**Still required:** independent Test Engineer verification of repair 2, then
Quality Reviewer code re-review. No repair 2 independent report is present in the
readiness evidence directory at handoff time. Readiness has used two recorded
repair rounds; if those gates discover another required product repair, check the
approval boundary rather than silently starting round 3.

## 9. Remaining S3-S5 Design Commitments

### S3: Feeds and Accounting

- Create the approved additive migration 035 with nullable
  `consumption.source_timezone`, `weather.units_schema`, and
  `prices.interval_schema`; no blanket historical rescaling/backfill.
- Process explicit source-day changes before comparing counter magnitude, with
  recorded local calendar/DST boundaries and independent device sequences.
- Preserve first-unknown, corrections, confirmed resets/backfills and genuine
  confirmed-zero requirements.
- Parse all ENTSO-E periods and durations; normalize completely covered hours
  using duration weighting. Preserve negative prices and actual currency/units.
- Normalize provider units, including the reproduced 36 km/h versus 10 m/s wind
  case; required nonfinite/missing values remain unavailable.
- Certify the same frozen provider/context input batch used by planning, not a
  global newest timestamp or mixed providers.
- Use interval-based energy/cost. Distinguish measured energy from estimated
  intra-interval tariff allocation and unpriced/partial coverage.

### S4: Conditional Diagnostic Planning

The user approved correcting the algorithm, not enabling ordinary pump writes.
The represented strategy is **DHW timing plus unchanged native SH**, using an
explicit `native_sh_trace_v1` history-conditioned or assumed trace. Unsupported
ECO/quiet/target-to-compressor behavior is `unproven`, not a made-up duty relationship.

- Keep actual initial tank/room states; disclose recovery-required violations
  rather than invent initial heat by clamping.
- Respect modeled room minimum and maximum, tank floors/hardware ceilings,
  deadlines, complete priced windows, gate coverage and pulse extrema.
- DHW and SH must be exclusive on the shared plant; replay includes diversion.
- Serialized commands must reproduce the modeled trajectories and cost under the
  declared trace, not merely the solver's unrepresentable continuous solution.
- Apply net heat/loss exactly once; electrical power is kW, elapsed time hours,
  price currency/kWh.
- Validate both feasible controls and genuinely infeasible cases. Refusing all
  strategies is not a positive proof of repair.

This is a modeled-conditional assurance, not a certified physical comfort envelope
or a guarantee of globally minimum household cost.

### S5: Remaining Contracts and Logging

- Finish R21 review acceptance and preserve explicit transition ownership.
- Configured timezone in learned usage; cooldown success only after commit;
  canonical UTC ordering/signatures; fresh evidence for non-actuating advice.
- Protect resource acquisition from its first await, including startup failures
  and cancellation. Drain before engine/session disposal.
- Return schema-valid unavailable indoor forecasts for empty installations.
- Separate observed hardware support from this build's denied write permission.
- Recover typed Redis `NoScriptError` with one bounded reload/retry.
- Fix the pruning test with deterministic iterator fixtures while preserving
  protected references, bounded batches and resumability.
- R35 best-effort logs: 1,000 total ownership slots, 100-row batches, maximum 8 KiB
  encoded row; nonblocking producer, commit-before-ack, bounded retry/age loss,
  five-second shutdown drainage and explicit drop accounting.
- Ordinary logs may duplicate after ambiguous commit or be lost on abrupt death.
  Transactional safety audit records must never use that dropping queue.

## 10. Test Results and What They Do Not Prove

Do not sum repeated runs, retry attempts, parameter variants or reused receipts
into a fictitious unique total. The following runs belong to different revisions.

| Evidence milestone | Recorded result | Meaning |
| --- | --- | --- |
| Original qualified COP/manual candidate | 1,640 backend; 120 manual-PG; 143 ordinary E2E; zero security findings; native/browser smokes | Historical candidate only |
| Observe-only increment, final pre-audit repair | 1,952 units, 10 existing skips; 561 mocked browser cases; fresh owned-PG checks | Historical increment, later audit still found defects |
| Full ML/planning audit | Linux 1,970 passed / one failed / five skipped; owned-PG 295 passed / one failed; 129 browser audit cases | Audit was NOT_READY |
| Audit contract completion | 110 passed / four failed across 114 cases | Coverage closure, not correctness |
| S1 final independent verification | 306 unit/containment and 71 PG passed; QR ran 26 focused checks | S1 accepted for that source identity |
| S2 repair 4 independent verification | 535 passed / eight failed; lint passed, added tests need formatting | S2 remains blocked |
| Readiness initial independent verification | 228 units, 12 PG, 17 parity checks; unavailable-label regression failed | Underlying initial pipeline proven with a localized display defect |
| Readiness repair 1 independent verification | Three affected browser cases passed; unchanged evidence reused | Known unavailable-label defect closed |
| Readiness repair 2 implementer evidence | 99 units, two PG, nine browser cases passed; static checks passed | Independent repair 2 gates not yet obtained |

The audit's Linux failure was the filesystem-order-dependent bounded-orphan test.
The PostgreSQL/Redis failure was a real typed-script-recovery defect. Neither was
retroactively a passed gate. Both have explicit remaining repair IDs.

A clean report must distinguish `RUN`, source-valid `REUSED`, scoped
`NOT_APPLICABLE`, `NOT_RUN`, `PENDING`, and `CHANGES_REQUIRED`. Test discovery with
zero tests is not execution. Passing helpers with fake no-op clocks are not proof
of elapsed-time deadline correctness. Good UI fixtures are not live pump data.

## 11. Evidence Locations and Design Artifacts

The evidence is outside the repository and may be temporary. This handoff preserves
the essential decisions, but the next worker should locate and retain the detailed
JSON/receipts before TEMP cleanup. Do not publish real archives, credentials or
household raw histories as CI artifacts.

```text
C:\Users\troop\AppData\Local\Temp\hpo-ml-repairs\delivery-state.json
C:\Users\troop\AppData\Local\Temp\hpo-ml-repairs\s1\
C:\Users\troop\AppData\Local\Temp\hpo-ml-repairs\s2\
C:\Users\troop\AppData\Local\Temp\hpo-ml-repairs\season-readiness\
C:\Users\troop\AppData\Local\Temp\hpo-ml-planning-audit\audit-verification.json
C:\Users\troop\AppData\Local\Temp\hpo-thermal-backtest\
C:\Users\troop\AppData\Local\Temp\hpo-obs-evidence\
```

Important readiness report hashes verified while preparing this document:

| Report | SHA-256 |
| --- | --- |
| Initial implementation | `db4e91452a6dfd5476605bf0c1dd6b6d98145dc35f20b9c4ae543c43563b3a6f` |
| Initial independent verification | `28a9607870da4840d4f21d471de588d6928ec1620a597433691a47f9e41158cb` |
| Repair 1 implementation | `6e61ec10d49d5eca49440af1c3e2e70e69f5ac1898108e092418ad126cde47e0` |
| Repair 1 independent verification | `cb372199dd4749cd8ee78c7b48500f33c394f2eac1cf9f1bb90bc0e3289be751` |
| Repair 2 implementation | `87f0feecea6179ecfba76a11897cd7bcc5f672dbe2f88ab35b89cc4d7596de07` |

Historical binding repair design hashes:

```text
Original repair packet: cc18bf49da09bd773d60496284cdd3bc3ff583c38f65e114a7227964306544a3
V2 addendum:           2a5da42d454ad045df76a6f441de8b03435ecd9ff7f2101fa85022e696c0d6f6
Completed audit JSON:  fd3c50919b786765999c41c9a31ff02e11de60187f7afdd8285494a760ca7a91
```

The design text was saved by the chat tooling. Locate it under this resource root:

```text
C:\Users\troop\AppData\Roaming\Code - Insiders\User\workspaceStorage\4ecc0c1b8c0cd4639671bdfda5b16445\GitHub.copilot-chat\chat-session-resources\bb1a78d5-881e-4d0b-9c91-bed025e27e98\

Original repair packet:
  call_MEKgBe7I1ZAYejUO3616uDvw__vscode-1791316689466\content.txt
V2 repair addendum:
  call_QajckIgWq8NNjbUU1CaF3Mg4__vscode-1791316689629\content.txt
Final localized readiness design:
  call_HGovBMDPk0pszyVN7h35Gcnd__vscode-1791316692009\content.txt
```

The final readiness design's recorded SHA-256 is
`d541d3e2d17e6b909c54fd7c849389c1d8277e6a4aa54b7f95f155c82fcfc3d6`.
Verify actual files before relying on them; do not mistake context artifacts for
new Git commits or newly executed independent review receipts.

## 12. Current Changed-File Inventory

The following 57 tracked files were modified when this handoff was prepared.
This is an inventory, not permission to stage unrelated work or proof every file
has current release evidence.

```text
heatpump-optimizer/README.md
heatpump-optimizer/docs/configuration-reference.md
heatpump-optimizer/packages/api/routers/models_router.py
heatpump-optimizer/packages/api/routers/optimizer.py
heatpump-optimizer/packages/api/routers/settings.py
heatpump-optimizer/packages/core/database.py
heatpump-optimizer/packages/core/device_data_quality.py
heatpump-optimizer/packages/core/device_status_ingestion.py
heatpump-optimizer/packages/core/device_status_snapshot.py
heatpump-optimizer/packages/core/learning_state.py
heatpump-optimizer/packages/core/models/__init__.py
heatpump-optimizer/packages/core/panasonic_diagnostics.py
heatpump-optimizer/packages/core/sensor_diagnostics.py
heatpump-optimizer/packages/core/services/aquarea.py
heatpump-optimizer/packages/core/settings_service.py
heatpump-optimizer/packages/ml/comfort_model.py
heatpump-optimizer/packages/ml/demand_model_core.py
heatpump-optimizer/packages/ml/forecast_quality.py
heatpump-optimizer/packages/ml/lifecycle.py
heatpump-optimizer/packages/ml/model_status.py
heatpump-optimizer/packages/ml/models_common.py
heatpump-optimizer/packages/ml/safe_persistence.py
heatpump-optimizer/packages/ml/seasonal_learning.py
heatpump-optimizer/packages/ml/thermal.py
heatpump-optimizer/packages/optimizer/executor_core.py
heatpump-optimizer/packages/optimizer/main.py
heatpump-optimizer/packages/poller/feeds.py
heatpump-optimizer/packages/poller/smartthings.py
heatpump-optimizer/tests/e2e/test_ml_lifecycle_postgresql.py
heatpump-optimizer/tests/e2e/test_phase2_safety_reverts.py
heatpump-optimizer/tests/test_aquarea_service.py
heatpump-optimizer/tests/test_comfort_model.py
heatpump-optimizer/tests/test_device_status_ingestion.py
heatpump-optimizer/tests/test_executor.py
heatpump-optimizer/tests/test_executor_shutdown.py
heatpump-optimizer/tests/test_feeds.py
heatpump-optimizer/tests/test_forecast_quality.py
heatpump-optimizer/tests/test_learning_mode_acceptance.py
heatpump-optimizer/tests/test_ml.py
heatpump-optimizer/tests/test_ml_lifecycle.py
heatpump-optimizer/tests/test_optimizer_api_contracts.py
heatpump-optimizer/tests/test_panasonic_diagnostics.py
heatpump-optimizer/tests/test_rules_engine_snapshot.py
heatpump-optimizer/tests/test_safe_persistence.py
heatpump-optimizer/tests/test_safety_reverts.py
heatpump-optimizer/tests/test_sensor_diagnostics.py
heatpump-optimizer/tests/test_settings_router.py
heatpump-optimizer/tests/test_settings_service.py
heatpump-optimizer/tests/test_thermal.py
heatpump-optimizer/web/app/settings/page.tsx
heatpump-optimizer/web/components/OptimizerStatus.tsx
heatpump-optimizer/web/e2e/fixtures.ts
heatpump-optimizer/web/e2e/learning-mode.spec.ts
heatpump-optimizer/web/e2e/optimizer-status.spec.ts
heatpump-optimizer/web/e2e/settings-accessibility.spec.ts
heatpump-optimizer/web/lib/api-types.ts
heatpump-optimizer/web/lib/constants.ts
```

New repair files, not yet tracked:

- [Migration 034](../migrations/versions/034_safety_attempt_claims.py#L1)
- [Safety attempt owner helper](../packages/core/safety_attempts.py#L1)

Migration 035 is planned, not listed as created. This handoff document is another
new untracked file after its creation. The scratch patch is unrelated user work.

## 13. Resume Plan and Required Final Gates

1. **Do not start background services.** Inspect this handoff, current Git state,
   tracker and latest readiness report; reconcile current files with the correct
   latest manifest without reverting user work.
2. **Verify readiness repair 2 independently.** Check per-head capability scope,
   malformed metadata failure isolation, live counts/watermarks, pure GET and
   explicit transition ownership. Run permitted non-service focused tests and
   static checks first. Service-dependent independent PG/browser checks remain
   pending unless permission is clarified. Implementer evidence alone is not PASS.
3. **Obtain readiness code re-review.** Preserve the already fixed unavailable
   label. Do not rerun an old repair merely because the tracker retains its failure.
4. **Resolve S2 approval boundary.** Request a specific additional bounded round
   for model-role origin independence and actual shadow serializer compatibility,
   plus test formatting. No repair 5 is currently authorized.
5. **Complete S2 acceptance before claiming ML completion.** Reproduce the latest
   failures on actual current source, preserve valid serializer/unique-origin and
   168-pair qualification controls, then obtain independent tests and review.
6. **Continue approved S3, S4, S5 scopes.** Read the owner and exact red oracle,
   make a small root-cause edit, immediately validate, then broaden within that
   slice. Preserve the full R-ID map and do not silently drop coupled cases.
7. **Final combined validation:** current locked Linux runtime/SDK, full optimizer
   units once the slices are finished, Ruff/format, migration heads/parity/retained
   data checks, fresh owner-proven PG/Redis race/integration evidence, affected web
   lint/typecheck/build and desktop/mobile mocked flows. Under the user's current
   no-services constraint, unavailable service-dependent gates must be documented,
   not reported as passing or started anyway.
8. **Independent final code review and Release Gate.** No unresolved confirmed
   HIGH/MEDIUM repair within scope, no weakened assertions, no pending owned
   resources, and exact current source identity are required for a meaningful pass.
9. **Ask separately before commit/push/versioning/deployment or maintenance
   activation.** A future rollout needs an approved compatible build, fresh
   validated backup, controlled writer drain preserving obligations, additive
   migrations, image commit/version identity, and operational health checks.

### Safe Initial Inspection Commands

These do not start services. They are examples for a future worker, not additional
checks executed as part of this document:

```powershell
git -C D:\apps\panasonic\aioaquarea rev-parse HEAD
git -C D:\apps\panasonic\aioaquarea status --short
git -C D:\apps\panasonic\aioaquarea diff --stat
git -C D:\apps\panasonic\aioaquarea diff --cached --name-only
Get-Content "$env:TEMP\hpo-ml-repairs\delivery-state.json" -Raw | ConvertFrom-Json
Get-Content "$env:TEMP\hpo-ml-repairs\season-readiness\repair2-implementation-report.json" -Raw | ConvertFrom-Json
```

For any permitted local Python test, establish the isolated environment **before
imports**, because settings and engines are created at import time:

```text
HPO_TEST_OFFLINE=1
APP_ENVIRONMENT=test
DATABASE_URL=postgresql+asyncpg://offline:offline@127.0.0.1:1/isolated_test
REDIS_URL=redis://127.0.0.1:1/0
MODEL_DIR=<fresh exclusive TEMP model directory>
API_TOKEN=test-token
SECRET_KEY=<public disposable test signing key, not a production secret>
PYTHONPATH=<both checkout roots required by the current harness>
```

Use current repository instructions and current test-harness guards, not generic
live-stack launchers. Historical TEMP scripts were edited by the user/formatter;
read and hash them before reuse. Do not install dependencies or silently change
the declared library pin. PowerShell sync commands share a shell: do not run two
terminal calls in parallel, and restore environment overrides after checks.

## 14. Definition of Done and Handoff Warning

The work is done only when all confirmed R01-R35 repairs and the underlying
readiness amendment have current independent evidence/review, the final combined
gates pass, and no retained obligation/data/safety contract has been weakened.

Even then, this does not certify every possible runtime branch, actual physical
temperature guarantees, winter response, or global household cost optimality.
Qualified-for-reporting is not approved-for-control.

**Current final disposition: HIGH risk, NOT_READY.** Original implementation
approval exists. Additional S2 repair approval and final release/production
authorization are separate unresolved boundaries. Preserve the current work;
do not treat this document, an implementer PASS, or a historical deployment as
authorization to publish or activate it.