#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
PRODUCTION_TEST_COMPOSE_FILE="$PROJECT_DIR/docker-compose.restore-production.test.yml"
PINNED_IMAGE=timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57

production_compose() {
    if [ -n "${RESTORE_PRODUCTION_TEST_PROJECT:-}" ]; then
        docker compose -p "$RESTORE_PRODUCTION_TEST_PROJECT" -f "$PRODUCTION_TEST_COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}

configure_test_resources() {
    RESTORE_VERIFY_TMPFS_BYTES=${RESTORE_VERIFY_TMPFS_BYTES:-2147483648}
    case $RESTORE_VERIFY_TMPFS_BYTES in
        ""|*[!0-9]*) echo "RESTORE_VERIFY_TMPFS_BYTES must be a positive byte count" >&2; exit 2 ;;
    esac
    [ "$RESTORE_VERIFY_TMPFS_BYTES" -ge 1073741824 ] || {
        echo "STATUS=verification_resource_failed REASON=tmpfs_capacity_guard" >&2; exit 2;
    }
    RESTORE_VERIFY_MEMORY_BYTES=$((RESTORE_VERIFY_TMPFS_BYTES + 2147483648))
    export RESTORE_VERIFY_TMPFS_BYTES RESTORE_VERIFY_MEMORY_BYTES
}

