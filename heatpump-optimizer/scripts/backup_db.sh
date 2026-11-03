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

# Parse before tools, locks or any destination mutation. Unknown options never
# accidentally enter the scheduler.
once=false; retention_mode=normal; transition_legacy=false
for option in "$@"; do
    case $option in
        --once) [ "$once" = false ] || exit 2; once=true ;;
        --once-preserve-archives)
            [ "$once" = false ] || exit 2
            once=true; retention_mode=preserve ;;
        --transition-legacy)
            [ "$transition_legacy" = false ] || exit 2; transition_legacy=true ;;
        *) exit 2 ;;
    esac
done
if [ "$retention_mode" = preserve ]; then
    [ -n "${BACKUP_EXPECTED_SOURCE_ID:-}" ] && [ -n "${BACKUP_EXPECTED_SCHEMA_REVISION:-}" ] || exit 2
fi
if [ "$transition_legacy" = true ]; then
    [ "$retention_mode" = preserve ] && [ -n "${BACKUP_LEGACY_EXPECTED_SHA256:-}" ] \
        && [ -n "${BACKUP_LEGACY_EXPECTED_UPDATED_AT:-}" ] || exit 2
fi

sql() {
    bounded 20 psql -X -qAt --set=ON_ERROR_STOP=1 "$@"
}

owned() {
    owned_limit=$1; shift
    # Separate process group permits settlement of descendants, not just timeout.
    # POSIX sh redirects an asynchronous list's stdin to /dev/null before
    # applying its redirections. Save the caller's heredoc in the MAIN shell.
    exec 7<&0
    setsid timeout -k 2 "$owned_limit" "$@" <&7 7<&- &
    child=$!
    exec 7<&-
    if wait "$child"; then command_result=0; else command_result=$?; fi
    kill -s KILL -- "-$child" 2>/dev/null || true
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
    backup_start=false; backup_finish=false; backup_retention_diagnostic=false; succeeded=false
    [ "$phase" != start ] || backup_start=true
    [ "$phase" != finish ] || backup_finish=true
    [ "$phase" != diagnostic ] || backup_retention_diagnostic=true
    [ "$status" != succeeded ] || succeeded=true
    phase_transition=false
    [ "$phase" != start ] || phase_transition=$transition_legacy
    sql -v backup_start="$backup_start" -v backup_finish="$backup_finish" \
        -v verification_start=false -v verification_finish=false \
        -v succeeded="$succeeded" \
        -v attempt="$attempt" -v archive="$archive_id" -v started="$started" \
        -v finished="${finished:-}" -v status="$status" -v reason="${reason:-}" \
        -v replica="${replica:-pending}" -v checksum="${checksum:-}" \
        -v source="${source:-}" -v revision="${revision:-}" \
        -v backup_retention_diagnostic="$backup_retention_diagnostic" \
        -v transition_legacy="$phase_transition" \
        -v retention_mode="$retention_mode" -v expected_state="${expected_state:-}" \
        -v expected_source="${BACKUP_EXPECTED_SOURCE_ID:-}" \
        -v expected_revision="${BACKUP_EXPECTED_SCHEMA_REVISION:-}" \
        -v legacy_sha256="${BACKUP_LEGACY_EXPECTED_SHA256:-}" \
        -v legacy_updated_at="${BACKUP_LEGACY_EXPECTED_UPDATED_AT:-}" \
        -v interval="$interval" -v timeout="$dump_timeout" \
        -f "$SCRIPT_DIR/backup_status.sql" >/dev/null 2>&1
}

reconcile_start() {
    capture sql -v attempt="$attempt" -v archive="$archive_id" -v started="$started" \
        -v source="$source" -v revision="$revision" -v mode="$retention_mode" <<'SQL' || return 1
SELECT COALESCE((SELECT details_json::jsonb->'last_attempt' @>
 jsonb_build_object('id',:'attempt','archive_id',:'archive','started_at',:'started',
 'source_id',:'source','schema_revision',:'revision','retention_mode',:'mode','status','running')
 FROM service_heartbeats WHERE service='backup'),false);
SQL
    [ "$captured" = t ]
}

fingerprint() {
    sql <<'SQL'
SELECT COALESCE((SELECT encode(sha256(convert_to(updated_at::text||'/'||details_json,'UTF8')),'hex')
 FROM service_heartbeats WHERE service='backup'),'absent');
SQL
}

