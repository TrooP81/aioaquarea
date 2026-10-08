#!/bin/sh
set -eu

PINNED_IMAGE=timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57
TIMESCALE_VERSION=2.26.4

fail() {
    echo "$*" >&2
    exit 2
}

identifier() {
    case $2 in
        ""|[0-9]*|*[!A-Za-z0-9_]*) fail "Refusing unsafe $1 name: $2" ;;
    esac
    [ "${#2}" -le 63 ] || fail "$1 name exceeds PostgreSQL's identifier limit"
}

database_identifier() {
    identifier database "$1"
    case $1 in postgres|template0|template1) fail "Refusing unsafe restore database name: $1" ;; esac
}

sql() {
    psql -X --set=ON_ERROR_STOP=1 --tuples-only --no-align --dbname="$1" --command="$2"
}

expect_true() {
    result=$(sql "$1" "$2")
    [ "$result" = "t" ] || fail "$3"
}

target_policy() {
    : "${PGHOST:?PGHOST is required}"
    : "${PGUSER:?PGUSER is required}"
    if [ "${RESTORE_DISPOSABLE:-false}" = "true" ]; then
        case ${RESTORE_ISOLATED_PROJECT:-} in
            restore-db-production-test-*) ;;
            *) fail "Disposable restores require an isolated restore-db-production-test- project" ;;
        esac
        [ "$PGHOST" = "restore-target" ] || fail "Disposable restore host must be restore-target"
        case ${PGDATABASE:-} in
            restore_production_test_*) ;;
            *) fail "Disposable maintenance database must start with restore_production_test_" ;;
        esac
    else
        [ "${RESTORE_ALLOW_LIVE_RESTORE:-false}" = "true" ] || fail "Live restore authorization is required"
    fi
    [ "${RESTORE_SOURCE_IMAGE:-}" = "$PINNED_IMAGE" ] || fail "Source image must exactly match the pinned restore image"
    [ "${RESTORE_SOURCE_TIMESCALE_VERSION:-}" = "$TIMESCALE_VERSION" ] || fail "Source TimescaleDB version must be exactly $TIMESCALE_VERSION; no override is supported"
}

server_compatibility() {
    expect_true postgres "SELECT current_setting('server_version_num')::int / 10000 = 16" \
        "PostgreSQL server major must be 16"
    expect_true postgres "SELECT EXISTS (SELECT 1 FROM pg_available_extension_versions WHERE name = 'timescaledb' AND version = '$TIMESCALE_VERSION')" \
        "Pinned TimescaleDB version is unavailable"
}

verify_database() {
    verify_database_name=$1
    database_identifier "$verify_database_name"
    case ${RESTORE_EXPECTED_REVISION:-} in
        ""|*[!0-9]*) fail "A trusted numeric RESTORE_EXPECTED_REVISION is required for legacy archives" ;;
    esac
    expect_true "$verify_database_name" "SELECT extversion = '$TIMESCALE_VERSION' FROM pg_extension WHERE extname = 'timescaledb'" \
        "Restored TimescaleDB version does not exactly match"
    expect_true "$verify_database_name" "SELECT version_num = '$RESTORE_EXPECTED_REVISION' FROM alembic_version" \
        "Restored Alembic revision does not match the trusted source revision"
    expect_true "$verify_database_name" "SELECT count(*) = 4 FROM timescaledb_information.hypertables WHERE hypertable_schema = 'public' AND hypertable_name IN ('device_status', 'consumption', 'prices', 'weather')" \
        "Restored database is missing required hypertables"
    expect_true "$verify_database_name" "SELECT count(DISTINCT constraints.conrelid) = 4 FROM pg_constraint constraints JOIN pg_class tables ON tables.oid = constraints.conrelid JOIN pg_namespace namespaces ON namespaces.oid = tables.relnamespace WHERE namespaces.nspname = 'public' AND tables.relname IN ('device_status', 'consumption', 'prices', 'weather') AND constraints.contype = 'p' AND constraints.convalidated" \
        "Restored hypertable parent primary keys are missing or unvalidated"
    expect_true "$verify_database_name" "SELECT to_regclass('public.plans') IS NOT NULL" \
        "Restored database is missing core tables"
    expect_true "$verify_database_name" "SELECT EXISTS (SELECT 1 FROM timescaledb_information.jobs WHERE scheduled) AND NOT EXISTS (SELECT 1 FROM timescaledb_information.jobs jobs LEFT JOIN pg_roles roles ON roles.rolname = jobs.owner::text WHERE roles.oid IS NULL)" \
        "Restored jobs are missing or have unresolved owner roles"
    expect_true "$verify_database_name" "SELECT current_setting('timescaledb.restoring') = 'off'" \
        "TimescaleDB post-restore state is not clear"
    if [ "${RESTORE_DISPOSABLE:-false}" != "true" ]; then
        expect_true "$verify_database_name" "SELECT EXISTS (SELECT 1 FROM settings WHERE key = 'aquarea_username' AND value <> '') AND EXISTS (SELECT 1 FROM settings WHERE key = 'aquarea_password' AND value <> '')" \
            "Restored database has no configured Panasonic credentials; services will remain stopped."
    fi
    sql "$verify_database_name" "SELECT hypertable_name, count(chunks.chunk_name) FROM timescaledb_information.hypertables hypertables LEFT JOIN timescaledb_information.chunks chunks USING (hypertable_schema, hypertable_name) WHERE hypertables.hypertable_schema = 'public' GROUP BY hypertable_name ORDER BY hypertable_name"
    sql "$verify_database_name" "SELECT 'device_status', count(*) FROM device_status UNION ALL SELECT 'consumption', count(*) FROM consumption UNION ALL SELECT 'prices', count(*) FROM prices UNION ALL SELECT 'weather', count(*) FROM weather UNION ALL SELECT 'plans', count(*) FROM plans"
}

