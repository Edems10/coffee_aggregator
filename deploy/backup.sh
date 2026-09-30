#!/usr/bin/env bash
# Dump the database, verify the dump, upload it, and only then prune old ones.
#
# The dump runs inside the db container, so pg_dump is always the same major
# version as the server and the host needs no postgresql-client at all.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/coffee-aggregator/deploy/compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/coffee-aggregator}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
# Dumps to keep on this machine. The remote always holds exactly one.
KEEP="${KEEP:-1}"
REMOTE_NAME="${REMOTE_NAME:-coffee-latest.dump}"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="coffee-${stamp}.dump"
mkdir -p "$BACKUP_DIR"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Write to .part first: a dump interrupted half-way must not be mistaken for a
# good one by the pruning below, which keeps the newest files by name.
compose exec -T db pg_dump \
    --format=custom --compress=9 --no-owner --no-privileges \
    -U coffee -d coffee > "${BACKUP_DIR}/${name}.part"

# A truncated custom-format dump still looks like a file. pg_restore --list
# reads the whole table of contents, so it fails on one that was cut short —
# which is the difference between having a backup and believing you have one.
if ! compose exec -T db pg_restore --list > /dev/null < "${BACKUP_DIR}/${name}.part"; then
    echo "backup: dump did not verify, keeping the previous ones" >&2
    rm -f "${BACKUP_DIR}/${name}.part"
    exit 1
fi

mv "${BACKUP_DIR}/${name}.part" "${BACKUP_DIR}/${name}"
echo "backup: ${name} ($(du -h "${BACKUP_DIR}/${name}" | cut -f1))"

# One file on the remote, always the newest, under a fixed name. It is uploaded
# beside the old one and moved into place afterwards: overwriting the only copy
# directly would leave a window where a failed upload has already destroyed the
# backup it was replacing.
if [[ -n "$RCLONE_REMOTE" ]]; then
    rclone copyto "${BACKUP_DIR}/${name}" "${RCLONE_REMOTE}/${REMOTE_NAME}.part"
    rclone moveto "${RCLONE_REMOTE}/${REMOTE_NAME}.part" "${RCLONE_REMOTE}/${REMOTE_NAME}"
    echo "backup: uploaded to ${RCLONE_REMOTE}/${REMOTE_NAME}"
fi

# Local pruning comes last, and only on the paths above having succeeded: a
# failed upload must never be the reason the older copies were deleted. The
# timestamp sorts lexicographically, so the newest names are the newest dumps.
shopt -s nullglob
dumps=("${BACKUP_DIR}"/coffee-*.dump)
shopt -u nullglob
printf '%s\n' "${dumps[@]}" | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "backup: pruned local $(basename "$old")"
done
