#!/usr/bin/env bash
# One night's work: crawl, then back up whatever the crawl left behind.
#
# The crawl's exit code is the run's exit code, so `systemctl status` and any
# OnFailure= handler see it — but the backup runs either way. A night where five
# shops rotted still produced data worth keeping, and exit 1 is the normal way
# the crawler says so. Chaining the backup with ExecStartPost= instead would
# skip it on exactly those nights.
set -uo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/coffee-aggregator/deploy/compose.yml}"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

SITE_WORKERS="${SITE_WORKERS:-8}"
WORKERS="${WORKERS:-2}"
DEADLINE="${DEADLINE:-17400}"

docker compose -f "$COMPOSE_FILE" run --rm crawler \
    crawl --site all --sink postgres \
    --site-workers "$SITE_WORKERS" --workers "$WORKERS" --deadline "$DEADLINE"
crawl_status=$?

if [[ $crawl_status -eq 0 ]]; then
    echo "run: crawl finished, every shop wrote something"
else
    echo "run: crawl exited ${crawl_status} — see 'coffee-aggregator runs'" >&2
fi

COMPOSE_FILE="$COMPOSE_FILE" "${HERE}/backup.sh"
backup_status=$?

# A backup that silently stopped working is worse than a crawl that missed a
# shop, so it fails the unit too — but the crawl's own code wins when both went
# wrong, because that is the one that says what happened to the data.
if [[ $crawl_status -ne 0 ]]; then
    exit "$crawl_status"
fi
exit "$backup_status"
