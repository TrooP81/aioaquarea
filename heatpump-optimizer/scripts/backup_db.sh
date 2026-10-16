#!/bin/sh
# Create portable PostgreSQL custom-format backups on a bounded schedule.
set -eu

: "${PGHOST:=db}"
: "${PGUSER:=heatpump}"
: "${PGDATABASE:=heatpump}"
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_INTERVAL_SECONDS:=86400}"
: "${BACKUP_RETENTION_DAYS:=14}"
: "${BACKUP_VERIFY_AFTER_DUMP:=false}"
: "${BACKUP_REPLICA_ENABLED:=false}"
: "${BACKUP_REPLICA_DIR:=/replica}"
: "${BACKUP_REPLICA_REQUIRED:=false}"
: "${BACKUP_REPLICA_ENCRYPTION_KEY:=}"

mkdir -p "$BACKUP_DIR"

record_backup_status() {
  status="$1"
  # Status values are fixed literals from this script. Keep the heartbeat small
  # and avoid shell-escaping arbitrary filesystem paths into SQL.
  psql --command \
    "INSERT INTO service_heartbeats (service, updated_at, details_json) VALUES ('backup', NOW(), '{\"status\":\"$status\"}') ON CONFLICT (service) DO UPDATE SET updated_at = EXCLUDED.updated_at, details_json = EXCLUDED.details_json" \
    >/dev/null 2>&1 || echo "Could not record backup health" >&2
}

replicate_backup() {
  archive="$1"
  if [ "$BACKUP_REPLICA_ENABLED" != "true" ]; then
    return 0
  fi
  if [ -z "$BACKUP_REPLICA_ENCRYPTION_KEY" ]; then
    echo "Backup replica is enabled but BACKUP_REPLICA_ENCRYPTION_KEY is empty" >&2
    return 1
  fi
  mkdir -p "$BACKUP_REPLICA_DIR"
  target="$BACKUP_REPLICA_DIR/$(basename "$archive").enc"
  openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 \
    -pass env:BACKUP_REPLICA_ENCRYPTION_KEY \
    -in "$archive" -out "$target"
  sha256sum "$target" > "${target}.sha256"
  echo "Encrypted backup replica completed: $target"
}

while true; do
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  final_path="$BACKUP_DIR/heatpump-${timestamp}.dump"
  temporary_path="${final_path}.partial"

  echo "Creating database backup: $final_path"
  if pg_dump --format=custom --no-owner --file="$temporary_path"; then
    mv "$temporary_path" "$final_path"
    echo "Database backup completed: $final_path"
    if replicate_backup "$final_path"; then
      :
    else
      record_backup_status "replica_failed"
      if [ "$BACKUP_REPLICA_REQUIRED" = "true" ]; then
        echo "Required backup replica failed" >&2
        sleep "$BACKUP_INTERVAL_SECONDS"
        continue
      fi
    fi
    if [ "$BACKUP_VERIFY_AFTER_DUMP" = "true" ]; then
      /bin/sh /scripts/verify_backup.sh "$final_path"
    fi
    record_backup_status "ok"
  else
    rm -f "$temporary_path"
    echo "Database backup failed" >&2
    record_backup_status "failed"
  fi

  # Keep current backups and remove only completed archives outside retention.
  find "$BACKUP_DIR" -type f -name 'heatpump-*.dump' -mtime "+$BACKUP_RETENTION_DAYS" -delete
  sleep "$BACKUP_INTERVAL_SECONDS"
done
