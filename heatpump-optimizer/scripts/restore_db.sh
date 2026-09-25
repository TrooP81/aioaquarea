#!/bin/sh
# Restore a completed custom-format archive. Production requires an interactive
# confirmation; --test is intentionally limited to docker-compose.test.yml.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
TEST_COMPOSE_FILE="$PROJECT_DIR/docker-compose.test.yml"
PRODUCTION_TEST_COMPOSE_FILE="$PROJECT_DIR/docker-compose.restore-production.test.yml"

production_compose() {
    if [ -n "${RESTORE_PRODUCTION_TEST_PROJECT:-}" ]; then
        docker compose -p "$RESTORE_PRODUCTION_TEST_PROJECT" -f "$RESTORE_PRODUCTION_TEST_COMPOSE_FILE" "$@"
    else
        docker compose -f "$COMPOSE_FILE" "$@"
    fi
}

configure_production_test_mode() {
    [ -z "${RESTORE_PRODUCTION_TEST_COMPOSE_FILE:-}" ] && return
    production_test_compose_dir=$(CDPATH= cd -- "$(dirname -- "$RESTORE_PRODUCTION_TEST_COMPOSE_FILE")" && pwd)
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
    [ -n "${RESTORE_PRODUCTION_TEST_BACKUP_DIR:-}" ] || {
        echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must be an external disposable directory" >&2
        exit 2
    }
    [ -d "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" ] || {
        echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must exist" >&2
        exit 2
    }
    project_backups_dir=$(CDPATH= cd -- "$PROJECT_DIR/backups" && pwd -P)
    production_test_backup_dir=$(CDPATH= cd -- "$RESTORE_PRODUCTION_TEST_BACKUP_DIR" && pwd -P)
    case $production_test_backup_dir in
        "$project_backups_dir"|"$project_backups_dir"/*)
            echo "RESTORE_PRODUCTION_TEST_BACKUP_DIR must be an external disposable directory" >&2
            exit 2
            ;;
    esac
    RESTORE_PRODUCTION_TEST_BACKUP_DIR=$production_test_backup_dir
    export RESTORE_PRODUCTION_TEST_BACKUP_DIR
}

usage() {
    echo "Usage: $0 --production heatpump-YYYYMMDDTHHMMSSZ.dump | --test /path/to/archive.dump" >&2
    exit 2
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

stop_production_services() {
    production_compose stop >/dev/null 2>&1 || true
}

run_production_restore() {
    archive_name=$1
    configure_production_test_mode
    case $archive_name in
        */*)
            echo "Production archive name must not contain a path separator" >&2
            exit 2
            ;;
    esac
    archive_dir=${RESTORE_PRODUCTION_TEST_BACKUP_DIR:-"$PROJECT_DIR/backups"}
    archive="$archive_dir/$archive_name"
    require_completed_archive "$archive"
    [ "$(dirname -- "$archive")" = "$archive_dir" ] || {
        echo "Production archives must be direct children of backups" >&2
        exit 2
    }

    production_compose run --rm --no-deps -T -e RESTORE_ARCHIVE="$archive_name" \
        backup-verify sh -ceu 'pg_restore --list "/backups/$RESTORE_ARCHIVE" >/dev/null' </dev/null

    printf 'Type RESTORE %s to replace the local database: ' "$archive_name" >&2
    IFS= read -r confirmation || exit 1
    confirmation=${confirmation%"$(printf '\r')"}
    [ "$confirmation" = "RESTORE $archive_name" ] || {
        echo "Confirmation did not match; no database was changed" >&2
        exit 1
    }

    # A failed restore must never leave background writers running.
    trap 'stop_production_services' EXIT HUP INT TERM
    running_writers=$(production_compose ps --services --status running 2>/dev/null \
        | grep -E '^(api|poller|optimizer|backup|migrate)$' || true)
    [ -z "$running_writers" ] || {
        echo "Stop writers before restoring: $running_writers" >&2
        exit 1
    }

    production_compose up -d --wait db
    readiness_attempt=1
    while ! production_compose exec -T db pg_isready -U "${POSTGRES_USER:-heatpump}" \
        -d "${POSTGRES_DB:-heatpump}" >/dev/null 2>&1; do
        [ "$readiness_attempt" -lt 20 ] || {
            echo "Timed out waiting for database readiness before restore" >&2
            exit 1
        }
        readiness_attempt=$((readiness_attempt + 1))
        sleep 1
    done
    production_compose run --rm --no-deps -e RESTORE_ARCHIVE="$archive_name" backup \
        sh -ceu '
            case ${PGDATABASE:-} in
                ""|postgres|template0|template1)
                    echo "Refusing unsafe restore database name: ${PGDATABASE:-<empty>}" >&2
                    exit 2
                    ;;
                [0-9]*|*[!A-Za-z0-9_]*)
                    echo "Refusing unsafe restore database name: $PGDATABASE" >&2
                    exit 2
                    ;;
                *) ;;
            esac
            psql -v ON_ERROR_STOP=1 -d postgres \
                -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '"'"'$PGDATABASE'"'"' AND pid <> pg_backend_pid()" >/dev/null
            dropdb --if-exists "$PGDATABASE"
            createdb "$PGDATABASE"
            pg_restore --no-owner --dbname="$PGDATABASE" "/backups/$RESTORE_ARCHIVE"
            psql -v ON_ERROR_STOP=1 --tuples-only --no-align --dbname="$PGDATABASE" \
                --command="SELECT version_num FROM alembic_version" | grep -Eq "^[0-9]+$"
            psql -v ON_ERROR_STOP=1 --tuples-only --no-align --dbname="$PGDATABASE" \
                --command="SELECT to_regclass('"'"'public.device_status'"'"') IS NOT NULL AND to_regclass('"'"'public.plans'"'"') IS NOT NULL" | grep -qx "t"
            psql -v ON_ERROR_STOP=1 --tuples-only --no-align --dbname="$PGDATABASE" --command="SELECT EXISTS (SELECT 1 FROM settings WHERE key='"'"'aquarea_username'"'"' AND value <> '"'"''"'"') AND EXISTS (SELECT 1 FROM settings WHERE key='"'"'aquarea_password'"'"' AND value <> '"'"''"'"')" | grep -qx "t" || {
                echo "Restored database has no configured Panasonic credentials; services will remain stopped." >&2
                exit 1
            }
        '
    production_compose run --rm --no-deps migrate alembic current
    trap - EXIT HUP INT TERM
    echo "Restore completed; application services remain stopped."
}

