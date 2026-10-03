# syntax=docker/dockerfile:1

# Two stages: the first resolves the locked dependency set into a virtual
# environment, the second carries nothing but that environment. Source, tests,
# uv itself and the build cache stay behind, so the runtime image holds only
# what the crawl actually executes.

ARG PYTHON_VERSION=3.14
# uv floats: what it installs is fixed by uv.lock and `--frozen` below, not by
# uv's own version, so tracking the current release costs nothing and keeps the
# image off an ageing installer. Pin it here when a build needs reproducing.
ARG UV_VERSION=latest

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS build

COPY --from=uv /uv /usr/local/bin/uv

# uv shells out to the git CLI for a git dependency, and the slim image has
# none. coffee-contracts is pinned to a tag and fetched from GitHub, so without
# this the build fails at `uv sync` with "git executable not found" — and only
# the build stage needs it, so it never reaches the runtime image.
RUN apt-get update \
    && apt-get install --no-install-recommends --yes git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src

# Dependencies first, without the project, so this layer is reused whenever only
# our own code changed. --frozen refuses to update uv.lock: the image is built
# from the resolution that was committed and tested, never from a fresh one.
#
# coffee-contracts is a git dependency on a tag. The repository is public —
# it is the published interface between these services — so resolving it
# needs no credential, and `git` in this stage is the only thing it costs.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself. --no-editable installs a real copy into the
# environment instead of a link back to /src, which the runtime image drops.
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:${PYTHON_VERSION}-slim AS runtime

# A fixed high uid, so a mounted volume gets predictable ownership, and a real
# home: the EUR/CZK fixing falls back to ~/.cache when there is no database to
# keep it in, and Path.home() on a user without one resolves to /.
RUN groupadd --gid 10001 coffee \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/coffee coffee

COPY --from=build /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/coffee

USER coffee
WORKDIR /home/coffee

# No secret is baked in: DATABASE_URL arrives from the environment at start, and
# the CLI refuses to run without one.
ENTRYPOINT ["coffee-aggregator"]
CMD ["crawl", "--sink", "postgres"]
