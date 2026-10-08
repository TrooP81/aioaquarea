#!/bin/sh
# Restore one archive into a disposable database and verify core tables exist.
set -eu

: "${PGHOST:=db}"
: "${PGUSER:=heatpump}"
: "${PGDATABASE:=heatpump}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_VERIFY_DATABASE:=heatpump_restore_verify}"
: "${BACKUP_LIVE_DATABASE:=heatpump}"
: "${BACKUP_VERIFY_TIMEOUT_SECONDS:=3600}"

if [ "${BACKUP_VERIFY_ISOLATED:-false}" != "true" ] ||
  [ "$PGHOST" = "db" ] ||
  [ "$PGDATABASE" = "$BACKUP_LIVE_DATABASE" ] ||
  [ "$BACKUP_VERIFY_DATABASE" = "$BACKUP_LIVE_DATABASE" ]; then
  echo "Backup verification requires an isolated target; live database verification is forbidden" >&2
  exit 2
fi

backup_path="${1:-}"
if [ -z "$backup_path" ]; then
  backup_path="$(find "$BACKUP_DIR" -type f -name 'heatpump-*.dump' -print | sort | tail -n 1)"
fi
if [ -z "$backup_path" ] || [ ! -f "$backup_path" ]; then
  echo "No completed backup archive found" >&2
  exit 1
fi

temporary_archive=""
verification_log=$(mktemp)
candidate_verified=false
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

cleanup() {
  if [ "$candidate_verified" = "true" ]; then
    dropdb --maintenance-db=postgres --if-exists --force "$BACKUP_VERIFY_DATABASE" >/dev/null 2>&1 || true
  fi
  [ -z "$temporary_archive" ] || rm -f "$temporary_archive"
  rm -f "$verification_log"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${backup_path##*.}" = "enc" ]; then
  : "${BACKUP_REPLICA_ENCRYPTION_KEY:?BACKUP_REPLICA_ENCRYPTION_KEY is required to verify an encrypted replica}"
  temporary_archive="$(mktemp /tmp/heatpump-backup-XXXXXX.dump)"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -pass env:BACKUP_REPLICA_ENCRYPTION_KEY \
    -in "$backup_path" -out "$temporary_archive"
  backup_path="$temporary_archive"
fi

echo "Validating backup archive: $backup_path"
export PGHOST PGUSER PGDATABASE
if RESTORE_DISPOSABLE=true timeout "$BACKUP_VERIFY_TIMEOUT_SECONDS" \
  sh "$SCRIPT_DIR/restore_archive.sh" restore "$backup_path" "$BACKUP_VERIFY_DATABASE" \
  > "$verification_log" 2>&1; then
  candidate_verified=true
  cat "$verification_log"
else
  verification_exit=$?
  status=verification_failed
  reason=restore_or_content
  case $verification_exit in
    137) status=verification_resource_failed; reason=container_oom ;;
    124|143) status=verification_resource_failed; reason=timeout ;;
    *)
      if grep -Eqi 'No space left on device|disk full' "$verification_log"; then
        status=verification_resource_failed
        reason=tmpfs_exhausted
      elif grep -Eqi 'out of memory|could not allocate memory|cannot allocate memory' "$verification_log"; then
        status=verification_resource_failed
        reason=postgres_memory
      fi
      ;;
  esac
  cat "$verification_log" >&2
  echo "STATUS=$status REASON=$reason" >&2
  exit 1
fi
echo "STATUS=verified"
echo "Backup restore verification passed"