settle_source() {
    # Killing a libpq client is not proof that its server-side query stopped.
    # Cancel only this random, source-bound owner's sessions, then prove absence.
    sql -v attempt="$attempt" -v source="$source" <<'SQL' >/dev/null 2>&1 || return 1
SELECT pg_cancel_backend(pid) FROM pg_stat_activity
WHERE application_name='backup:'||:'attempt' AND pid<>pg_backend_pid()
 AND :'source'=(SELECT md5(system_identifier::text||'/'||current_database()) FROM pg_control_system());
SQL
    settlement_tries=0
    while [ "$settlement_tries" -lt 3 ]; do
        capture sql -v attempt="$attempt" <<'SQL' || return 1
SELECT NOT EXISTS(SELECT 1 FROM pg_stat_activity
 WHERE application_name='backup:'||:'attempt' AND pid<>pg_backend_pid());
SQL
        [ "$captured" != t ] || return 0
        bounded 2 sleep 1 || return 1
        settlement_tries=$((settlement_tries+1))
    done
    return 1
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
    # The transaction, not this shortcut, authenticates the exact legacy shape.
    [ "$transition_legacy" != true ] || { printf 't\n'; return; }
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
    (set -C; : > "$target.partial") || return 1
    replica_scratch_owned=true
    bounded "$dump_timeout" openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
        -pass env:BACKUP_REPLICA_ENCRYPTION_KEY -in "$archive" -out "$target.partial" || return 1
    publish "$target.partial" "$target" || return 1
    replica_scratch_owned=false
    (set -C; : > "$target.sha256.partial") || return 1
    replica_checksum_scratch_owned=true
    bounded "$dump_timeout" sha256sum "$target" > "$target.sha256.partial" || return 1
    publish "$target.sha256.partial" "$target.sha256" || return 1
    replica_checksum_scratch_owned=false
}

cleanup_scratch() {
    [ "${scratch_owned:-false}" != true ] || rm -f "$partial"
    [ "${checksum_scratch_owned:-false}" != true ] || rm -f "$archive.sha256.partial"
    [ "${manifest_scratch_owned:-false}" != true ] || rm -f "$archive.json.partial"
    [ "${replica_scratch_owned:-false}" != true ] || rm -f "$target.partial"
    [ "${replica_checksum_scratch_owned:-false}" != true ] || rm -f "$target.sha256.partial"
    scratch_owned=false; checksum_scratch_owned=false; manifest_scratch_owned=false
    replica_scratch_owned=false; replica_checksum_scratch_owned=false
}

