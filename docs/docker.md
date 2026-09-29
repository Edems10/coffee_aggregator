# The container image

## What is in it

`Dockerfile` has two stages. The first installs the locked dependency set into
`/opt/venv` with `uv sync --frozen`, then installs the project itself with
`--no-editable` so the environment holds a real copy rather than a link back to
the build tree. The second stage starts from a clean `python:3.14-slim` and
copies only that environment.

What that leaves out is the point: no source tree, no `uv`, no dev
dependencies, no tests, no uv cache. About 270 MB, most of it the Python base
image and the `lxml` and `psycopg` wheels.

`--frozen` refuses to touch `uv.lock`. The image is built from the resolution
that was committed and tested; a build can never silently pick up a newer
dependency than CI ran against.

`.dockerignore` is an allowlist — everything is excluded and then
`pyproject.toml`, `uv.lock`, `README.md` and `src/` are let back in. A new
directory in the repo cannot enlarge the image or invalidate its cache by
accident, and `.env` cannot reach a layer.

## What it needs at runtime

| | |
| --- | --- |
| Required | `DATABASE_URL` |
| Filesystem | none — run it `read_only` |
| User | uid 10001, `coffee`, with a real home at `/home/coffee` |
| Entry point | `coffee-aggregator`; the command is the sub-command and its flags |
| Default command | `crawl --sink postgres` |
| Exit code | `0` every shop wrote something, `1` a shop wrote nothing, `2` bad configuration |

The home directory is not decoration: with no database to keep it in, the
EUR/CZK fixing falls back to `~/.cache`, and `Path.home()` on a user without a
home resolves to `/`. Under `--sink postgres` the rate goes to the `fx_rates`
table and nothing is written to disk at all, which is why the compose service
sets `read_only: true`. If that ever starts failing, something began writing
where it should not.

## Building and publishing

```bash
docker build -t coffee-aggregator:$(git rev-parse --short HEAD) .
```

The installer floats: `UV_VERSION` defaults to `latest`, because what lands in
the environment is fixed by `uv.lock` and `--frozen`, not by uv's own version.
To reproduce an older build exactly, pin it for that build:
`--build-arg UV_VERSION=0.12.19`.

Tag by commit, not by `latest`: the thing a scheduled run is executing should be
answerable from the tag alone. Build for the target's architecture — an image
built on an Apple Silicon machine is arm64, and AWS Lambda and most ECS tasks
run amd64:

```bash
docker buildx build --platform linux/amd64 -t coffee-aggregator:amd64 .
```

## Reaching the database

| From | Host in `DATABASE_URL` |
| --- | --- |
| a compose service | `db` — the service name, and port 5432, not the published one |
| a container, database on this Mac | `host.docker.internal` |
| AWS, same VPC | the RDS endpoint, `?sslmode=require` |
| AWS, public endpoint | the RDS endpoint, `?sslmode=require`, and the security group must admit the function |

Ten workers means up to ten concurrent sessions. A managed instance with a low
`max_connections` refuses them before the crawl notices, so either keep
`--workers` under the instance's limit or put a pooler in front of it.

## Two identities, and neither is a person

The **database identity** is the Postgres role in the DSN. It needs `CONNECT`,
`USAGE` on the schema and `SELECT`/`INSERT`/`UPDATE` on the five tables;
`init-db` additionally needs `CREATE` on the schema. It does not need
`CREATEDB` — nothing in the project ever creates a database.

The **platform identity** is whatever the container runs as: an IAM execution
role on Lambda or ECS, a service account elsewhere. It decides what the process
may reach — the secret holding the DSN, the VPC, the log group — and nothing
about what may be read or written in the database.

Locally the process runs as you and connects as `coffee`. In production it runs
as an unprivileged container user and connects as `coffee`. In neither case is a
personal account involved, and the DSN is passed in at start rather than stored
in the image.

## Scheduling the daily run

Python 3.14 has no managed Lambda runtime, so the function ships as this
container image. EventBridge fires four invocations a day, each with its own
shard:

```
crawl --sink postgres --shard 0/4 --workers 10 --deadline 840
crawl --sink postgres --shard 1/4 --workers 10 --deadline 840
…
```

Sharding is balanced by estimated request-seconds, so four shards cover all 157
shops with no overlap. `--deadline` keeps a run inside its invocation window and
records that it stopped early instead of being killed mid-write.

Two things to set on the function:

* leave `COFFEE_AGG_CACHE_DIR` unset — Lambda's `/tmp` is 512 MB, and the disk
  cache exists for offline re-parsing during development, not for production
* read `DATABASE_URL` from Secrets Manager into the environment at start

caffeoro.sk declares `Crawl-delay: 30` and fits into no fifteen-minute shard. It
needs its own slower schedule, excluded from the sharded run.

## Alerting

The exit code is the signal. `1` means at least one shop wrote nothing, which is
what a rotted selector looks like, and it is worth waking up for only when it
persists. The detail is in `crawl_run` — one row per shop per run, with counts
and errors — readable with `coffee-aggregator runs`.
