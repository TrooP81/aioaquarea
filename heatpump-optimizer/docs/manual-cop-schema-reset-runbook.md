# Manual COP schema change and ledger reset runbook

This runbook covers migration `036_manual_cop_local_periods` and the standalone `manual_cop` reset scope. Approving the code does not approve a production migration, deployment or reset; each needs its own authorization.

## What migration 036 changes

- Drops `manual_cop_observations.period_start` and `period_end`, the checks that compared them with SQL-computed local midnights, and the function `public.manual_cop_calendar_midnight`.
- Keeps every row, every revision token, `manual_cop_revision_seq` and all other tables unchanged.
- The API keeps returning `period_start`/`period_end`, now derived from `period_date`, `granularity` and `timezone`. Contract version: `2026-10-12.1`.
- Forward-only. `alembic downgrade 035` raises `manual_cop_local_periods_forward_only` and changes nothing.

## Required approval and evidence gate

Do not run any command in the Upgrade, Rollback or Manual ledger reset sections until a human has approved, in writing, all of the following for this specific operation:

1. **Exact target.** The database host, database name and environment the command will run against. Approval for one target does not cover another.
2. **Exact candidate.** The commit SHA and image tag/digest to be deployed, matching the independently reviewed and CI-qualified candidate.
3. **Exact operation.** Which of upgrade to `036`, ledger reset, or snapshot restore is being done, and in what order.
4. **Safety-preservation evidence**, collected from the target immediately before the operation:
   - `alembic_version` (expected `035` before an upgrade);
   - the manual row count and the `manual_cop_revision_seq` state;
   - every pending, dispatched or executing restore obligation in `plan_actions` (non-null `reverts_action_id` with status `pending`, `dispatched` or `executing`), with its `safety_claim_token`, `safety_claimed_at` and `safety_next_retry_at`. These must be settled through the normal executor first, or explicitly preserved unchanged. Never delete them, rewrite their claims or truncate their tables to make an operation succeed.
5. **Recovery choice.** Agreement on how to recover if the operation fails (see Rollback). A pre-operation snapshot is optional when the approver accepts losing manual readings; record that decision.

Stop if any of these is missing or the target's state differs from the evidence.

## Before upgrading

Builds older than the reviewed candidate read and write `period_start`/`period_end`, which `036` removes. Once `036` is applied, every such process fails on manual COP reads, not only on writes. Short reads are therefore not safe to leave running.

1. Complete the approval and evidence gate above.
2. Quiesce **every** process that is not the reviewed `036`-compatible candidate and can touch the database: the `api`, `poller`, `optimizer` and `web` services, any extra workers or replicas, scheduled or one-off jobs, and anything that installs or alters schema from application metadata (for example an old image's table-creation step or an ad-hoc script). Stop them; do not just drain traffic. The `db` service keeps running.
3. Confirm that nothing else is connected. On the target, `pg_stat_activity` for the application database must show only your own session.
4. Do not start any old image again after this point, including for diagnosis.

## Upgrade

Run the migration once, with the candidate image, while everything else is stopped:

```bash
alembic upgrade head
```

The migration sets `lock_timeout = 5s` and `statement_timeout = 20s` for its transaction. If it cannot get the lock it fails with a lock-timeout error and the database stays at `035` with nothing changed. Find the session that still holds the lock, stop the process that owns it (step 2 above), then run the command again. Do not raise the timeouts.

Then start **only** the reviewed `036`-compatible candidate (the exact commit and image approved in the gate) for `api`, `poller` and `optimizer`. Keep `web` and any user-facing access closed until verification passes.

## Verify

Check all of the following before reopening the service. If any check fails, keep the service closed and go to Rollback.

- `alembic_version` is `036`.
- The manual row count and the `manual_cop_revision_seq` state are unchanged.
- `SELECT to_regprocedure('public.manual_cop_calendar_midnight(timestamp without time zone,text)')` returns `NULL`.
- Every running process reports the approved candidate commit and image; no older build is running.
- Model maintenance is still disabled (`ml_maintenance_enabled = false`) and the build is still observe-only: ordinary device writes stay blocked and no setting has enabled optimization or forecasting.
- The restore obligations recorded in the gate are still present with their claims unchanged, apart from settlement by the normal executor.
- `GET /api/cop/manual?device_id=...` returns the same readings with derived boundaries and `scope: "unknown"`.

Only after every check passes, start `web` and reopen user access.

## Rollback

There is no schema downgrade. Prefer a forward repair, or a compatible observe-only build that runs on `036`, with forecasting turned off (`manual_cop_monitoring_enabled = false`).

Restoring a database snapshot is a separate operation that needs its own approval through the gate above, naming the snapshot and the target. Before restoring, reconcile safety state:

- Compare the snapshot's restore obligations with the live ones. Obligations created or settled after the snapshot must be carried forward or re-settled, not lost.
- **Restoring the database does not reverse anything already done to the heat pump.** Device commands, restores and mode changes that ran after the snapshot stay in effect on the device. Check the actual device state and settle it through the normal executor; never assume the restored rows describe the device.

Writes made after the snapshot are not in it.

## Manual ledger reset

Complete the approval and evidence gate above first, naming this reset as the operation. Then send `POST /api/admin/reset` with body `{"scopes": ["manual_cop"]}`. The scope must be the only one in the request. The Reset Data card offers it as a separate action.

In one transaction (`lock_timeout` 5 s, `statement_timeout` 20 s), the reset:

- deletes all manual readings for every device;
- upserts `manual_cop_monitoring_enabled = false`;
- inserts exactly one `reset_data` audit row.

It does not reset `manual_cop_revision_seq`, so earlier revision tokens keep conflicting. It does not touch any other table, setting, model, artifact, Redis key or restore safety claim.

If the reset fails, the response is 503 `manual_cop_reset_unavailable` and nothing is committed, although an allocated audit id may be skipped. Deleted readings can only be recovered from a snapshot, through a separately approved restore that follows the Rollback rules above. If the approver has accepted losing the readings, no snapshot is needed.