run_test_restore() {
    archive=$1
    require_completed_archive "$archive"
    test_project=${RESTORE_TEST_PROJECT:-restore-db-test-$$}
    case $test_project in
        restore-db-test-*) ;;
        *) echo "RESTORE_TEST_PROJECT must start with restore-db-test-" >&2; exit 2 ;;
    esac
    test_database=${RESTORE_TEST_DATABASE:-restore_test_$$}
    case $test_database in
        restore_test_*) ;;
        *) echo "RESTORE_TEST_DATABASE must start with restore_test_" >&2; exit 2 ;;
    esac

    if [ "${RESTORE_TEST_KEEP_STACK:-false}" = "true" ]; then
        trap ':' EXIT HUP INT TERM
    else
        trap 'docker compose -p "$test_project" -f "$TEST_COMPOSE_FILE" down -v >/dev/null 2>&1 || true' EXIT HUP INT TERM
    fi
    docker compose -p "$test_project" -f "$TEST_COMPOSE_FILE" up -d --wait test-db
    readiness_attempt=1
    while ! docker compose -p "$test_project" -f "$TEST_COMPOSE_FILE" exec -T \
        -e PGPASSWORD=heatpump_test test-db pg_isready -U heatpump -d heatpump_test >/dev/null 2>&1; do
        [ "$readiness_attempt" -lt 20 ] || {
            echo "Timed out waiting for isolated test database readiness" >&2
            exit 1
        }
        readiness_attempt=$((readiness_attempt + 1))
        sleep 1
    done
    container=$(docker compose -p "$test_project" -f "$TEST_COMPOSE_FILE" ps -q test-db)
    [ -n "$container" ] || { echo "Test database container was not created" >&2; exit 1; }
    docker cp "$archive" "$container:/tmp/restore.dump"
    docker compose -p "$test_project" -f "$TEST_COMPOSE_FILE" exec -T \
        -e RESTORE_TEST_DATABASE="$test_database" -e PGPASSWORD=heatpump_test test-db sh -ceu '
            pg_restore -U heatpump --list /tmp/restore.dump >/dev/null
            dropdb -U heatpump --maintenance-db=postgres --if-exists "$RESTORE_TEST_DATABASE"
            createdb -U heatpump --maintenance-db=postgres "$RESTORE_TEST_DATABASE"
            pg_restore -U heatpump --no-owner --dbname="$RESTORE_TEST_DATABASE" /tmp/restore.dump
            psql -U heatpump --dbname="$RESTORE_TEST_DATABASE" --tuples-only --no-align \
                --command="SELECT version_num FROM alembic_version" | grep -Eq "^[0-9]+$"
        '
    echo "Restored isolated test database: $test_database"
}

[ "$#" -eq 2 ] || usage
case $1 in
    --production) run_production_restore "$2" ;;
    --test) run_test_restore "$2" ;;
    *) usage ;;
esac