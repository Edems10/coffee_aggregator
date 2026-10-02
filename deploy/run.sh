#!/usr/bin/env bash
# One night's work: crawl, report on what it did, then back up what it left.
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
# Unset means the page is not written. The report still goes to the journal.
REPORT_HTML="${REPORT_HTML:-}"

docker compose -f "$COMPOSE_FILE" run --rm crawler \
    crawl --site all --sink postgres \
    --site-workers "$SITE_WORKERS" --workers "$WORKERS" --deadline "$DEADLINE"
crawl_status=$?

if [[ $crawl_status -eq 0 ]]; then
    echo "run: crawl finished, every shop wrote something"
else
    echo "run: crawl exited ${crawl_status} — see 'coffee-aggregator runs'" >&2
fi

# The report is a read, not an alarm. Whether the night was fine is already in
# the crawl's own exit code, and a summary that could fail the unit a second
# time for the same bad night would only make it cry wolf about data that is
# sitting safely in the database — the same reasoning that keeps the backup out
# of an ExecStartPost=. So nothing below is allowed to change $?, and the exits
# at the bottom still see only the crawl's code and the backup's. It cannot
# hang the run either: every connection carries a 60-second statement_timeout.
#
# -T because the output is captured rather than watched; a pseudo-TTY would put
# a carriage return at the end of every line of the page.
docker compose -f "$COMPOSE_FILE" run --rm -T crawler report --format text || true

# The page, for whoever would rather read it in a browser than in the journal.
# Rendered to a temporary file and moved into place only once it is whole: a
# redirect straight onto the served path truncates yesterday's page to nothing
# before the first byte of today's is written, and a failed render would then
# leave an empty page where a readable one used to be.
if [[ -n $REPORT_HTML ]]; then
    report_tmp="${REPORT_HTML}.tmp"
    if docker compose -f "$COMPOSE_FILE" run --rm -T crawler report --format html \
        >"$report_tmp" && [[ -s $report_tmp ]]; then
        mv -f "$report_tmp" "$REPORT_HTML" || true
        echo "run: wrote ${REPORT_HTML}"
    else
        rm -f "$report_tmp" || true
        echo "run: the HTML report failed; ${REPORT_HTML} is unchanged" >&2
    fi
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
