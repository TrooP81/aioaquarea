# ML optimization tracker

## At a glance

- **Goal:** Create ML-optimized plans that follow the comfort schedule in Settings and keep hot water ready at the lowest Tibber cost, with safe, approved control.
- **Current phase:** Phase 1 — Reliable measurement. Starting 2026-10-16.
- **Overall status:** 🔵 In progress. Overall roadmap risk: **HIGH**.
- **Safety now:** The build is observe-only. Ordinary pump writes are prohibited in code.
- **Models now:** All three ML models are unqualified after the 2026-10-16 13:04 data reset.
- **Next step:** Repair the test-only ownership setup for the authorized Windows Compose integration gate, then rerun P1.1's database tests and Release Gate; P1.2/P1.3 backup work remains separate.
- **Waiting on you:** No Docker permission is outstanding. P1.1 integration is blocked by the existing E2E ownership guard, not a demonstrated G03 defect; a compatible guarded test harness is needed. P1.2/P1.3 await the owner's HIGH-risk implementation approval and backup decisions listed below.
- **Pre-commit cleanup:** OP1 is user-confirmed complete (not agent-verified); OP2 is 🟢 Done. Four approved local commit groups were committed locally on 2026-10-16 on fix/phase2-safety-reverts (not pushed). Push is your decision (repo is public).
- **Last updated:** 2026-10-16 — Implementer (agent).

## Status legend

| Status | Meaning |
| --- | --- |
| ⬜ Not started | Work has not started. |
| 🔵 In progress | Work is under way. |
| 🧪 In verification (testing/review) | Tests or independent review are under way. |
| 🟡 Waiting on you | A decision or action from you is needed. |
| 🟢 Done | All required gates have passed, including Release Gate. |
| 🔴 Blocked | Work cannot continue until a blocker is resolved. |

**Who:** Agents / You / Both. “You” means the house owner.
Effort estimates are approximate. Data collection takes calendar time.

## Phase overview

| Phase | Goal | Status | Est. effort |
| --- | --- | --- | --- |
| 1 — Reliable measurement | Trust the results, backups, and alerts. | 🔵 In progress | ~1 week |
| 2 — Configurable house | Use one set of house and tariff settings everywhere. | ⬜ Not started | ~1 week |
| 3 — Baseline | Measure native-pump costs and comfort for comparison. | ⬜ Not started | ~2 weeks of data; parallel with Phase 4 |
| 4 — ML for planning | Prove that ML plans improve on native operation. | ⬜ Not started | ≥14 days of heating-season data |
| 5 — Control-enable build | Add safe control only with your approval. | ⬜ Not started | Not yet estimated; HIGH risk |
| 6 — Pilot | Start small and expand only with good results. | ⬜ Not started | ≥14 days before automatic hot-water control; total not yet estimated |

