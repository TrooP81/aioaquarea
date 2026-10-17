-- Parameterized, fenced and atomic history merge. Never use updated_at as success.
\set ON_ERROR_STOP on
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('backup:status', 0));
CREATE TEMP TABLE backup_state ON COMMIT DROP AS
SELECT COALESCE(
  (SELECT details_json::jsonb FROM service_heartbeats WHERE service = 'backup'),
  '{"schema_version":1}'::jsonb
) AS state;
-- Unsupported evidence is not silently replaced, including legacy status=ok.
SELECT 1 / CASE WHEN state->'schema_version' = '1'::jsonb THEN 1 ELSE 0 END FROM backup_state;
\if :backup_start
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_attempt', jsonb_build_object('id', :'attempt', 'started_at', :'started',
    'status', 'running', 'archive_id', :'archive', 'replica_status', 'pending'));
\endif
\if :backup_finish
SELECT 1 / CASE WHEN state->'last_attempt'->>'id' = :'attempt' THEN 1 ELSE 0 END FROM backup_state;
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_attempt', state->'last_attempt' || jsonb_build_object('finished_at', :'finished',
    'status', :'status', 'reason_code', :'reason', 'replica_status', :'replica'));
\if :succeeded
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_success', jsonb_build_object('archive_id', :'archive', 'snapshot_at', :'started',
    'completed_at', :'finished', 'sha256', :'checksum', 'source_id', :'source',
    'schema_revision', :'revision'));
\endif
\endif
\if :verification_start
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_verification', jsonb_build_object('id', :'attempt', 'archive_id', :'archive',
    'sha256', :'checksum', 'started_at', :'started', 'status', 'running'));
\endif
\if :verification_finish
SELECT 1 / CASE WHEN state->'last_verification'->>'id' = :'attempt'
  AND state->'last_verification'->>'archive_id' = :'archive'
  AND state->'last_verification'->>'sha256' = :'checksum' THEN 1 ELSE 0 END FROM backup_state;
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_verification', state->'last_verification' || jsonb_build_object(
    'finished_at', :'finished', 'status', :'status', 'reason_code', :'reason'));
\if :succeeded
UPDATE backup_state SET state = state || jsonb_build_object(
  'last_verified', jsonb_build_object('archive_id', :'archive', 'sha256', :'checksum',
    'verified_at', :'finished', 'source_id', :'source', 'schema_revision', :'revision'));
\endif
\endif
INSERT INTO service_heartbeats(service, updated_at, details_json)
SELECT 'backup', now(), state::text FROM backup_state
ON CONFLICT(service) DO UPDATE SET updated_at = EXCLUDED.updated_at,
  details_json = EXCLUDED.details_json;
COMMIT;
