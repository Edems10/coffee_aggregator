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
    # --s3-no-head, because the upload is not what fails. R2 answers the PUT
    # with 200 and an x-amz-version-id; rclone then verifies the object with
    # HEAD <key>?versionId=..., and R2 answers 501 to a versionId-qualified
    # request. rclone calls that "Failed to copy" although the object is there,
    # and the retry then finds it already uploaded and reports success — which
    # is why a backup that worked logged three ERROR lines every night.
    #
    # Dropping rclone's own check is sound here only because the PUT carries
    # Content-Md5, which R2 verifies server-side and rejects on a mismatch, and
    # because the dump was already verified with pg_restore --list. The size is
    # still read back below, with a plain HEAD that R2 does implement.
    rclone --s3-no-head copyto "${BACKUP_DIR}/${name}" "${RCLONE_REMOTE}/${name}"

    uploaded=$(rclone lsf --format s "${RCLONE_REMOTE}/${name}" | head -1)
    local_size=$(stat -c %s "${BACKUP_DIR}/${name}")
    if [[ "$uploaded" != "$local_size" ]]; then
        echo "backup: ${name} is ${uploaded:-missing} bytes off-site, ${local_size} here" >&2
        exit 1
    fi
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
