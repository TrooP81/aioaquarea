#!/bin/sh
# One source, one destination lock, bounded phases, durable success-anchored schedule.
set -eu
umask 077
SCRIPT_DIR=${0%/*}
[ "$SCRIPT_DIR" != "$0" ] || SCRIPT_DIR=.
: "${PGHOST:=db}"
: "${PGUSER:=heatpump}"
: "${PGDATABASE:=heatpump}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_INTERVAL_SECONDS:=2592000}"
: "${BACKUP_RETENTION_DAYS:=100}"
: "${BACKUP_MAX_AGE_SECONDS:=2764800}"
: "${BACKUP_TIMEOUT_SECONDS:=3600}"
: "${BACKUP_REPLICA_ENABLED:=false}"
: "${BACKUP_REPLICA_REQUIRED:=false}"
: "${BACKUP_REPLICA_DIR:=/replica}"
: "${BACKUP_REPLICA_ENCRYPTION_KEY:=}"
export PGHOST PGUSER PGDATABASE
export PGCONNECT_TIMEOUT=5 PGOPTIONS="-c statement_timeout=20000 -c lock_timeout=5000"

sql() {
    bounded 20 psql -X -qAt --set=ON_ERROR_STOP=1 "$@"
}

owned() {
    owned_limit=$1; shift
    # Separate process group permits settlement of descendants, not just timeout.
    setsid timeout -k 2 "$owned_limit" "$@" <&0 &
    child=$!
    if wait "$child"; then command_result=0; else command_result=$?; fi
    kill -KILL -- "-$child" 2>/dev/null || true
    child=""
    return "$command_result"
}

bounded() {
    limit=$1; shift
    if [ -n "${deadline_epoch:-}" ]; then
        # This clock command is also owned by the MAIN shell, never a subshell.
        owned 5 date -u +%s > "$work/clock" || return 1
        IFS= read -r clock_epoch < "$work/clock" || return 1
        remaining=$((deadline_epoch-clock_epoch))
        [ "$remaining" -gt 0 ] || return 124
        [ "$remaining" -ge "$limit" ] || limit=$remaining
    fi
    owned "$limit" "$@"
}

read_text() {
    # Builtins only; preserve embedded newlines and strip trailing ones like
    # shell capture, without moving the command/process ledger into a subshell.
    captured=""; capture_separator=""; capture_line=""
    while IFS= read -r capture_line || [ -n "$capture_line" ]; do
        captured="$captured$capture_separator$capture_line"
        capture_separator='
'
    done < "$1"
    while :; do
        case $captured in *'
') captured=${captured%?} ;; *) break ;; esac
    done
}

capture() {
    "$@" > "$work/output" || return 1
    read_text "$work/output"
}

read_policy() {
    # Empty persisted values mean environment fallback, exactly as the settings service.
    capture sql -v interval="$BACKUP_INTERVAL_SECONDS" -v retention="$BACKUP_RETENTION_DAYS" \
        -v freshness="$BACKUP_MAX_AGE_SECONDS" -v timeout="$BACKUP_TIMEOUT_SECONDS" <<'SQL' || return 1
WITH defaults(key, value) AS (VALUES
 ('backup_interval_seconds', :'interval'), ('backup_retention_days', :'retention'),
 ('backup_max_age_seconds', :'freshness'), ('backup_timeout_seconds', :'timeout')),
merged AS (SELECT d.key, COALESCE(NULLIF(s.value, ''), d.value) AS value
 FROM defaults d LEFT JOIN settings s USING(key)),
typed AS (SELECT key, CASE WHEN btrim(value) ~ '^[+-]?[0-9]+$' THEN btrim(value)::bigint END AS value FROM merged),
p AS (SELECT max(value) FILTER (WHERE key='backup_interval_seconds') AS i,
 max(value) FILTER (WHERE key='backup_retention_days') AS r,
 max(value) FILTER (WHERE key='backup_max_age_seconds') AS f,
 max(value) FILTER (WHERE key='backup_timeout_seconds') AS t FROM typed)
SELECT i || '|' || r || '|' || f || '|' || t FROM p
WHERE i BETWEEN 3600 AND 7776000 AND r BETWEEN 2 AND 365
 AND f BETWEEN 3600 AND 10368000 AND t BETWEEN 60 AND 21600
 AND f >= i+t+120 AND r*86400 > i AND r*86400 >= f;
SQL
    policy=$captured
    [ -n "$policy" ] || return 1
    IFS='|' read -r interval retention freshness dump_timeout <<EOF
$policy
EOF
}

record_status() {
    phase=$1
    backup_start=false; backup_finish=false; succeeded=false
    [ "$phase" != start ] || backup_start=true
    [ "$phase" != finish ] || backup_finish=true
    [ "$status" != succeeded ] || succeeded=true
    sql -v backup_start="$backup_start" -v backup_finish="$backup_finish" \
        -v verification_start=false -v verification_finish=false \
        -v succeeded="$succeeded" \
        -v attempt="$attempt" -v archive="$archive_id" -v started="$started" \
        -v finished="${finished:-}" -v status="$status" -v reason="${reason:-}" \
        -v replica="${replica:-pending}" -v checksum="${checksum:-}" \
        -v source="${source:-}" -v revision="${revision:-}" \
        -f "$SCRIPT_DIR/backup_status.sql" >/dev/null
}

source_identity() {
    sql <<'SQL'
SELECT md5(system_identifier::text || '/' || current_database()) || '|' ||
 current_setting('server_version_num')::int / 10000 || '|' ||
 (SELECT extversion FROM pg_extension WHERE extname='timescaledb') || '|' ||
 (SELECT version_num FROM alembic_version) || '|' || pg_database_size(current_database())
FROM pg_control_system();
SQL
}

due() {
    # Invalid/legacy state is not a clean activation. Invalid casts fail closed.
    sql -v interval="$interval" -v timeout="$dump_timeout" <<'SQL'
WITH h AS (SELECT details_json::jsonb AS s FROM service_heartbeats WHERE service='backup')
SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM h) THEN 't' ELSE COALESCE((SELECT
 s->'schema_version' = '1'::jsonb
 AND (NOT s ? 'last_success' OR
   (s->'last_success'->>'archive_id' ~ '^heatpump-[0-9a-f]{32}$'
    AND s->'last_success'->>'sha256' ~ '^[0-9a-f]{64}$'
    AND length(s->'last_success'->>'source_id') > 0
    AND length(s->'last_success'->>'schema_revision') > 0
    AND (s->'last_success'->>'snapshot_at')::timestamptz <= now()
    AND (s->'last_success'->>'snapshot_at')::timestamptz + make_interval(secs => :interval) <= now()))
 AND jsonb_typeof(s->'last_attempt') = 'object'
 AND s->'last_attempt'->>'id' ~ '^[0-9a-f]{32}$'
 AND s->'last_attempt'->>'status' IN ('running','succeeded','failed','interrupted')
 AND ((s->'last_attempt'->>'started_at')::timestamptz <= now()
    AND COALESCE((s->'last_attempt'->>'finished_at')::timestamptz,
                 (s->'last_attempt'->>'started_at')::timestamptz) + interval '5 minutes' <= now()
    AND (s->'last_attempt'->>'status' <> 'running'
      OR (s->'last_attempt'->>'started_at')::timestamptz
         + make_interval(secs => :timeout + 120) <= now()))
 FROM h), false) END;
SQL
}

replicate() {
    [ "$BACKUP_REPLICA_ENABLED" = true ] || {
        [ "$BACKUP_REPLICA_REQUIRED" != true ]
        return
    }
    [ -n "$BACKUP_REPLICA_ENCRYPTION_KEY" ] || return 1
    [ -d "$BACKUP_REPLICA_DIR" ] && [ ! -L "$BACKUP_REPLICA_DIR" ] || return 1
    target="$BACKUP_REPLICA_DIR/$archive_id.dump.enc"
    bounded "$dump_timeout" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
        -pass env:BACKUP_REPLICA_ENCRYPTION_KEY -in "$archive" -out "$target.partial" || return 1
    mv "$target.partial" "$target" || return 1
    bounded "$dump_timeout" sha256sum "$target" > "$target.sha256" || return 1
}

manifest_fields() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    read_text "$1"
    sql -v manifest="$captured" -v source="$source" <<'SQL'
WITH m AS (SELECT :'manifest'::jsonb AS j)
SELECT (j->>'archive_id') || '|' || (j->>'sha256') || '|' ||
 extract(epoch FROM (j->>'snapshot_at')::timestamptz)::bigint FROM m
WHERE j->'schema_version'='1'::jsonb AND j->>'source_id'=:'source'
 AND j->'eligible'='true'::jsonb AND j->>'archive_id' ~ '^heatpump-[0-9a-f]{32}$'
 AND j->>'sha256' ~ '^[0-9a-f]{64}$'
 AND (j->>'snapshot_at')::timestamptz <= (j->>'completed_at')::timestamptz
 AND (j->>'completed_at')::timestamptz <= now();
SQL
}

retain() {
    # No inferred "newest filename" protection. Both recorded bindings must be proven.
    capture sql -v source="$source" <<'SQL' || return 1
SELECT (details_json::jsonb->'last_success'->>'archive_id') || '|' ||
 (details_json::jsonb->'last_success'->>'sha256') || '|' ||
 (details_json::jsonb->'last_verified'->>'archive_id') || '|' ||
 (details_json::jsonb->'last_verified'->>'sha256')
FROM service_heartbeats WHERE service='backup'
 AND details_json::jsonb->'schema_version'='1'::jsonb
 AND details_json::jsonb->'last_success'->>'source_id'=:'source'
 AND details_json::jsonb->'last_verified'->>'source_id'=:'source'
 AND (details_json::jsonb->'last_verified'->>'verified_at')::timestamptz <= now()
 AND details_json::jsonb->'last_verification'->>'status'='succeeded'
 AND details_json::jsonb->'last_verification'->>'archive_id'
     = details_json::jsonb->'last_verified'->>'archive_id'
 AND details_json::jsonb->'last_verification'->>'sha256'
     = details_json::jsonb->'last_verified'->>'sha256';
SQL
    protection=$captured
    [ -n "$protection" ] || return 1
    IFS='|' read -r success_id success_sha verified_id verified_sha <<EOF
$protection
EOF
    inventory="$work/inventory"
    : > "$inventory"
    count=0; newest_id=""; newest_time=0; success_found=false; verified_found=false
    for candidate in "$BACKUP_DIR"/heatpump-*.dump; do
        [ -e "$candidate" ] || [ -L "$candidate" ] || continue
        [ -f "$candidate" ] && [ ! -L "$candidate" ] || { rm -f "$inventory"; return 1; }
        [ -f "$candidate.sha256" ] && [ ! -L "$candidate.sha256" ] || { rm -f "$inventory"; return 1; }
        capture manifest_fields "$candidate.json" || { rm -f "$inventory"; return 1; }
        fields=$captured
        [ -n "$fields" ] || { rm -f "$inventory"; return 1; }
        IFS='|' read -r managed_id managed_sha snapshot_epoch <<EOF
$fields
EOF
        [ "$candidate" = "$BACKUP_DIR/$managed_id.dump" ] || { rm -f "$inventory"; return 1; }
        capture bounded "$dump_timeout" sha256sum "$candidate" || { rm -f "$inventory"; return 1; }
        actual_sha=$captured
        actual_sha=${actual_sha%% *}
        [ "$actual_sha" = "$managed_sha" ] || { rm -f "$inventory"; return 1; }
        read_text "$candidate.sha256"
        [ "$captured" = "$managed_sha  $managed_id.dump" ] || { rm -f "$inventory"; return 1; }
        [ "$managed_id|$managed_sha" != "$success_id|$success_sha" ] || success_found=true
        [ "$managed_id|$managed_sha" != "$verified_id|$verified_sha" ] || verified_found=true
        if [ "$snapshot_epoch" -ge "$newest_time" ]; then newest_id=$managed_id; newest_time=$snapshot_epoch; fi
        printf '%s\n' "$fields" >> "$inventory"
        count=$((count+1))
    done
    # Managed-looking orphan metadata is uncertain inventory, never a deletion hint.
    for metadata in "$BACKUP_DIR"/heatpump-*.dump.json "$BACKUP_DIR"/heatpump-*.dump.sha256; do
        [ -e "$metadata" ] || [ -L "$metadata" ] || continue
        case $metadata in *.json) original=${metadata%.json} ;; *) original=${metadata%.sha256} ;; esac
        [ -f "$original" ] && [ ! -L "$original" ] || { rm -f "$inventory"; return 1; }
    done
    if [ "$success_found" != true ] || [ "$verified_found" != true ]; then rm -f "$inventory"; return 1; fi
    capture bounded 5 date -u +%s || { rm -f "$inventory"; return 1; }
    cutoff=$((captured-retention*86400))
    while IFS='|' read -r managed_id managed_sha snapshot_epoch; do
        [ "$count" -gt 2 ] || break
        [ "$snapshot_epoch" -lt "$cutoff" ] || continue
        case $managed_id in "$success_id"|"$verified_id"|"$newest_id") continue ;; esac
        # Direct managed children only; never recurse or follow a link.
        rm "$BACKUP_DIR/$managed_id.dump" "$BACKUP_DIR/$managed_id.dump.json" \
            "$BACKUP_DIR/$managed_id.dump.sha256" || { rm -f "$inventory"; return 1; }
        count=$((count-1))
    done < "$inventory"
    rm "$inventory"
}

attempt_backup() {
    capture bounded 5 tr -d '-' < /proc/sys/kernel/random/uuid || return 1
    attempt=$captured
    archive_id="heatpump-$attempt"
    archive="$BACKUP_DIR/$archive_id.dump"
    partial="$archive.partial"
    capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ || return 1
    started=$captured; finished=""
    capture bounded 5 date -u +%s || return 1
    deadline_epoch=$((captured+dump_timeout))
    status=running; checksum=""; source=""; revision=""; reason=""; replica=pending
    record_status start || return 1
    capture source_identity || return 1
    identity=$captured
    IFS='|' read -r source major timescale revision size <<EOF
$identity
EOF
    [ "$major" = 16 ] && [ "$timescale" = 2.26.4 ] || return 1
    [ -z "${BACKUP_EXPECTED_SOURCE_ID:-}" ] || [ "$source" = "$BACKUP_EXPECTED_SOURCE_ID" ] || return 1
    bounded "$dump_timeout" pg_dump --format=custom --no-owner \
        --lock-wait-timeout=10000 --file="$partial" || return 1
    bounded 30 pg_restore --list "$partial" >/dev/null || return 1
    # Database size may change during a valid dump; it is capacity metadata, not identity.
    capture source_identity || return 1
    current_identity=$captured
    [ "${current_identity%|*}" = "${identity%|*}" ] || return 1
    capture bounded "$dump_timeout" sha256sum "$partial" || return 1
    checksum=$captured
    checksum=${checksum%% *}
    mv "$partial" "$archive" || return 1
    printf '%s  %s\n' "$checksum" "$archive_id.dump" > "$archive.sha256.partial"
    mv "$archive.sha256.partial" "$archive.sha256" || return 1
    replica=disabled
    if replicate; then
        [ "$BACKUP_REPLICA_ENABLED" != true ] || replica=succeeded
    elif [ "$BACKUP_REPLICA_REQUIRED" = true ]; then
        replica=required_failed; reason=backup_replica_required_failed; return 1
    else
        replica=optional_failed
    fi
    capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ || return 1
    finished=$captured
    if ! sql -v archive="$archive_id" -v checksum="$checksum" -v source="$source" \
        -v revision="$revision" -v major="$major" -v timescale="$timescale" \
        -v size="$size" -v started="$started" -v finished="$finished" <<'SQL' > "$archive.json.partial"
SELECT jsonb_build_object('schema_version',1,'eligible',true,'archive_id',:'archive',
 'sha256',:'checksum','source_id',:'source','schema_revision',:'revision',
 'postgres_major',:major,'timescale_version',:'timescale','database_size_bytes',:size,
 'snapshot_at',:'started','completed_at',:'finished');
SQL
    then return 1; fi
    mv "$archive.json.partial" "$archive.json" || return 1
    status=succeeded
    record_status finish || { status=failed; return 1; }
    if ! retain; then
        reason=backup_retention_blocked
        # Preserve the completed success while making blocked retention visible.
        record_status finish || return 1
    fi
}

settle() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ -n "${child:-}" ]; then
        if kill -TERM -- "-$child" 2>/dev/null; then
            sleep 2 || true
            kill -KILL -- "-$child" 2>/dev/null || true
        fi
        wait "$child" || true
    fi
    # A failed terminal attempt has already been reported. Normal --once exit
    # must not relabel that failure as interruption.
    if [ "${status:-}" = running ]; then
        status=interrupted
        reason=backup_attempt_failed
        deadline_epoch="" # interruption reporting has its own bounded opportunity
        if capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ; then
            finished=$captured
            record_status finish || true
        fi
    fi
    [ -z "${partial:-}" ] || rm -f "$partial" "$archive.json.partial" "$archive.sha256.partial"
    if [ "${work_created:-false}" = true ]; then
        rm -f "$work/output" "$work/clock" "$work/inventory" || result=1
        rmdir "$work" || result=1
    fi
    exit "$result"
}

# Destination creation/protection is an operator responsibility, never implicit.
[ -d "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR" ] || exit 2
for tool in flock timeout setsid psql pg_dump pg_restore sha256sum; do command -v "$tool" >/dev/null || exit 2; done
[ ! -L "$BACKUP_DIR/.backup.lock" ] || exit 2
exec 9>"$BACKUP_DIR/.backup.lock"
trap settle EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
work_created=false
deadline_epoch=""
workspace_tries=0
while [ "$workspace_tries" -lt 10 ]; do
    # The Linux kernel UUID source is already required for archive IDs. Read it
    # with a builtin: no bootstrap output file or hidden command substitution.
    IFS= read -r workspace_uuid < /proc/sys/kernel/random/uuid || exit 2
    case $workspace_uuid in ""|*[!0-9a-f-]*) exit 2 ;; esac
    [ "${#workspace_uuid}" -eq 36 ] || exit 2
    work="$BACKUP_DIR/.work-$workspace_uuid"
    if bounded 5 mkdir "$work"; then
        work_created=true
        break
    fi
    # Retry an existing random name, but never adopt or clean it. Other storage
    # failures fail closed without attempting a broad cleanup.
    [ -e "$work" ] || [ -L "$work" ] || exit 2
    workspace_tries=$((workspace_tries+1))
done
[ "$work_created" = true ] || exit 2
while :; do
    # Nonblocking acquisition; policy and durable due state are read under the lock.
    if flock -n 9; then
        deadline_epoch=""
        if read_policy && capture due && [ "$captured" = t ]; then
            # The shell remains the lifecycle owner; timeout bounds/reaps each tool.
            if ! attempt_backup; then
                status=failed
                # Terminal reporting gets its own bounded 20-second opportunity.
                deadline_epoch=""
                reason=${reason:-backup_attempt_failed}
                if capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ; then
                    finished=$captured
                    record_status finish || true
                fi
                [ -z "${partial:-}" ] || rm -f "$partial"
            fi
        fi
        flock -u 9
    fi
    [ "${1:-}" != --once ] || break
    setsid sleep 60 &
    child=$!; wait "$child"; child=""
done