## Phase 1 — Reliable measurement

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P1.1 | Fix the crashing results summary and heat-curve advice. | G03 | Agents | 🟢 Done | Release Gate passed against PR CI run 38923249300 / head ae98367b8150b353daac7ac86900fb9ce09e4f61 (includes fix 804a210). Owned PostgreSQL E2E ordinary phase passed (231 passed); all five collected TestG03ReadOnlyRoutes nodes were included, no skips/deselections reported. Receipt reconciliation/final seal step completed; backend-e2e and backend-unit jobs succeeded. | .agent-runs/20261016-143346-tracking-and-phase1/release-gate-p11-11-sanitized-proof.log (same folder); CI run 38923249300 |
| P1.2 | Schedule backups of the active database, verify a test restore, and show backup status in UI alerts. | G04 | Agents | 🔵 In progress | In repair on branch feat/p1.2-backup-worker (draft PR #35). Moved off PR #34 on 2026-10-16 so PR #34 can merge. Owner approved local Docker diagnosis plus one more repair loop. Backup service not activated; live stack untouched. |
| P1.3 | Show the last successful backup separately from a fresh failed attempt. | G14 | Agents | 🔵 In progress | In repair on branch feat/p1.2-backup-worker (draft PR #35). Moved off PR #34 on 2026-10-16 so PR #34 can merge. Owner approved local Docker diagnosis plus one more repair loop. Backup service not activated; live stack untouched. |
| P1.4 | Stop misleading “no fresh sensor” warnings when the cause is too few scored forecasts or the observe-only build. | G12 | Agents | 🟢 Done (pending CI on PR #34) | Reason-specific forecast/sensor messaging and separate observe-only write-prohibition copy; assessment states, optimizer decisions and write/rate-limit gates unchanged. Evidence: unit, API and Playwright coverage (`thermal-forecast-limitation.spec.ts`), Release Gate PASS; .agent-runs/20261016-221904-p1-split-and-repair/p14-*.log, p14verify-*.log and gate-p14-*.log. LOW follow-ups: backend `observe_only_build` summary is unreachable (the UI notice covers it); the sensor-reason list is duplicated in Python and TypeScript. |
| P1.5 | Add automatic restarts for the database and Redis, plus an external heartbeat monitor. | G13 | Agents | ⬜ Not started | Detect when the system stops reporting. |
| P1.6 | P1.6a: Timeline current hour, status labels, scoped stats, half-open reporting windows and reporting-only accounting. P1.6b: selected Tibber home binding/tariff migration (separate HIGH-risk packet). | G16 | Agents | P1.6a 🟢 Done (pending CI); P1.6b 🔵 In progress | P1.6a excludes feed persistence, planner price selection and rules baseline changes. P1.6b owner approved (a) on 2026-10-16 22:55. |
| P1.7 | Fix planner read-only ORM escapes | G03 follow-up | Agents | 🟢 Done/merged | Owner final release approval: 2026-10-17 09:11. rules_v8 / milp_v2; detached snapshots, rollback regressions, and owned-PG acceptance coverage. Approved for merge after green CI; observe-only policy unchanged (HIGH). |

## Phase 2 — Configurable house

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P2.1 | Make the UI, planner, forecasts, and results use the same comfort schedule and targets from Settings. Save a snapshot with each plan. | G02 | Agents | ⬜ Not started | A plan must retain the settings it used. |
| P2.2 | Add a protected-rooms setting and comfort reporting weighted by time. | G08 | Agents | ⬜ Not started | Show how long rooms meet their comfort targets. |
| P2.3 | Add an optional hot-water schedule that defaults to the comfort schedule. | G15 | Agents | ⬜ Not started | Keep the shared schedule unless changed in Settings. |
| P2.4 | Add tariff settings: extra per-kWh fee, fixed monthly cost, and settlement interval. Save the tariff with each plan. | G06/G07 | Agents | ⬜ Not started | Extra fee defaults to 0: Tibber total already includes grid fee. Monthly cost is reporting only. Default settlement is hourly. |
| P2.5 | Record the real heating curve from the Panasonic panel. | — | You | ⬜ Not started | Record the panel values for later comparison. |

## Phase 3 — Baseline

About two weeks of data. This can run alongside Phase 4.

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P3.1 | Set 22–23 °C in the comfort schedule in Settings. | — | You | ⬜ Not started | Use Settings as the source of comfort targets. |
| P3.2 | Collect a native-pump baseline and compare net savings against it. | G09 | Agents | ⬜ Not started | Measure normal pump operation before claiming savings. |
| P3.3 | Compare calculated cost with one Tibber invoice. | — | Both | ⬜ Not started | Resolve any difference before trusting savings reports. |

## Phase 4 — ML for planning

Requires at least 14 days of heating-season data.

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P4.1 | Enable model maintenance and qualify the demand model. | G11 | Agents | ⬜ Not started | Show that its forecasts meet the required checks. |
| P4.2 | Redesign the thermal model to include heat input. Test 1–6 hour forecasts against “no change”. | — | Agents | ⬜ Not started | Compare against assuming the temperature stays the same. |
| P4.3 | Replace the comfort model's hard-coded “never ready” result with the shared control policy. | — | Agents | ⬜ Not started | Readiness must follow the shared safety rules. |
| P4.4 | Use demand forecasts and sh_max_power_kw to declare space-heating load so normal data produces admissible plans. | G05 | Agents | ⬜ Not started | Plans must pass the load and capacity checks. |
| P4.5 | Make the planner use tariff settings and record the model versions used by each plan. | — | Agents | ⬜ Not started | Make each plan's price assumptions and models traceable. |
| P4.6 | Compare ML plans with native operation in shadow mode for 14+ days. | — | Agents | ⬜ Not started | Shadow mode evaluates plans without controlling the pump. |

## Phase 5 — Control-enable build

**HIGH risk. Your approval of the independently reviewed design is required before implementation.**

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P5.1 | Add one control policy: observe_only / supervised / automatic, with a kill switch. | G01 | Agents | ⬜ Not started | Default to observe_only. |
| P5.2 | Add hot-water-only dispatch with a linked restore for every write, rate limits, readback, and respect for manual boosts. | — | Agents | ⬜ Not started | Verify the pump's response and preserve manual choices. |
| P5.3 | Physically test recovery after internet, cloud, or app failure. | G10 | Agents | ⬜ Not started | Requires separate device-operation approval; no live test is authorized here. |
| P5.4 | Approve the control-enable design. | — | You | ⬜ Not started | Required before HIGH-risk implementation, not just before use. |
| P5.5 | Update tests that currently pin the observe-only gate. | — | Agents | ⬜ Not started | Preserve safety coverage under the approved policy. |

## Phase 6 — Pilot

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| P6.1 | Start supervised hot-water control. | — | Agents | ⬜ Not started | Only after the required approvals and safety gates. |
| P6.2 | Start automatic hot-water control after 14 days without comfort or hot-water violations and with positive net savings. | — | Agents | ⬜ Not started | Both comfort and savings conditions must pass. |
| P6.3 | Consider space-heating control later. | — | Agents | ⬜ Not started | Not part of the initial hot-water pilot. |

## Ops / prerequisites

| ID | Item (plain language) | Gap ref | Who | Status | Evidence/notes |
| --- | --- | --- | --- | --- | --- |
| OP1 | Rotate SECRET_KEY and set APP_ENVIRONMENT=production BEFORE any model is committed. | — | Both | 🟢 Done | SECRET_KEY rotated and APP_ENVIRONMENT=production set — confirmed by user 2026-10-16 15:45 (not agent-verified; .env not read) |
| OP2 | Commit or set aside uncommitted working-tree changes before implementation touches the same files. | — | Both | 🟢 Done | Committed locally 2026-10-16 on fix/phase2-safety-reverts (not pushed): 804a210 G03 fix, f732e0c OP3 tests, 3f72d95 tracker + agent workflow, 3d98913 review docs. Scratch files deleted; live evidence kept local via .gitignore. Push is the user's decision (repo is public). |
| OP3 | Windows full-suite failures (fsync permission/symlink) — pre-existing, environment | — | Agents | 🟢 Done | Release Gate PASS 2026-10-16; .agent-runs/20261016-143346-tracking-and-phase1/release-gate-04-unit-tests.log: 3865 passed, 74 skipped, EXIT 0 (gate evidence release-gate-01 through release-gate-07 in the same folder). Independent Test Engineer verification PASS for OP3-AC1–AC4 at c5c7d9a + preserved dirty edits: nine nodes 8 passed / 1 Windows skip; full suite 3865 passed / 74 skipped; Ruff lint/format pass (all EXIT 0). Disposable probes reproduce directory-open PermissionError and symlink WinError 1314. AST comparison preserves all original bodies/assertions; eight opt-in cases replace only directory fsync on Windows. Executed extracted fixture with non-Windows os proxy: no replacement; POSIX skip predicate false (not a native POSIX suite run). Restoration checked after normal teardown and teardown assertion failure; no fixture leak. Production/G03 fingerprints unchanged. Evidence: .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-op3-02-nine-nodes.log, test-engineer-op3-03-full-unit.log, test-engineer-op3-04-ruff-check.log, test-engineer-op3-05-ruff-format.log, test-engineer-op3-06-contract-rerun.log (same folder). Initial test-engineer-op3-01-contract.log EXIT 1 was a disposable audit-helper exception-class error, fixed and rerun; original log retained. Implementer baseline/lifecycle evidence remains in implementer-op3-01b-baseline.log and implementer-op3-03-lifecycle.log. Windows production publication still fails closed at directory fsync; no production fix or deployment approval claimed. |
| TR-docs | Keep roadmap status and agent tracking duties current. | — | Agents | 🟢 Done | Release Gate re-evaluation 2026-10-16 PASS for TR-docs (tracker + agent tracking rules). Evidence: .agent-runs/20261016-143346-tracking-and-phase1/release-gate-18-agent-customization-validate.log, release-gate-19-scoped-diff-check.log, release-gate-20-tracker-sync.log (all EXIT 0, same folder). |

## Decisions

| Date | Decision | Effect |
| --- | --- | --- |
| 2026-10-16 | Pending owner approval: P1.2/P1.3 HIGH-risk backup implementation. | Quality Reviewer approved the Repair 1 design, not implementation. Nothing implemented yet; owner approval is required before implementation. |
| 2026-10-16 | Pending owner decisions: backup storage path; off-host replica yes/no; daily backups / 14-day retention / 26 h freshness. | Confirm backup location, replica choice, schedule, retention, and freshness policy before implementation. |
| 2026-10-16 | Pending owner acceptances: recovery-boundary, local-only, and initial-drill-only. | Confirm these design boundaries before implementation. Evidence: .agent-runs\20261016-143346-tracking-and-phase1\architect-03-p12-p13-backup-design-packet-r1.md. |
| 2026-10-16 15:45 | User approved pre-commit cleanup and four local commit groups: G03, OP3, tracker/agent workflow, review docs. | Delete three stale root scratch files; keep live review evidence local through .gitignore; scan four review files and this tracker for public-repository privacy. Exclude any review file containing personal data without editing it. A separate operator will commit; no push authorized. |
| 2026-10-16 15:45 | User confirmed SECRET_KEY rotation and APP_ENVIRONMENT=production. | OP1 recorded as user-confirmed complete, not agent-verified; .env not read. No model, deployment, or device-operation approval implied. |
| 2026-10-16 15:10 | User chose to fix OP3 first. | Authorizes LOW-risk, test-only Windows accommodations and local offline gates; no production changes or device operations. |
| 2026-10-16 | Grid fee is already included in Tibber's price. | No grid fee to enter. Extra-fee setting defaults to 0; available for other houses. |
| 2026-10-16 | Fixed monthly power fee is for reporting only. | The optimizer ignores this fee. |
| 2026-10-16 | The Tibber contract settles per hour. | Current hourly prices match the contract. |
| 2026-10-16 | Comfort and grid settings must be configurable in Settings. | The system must work for other houses too. |
| 2026-10-16 | Comfort follows the comfort schedule in Settings. | Do not use separate hard-coded comfort targets. |
| 2026-10-16 | Hot water is mainly needed mornings, evenings, and weekends. Colder water is acceptable on work-from-home days. | Current schedule-derived ready-by times already match. Manual boosts must be respected. |

## Activity log

- 2026-10-17: P1.7 final owner release approval recorded (09:11); rebased onto main and approved for merge after green CI.

Newest first. Evidence paths are relative to the repository root.

| Date | Who (agent/role) | Item | What happened | Evidence |
| --- | --- | --- | --- | --- |
| 2026-10-16 23:26 | Release Gate / commit operator | P1.6a / G16 | Reporting repair complete, pending CI; stacked on PR #34. P1.6b in progress: owner approved (a) at 22:55. Design, Test Engineer and Quality Reviewer passed repair loop 1; coordinator accepted superseded intermediate failures. Fresh rebased gates PASS: 3913 unit tests, Ruff check/format, web lint/typecheck. Planner inputs unchanged. | .agent-runs/20261016-221904-p1-split-and-repair/gate-p16a-*.log; p16averify-r1-*.log |
| 2026-10-16 | Release Gate / commit operator | P1.4 / G12 | Release Gate PASS: full offline unit/API suite, Ruff check/format, web lint/typecheck; independently verified Playwright coverage. Done pending CI on PR #34. Four LOW review findings remain non-blocking; follow-ups recorded in P1.4. | .agent-runs/20261016-221904-p1-split-and-repair/gate-p14-*.log; p14verify-*.log |
| 2026-10-16 | Commit operator (agent) | P1.2 / P1.3 | Owner-approved split: moved backup work to feat/p1.2-backup-worker, draft PR #35 (https://github.com/TrooP81/aioaquarea/pull/35), which must merge only after PR #34. Reverted originals on PR #34 in order: ebfb11c via 00dd569, df81079 via 7b4b6c0, c42ccb4 via ccd727d; verified the resulting tree exactly matches ae98367 before this tracker update. Owner approved local Docker diagnosis plus one more repair loop. Backup service not activated; live stack untouched. | .agent-runs/20261016-221904-p1-split-and-repair/operator-11-revert-backup-commits.log; operator-13-baseline-identical.log (same folder); draft PR #35 |
| 2026-10-16 | Implementer (agent) | P1.1 / G03 | Added five owned PostgreSQL route-regression cases for heat-curve advice and outcome-experiment status; set 🔵 In progress per owner approval at 16:52. Local collection, Ruff and 48 G03 units pass; inventory/manifest units have 194 passes and one Windows symlink-privilege failure. Real PostgreSQL gate NOT_RUN locally: ownership receipt is required. Awaiting test review, push decision and PR CI owned E2E run; no services or production code touched. | .agent-runs\20261016-143346-tracking-and-phase1\implementer-p11-09-ci-inventory.log (EXIT 0); implementer-p11-10-ruff-check-capture.log through implementer-p11-12-g03-unit-capture.log (EXIT 0); implementer-p11-08-inventory-unit-cwd.log (EXIT 1); implementer-p11-13-guarded-collection-capture.log (EXIT 4); implementer-p11-14-final-diff.log (EXIT 0). Initial transcript output gaps are preserved; capture reruns contain complete output. |
| 2026-10-16 | Implementer (agent) | P1.2 / P1.3 | P1.2/P1.3 backup design: architect packet r1; Quality Reviewer re-review PASS (HIGH-1, HIGH-2, MEDIUM-3 resolved; 1 LOW open). Waiting for owner approval. | .agent-runs\20261016-143346-tracking-and-phase1\architect-03-p12-p13-backup-design-packet-r1.md |
| 2026-10-16 | Implementer (agent) | OP2 | Recorded 🟢 Done: committed locally on fix/phase2-safety-reverts (not pushed): 804a210 G03 fix, f732e0c OP3 tests, 3f72d95 tracker + agent workflow, 3d98913 review docs. Scratch files deleted; live evidence kept local via .gitignore. Push is the user's decision (repo is public). Next: user decides on the Docker test stack for P1.1's database test; otherwise continue with P1.2/P1.3 backup work. | User update 2026-10-16; .agent-runs/20261016-143346-tracking-and-phase1/implementer-op2-01.log |
| 2026-10-16 | Implementer (agent) | OP2 | Deleted the three approved untracked scratch files; added local-only live evidence ignore rules and verified four review files remain visible. Privacy scan found only synthetic values, loopback addresses, and technical prose/code references; no review files excluded. Four local commit groups remain pending for a separate operator; no commit or push performed. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-prep-02-ignore-check.log; implementer-prep-05-final-validation.log (same folder) |
| 2026-10-16 | Implementer (agent) | OP1 | Recorded the user's 2026-10-16 15:45 confirmation that SECRET_KEY was rotated and APP_ENVIRONMENT=production set. Not agent-verified; .env not read. | User confirmation 2026-10-16 15:45; .agent-runs/20261016-143346-tracking-and-phase1/implementer-prep-05-final-validation.log |
| 2026-10-16 | Implementer (agent) | TR-docs | Recorded Release Gate re-evaluation PASS for tracker + agent tracking rules; set 🟢 Done. OP3 remains 🟢 Done; P1.1 remains 🟡 Waiting on you for permission to start the Docker test stack. | .agent-runs/20261016-143346-tracking-and-phase1/release-gate-18-agent-customization-validate.log, release-gate-19-scoped-diff-check.log, release-gate-20-tracker-sync.log (all EXIT 0, same folder) |
| 2026-10-16 | Implementer (agent) | TR-docs | Completed the validation started below: seven YAML frontmatters, exact tracking agent references, tool/edit restrictions, handoffs and relative links PASS; five negative controls reject invalid inputs. No agent-customization defects found or agent edits made. Awaiting independent review/Release Gate re-evaluation. Initial evidence inspection hit a console encoding error; retained the failed log and reran successfully with UTF-8 capture. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-sync-03-validation.log (EXIT 0); implementer-sync-01-baseline.log (EXIT 1); implementer-sync-02-inspect.log (EXIT 0, same folder) |
| 2026-10-16 | Implementer (agent) | OP3 | Recorded Release Gate PASS; set 🟢 Done for the test-only Windows repair. Unit suite: 3865 passed, 74 skipped, EXIT 0. No production repair or deployment approval implied. | .agent-runs/20261016-143346-tracking-and-phase1/release-gate-04-unit-tests.log; release-gate-01 through release-gate-07 (same folder) |
| 2026-10-16 | Test Engineer (agent) | P1.1 / G03 | user authorized Docker test stack 2026-10-16 16:11. At clean 19b4f60, disposable services became healthy (EXIT 0), but pytest could not collect tests: missing COP_MANUAL_TEST_RECEIPT (EXIT 4). Set 🔴 Blocked for the pre-existing test harness, not a demonstrated G03 defect. Teardown succeeded (EXIT 0), zero test containers remain, existing containers unchanged. No guard bypass or live calls. Compatible ownership setup and rerun are required before Release Gate. | .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-int-05-stack-up.log; test-engineer-int-06-backend-e2e.log; test-engineer-int-07-teardown.log; test-engineer-int-08-baseline-scope.log (same folder) |
| 2026-10-16 | Implementer (agent) | P1.1 | Recorded unit suite, Ruff, format and all reviews PASS; set 🟡 Waiting on you. Required isolated PostgreSQL/Redis integration gate NOT_RUN pending permission to start docker-compose.test.yml on ports 5433/6380. | .agent-runs/20261016-143346-tracking-and-phase1/release-gate-01-inventory.log through release-gate-07-final-diff-review.log |
| 2026-10-16 | Implementer (agent) | TR-docs | Recorded Release Gate CHANGES_REQUIRED (stale next step; incomplete agent-customization validation). Updated “At a glance”: user decides on P1.1 Docker test stack, then P1.2/P1.3 backups. Offline YAML/name/tool/handoff/link validation is being performed now; unrelated agent modifications are preserved. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-sync-02-inspect.log; original release-gate-01 through release-gate-07 (same folder) |
| 2026-10-16 | Test Engineer (agent) | OP3 / P1.1 | Independently verified OP3-AC1–AC4; 8 targeted cases pass with 1 expected Windows skip, full suite 3865 passed / 74 skipped, Ruff checks pass. Root causes reproduced with disposable paths; non-Windows fixture branch executed in isolation and AST confirms assertions unchanged. No production/G03 edits. Full-suite result now supplies green P1.1/G03 evidence on the same dirty code tree; both items remain 🧪 pending review/Release Gate. Initial disposable audit-helper error corrected and original log preserved. | .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-op3-01-contract.log (helper error, EXIT 1); test-engineer-op3-02-nine-nodes.log through test-engineer-op3-06-contract-rerun.log (all EXIT 0, same folder) |
| 2026-10-16 | Implementer (agent) | OP3 | Completed test-only Windows repair; all four requested gates pass (full suite 3865 passed / 74 skipped). Eight cases retain real signed publication and lifecycle assertions; only symlink-capability case skips on Windows. No production/G03 edits. Set 🧪 for independent verification/review. Initial baseline runner hit console encoding failure; incomplete 01 log retained, replaced as evidence by complete 01b baseline log. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-op3-01b-baseline.log through implementer-op3-06-full-unit.log; final scoped diff: implementer-op3-07-final-diff.log |
| 2026-10-16 | Implementer (agent) | P1.1 | Recorded user-supplied review handoff: Quality Reviewer code review PASS (no findings); Test Engineer verification PASS for G03-AC1–AC6. Set 🧪; awaiting Release Gate after OP3 makes full suite green. Tracker only; G03 code untouched. | .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-15-final-evidence.log |
| 2026-10-16 | Implementer (agent) | OP3 | User chose to fix OP3 first (2026-10-16 15:10). Started scoped Windows test repair and offline validation; P1.1 unchanged. | .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-12-baseline-failures.log; new evidence uses implementer-op3-NN-*.log in the same folder. |
| 2026-10-16 | Test Engineer (agent) | P1.1 / G03 / OP3 | Reviewed AC1–AC7 and all 48 real-ORM cases; no material test additions needed. Focused/lint gates pass; four in-memory mutations fail as expected and unmutated rerun passes, without production edits. Full suite blocked by 9 Windows failures, all independently reproduced at clean baseline; added OP3. Disposable baseline worktree removed; unrelated changes preserved. | .agent-runs/20261016-143346-tracking-and-phase1/test-engineer-01-focused.log through test-engineer-14-baseline-remove.log (numbered command logs); baseline limitation: excludes user uncommitted changes. |
| 2026-10-16 | Implementer (agent) | P1.1 | Implemented approved boundary repair; focused checks and four revert controls pass. Full-suite gate blocked by 9 pre-existing Windows fsync/symlink failures, reproduced with original G03 functions; no planner edits. | c5c7d9a + scoped dirty edits; implementer conversation command output (sessions 2753–2763). Requested .agent-runs command logs unavailable, not created. Call-site inventory: .agent-runs/20261016-143346-tracking-and-phase1/architect-01-g03-design-packet.md §3. |
| 2026-10-16 | Quality Reviewer (agent) | P1.1 / G03 | G03 design approved by Quality Reviewer; no blocking findings. | .agent-runs/20261016-143346-tracking-and-phase1/architect-01-g03-design-packet.md; user handoff 2026-10-16 |
| 2026-10-16 | Lightweight Reviewer (agent) | TR-docs | 🧪 In verification: Lightweight Reviewer re-review PASS; awaiting Release Gate. | .agent-runs/20261016-143346-tracking-and-phase1/lightweight-reviewer-02-repair1-diff.log |
| 2026-10-16 | Implementer (agent) | TR-docs | Repair 1 clarified direct edits versus read-only handoffs and applied the supplied design update. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-03-repair1.log |
| 2026-10-16 | Solution Architect (agent) | P1.1 | Design for the crash fix completed; going to independent design review. | n/a |
| 2026-10-16 | Lightweight Reviewer (agent) | TR-docs | Review found one wording conflict for read-only agents; fixed in repair 1. | .agent-runs/20261016-143346-tracking-and-phase1/lightweight-reviewer-01-diff.log |
| 2026-10-16 | Implementer (agent) | TR-1–TR-10 | Created this tracker and added agent update duties. Roadmap delivery has not been verified or completed by this task. | .agent-runs/20261016-143346-tracking-and-phase1/implementer-01-diff.log; .agent-runs/20261016-143346-tracking-and-phase1/implementer-02-linkcheck.log |

## Approvals

| Date | What was approved | By |
| --- | --- | --- |
| 2026-10-16 16:11 | Start the disposable docker-compose.test.yml PostgreSQL/Redis stack for P1.1 / G03 integration verification only; no live-stack access or deployment. | user ("go ahead and proceed") |
| 2026-10-16 15:45 | Pre-commit cleanup: delete three stale scratch files, ignore local live review evidence, privacy-scan review docs and tracker, update OP1/OP2, and prepare four local commit groups. Separate operator commits afterward; no push. | user ("go") |
| 2026-10-16 15:10 | Fix OP3 first; LOW-risk test-only Windows repair. | user |
| 2026-10-16 | Start Phase 1 | user |

## How this document is maintained

- This is the single user-facing status for the ML-optimization roadmap.
- Implementer and Test Engineer update their item's status, evidence, and one activity line before returning.
- Read-only specialists return a TRACKER line. Delivery Lead forwards it to an editing specialist and ensures every gate result is recorded.
- Delivery Lead records your decisions and approvals, and arranges a final tracker-only sync after the final gate.
- Update “At a glance” when the phase, next step, or waiting-on-you changes. Keep the update date and role current.
- Mark 🟢 Done only after all required gates pass, including Release Gate PASS.
- Use short, plain-English notes. Do not copy long logs or secrets here.
- Log paths point to the git-ignored `.agent-runs` folder. They are local evidence, not published documentation.

## Background

- [House readiness gap analysis](./reviews/home-readiness-2026-10-16/gap-analysis.md)
- [ML planning handoff](./ml-planning-session-handoff-2026-10-12.md)
- [Diagnostic planning](./diagnostic-planning.md)
- [Configuration reference](./configuration-reference.md)
