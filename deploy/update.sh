#!/usr/bin/env bash
# Pull, rebuild, migrate — the three steps a deployment needs, in the one order
# that works.
#
# The rebuild is the step that is easy to forget and silent when forgotten: the
# containers run an image that was built once, so `git pull` on its own changes
# nothing at all and the crawl keeps running yesterday's code.
set -euo pipefail

REPO="${REPO:-/opt/coffee-aggregator}"
ENV_FILE="${ENV_FILE:-/etc/coffee-aggregator/env}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO}/deploy/compose.yml}"
crawl=no

usage() {
    cat <<'TXT'
usage: update.sh [--crawl]

  --crawl   start a crawl when the update is done, instead of leaving it to the
            05:00 timer. It runs in the background; follow it with
            journalctl -u coffee-aggregator.service -f
TXT
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --crawl) crawl=yes ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# A server that was edited by hand is a server whose next pull conflicts, and
# the middle of a deployment is the worst moment to find that out.
if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
    echo "update: ${REPO} has uncommitted changes; nothing was pulled" >&2
    git -C "$REPO" status --short >&2
    exit 1
fi

before="$(git -C "$REPO" rev-parse HEAD)"
git -C "$REPO" fetch --quiet origin
target="$(git -C "$REPO" rev-parse '@{u}')"

if [[ "$before" == "$target" ]]; then
    echo "update: already at $(git -C "$REPO" log --oneline -1)"
else
    # --ff-only, so a server that somehow diverged stops here rather than
    # producing a merge commit nobody will ever review.
    git -C "$REPO" merge --ff-only --quiet "$target"
    echo "update: pulled $(git -C "$REPO" rev-list --count "${before}..${target}") commit(s)"
    git -C "$REPO" log --oneline --no-decorate "${before}..${target}"
fi

# Unconditional: when nothing was pulled this is a few cached seconds, and when
# something was it is the whole point. The database and the table browser come
# up too, so a change to compose.yml itself takes effect.
echo "update: building the crawler image"
compose --profile crawler build
compose up -d

# Idempotent: prints what it applied, or that there was nothing to apply. It has
# to run before any crawl — a sink writing a column the schema does not have yet
# fails the whole run.
echo "update: applying migrations"
compose run --rm crawler init-db

if [[ "$crawl" == yes ]]; then
    echo "update: starting a crawl in the background"
    systemctl start --no-block coffee-aggregator.service
    echo "update: follow it with  journalctl -u coffee-aggregator.service -f"
else
    echo "update: done; the 05:00 timer will pick this up"
fi
