-- Private worker-owned transaction. Optional parameters preserve verifier callers.
\set ON_ERROR_STOP on
\if :{?replica}
\else
\set replica pending
\endif
\if :{?transition_legacy}
\else
\set transition_legacy false
\endif
\if :{?backup_retention_diagnostic}
\else
\set backup_retention_diagnostic false
\endif
\if :{?retention_mode}
\else
\set retention_mode normal
\endif
\if :{?expected_state}
\else
\set expected_state ''
\endif
\if :{?expected_source}
\else
\set expected_source ''
\endif
\if :{?expected_revision}
\else
\set expected_revision ''
\endif
\if :{?legacy_sha256}
\else
\set legacy_sha256 ''
\endif
\if :{?legacy_updated_at}
\else
\set legacy_updated_at ''
\endif
\if :{?interval}
\else
\set interval 2592000
\endif
\if :{?timeout}
\else
\set timeout 3600
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('backup:status', 0));
CREATE TEMP TABLE backup_request ON COMMIT DROP AS SELECT
 :'backup_start'::boolean AS bs, :'backup_finish'::boolean AS bf,
 :'verification_start'::boolean AS vs, :'verification_finish'::boolean AS vf,
 :'backup_retention_diagnostic'::boolean AS diagnostic,
 :'transition_legacy'::boolean AS transition, :'succeeded'::boolean AS succeeded,
 :'attempt'::text AS attempt, :'archive'::text AS archive, :'started'::text AS started,
 :'finished'::text AS finished, :'status'::text AS status, :'reason'::text AS reason,
 :'replica'::text AS replica, :'checksum'::text AS checksum, :'source'::text AS source,
 :'revision'::text AS revision, :'retention_mode'::text AS mode,
 :'expected_state'::text AS fingerprint, :'expected_source'::text AS expected_source,
 :'expected_revision'::text AS expected_revision, :'legacy_sha256'::text AS legacy_sha256,
 :'legacy_updated_at'::text AS legacy_updated_at, :'interval'::int AS interval_seconds,
 :'timeout'::int AS timeout_seconds;

CREATE FUNCTION pg_temp.require(ok boolean) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
 IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'backup_state_rejected'; END IF;
END $$;
CREATE FUNCTION pg_temp.attempt_valid(a jsonb) RETURNS boolean LANGUAGE sql AS $$
 SELECT COALESCE(jsonb_typeof(a)='object'
 AND a->>'id' ~ '^[0-9a-f]{32}$' AND a->>'archive_id' ~ '^heatpump-[0-9a-f]{32}$'
 AND a->>'archive_id'='heatpump-'||(a->>'id')
 AND a->>'status' IN ('running','succeeded','failed','interrupted')
 AND a->>'replica_status' IN ('pending','disabled','succeeded','required_failed','optional_failed')
 AND isfinite((a->>'started_at')::timestamptz)
 AND (a->>'started_at')::timestamptz <= now()
 AND (NOT a ? 'retention_mode' OR a->>'retention_mode' IN ('normal','preserve'))
 AND (a->>'status' = 'running' AND NOT a ? 'finished_at'
   OR a->>'status' <> 'running'
      AND (a->>'finished_at')::timestamptz >= (a->>'started_at')::timestamptz
      AND (a->>'finished_at')::timestamptz <= now()),false)
$$;
CREATE FUNCTION pg_temp.binding_valid(a jsonb) RETURNS boolean LANGUAGE sql AS $$
 SELECT COALESCE(jsonb_typeof(a)='object'
 AND a->>'archive_id' ~ '^heatpump-[0-9a-f]{32}$' AND a->>'sha256' ~ '^[0-9a-f]{64}$'
 AND length(a->>'source_id')>0 AND length(a->>'schema_revision')>0
 AND (a->>'snapshot_at')::timestamptz <= (a->>'completed_at')::timestamptz
 AND isfinite((a->>'snapshot_at')::timestamptz)
 AND (a->>'completed_at')::timestamptz <= now(),false)
