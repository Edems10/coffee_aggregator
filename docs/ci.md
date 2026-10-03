# Continuous integration

Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

Triggers: every `pull_request`, and every `push` to `main`. Runs for the same ref
cancel each other, and the workflow token is read-only (`contents: read`).

Every job checks out the repo, installs uv with `astral-sh/setup-uv@v5`
(`enable-cache: true`, no interpreter pin — uv installs the version
`.python-version` names) and runs `uv sync --locked`, which fails when
`uv.lock` no longer matches `pyproject.toml`.

| Job | Runs | Reproduce locally |
| --- | --- | --- |
| `lint` | `ruff check --output-format=github`, `ruff format --check`, `mypy` | `uv sync --locked && uv run ruff check && uv run ruff format --check && uv run mypy` |
| `hooks` | `pre-commit run --all-files --show-diff-on-failure` | `uv run pre-commit run --all-files --show-diff-on-failure` |
| `test` | `pytest -q` against a `postgres:18-alpine` service container | `docker compose up -d && TEST_DATABASE_URL=postgresql://coffee:coffee@localhost:5432/coffee_test uv run pytest -q` |
| `image` | `docker build` (with the contracts token as a BuildKit secret), then checks the image lists the same shops as the working tree, runs as uid 10001, and exits 2 with no `DATABASE_URL` | `docker build -t coffee-aggregator:ci . && docker run --rm coffee-aggregator:ci list-sites` |
| `diff-summary` | Classifies the pull request's files against the merge base into the job summary | `git diff --name-status $(git merge-base origin/main HEAD)` |

Every job that resolves dependencies first runs the local composite action
[`.github/actions/contracts-auth`](../.github/actions/contracts-auth/action.yml).
`coffee-contracts` is a git dependency on a tag and that repository is private
while this one is public, so the workflow token — scoped to this repository
alone — cannot fetch it. The action configures git with the `CONTRACTS_TOKEN`
secret when it is set and does nothing when it is not; the `image` job passes
the same token to `docker build` as a BuildKit secret, because the daemon does
not inherit the runner's git config. **Until `CONTRACTS_TOKEN` exists every one
of those jobs fails at `uv sync`.** Making `coffee-contracts` public removes the
need for all of it.

Notes:

* `ruff check --output-format=github` emits workflow annotations, so findings
  appear inline on the pull request diff.
* The repo's ruff pre-commit hooks are fixers (`ruff check --fix`,
  `ruff format`). When they fail they have already rewritten the files, so
  `--show-diff-on-failure` is what makes the CI log readable: it prints the
  rewrite the hook wants.
* The pytest hook is a `pre-push` hook, so `pre-commit run --all-files` does not
  run it; the `test` job does, with a database attached.
* The `image` job builds `Dockerfile` but pushes nothing; it exists because a
  packaging mistake is silent. The shop configs are TOML files inside the
  package and reach the image only because hatchling packages them — if they
  stopped, the build would still succeed and the crawl would simply find no
  shops. Comparing the image's `list-sites` against the working tree's catches
  that, and makes a shop added in a pull request prove it arrives in both.
* `diff-summary` is informational only. The suite runs whole on every run —
  selecting tests from the diff would silently skip regressions, because a change
  to `normalize.py` or the shared label vocabulary breaks adapters whose files
  the pull request never touched.

## Running the PostgreSQL integration tests locally

The integration tests in `tests/test_postgres_integration.py` skip unless
`TEST_DATABASE_URL` is set. They drop and recreate every table the migrations
own, so point them at a throwaway database — never at your development one.

```bash
docker compose up -d                                   # PostgreSQL 18 on localhost:5432
docker compose exec db createdb -U coffee coffee_test  # once
TEST_DATABASE_URL=postgresql://coffee:coffee@localhost:5432/coffee_test uv run pytest -q
```

The tests apply the migrations themselves through
`src/coffee_aggregator/db/migrate.py`; an empty database is enough. To apply them
by hand instead:

```bash
uv run coffee-aggregator init-db --dsn postgresql://coffee:coffee@localhost:5432/coffee_test
```

## Repository hygiene

`tests/test_repo_hygiene.py` enforces two fixture rules in the suite itself, not
only in CI:

* every file under `tests/fixtures/` is at most 300 KB, matching the
  `check-added-large-files --maxkb=300` pre-commit hook;
* every directory under `tests/fixtures/` is named by at least one test module —
  by its own name, or, for `shoptet_<id>` / `woo_<id>`, by the bare site id.

A captured page set with no test that reads it is a failure, not a warning.