restore_archive() {
    archive=$1
    restore_database=$2
    database_identifier "$restore_database"
    case ${RESTORE_EXPECTED_REVISION:-} in
        ""|*[!0-9]*) fail "A trusted numeric RESTORE_EXPECTED_REVISION is required for legacy archives" ;;
    esac
    [ -f "$archive" ] && [ ! -L "$archive" ] || fail "Archive must be a regular, non-symlink file"
    work_dir=$(mktemp -d)
    candidate_created=false
    pre_restore_succeeded=false
    created_roles=""

    finish_restore() {
        restore_status=$?
        trap - EXIT HUP INT TERM
        if [ "$pre_restore_succeeded" = "true" ]; then
            if ! sql "$restore_database" "SELECT timescaledb_post_restore()" >/dev/null; then
                echo "TimescaleDB post-restore cleanup failed" >&2
                [ "$restore_status" -ne 0 ] || restore_status=1
            fi
        fi
        if [ "$restore_status" -ne 0 ] && [ "$candidate_created" = "true" ]; then
            dropdb --maintenance-db=postgres --if-exists --force "$restore_database" || true
        fi
        if [ "$restore_status" -ne 0 ]; then
            for created_role in $created_roles; do
                sql postgres "DROP ROLE \"$created_role\"" >/dev/null 2>&1 || true
            done
        fi
        rm -rf "$work_dir"
        exit "$restore_status"
    }
    trap finish_restore EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    pg_restore --list "$archive" > "$work_dir/toc"
    source_major=$(awk '/^;[[:space:]]*Dumped from database version:/ { split($NF, version, "."); print version[1] }' "$work_dir/toc")
    [ "$source_major" = "16" ] || fail "Archive PostgreSQL major must be 16"
    server_compatibility
    expect_true postgres "SELECT NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$restore_database')" \
        "Candidate database already exists; it will not be replaced"

    pg_restore --data-only --table=bgw_job --file="$work_dir/jobs.sql" "$archive"
    awk '/^[0-9]+; / && $NF != "-" { print $NF }' "$work_dir/toc" > "$work_dir/roles"
    awk '
        /^COPY _timescaledb_config.bgw_job / {
            columns = $0
            sub(/^[^(]*\(/, "", columns)
            sub(/\).*/, "", columns)
            count = split(columns, names, ", ")
            for (column = 1; column <= count; column++) if (names[column] == "owner") owner_column = column
            in_jobs = 1
            next
        }
        in_jobs && /^\\\.$/ { in_jobs = 0 }
        in_jobs && owner_column { split($0, values, "\t"); print values[owner_column] }
    ' "$work_dir/jobs.sql" >> "$work_dir/roles"
    for required_role in ${RESTORE_REQUIRED_ROLES:-}; do
        printf '%s\n' "$required_role" >> "$work_dir/roles"
    done
    sort -u "$work_dir/roles" > "$work_dir/required_roles"
    while IFS= read -r required_role; do
        identifier role "$required_role"
        role_exists=$(sql postgres "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$required_role')")
        if [ "$role_exists" != "t" ]; then
            [ "${RESTORE_DISPOSABLE:-false}" = "true" ] || fail "Required source/job role is absent: $required_role; production never creates roles"
            sql postgres "CREATE ROLE \"$required_role\" NOLOGIN" >/dev/null
            created_roles="$created_roles $required_role"
        fi
    done < "$work_dir/required_roles"

    createdb --maintenance-db=postgres "$restore_database"
    candidate_created=true
    sql "$restore_database" "CREATE EXTENSION IF NOT EXISTS timescaledb VERSION '$TIMESCALE_VERSION'" >/dev/null
    sql "$restore_database" "SELECT timescaledb_pre_restore()" >/dev/null
    pre_restore_succeeded=true
    pg_restore --exit-on-error --no-owner --no-privileges --dbname="$restore_database" "$archive"
    sql "$restore_database" "SELECT timescaledb_post_restore()" >/dev/null
    pre_restore_succeeded=false
    verify_database "$restore_database"
    echo "Candidate restore verified: $restore_database"
}