$$;
CREATE FUNCTION pg_temp.legacy_valid(e jsonb, a jsonb) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE raw text; first_event jsonb;
BEGIN
 IF e IS NULL THEN RETURN true; END IF;
 raw := e->'original'->>'details_json';
 first_event := e->'first_attempt';
 RETURN COALESCE(jsonb_typeof(e)='object' AND e->'schema_version'='1'::jsonb
 AND e->>'kind'='legacy-status-ok-to-v1'
 AND (e->>'transitioned_at')::timestamptz <= now()
 AND length(e->>'source_id')>0 AND length(e->>'schema_revision')>0
 AND e->'original'->>'service'='backup'
 AND (e->'original'->>'updated_at')::timestamptz <= (e->>'transitioned_at')::timestamptz
 AND raw::jsonb='{"status":"ok"}'::jsonb
 AND (SELECT count(*)=1 FROM json_each(raw::json))
 AND encode(sha256(convert_to(raw,'UTF8')),'hex')=e->'original'->>'details_sha256'
 AND pg_temp.attempt_valid(first_event)
 AND (first_event->>'started_at')::timestamptz <= (e->>'transitioned_at')::timestamptz
 AND first_event->>'id'=e->>'first_attempt_id'
 AND first_event->>'retention_mode'='preserve'
 AND first_event->>'source_id'=e->>'source_id'
 AND first_event->>'schema_revision'=e->>'schema_revision'
 AND (first_event->>'status'<>'succeeded' OR
   (first_event->>'reason_code'='' AND first_event->>'sha256' ~ '^[0-9a-f]{64}$'))
 AND (a->>'id'<>e->>'first_attempt_id' OR first_event=a),false);
EXCEPTION WHEN OTHERS THEN RETURN false;
END $$;

DO $$
DECLARE
 r record; h record; s jsonb; old jsonb; event jsonb; binding jsonb; canonical jsonb;
 fingerprint text; actual_source text; actual_revision text; transition_envelope jsonb;
 changed_rows int;
