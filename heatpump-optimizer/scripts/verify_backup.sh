#!/bin/sh
# Restore one archive into a disposable database and verify core tables exist.
set -eu

: "${PGHOST:=db}"
: "${PGUSER:=heatpump}"
: "${PGDATABASE:=heatpump}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_VERIFY_DATABASE:=heatpump_restore_verify}"

backup_path="${1:-}"
if [ -z "$backup_path" ]; then
  backup_path="$(find "$BACKUP_DIR" -type f -name 'heatpump-*.dump' -print | sort | tail -n 1)"
fi
if [ -z "$backup_path" ] || [ ! -f "$backup_path" ]; then
  echo "No completed backup archive found" >&2
  exit 1
fi

temporary_archive=""

cleanup() {
  dropdb --if-exists "$BACKUP_VERIFY_DATABASE" >/dev/null 2>&1 || true
  [ -z "$temporary_archive" ] || rm -f "$temporary_archive"
}
trap cleanup EXIT INT TERM

if [ "${backup_path##*.}" = "enc" ]; then
  : "${BACKUP_REPLICA_ENCRYPTION_KEY:?BACKUP_REPLICA_ENCRYPTION_KEY is required to verify an encrypted replica}"
  temporary_archive="$(mktemp /tmp/heatpump-backup-XXXXXX.dump)"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -pass env:BACKUP_REPLICA_ENCRYPTION_KEY \
    -in "$backup_path" -out "$temporary_archive"
  backup_path="$temporary_archive"
fi

echo "Validating backup archive: $backup_path"
pg_restore --list "$backup_path" >/dev/null
dropdb --if-exists "$BACKUP_VERIFY_DATABASE"
createdb "$BACKUP_VERIFY_DATABASE"
pg_restore --no-owner --dbname="$BACKUP_VERIFY_DATABASE" "$backup_path"
psql --dbname="$BACKUP_VERIFY_DATABASE" --tuples-only --no-align \
  --command="SELECT CASE WHEN to_regclass('public.plans') IS NOT NULL AND to_regclass('public.device_status') IS NOT NULL THEN 'ok' ELSE 'missing_core_tables' END" \
  | grep -qx "ok"
echo "Backup restore verification passed"