worker_gate() {
    expect_true "$1" "SELECT extversion = '$TIMESCALE_VERSION' FROM pg_extension WHERE extname = 'timescaledb'" \
        "Cutover requires exactly TimescaleDB $TIMESCALE_VERSION"
    expect_true "$1" "SELECT to_regprocedure('_timescaledb_functions.stop_background_workers()') IS NOT NULL AND to_regprocedure('_timescaledb_functions.start_background_workers()') IS NOT NULL" \
        "Required TimescaleDB $TIMESCALE_VERSION worker-control functions are absent"
}

suspend_workers() {
    sql postgres "ALTER DATABASE \"$1\" SET timescaledb.restoring = 'on'" >/dev/null
    sql "$1" "SELECT _timescaledb_functions.stop_background_workers()" >/dev/null
    expect_true "$1" "SELECT current_setting('timescaledb.restoring') = 'on'" \
        "Database-scoped restoring state did not survive reconnect"
}

resume_workers() {
    sql postgres "ALTER DATABASE \"$1\" RESET timescaledb.restoring" >/dev/null
    expect_true "$1" "SELECT current_setting('timescaledb.restoring') = 'off'" \
        "New live database remains in restoring mode"
    sql "$1" "SELECT _timescaledb_functions.start_background_workers()" >/dev/null
}

swap_databases() {
    live_database=$1
    replacement_database=$2
    retained_database=$3
    for swap_database in "$live_database" "$replacement_database" "$retained_database"; do
        database_identifier "$swap_database"
    done
    [ "$live_database" != "$replacement_database" ] &&
        [ "$live_database" != "$retained_database" ] &&
        [ "$replacement_database" != "$retained_database" ] || fail "Cutover database names must be distinct"
    server_compatibility
    worker_gate "$live_database"
    worker_gate "$replacement_database"
    expect_true postgres "SELECT NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$retained_database')" \
        "Retained database name already exists"
    live_renamed=false
    replacement_renamed=false
    finish_swap() {
        swap_status=$?
        trap - EXIT HUP INT TERM
        if [ "$swap_status" -ne 0 ] && [ "$live_renamed" = "true" ] && [ "$replacement_renamed" = "false" ]; then
            sql postgres "ALTER DATABASE \"$retained_database\" RENAME TO \"$live_database\"" >/dev/null || {
                echo "Partial rename rollback failed; operator rename rollback is required" >&2
            }
        fi
        [ "$swap_status" -eq 0 ] || echo "Cutover failed; writers remain stopped. Follow the rename rollback runbook." >&2
        exit "$swap_status"
    }
    trap finish_swap EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    suspend_workers "$live_database"
    suspend_workers "$replacement_database"
    sql postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$live_database', '$replacement_database') AND pid <> pg_backend_pid()" >/dev/null
    cutover_time=$(sql postgres "SELECT clock_timestamp()")
    sql postgres "ALTER DATABASE \"$live_database\" RENAME TO \"$retained_database\"" >/dev/null
    live_renamed=true
    sql postgres "ALTER DATABASE \"$replacement_database\" RENAME TO \"$live_database\"" >/dev/null
    replacement_renamed=true
    resume_workers "$live_database"
    expect_true "$retained_database" "SELECT current_setting('timescaledb.restoring') = 'on'" \
        "Retained old database is not in restoring mode"
    echo "CUTOVER_TIME=$cutover_time"
    echo "RETAINED_DATABASE=$retained_database"
    echo "Writers remain stopped; BK-7A/BK-7B checks are required before restarting them."
}

[ "$#" -ge 2 ] || fail "Usage: restore_archive.sh restore ARCHIVE CANDIDATE | verify DATABASE | cutover LIVE CANDIDATE OLD | rollback LIVE OLD FAILED"
target_policy
case $1 in
    restore) [ "$#" -eq 3 ] || fail "restore requires archive and candidate"; restore_archive "$2" "$3" ;;
    verify) [ "$#" -eq 2 ] || fail "verify requires database"; server_compatibility; verify_database "$2" ;;
    cutover|rollback) [ "$#" -eq 4 ] || fail "cutover/rollback requires three database names"; swap_databases "$2" "$3" "$4" ;;
    *) fail "Unknown restore operation" ;;
esac