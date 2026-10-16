#!/bin/sh
# Execute only in the inspected source worker. Private copy survives later retention.
set -eu
umask 077
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_STAGE_TOKEN:?unpredictable coordinator lease token required}"
case $BACKUP_STAGE_TOKEN in ""|*[!0-9a-f]*) exit 2 ;; esac
[ "${#BACKUP_STAGE_TOKEN}" -eq 32 ] || exit 2
[ -d "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR" ] && [ ! -L "$BACKUP_DIR/.backup.lock" ] || exit 2
stage="$BACKUP_DIR/.stage-$BACKUP_STAGE_TOKEN"
mode=${1:-standalone}
case $mode in
    reserve) exec timeout -k 2 5 sh "$0" reserve-inner ;;
    reserve-inner)
        mkdir "$stage" # exclusive: never adopt a directory
        printf '%s\n' "$BACKUP_STAGE_TOKEN" > "$stage/owner"
        printf '%s\n' reserved > "$stage/state"
        : > "$stage/control.lock"
        exit ;;
    cleanup) exec timeout -k 2 55 sh "$0" cleanup-inner ;;
    run|standalone|cleanup-inner)
        if [ "$mode" = standalone ] && [ ! -e "$stage" ]; then
            sh "$0" reserve
        fi
        [ -d "$stage" ] && [ ! -L "$stage" ] || exit 2
        for file in owner state control.lock; do
            [ -f "$stage/$file" ] && [ ! -L "$stage/$file" ] || exit 2
        done
        exec 8<>"$stage/control.lock"
        if [ "$mode" != cleanup-inner ]; then flock -n 8; else flock -w 50 8; fi
        [ "$(cat "$stage/owner")" = "$BACKUP_STAGE_TOKEN" ] || exit 2
        case $(cat "$stage/state") in reserved|running|settled) ;; *) exit 2 ;; esac
        if [ "$mode" = cleanup-inner ]; then
            # Every copy descendant inherits fd 8. Acquiring this lock proves
            # the supervisor AND its bounded copy process tree have settled.
            for file in archive.dump archive.json owner state control.lock; do
                [ ! -L "$stage/$file" ] || exit 2
            done
            rm -f "$stage/archive.dump" "$stage/archive.json" \
                "$stage/owner" "$stage/state" "$stage/control.lock"
            rmdir "$stage" # unexpected contents are unresolved, never recurse
            printf '%s\n' settled
            exit
        fi
        [ "$(cat "$stage/state")" = reserved ] || exit 2
        seconds=${BACKUP_STAGE_TIMEOUT_SECONDS:-45}
        case $seconds in ""|*[!0-9]*) exit 2 ;; esac
        [ "$seconds" -ge 1 ] && [ "$seconds" -le 45 ] || exit 2
        printf '%s\n' running > "$stage/state"
        setsid sh "$0" bounded-copy "$seconds" <&0 &
        copy_pid=$!
        if wait "$copy_pid"; then result=0; else result=$?; fi
        kill -s KILL -- "-$copy_pid" 2>/dev/null || true
        printf '%s\n' settled > "$stage/state"
        exit "$result" ;;
    bounded-copy)
        # BusyBox timeout can kill only its monitored PID. An independent
        # group watchdog also settles descendants if this exec's caller dies.
        # fd 8 stays inherited by the watchdog until the entire group is dead.
        group=$$
        trap '' TERM HUP INT
        (sleep "$(($2+2))"; kill -s KILL -- "-$group") &
        exec timeout -k 2 "$2" sh "$0" copy ;;
    copy)
        # Keep the timeout's monitored shell alive through TERM so kill-after
        # also kills TERM-resistant descendants, not just the local CLI.
        trap '' TERM ;;
    *) exit 2 ;;
esac
exec 9>"$BACKUP_DIR/.backup.lock"
flock -w 30 9
# Selection itself, validation and copying all happen while retention is excluded.
selection=$(psql -XqAt -v ON_ERROR_STOP=1 <<'SQL'
SELECT (details_json::jsonb->'last_success'->>'archive_id') || '|' ||
 (details_json::jsonb->'last_success'->>'sha256') || '|' ||
 (details_json::jsonb->'last_success'->>'source_id')
FROM service_heartbeats WHERE service='backup' AND details_json::jsonb->>'schema_version'='1';
SQL
)
IFS='|' read -r archive_id checksum source <<EOF
$selection
EOF
case $archive_id in heatpump-*) ;; *) exit 2 ;; esac
token=${archive_id#heatpump-}
case $token in ""|*[!0-9a-f]*) exit 2 ;; esac
[ "${#token}" -eq 32 ] || exit 2
archive="$BACKUP_DIR/$archive_id.dump"
[ -f "$archive" ] && [ ! -L "$archive" ] && [ -f "$archive.json" ] && [ ! -L "$archive.json" ] || exit 2
valid=$(psql -XqAt -v ON_ERROR_STOP=1 -v manifest="$(cat "$archive.json")" \
    -v archive="$archive_id" -v checksum="$checksum" -v source="$source" <<'SQL'
SELECT :'manifest'::jsonb @> jsonb_build_object('schema_version',1,'eligible',true,
 'archive_id',:'archive','sha256',:'checksum','source_id',:'source');
SQL
)
[ "$valid" = t ] || exit 2
actual=$(sha256sum "$archive"); actual=${actual%% *}
[ "$actual" = "$checksum" ] || exit 2
# Do not adopt or erase a previous stage, even after an interrupted run.
cp "$archive" "$stage/archive.dump"
cp "$archive.json" "$stage/archive.json"
staged=$(sha256sum "$stage/archive.dump"); staged=${staged%% *}
[ "$staged" = "$checksum" ] || exit 2
cmp "$archive.json" "$stage/archive.json"
printf '%s\n' "$stage"
# Closing fd 9 releases the destination lock only after the copy is verified.