configure_production_test_mode() {
    if [ -z "${RESTORE_PRODUCTION_TEST_COMPOSE_FILE:-}${RESTORE_PRODUCTION_TEST_PROJECT:-}${RESTORE_PRODUCTION_TEST_BACKUP_DIR:-}" ]; then
        return
    fi
    [ -n "${RESTORE_PRODUCTION_TEST_COMPOSE_FILE:-}" ] || {
        echo "RESTORE_PRODUCTION_TEST_COMPOSE_FILE must name the canonical disposable compose file" >&2; exit 2;
    }
    production_test_compose_dir=$(CDPATH='' cd -- "$(dirname -- "$RESTORE_PRODUCTION_TEST_COMPOSE_FILE")" && pwd -P)
    [ "$production_test_compose_dir/$(basename -- "$RESTORE_PRODUCTION_TEST_COMPOSE_FILE")" = "$PRODUCTION_TEST_COMPOSE_FILE" ] || {
        echo "RESTORE_PRODUCTION_TEST_COMPOSE_FILE must name the canonical disposable compose file" >&2
        exit 2
    }
    case ${RESTORE_PRODUCTION_TEST_PROJECT:-} in
        restore-db-production-test-*) ;;
        *) echo "RESTORE_PRODUCTION_TEST_PROJECT must start with restore-db-production-test-" >&2; exit 2 ;;
    esac
    case ${POSTGRES_DB:-} in
        restore_production_test_*) ;;
        *) echo "POSTGRES_DB must start with restore_production_test_ in production test mode" >&2; exit 2 ;;
    esac
    case ${RESTORE_PRODUCTION_TEST_BACKUP_DIR:-} in
        ""|"$PROJECT_DIR/backups"|"$PROJECT_DIR/backups/"*)
            echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must be an external disposable directory" >&2; exit 2 ;;
    esac
    [ -d "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" ] || {
        echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must be an external disposable directory that exists" >&2
        exit 2
    }
    project_backups_dir="$PROJECT_DIR/backups"
    if [ -d "$project_backups_dir" ]; then
        project_backups_dir=$(CDPATH='' cd -- "$PROJECT_DIR/backups" && pwd -P)
    fi
    production_test_backup_dir=$(CDPATH='' cd -- "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" && pwd -P)
    case $production_test_backup_dir in
        "$project_backups_dir"|"$project_backups_dir"/*)
            echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must be an external disposable directory" >&2
            exit 2
            ;;
    esac
    RESTORE_PRODUCTION_TEST_BACKUP_DIR=$production_test_backup_dir
    RESTORE_DISPOSABLE=true
    RESTORE_ISOLATED_PROJECT=$RESTORE_PRODUCTION_TEST_PROJECT
    export RESTORE_PRODUCTION_TEST_BACKUP_DIR RESTORE_DISPOSABLE RESTORE_ISOLATED_PROJECT
    configure_test_resources
}

usage() {
    echo "Usage: $0 --production [--allow-live-restore] heatpump-YYYYMMDDTHHMMSSZ.dump | --test /path/to/archive.dump" >&2
    exit 2
}

database_name() {
    case $1 in
        ""|postgres|template0|template1|[0-9]*|*[!A-Za-z0-9_]*)
            echo "Refusing unsafe restore database name: $1" >&2; exit 2 ;;
    esac
    [ "${#1}" -le 63 ] || { echo "Restore database name must be at most 63 characters" >&2; exit 2; }
}

require_completed_archive() {
    archive=$1
    case $(basename -- "$archive") in
        heatpump-*.dump) ;;
        *) echo "Archive must be a named completed heatpump-*.dump file" >&2; exit 2 ;;
    esac
    [ ! -L "$archive" ] || { echo "Archive must not be a symbolic link: $archive" >&2; exit 2; }
    [ -f "$archive" ] || { echo "Archive does not exist: $archive" >&2; exit 2; }
}

restore_tool() {
    production_compose run --rm --no-deps -T \
        -e RESTORE_SOURCE_IMAGE -e RESTORE_SOURCE_TIMESCALE_VERSION -e RESTORE_EXPECTED_REVISION \
        -e RESTORE_REQUIRED_ROLES -e RESTORE_DISPOSABLE -e RESTORE_ISOLATED_PROJECT \
        -e RESTORE_ALLOW_LIVE_RESTORE restore-tool sh /scripts/restore_archive.sh "$@"
}

stop_production_writers() {
    for writer in api poller optimizer backup migrate; do
        production_compose stop "$writer" >/dev/null 2>&1 || true
    done
}

run_production_restore() {
    archive_name=$1
    RESTORE_DISPOSABLE=false
    RESTORE_ISOLATED_PROJECT=""
    configure_production_test_mode
    if [ "${RESTORE_DISPOSABLE:-false}" != "true" ] && [ "$allow_live_restore" != "true" ]; then
        echo "Real production restore requires --allow-live-restore" >&2; exit 2
    fi
    RESTORE_ALLOW_LIVE_RESTORE=$allow_live_restore
    export RESTORE_ALLOW_LIVE_RESTORE
    case $archive_name in
        */*)
            echo "Production archive name must not contain a path separator" >&2
            exit 2
            ;;
    esac
    live_database=${POSTGRES_DB-heatpump}
    database_name "$live_database"
    archive_dir=${RESTORE_PRODUCTION_TEST_BACKUP_DIR:-"$PROJECT_DIR/backups"}
    archive="$archive_dir/$archive_name"
    require_completed_archive "$archive"
    [ "$(dirname -- "$archive")" = "$archive_dir" ] || {
        echo "Production archives must be direct children of backups" >&2
        exit 2
    }

    # $RESTORE_ARCHIVE is intentionally expanded by the restore-tool container shell.
    # shellcheck disable=SC2016
    production_compose run --rm --no-deps -T -e RESTORE_ARCHIVE="$archive_name" \
        restore-tool sh -ceu 'pg_restore --list "/backups/$RESTORE_ARCHIVE" >/dev/null' </dev/null

    printf 'Type RESTORE %s to replace the local database: ' "$archive_name" >&2
    IFS= read -r confirmation || exit 1
    confirmation=${confirmation%"$(printf '\r')"}
    [ "$confirmation" = "RESTORE $archive_name" ] || {
        echo "Confirmation did not match; no database was changed" >&2
        exit 1
    }

    restore_lock="${TMPDIR:-/tmp}/heatpump-production-restore.lock"
    mkdir "$restore_lock" 2>/dev/null || { echo "Another restore is already running; refusing concurrent cutover" >&2; exit 1; }
    candidate_database="heatpump_restore_$(date -u +%Y%m%dT%H%M%S)_$$"
    old_database="heatpump_old_$(date -u +%Y%m%dT%H%M%S)_$$"
    candidate_started=false
    finish_production_restore() {
        production_status=$?
        trap - EXIT HUP INT TERM
        if [ "$production_status" -ne 0 ] && [ "$candidate_started" = "true" ]; then
            # $RESTORE_CANDIDATE is intentionally expanded by the restore-tool container shell.
            # shellcheck disable=SC2016
            production_compose run --rm --no-deps -T -e RESTORE_CANDIDATE="$candidate_database" \
                restore-tool sh -ceu 'dropdb --maintenance-db=postgres --if-exists --force "$RESTORE_CANDIDATE"' >/dev/null 2>&1 || true
        fi
        stop_production_writers
        rmdir "$restore_lock" || true
        exit "$production_status"
    }
    trap finish_production_restore EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    writer_services=$(production_compose ps --services --status running)
    running_writers=$(printf '%s\n' "$writer_services" | grep -E '^(api|poller|optimizer|backup|migrate)$' || true)
    [ -z "$running_writers" ] || {
        echo "Stop writers before restoring: $running_writers" >&2
        exit 1
    }

    configured_image=$(production_compose config --images db)
    [ "$configured_image" = "$PINNED_IMAGE" ] || {
        echo "Configured database image does not exactly match pinned restore image" >&2; exit 2;
    }
    production_compose up -d --wait db
    production_compose exec -T db pg_isready -U "${POSTGRES_USER:-heatpump}" -d "$live_database" >/dev/null
    container=$(production_compose ps -q db)
    actual_image=$(docker inspect --format '{{.Image}}' "$container")
    expected_image=$(docker image inspect --format '{{.Id}}' "$PINNED_IMAGE")
    [ "$actual_image" = "$expected_image" ] || { echo "Database image does not exactly match pinned restore image" >&2; exit 2; }
    restore_tool restore "/backups/$archive_name" "$candidate_database"
    candidate_started=true
    if [ "${RESTORE_DISPOSABLE:-false}" != "true" ]; then
        production_compose run --rm --no-deps -T -e RESTORE_CANDIDATE="$candidate_database" migrate \
            python -c 'import os, subprocess; from sqlalchemy.engine import make_url; os.environ["DATABASE_URL"] = make_url(os.environ["DATABASE_URL"]).set(database=os.environ["RESTORE_CANDIDATE"]).render_as_string(hide_password=False); subprocess.run(["alembic", "upgrade", "head"], check=True); subprocess.run(["alembic", "current"], check=True)'
        RESTORE_EXPECTED_REVISION=$(production_compose run --rm --no-deps -T migrate \
            python -c 'from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config("alembic.ini")).get_current_head())')
        export RESTORE_EXPECTED_REVISION
        restore_tool verify "$candidate_database"
    fi
    writer_services=$(production_compose ps --services --status running)
    running_writers=$(printf '%s\n' "$writer_services" | grep -E '^(api|poller|optimizer|backup|migrate)$' || true)
    [ -z "$running_writers" ] || { echo "Stop writers before cutover: $running_writers" >&2; exit 1; }
    restore_tool cutover "$live_database" "$candidate_database" "$old_database"
    candidate_started=false
    echo "Restore completed; application services remain stopped. Retained original: $old_database"
}