BEGIN
 SELECT * INTO r FROM backup_request;
 PERFORM pg_temp.require(r.bs::int+r.bf::int+r.vs::int+r.vf::int+r.diagnostic::int=1);
 SELECT * INTO h FROM service_heartbeats WHERE service='backup' FOR UPDATE;
 fingerprint := CASE WHEN h.service IS NULL THEN 'absent'
   ELSE encode(sha256(convert_to(h.updated_at::text||'/'||h.details_json,'UTF8')),'hex') END;
 s := CASE WHEN h.service IS NULL THEN '{"schema_version":1}'::jsonb ELSE h.details_json::jsonb END;
 old := s;
 PERFORM pg_temp.require(jsonb_typeof(s)='object');
 IF r.transition THEN
   PERFORM pg_temp.require(r.bs AND r.mode='preserve' AND h.service='backup'
     AND s='{"status":"ok"}'::jsonb
     AND (SELECT count(*)=1 FROM json_each(h.details_json::json))
     AND encode(sha256(convert_to(h.details_json,'UTF8')),'hex')=r.legacy_sha256
     AND h.updated_at=r.legacy_updated_at::timestamptz);
 ELSE
   PERFORM pg_temp.require(s->'schema_version'='1'::jsonb);
   PERFORM pg_temp.require(pg_temp.legacy_valid(s->'legacy_transition',s->'last_attempt'));
 END IF;
 IF r.bs OR r.bf OR r.diagnostic THEN
   PERFORM pg_temp.require(r.attempt ~ '^[0-9a-f]{32}$'
     AND r.archive='heatpump-'||r.attempt AND r.mode IN ('normal','preserve')
     AND r.started::timestamptz <= now());
   IF NOT r.transition AND s ? 'last_success' THEN
     PERFORM pg_temp.require(pg_temp.binding_valid(s->'last_success'));
   END IF;
 END IF;
 IF r.bs THEN
   PERFORM pg_temp.require(fingerprint=r.fingerprint AND r.status='running'
     AND NOT r.succeeded AND r.interval_seconds BETWEEN 3600 AND 7776000
     AND r.timeout_seconds BETWEEN 60 AND 21600);
   SELECT md5(system_identifier::text||'/'||current_database()) INTO actual_source FROM pg_control_system();
   SELECT version_num INTO STRICT actual_revision FROM alembic_version;
   PERFORM pg_temp.require(actual_source=r.source AND actual_revision=r.revision
     AND (r.expected_source='' OR actual_source=r.expected_source)
     AND (r.expected_revision='' OR actual_revision=r.expected_revision));
   IF r.mode='preserve' THEN
     PERFORM pg_temp.require(r.expected_source<>'' AND r.expected_revision<>''
       AND current_setting('server_version_num')::int/10000=16
       AND (SELECT extversion='2.26.4' FROM pg_extension WHERE extname='timescaledb'));
   END IF;
   IF NOT r.transition AND h.service IS NOT NULL THEN
     PERFORM pg_temp.require(pg_temp.attempt_valid(s->'last_attempt')
       AND (NOT s ? 'last_success' OR pg_temp.binding_valid(s->'last_success')));
     PERFORM pg_temp.require((NOT s ? 'last_success' OR
        (s->'last_success'->>'snapshot_at')::timestamptz
           +make_interval(secs=>r.interval_seconds)<=now())
       AND COALESCE((s->'last_attempt'->>'finished_at')::timestamptz,
          (s->'last_attempt'->>'started_at')::timestamptz)+interval '5 minutes'<=now()
       AND (s->'last_attempt'->>'status'<>'running' OR
          (s->'last_attempt'->>'started_at')::timestamptz
            +make_interval(secs=>r.timeout_seconds+120)<=now()));
     -- Old server-side dump activity must have settled, even across destinations.
     PERFORM pg_temp.require(NOT EXISTS(SELECT 1 FROM pg_stat_activity
        WHERE application_name='backup:'||(s->'last_attempt'->>'id')
          AND pid<>pg_backend_pid()));
   END IF;
   event := jsonb_build_object('id',r.attempt,'archive_id',r.archive,'started_at',r.started,
     'status','running','replica_status','pending','retention_mode',r.mode,
     'source_id',r.source,'schema_revision',r.revision);
   IF r.transition THEN
     transition_envelope := jsonb_build_object('schema_version',1,
       'kind','legacy-status-ok-to-v1','transitioned_at',now(),'source_id',r.source,
       'schema_revision',r.revision,'first_attempt_id',r.attempt,'first_attempt',event,
       'original',jsonb_build_object('service',h.service,'updated_at',h.updated_at,
          'details_json',h.details_json,'details_sha256',r.legacy_sha256));
     s := jsonb_build_object('schema_version',1,'legacy_transition',transition_envelope);
   END IF;
   s := s || jsonb_build_object('last_attempt',event);
 ELSIF r.bf OR r.diagnostic THEN
   event := s->'last_attempt';
   PERFORM pg_temp.require(pg_temp.attempt_valid(event)
     AND event->>'id'=r.attempt AND event->>'archive_id'=r.archive
     AND event->>'started_at'=r.started AND event->>'retention_mode'=r.mode
     AND event->>'source_id'=r.source AND event->>'schema_revision'=r.revision
     AND r.status IN ('succeeded','failed','interrupted')
     AND r.succeeded=(r.status='succeeded')
     AND r.finished::timestamptz >= r.started::timestamptz
     AND r.finished::timestamptz <= now()
     AND r.replica IN ('pending','disabled','succeeded','required_failed','optional_failed')
     AND (r.status<>'succeeded' OR r.replica IN ('disabled','succeeded','optional_failed')));
   binding := jsonb_build_object('archive_id',r.archive,'snapshot_at',r.started,
     'completed_at',r.finished,'sha256',r.checksum,'source_id',r.source,'schema_revision',r.revision);
   IF r.status='succeeded' THEN PERFORM pg_temp.require(pg_temp.binding_valid(binding)); END IF;
   canonical := event || jsonb_build_object('finished_at',r.finished,'status',r.status,
     'reason_code',CASE WHEN r.diagnostic THEN '' ELSE r.reason END,'replica_status',r.replica,
     'sha256',r.checksum);
   IF r.diagnostic THEN
     PERFORM pg_temp.require(r.reason='backup_retention_blocked' AND r.mode='normal'
       AND event->>'status'='succeeded' AND r.status='succeeded'
       AND event->>'reason_code' IN ('','backup_retention_blocked')
       AND (event-'reason_code')=(canonical-'reason_code') AND s->'last_success'=binding);
     s := jsonb_set(s,'{last_attempt,reason_code}',to_jsonb(r.reason));
   ELSE
     PERFORM pg_temp.require(r.reason<>'backup_retention_blocked'
       AND (r.status<>'succeeded' OR r.reason=''));
     IF event->>'status'='running' THEN
       s := s || jsonb_build_object('last_attempt',canonical);
       IF r.succeeded THEN s := s || jsonb_build_object('last_success',binding); END IF;
       IF s->'legacy_transition'->>'first_attempt_id'=r.attempt THEN
         s := jsonb_set(s,'{legacy_transition,first_attempt}',canonical);
       END IF;
     ELSE
       PERFORM pg_temp.require((event=canonical OR
         (event->>'status'='succeeded' AND event->>'reason_code'='backup_retention_blocked'
          AND r.reason='' AND (event-'reason_code')=(canonical-'reason_code')))
         AND (NOT r.succeeded OR s->'last_success'=binding));
       -- An exact replay never clears a later diagnostic or advances updated_at.
     END IF;
   END IF;
 ELSE
   -- Existing coordinator parameters remain sufficient; merge under the same lock.
   PERFORM pg_temp.require(r.attempt ~ '^[0-9a-f]{32}$'
     AND r.archive ~ '^heatpump-[0-9a-f]{32}$' AND r.checksum ~ '^[0-9a-f]{64}$'
     AND isfinite(r.started::timestamptz) AND r.started::timestamptz<=now());
   IF r.vs THEN
     s := s || jsonb_build_object('last_verification',jsonb_build_object(
       'id',r.attempt,'archive_id',r.archive,'sha256',r.checksum,
       'started_at',r.started,'status','running'));
   ELSE
     PERFORM pg_temp.require(s->'last_verification'->>'id'=r.attempt
       AND s->'last_verification'->>'archive_id'=r.archive
       AND s->'last_verification'->>'sha256'=r.checksum
       AND jsonb_typeof(s->'last_verification')='object'
       AND s->'last_verification'->>'status' IN ('running','succeeded','failed','interrupted')
       AND s->'last_verification'->>'started_at'=r.started
       AND r.status IN ('succeeded','failed','interrupted')
       AND r.succeeded=(r.status='succeeded')
       AND r.finished::timestamptz>=r.started::timestamptz
       AND r.finished::timestamptz<=now());
     s := s || jsonb_build_object('last_verification',s->'last_verification' ||
       jsonb_build_object('finished_at',r.finished,'status',r.status,'reason_code',r.reason));
     IF r.succeeded THEN
       s := s || jsonb_build_object('last_verified',jsonb_build_object('archive_id',r.archive,
         'sha256',r.checksum,'verified_at',r.finished,'source_id',r.source,'schema_revision',r.revision));
     END IF;
   END IF;
 END IF;
 PERFORM pg_temp.require(pg_temp.legacy_valid(s->'legacy_transition',s->'last_attempt'));
 IF s IS DISTINCT FROM old THEN
   IF h.service IS NULL THEN
     INSERT INTO service_heartbeats(service,updated_at,details_json)
       VALUES ('backup',now(),s::text) ON CONFLICT(service) DO NOTHING;
   ELSE
     UPDATE service_heartbeats SET updated_at=now(),details_json=s::text
       WHERE service='backup' AND updated_at=h.updated_at
         AND details_json IS NOT DISTINCT FROM h.details_json;
   END IF;
   GET DIAGNOSTICS changed_rows = ROW_COUNT;
   PERFORM pg_temp.require(changed_rows=1);
 END IF;
END $$;
COMMIT;
