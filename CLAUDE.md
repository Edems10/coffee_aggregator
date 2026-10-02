# coffee-aggregator

A crawler that collects bean prices and lot data from Czech and Slovak coffee
shops into Postgres. It is the data layer for a planned coffee recommender, and
the one question it exists to answer is *what does this coffee cost per
kilogram, here, today*.

## Tooling

* **uv only.** `uv run …`, `uv sync`, `uv lock --upgrade`. Never pip.
* Python 3.14. `ruff` with `select = ["ALL"]`, `mypy` strict. Run
  `uv run ruff check && uv run ruff format && uv run mypy && uv run pytest`
  before calling anything done.
* `uv run pre-commit run --all-files` is what CI runs; it is the real gate.

## House style

* **No module docstrings and no copyright headers.** Files never open with a
  docstring or a comment, except a constants file whose header explains the
  constants. `D100`, `D104` and `CPY001` are ignored for exactly this reason.
* Google-style docstrings on functions, classes and methods.
* **Thin `__init__.py`** — re-exports only. Logic lives in a named module beside
  it (`sites/loader.py`, `db/migrate.py`, `http/fetcher.py`, `labels/read.py`).
* Comments explain *why*, never *what*. A comment that restates the line below
  it is noise; a comment that records the measurement or the bug behind a
  decision is worth more than the code it sits on.
* README and `docs/` are a developer reference: fields, storage, options, how to
  run it. No pitch, no feature list, no marketing prose.

## Data rules

These are the ones that have cost real bugs.

* **Never invent a value the page does not state.** Roast level was once derived
  from tasting notes, so "chocolate" became a dark roast. A missing value is
  always better than a wrong one, and `NULL` is a truthful answer.
* **Anything derived goes in its own column, with its provenance.** The typed
  column means "what the shop said". An estimate — from a brew profile, from
  prose, from a model — belongs beside it with a flag saying where it came from.
  This matters twice over because the data is meant to be trained on: nobody
  should train on our own guesses believing they are facts.
* **Measure the ceiling before chasing a number.** Much of what looks like a
  parsing gap is a shop that publishes nothing. Before "improving" a field,
  split the catalogue into specialty roasters and resellers of commercial
  blends and count how many pages state the datum at all. A blend of twenty
  origins has no origin country, and no parser will find one.
* **A per-kilogram price is published only when the weight describes what the
  price buys.** A carton of 24 one-kilogram bags divided by one kilogram is a
  figure nobody can compare, which is worse than no figure.
* Ordinary Czech and Slovak terms belong in `labels/terms.py`, where every shop
  gains them at once. A per-shop `label_map` or `ignore` entry is for that
  shop's own oddities; a test fails if a config repeats something the shared
  vocabulary already knows.

## Crawling policy

* Only `User-agent: *` rules and `Crawl-delay` bind us. AI-crawler directives
  (GPTBot, ClaudeBot, `Content-Signal: ai-train=no`) do not apply — this is a
  conventional crawler identified as `coffee-aggregator`, not an AI bot.
* `COFFEE_AGG_CONTACT` must be a real address. Being identifiable is what keeps
  the crawler welcome, and it is why it does not run behind a VPN: shops switch
  currency by IP, and the parser reads currency off the page, so a foreign exit
  node would write EUR where CZK belongs.
* Sellers only. Price-comparison aggregators are out of scope; review sites are
  an enrichment tier for later, not a source.

## Database

* **Migrations are append-only.** A new numbered file beside `0001_initial.sql`;
  never edit one that has been applied. The runner records a checksum and will
  refuse a file that changed.
* A database password goes into a `postgresql://` URL, so generate it with
  `openssl rand -hex`, never `-base64`. A `/` ends the URL's authority early and
  the DSN then resolves the host as `coffee`.
* Nothing in the schema is vendor-specific. `DATABASE_URL` is the only thing
  that changes between a laptop, a home server and a managed Postgres.

## Repository hygiene

* A fixture ships only with a test that reads it, under 300 KB. No whole-site
  captures, no survey data in git.
* **Push only when asked.** Local commits without being asked are fine; `git
  push` and anything else that publishes waits for the word.

## Deployment

`deploy/` holds everything the server needs; `deploy/README.md` is the
procedure. One command updates a running server:

```bash
sudo /opt/coffee-aggregator/deploy/update.sh [--crawl]
```

It pulls, **rebuilds the image**, then migrates, in that order. The rebuild is
the step that is silent when forgotten — the containers run an image built once,
so a pull alone leaves yesterday's code running. `update.sh` is deliberately
generic and should not need editing when code, configs or migrations change: the
migration runner discovers new files by itself. Only a deployment step that is
neither pull, build nor migrate — a one-off data backfill, say — would call for
a change there.

Parsing fixes never rewrite rows already stored; they take effect on the next
crawl, which upserts.