run_test_restore() {
    archive=$1
    require_completed_archive "$archive"
    test_project=${RESTORE_TEST_PROJECT:-restore-db-production-test-$$}
    case $test_project in
        restore-db-production-test-*) ;;
        *) echo "RESTORE_TEST_PROJECT must start with restore-db-production-test-" >&2; exit 2 ;;
    esac
    test_database=${RESTORE_TEST_DATABASE:-restore_production_test_$$}
    case $test_database in
        restore_production_test_*) ;;
        *) echo "RESTORE_TEST_DATABASE must start with restore_production_test_" >&2; exit 2 ;;
    esac
    database_name "$test_database"
    POSTGRES_DB=${POSTGRES_DB:-restore_production_test_maintenance}
    case $POSTGRES_DB in
        restore_production_test_*) ;;
        *) echo "POSTGRES_DB must start with restore_production_test_ in --test mode" >&2; exit 2 ;;
    esac
    database_name "$POSTGRES_DB"
    RESTORE_PRODUCTION_TEST_BACKUP_DIR=$(CDPATH='' cd -- "$(dirname -- "$archive")" && pwd -P)
    RESTORE_DISPOSABLE=true
    RESTORE_ISOLATED_PROJECT=$test_project
    export POSTGRES_DB RESTORE_PRODUCTION_TEST_BACKUP_DIR RESTORE_DISPOSABLE RESTORE_ISOLATED_PROJECT
    configure_test_resources
    test_compose() {
        docker compose -p "$test_project" -f "$PRODUCTION_TEST_COMPOSE_FILE" "$@"
    }
    finish_test_restore() {
        test_status=$?
        trap - EXIT HUP INT TERM
        if [ "${RESTORE_TEST_KEEP_STACK:-false}" != "true" ]; then
            test_compose down -v >/dev/null 2>&1 || true
        fi
        exit "$test_status"
    }
    trap finish_test_restore EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    test_compose up -d --wait db
    test_compose run --rm --no-deps -T \
        -e RESTORE_SOURCE_IMAGE -e RESTORE_SOURCE_TIMESCALE_VERSION -e RESTORE_EXPECTED_REVISION \
        -e RESTORE_REQUIRED_ROLES -e RESTORE_DISPOSABLE -e RESTORE_ISOLATED_PROJECT \
        restore-tool sh /scripts/restore_archive.sh restore "/backups/$(basename -- "$archive")" "$test_database"
    echo "Restored isolated test database: $test_database"
}

if ! { [ "$#" -ge 2 ] && [ "$#" -le 3 ]; }; then
    usage
fi
mode=$1
shift
allow_live_restore=false
if [ "$1" = "--allow-live-restore" ]; then
    allow_live_restore=true
    shift
fi
[ "$#" -eq 1 ] || usage
case $mode in
    --production) run_production_restore "$1" ;;
    --test)
        [ "$allow_live_restore" = "false" ] || { echo "--test rejects --allow-live-restore" >&2; exit 2; }
        run_test_restore "$1" ;;
    *) usage ;;
esac