publish() {
    [ ! -e "$2" ] && [ ! -L "$2" ] || return 1
    mv -n "$1" "$2" || return 1
    [ ! -e "$1" ] && [ ! -L "$1" ]
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
    owns_attempt=false; start_pending=false; terminal_pending=false; scratch_owned=false
    checksum_scratch_owned=false; manifest_scratch_owned=false
    replica_scratch_owned=false; replica_checksum_scratch_owned=false
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
    # Preflight cannot create history or archive files.
    capture fingerprint || return 1
    expected_state=$captured
    capture source_identity || return 1
    identity=$captured
    IFS='|' read -r source major timescale revision size <<EOF
$identity
EOF
    [ "$major" = 16 ] && [ "$timescale" = 2.26.4 ] || return 1
    [ -z "${BACKUP_EXPECTED_SOURCE_ID:-}" ] || [ "$source" = "$BACKUP_EXPECTED_SOURCE_ID" ] || return 1
    [ -z "${BACKUP_EXPECTED_SCHEMA_REVISION:-}" ] || [ "$revision" = "$BACKUP_EXPECTED_SCHEMA_REVISION" ] || return 1
    for output in "$partial" "$archive" "$archive.sha256" "$archive.sha256.partial" \
        "$archive.json" "$archive.json.partial" "$BACKUP_REPLICA_DIR/$archive_id.dump.enc" \
        "$BACKUP_REPLICA_DIR/$archive_id.dump.enc.partial" "$BACKUP_REPLICA_DIR/$archive_id.dump.enc.sha256" \
        "$BACKUP_REPLICA_DIR/$archive_id.dump.enc.sha256.partial"; do
        [ ! -e "$output" ] && [ ! -L "$output" ] || return 1
    done
    PGAPPNAME="backup:$attempt"; export PGAPPNAME
    start_pending=true
    if record_status start || reconcile_start; then
        owns_attempt=true; start_pending=false
    else
        return 1
    fi
    # Only invocation-created scratch is eligible for cleanup.
    (set -C; : > "$partial") || return 1
    scratch_owned=true
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
    publish "$partial" "$archive" || return 1
    scratch_owned=false
    (set -C; : > "$archive.sha256.partial") || return 1
    checksum_scratch_owned=true
    printf '%s  %s\n' "$checksum" "$archive_id.dump" > "$archive.sha256.partial"
    publish "$archive.sha256.partial" "$archive.sha256" || return 1
    checksum_scratch_owned=false
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
    (set -C; : > "$archive.json.partial") || return 1
    manifest_scratch_owned=true
    if ! sql -v archive="$archive_id" -v checksum="$checksum" -v source="$source" \
        -v revision="$revision" -v major="$major" -v timescale="$timescale" \
        -v size="$size" -v started="$started" -v finished="$finished" <<'SQL' > "$archive.json.partial"
SELECT jsonb_build_object('schema_version',1,'eligible',true,'archive_id',:'archive',
 'sha256',:'checksum','source_id',:'source','schema_revision',:'revision',
 'postgres_major',:major,'timescale_version',:'timescale','database_size_bytes',:size,
 'snapshot_at',:'started','completed_at',:'finished');
SQL
    then return 1; fi
    publish "$archive.json.partial" "$archive.json" || return 1
    manifest_scratch_owned=false
    status=succeeded
    terminal_pending=true
    # Replaying this exact event reconciles a lost commit acknowledgement. Never
    # generate a new timestamp or downgrade a possibly committed success.
    retention_deadline_epoch=$deadline_epoch
    deadline_epoch=""
    record_status finish || record_status finish || {
        printf '%s\n' backup_reporting_incomplete >&2; return 1;
    }
    terminal_pending=false
    # Retention shares the original absolute attempt budget, not a fresh budget
    # after reporting. Diagnostics alone get a separate bounded opportunity.
    deadline_epoch=$retention_deadline_epoch
    [ "$retention_mode" != preserve ] || return 0
    if ! retain; then
        reason=backup_retention_blocked
        deadline_epoch=""
        record_status diagnostic || record_status diagnostic || {
            printf '%s\n' backup_reporting_incomplete >&2; return 1;
        }
    fi
}

settle() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ -n "${child:-}" ]; then
        if kill -s TERM -- "-$child" 2>/dev/null; then
            sleep 2 || true
            kill -s KILL -- "-$child" 2>/dev/null || true
        fi
        wait "$child" || true
    fi
    # A failed terminal attempt has already been reported. Normal --once exit
    # must not relabel that failure as interruption.
    deadline_epoch=""
    if [ "${start_pending:-false}" = true ] && reconcile_start; then
        owns_attempt=true
    fi
    if [ "${owns_attempt:-false}" = true ] && [ "${status:-}" = running ] && settle_source; then
        status=interrupted
        reason=backup_attempt_failed
        deadline_epoch="" # interruption reporting has its own bounded opportunity
        if capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ; then
            finished=$captured
            record_status finish || record_status finish || true
        fi
    fi
    cleanup_scratch
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
    disposition=1
    # Nonblocking acquisition; policy and durable due state are read under the lock.
    if flock -n 9; then
        deadline_epoch=""
        if read_policy && capture due && [ "$captured" = t ]; then
            # The shell remains the lifecycle owner; timeout bounds/reaps each tool.
            if ! attempt_backup; then
                deadline_epoch=""
                if [ "${owns_attempt:-false}" = true ] && [ "${status:-}" = running ] && settle_source; then
                    status=failed
                    # Terminal reporting gets its own bounded opportunity.
                    reason=${reason:-backup_attempt_failed}
                    if capture bounded 5 date -u +%Y-%m-%dT%H:%M:%SZ; then
                        finished=$captured
                        record_status finish || record_status finish || true
                    fi
                fi
            else
                disposition=0
            fi
            cleanup_scratch
        fi
        flock -u 9
    fi
    if [ "$once" = true ]; then
        if [ "$retention_mode" = preserve ]; then
            [ "$disposition" = 0 ] || printf '%s\n' 'backup_reporting_incomplete' >&2
            exit "$disposition"
        fi
        break
    fi
    setsid sleep 60 &
    child=$!; wait "$child"; child=""
done
