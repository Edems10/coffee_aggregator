#!/usr/bin/env bash
# Dump the database, verify the dump, upload it, and only then prune old ones.
#
# The dump runs inside the db container, so pg_dump is always the same major
# version as the server and the host needs no postgresql-client at all.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/coffee-aggregator/deploy/compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/coffee-aggregator}"
RCLONE_REMOTE="${RCLONE_REMOTE:-}"
# Dumps to keep on this machine. The remote keeps its own: nothing here ever
# deletes from it, because a bucket lock would refuse and that is the point.
KEEP="${KEEP:-7}"

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

# A new object every night, never an overwrite, so the bucket lock on the remote
# can refuse every delete and every overwrite — including one issued with this
# machine's own credentials. Whoever can read the key here already has root and
# can burn the database and the local dumps; the copy up there is the one thing
# that should survive that, so nothing in this script is allowed to remove it.
# Old objects are expired by a lifecycle rule on the bucket instead.
#
# No .part dance either: that existed to protect a single overwritten file. A
# half-finished upload now just leaves one bad object that tomorrow supersedes,
# while every good one before it is untouched.
if [[ -n "$RCLONE_REMOTE" ]]; then
    # --s3-no-check-bucket on the command line, not only in the remote: without
    # it rclone calls CreateBucket before every upload, and R2 answers 501 to a
    # token scoped to objects. The README tells you to set it on the remote too,
    # but the script cannot see how the remote was actually configured, and this
    # is the one failure that costs a backup rather than logging one.
    #
    # It is NOT confirmed to be the cause of the nightly 501 seen on 2026-10-03:
    # that upload succeeded on rclone's own retry, which this theory does not
    # explain, since a refused CreateBucket would be refused again. Run
    # `rclone -vv --retries 1 --dump headers copyto …` to find out which call
    # R2 is actually refusing before blaming this one.
    rclone --s3-no-check-bucket copyto "${BACKUP_DIR}/${name}" "${RCLONE_REMOTE}/${name}"
    echo "backup: uploaded to ${RCLONE_REMOTE}/${name}"
fi

# Local pruning comes last, and only on the paths above having succeeded: a
# failed upload must never be the reason the older local copies were deleted.
# The timestamp sorts lexicographically, so the newest names are the newest.
shopt -s nullglob
dumps=("${BACKUP_DIR}"/coffee-*.dump)
shopt -u nullglob
printf '%s\n' "${dumps[@]}" | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "backup: pruned local $(basename "$old")"
done
