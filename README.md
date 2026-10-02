# coffee-aggregator

A polite, extendable collector of **coffee-bean product data from Czech and Slovak
e-shops**. It walks a shop's catalogue, extracts *everything* a product page exposes,
normalises it into one canonical shape (enums, ISO country codes, grams, metres,
price per kilogram, and every price in **both** CZK and EUR) and stores it in plain
PostgreSQL — ready to feed an AI tool.

Two product rules drive the design:

1. **Nothing is dropped.** Every `LABEL: value` pair an adapter sees is kept in the
   `raw_attributes` catch-all, even when it is also mapped to a typed field.
2. **Everything is standardised.** One shared normaliser (`normalize.py`) turns
   `"praná"`, `"washed"` and `"mytá"` into `ProcessMethod.WASHED`, `"1 kg"` into
   `1000`, `"249,-"` into `(249.0, None)`, `"kubánska"` into `"CU"`.

## Requirements

* Python 3.14+
* [uv](https://docs.astral.sh/uv/) — the only supported package manager
* PostgreSQL 15 or newer (docker compose ships the current major, 18)

## Setup

```bash
uv sync                     # create .venv and install runtime + dev dependencies
cp .env.example .env        # then edit it; .env is git-ignored
```

Every command below can also be run as `uv run coffee-aggregator …` without
activating the virtual environment.

## Local database

```bash
docker compose up -d                       # PostgreSQL 18 on localhost:5432
# already running something on 5432?
COFFEE_DB_PORT=5433 docker compose up -d   # then use …@localhost:5433/coffee

uv run coffee-aggregator init-db           # applies every pending migration
```

`init-db` applies the migrations and prints the versions it applied; a second run
prints `up to date`. See [Migrations](#migrations) for what it runs.

### Pointing at a hosted database later

Nothing but `DATABASE_URL` changes — there is no vendor SDK anywhere in the code.

| Deployment | `DATABASE_URL` |
| --- | --- |
| local docker | `postgresql://coffee:coffee@localhost:5432/coffee` |
| AWS RDS | `postgresql://USER:PASSWORD@host.eu-central-1.rds.amazonaws.com:5432/coffee?sslmode=require` |
| Supabase | `postgresql://postgres:PASSWORD@db.PROJECT.supabase.co:5432/postgres` (the *direct* Postgres URL, not the REST API) |
| any managed Postgres | the same `postgresql://` URL |

## Running in a container

`Dockerfile` builds the image the scheduled run uses: a two-stage build that
resolves `uv.lock` into `/opt/venv` and then carries nothing but that
environment — no source tree, no uv, no dev dependencies, ~270 MB. It runs as
uid 10001 and needs no writable filesystem.

```bash
docker compose up -d                                  # database only
docker compose run --rm crawler init-db               # schema, from the image
docker compose run --rm crawler crawl --sink postgres --site kafista
docker compose run --rm crawler                       # the daily crawl
```

The `crawler` service sits behind a compose profile, so `docker compose up -d`
still starts only the database. Inside the compose network the database answers
to `db:5432`, not `localhost` — from a container on this machine to a database
on the host, use `host.docker.internal`.

`DATABASE_URL` arrives from the environment at start and is never baked into a
layer; `.dockerignore` is an allowlist, so `.env` cannot reach the build context
at all. See [docs/docker.md](docs/docker.md) for deploying the image.

## CLI

```bash
coffee-aggregator list-sites
coffee-aggregator init-db [--dsn URL] [--dry-run]
coffee-aggregator runs [--site <id>] [--limit N] [--dsn URL]
coffee-aggregator fx [--refresh] [--dsn URL]
coffee-aggregator crawl --site <id>|all --sink jsonl|postgres \
    [--out PATH] [--dsn URL] [--limit N] [--max-pages N] \
    [--deadline SECONDS] [--shard I/N] [--batch-size 50] \
    [--workers 2] [--site-workers 1] [--delay 1.0] [--cache-dir DIR] \
    [--timeout 15] [--retries 3] [--max-retry-wait 60] [--robots-retry 300]
coffee-aggregator parse --site <id> --file page.html [--url URL]
```

* `-v` switches logging to DEBUG, and is accepted before *or* after the
  sub-command. Logs go to stderr; only JSON goes to stdout.
* `--dsn` overrides `DATABASE_URL` for one invocation.
* `--limit` / `--max-pages` make a run *partial*: delisting is then skipped, so a
  short debugging crawl can never mark the rest of a catalogue as gone.
* `--deadline SECONDS` caps the whole crawl. It is checked at batch and listing-page
  boundaries — never inside one, so no batch is ever half written — and a run that
  hits it is reported with `deadline_reached: true` and `complete: false`, which is
  what stops a truncated shard from delisting the catalogue it never finished
  reading. One deadline covers every shop of the invocation, not one per shop.
* `--shard I/N` crawls one deterministic slice of the registered shops. See
  [Sharding the daily run](#sharding-the-daily-run).
* `--batch-size` is how many products are fetched and written per round;
  `--timeout`, `--retries`, `--max-retry-wait` and `--robots-retry` are the
  transport knobs (`--timeout 8 --retries 1 --max-retry-wait 10` is the short-lived
  profile a Lambda wants). Each one has an environment variable below.
* `--workers` is the fetch pool *inside* one shop and defaults to **2**: every URL
  of a batch belongs to one host, the per-host delay is the real bound, and four
  workers measured slightly *slower* than one. `--site-workers` is the knob that
  turns hours into minutes — it crawls that many shops at once, each still paced
  on its own host.
* `runs` prints the recent rows of `crawl_run`, newest first — what each shop
  discovered, wrote and failed at, and the command line it was crawled with. It
  needs a DSN; see [Run history](#run-history).
* `init-db --dry-run` prints the migrations that *would* be applied and changes
  nothing.
* `parse` is the fixture workflow: save a page, parse it offline, print the JSON.
* `fx` prints the EUR/CZK fixing the crawls stamp on their rows, fetching it when
  none is stored; `--refresh` fetches even when one is. See
  [Price normalisation](#price-normalisation).
### Exit codes

| code | meaning |
| --- | --- |
| `0` | every shop that was crawled stored at least one product |
| `1` | a shop wrote nothing, a shop's discovery failed, or there was nothing to crawl |
| `2` | configuration error: missing DSN, unknown site id, malformed `--shard` |

A daily job that always exits `0` is a daily job nobody reads, so `crawl` fails the
run when **any** selected shop stored no product or its discovery broke — the two
shapes a rotted selector takes. `--limit`, `--max-pages` and `--deadline` never fail
a run by themselves: they make it *partial*, which suppresses delisting, but a cap
of one product is still expected to produce one product. A `--site all` crawl names
every empty shop in its summary before it exits. `list-sites` also exits `2` when a
shop TOML or adapter module could not be loaded, printing each failure; a crawl logs
how many were lost and carries on with the rest.
* `--sink jsonl` writes to a `.tmp` file and moves it into place only when the
  crawl finishes, so an interrupted run never truncates the previous output.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | — | PostgreSQL DSN; required only by `init-db` and `--sink postgres` |
| `COFFEE_AGG_USER_AGENT` | `coffee-aggregator/<version> (+<contact>)` | identifying User-Agent |
| `COFFEE_AGG_UA_TOKEN` | first token of the User-Agent | the product token robots.txt rules are matched on |
| `COFFEE_AGG_CONTACT` | project URL | how a shop can reach you |
| `COFFEE_AGG_DELAY` | `1.0` | minimum seconds between request starts per host |
| `COFFEE_AGG_WORKERS` | `2` | fetch thread-pool size, within one shop |
| `COFFEE_AGG_SITE_WORKERS` | `1` | how many shops are crawled at the same time |
| `COFFEE_AGG_TIMEOUT` | `15.0` | per-request timeout in seconds |
| `COFFEE_AGG_RETRIES` | `3` | how often a 429/5xx is retried |
| `COFFEE_AGG_MAX_RETRY_WAIT` | `60.0` | longest a single retry may sleep |
| `COFFEE_AGG_ROBOTS_RETRY` | `300.0` | how long an unreadable robots.txt keeps a host closed |
| `COFFEE_AGG_BATCH_SIZE` | `50` | products fetched and written per round |
| `COFFEE_AGG_DEADLINE` | — | seconds one whole crawl may take |
| `COFFEE_AGG_CRAWL_DELAYS` | `caffeoro.sk=30` | `host=seconds` pairs used to weigh the shards |
| `COFFEE_AGG_CACHE_DIR` | — | directory for the conditional-GET cache |
| `COFFEE_AGG_FX_CACHE` | `<cache dir>/fx_rates.json`, else `~/.cache/coffee-aggregator/fx_rates.json` | the JSON rate store used when there is no database |
| `COFFEE_DB_PORT` | `5432` | host port docker compose publishes Postgres on |
| `TEST_DATABASE_URL` | — | enables the PostgreSQL integration tests |

A `.env` file in the working directory is loaded at startup and **never** overrides
a real environment variable.

## Politeness policy

Every request in the project goes through one `PoliteFetcher`:

* **robots.txt is enforced and cannot be disabled from the CLI.** A missing
  robots.txt (404) allows everything; an *unreachable* one (5xx, timeout) disallows
  the whole host — it fails closed.
* An identifying `User-Agent` with a contact URL, and `Accept-Language: sk,cs;q=0.9,en;q=0.5`.
* A per-host rate limiter with jitter that paces request *starts*, so raising
  `--workers` never raises the request rate against one shop. A `Crawl-delay` in
  robots.txt wins whenever it is larger than `--delay`.
* Retries with exponential backoff on 429/500/502/503/504, honouring `Retry-After` —
  and **through the limiter**: the `httpx2` transport is built with `retries=0`, so
  nothing below `PoliteFetcher` ever retries behind the limiter's back and every
  retry re-checks robots.txt and waits out the host's delay like a first request.
  `--retries` caps how many, `--max-retry-wait` how long any one of them may sleep.
* A `Crawl-delay` is applied to the *first* content request as well: the host's
  next slot is re-booked from the moment the robots.txt request started.
* robots.txt itself is fetched without automatic redirects; each hop is checked
  and paced by hand, up to five of them.
* The robots product token is logged at startup, and a `COFFEE_AGG_USER_AGENT`
  that would make robots.txt match a browser name is warned about.
* An optional on-disk cache sending `If-None-Match` / `If-Modified-Since`, so a
  re-crawl costs the shop a 304 instead of a page. Validators are filed under the
  URL the server finally served, with an alias from the URL that redirected there.
* A body whose `Content-Type` names no charset is decoded by sniffing rather than
  by the legacy ISO-8859-1 fallback, so Czech and Slovak diacritics survive.

## Supported shops

157 shops — 102 Czech, 55 Slovak. `coffee-aggregator list-sites` prints the live
list with each shop's id, country, kind and base URL; it is generated from the
registry, so it is never out of date the way a table here would be.

| kind | shops | what drives it |
| --- | --- | --- |
| Shoptet | 99 | `platforms/shoptet.py` + one TOML file per shop |
| WooCommerce | 47 | `platforms/woocommerce.py` + one TOML file per shop |
| Shopify | 8 | `platforms/shopify.py` + one TOML file per shop |
| bespoke | 3 | `sites/coffeein.py`, `sites/fathers.py`, `sites/nordbeans.py` |

A shop needs a module of its own only when its pages fit none of the three
platform adapters. The other 154 are configuration: see
[Adding a shop](#adding-a-shop).

Czech and Slovak shops still outside the catalogue mostly run Upgates,
Eshop-rychle or PrestaShop, or are custom builds. None of them expose a public
product JSON, so each needs HTML-first discovery — a new platform adapter, not a
config file.

**coffeein** walks `/kategoria/2/cerstvo-prazena-zrnkova-kava/<n>/` page by page and
stops as soon as a page redirects or adds no new product, building every reference
from the real `a.headline` href (slugs are never rebuilt from names). The detail
parser reads the schema.org microdata for price, availability, rating and reviews,
and splits the `<br/>`-separated `LABEL: value` block line by line — every label
lands in `raw_attributes`, the known ones also in typed fields. Blends are kept.
`CoffeeinSite(use_sitemap=True)` enumerates `/sitemap.xml` instead, which covers the
whole catalogue (mugs and filters included), so the category walk stays the default.

The 99 **Shoptet** shops are one adapter — `platforms/shoptet.py` — driven by
`sites/configs/*.toml`. The Shoptet adapter walks
`/<category>/strana-<n>/` (or `?page=<n>`, set `pagination = "query"`) until a page
lists nothing or repeats the page before it, reading only the real `#products` grid
so the recommendation carousel above it cannot fake a page. Products already seen in
another category are yielded once, but that de-duplication never ends a category's
pagination — overlapping categories are the norm. On a detail page it reads, in order, the
`schema.org/Product` microdata inside `div.p-detail` (id, sku, price, currency,
availability, image, rating, category path), `table.detail-parameters`, the variant
`<select data-parameter-name=…>` elements, the hand-written `label`/`value` spec
block roasteries put in the description, and every `LABEL: value` line of the
description. Every label seen lands in `raw_attributes`; the recognised ones also
feed typed fields. Related-product cards reuse the same microdata inside
`.p-detail`, so every lookup rejects elements nested in a `[data-micro="product"]`
card. One `[itemprop=offers]` block is rendered per purchasable variant, in the same
order as the weight select's options, which is how variants get their grammage.

The shops show both extremes: kavypitel fills in a rich parameter table, redfawn
states almost nothing there and everything in the description. A label is matched
exactly first and then only on whole words, and only when it is short enough to be
a parameter name — otherwise a sentence such as *"Objednávky nad 1 kg zasíláme
zdarma"* would claim the weight field. Mapped values are sanity-checked too: a
weight must parse to 50–5000 g, a process may not be a paragraph, and a country
must resolve to an ISO code; anything that fails stays in `raw_attributes` only.

## Adding a shop

### (a) A bespoke site — one module

Create `src/coffee_aggregator/sites/<shop>.py`:

```python
from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from coffee_aggregator.sites.registry import register


@register
class MyShop(SiteAdapter):
    site_id = "myshop"
    name = "My Roastery"
    country = "CZ"
    base_url = "https://myshop.cz"

    def discover(
        self, fetcher, *, max_pages=None
    ): ...  # yield ProductRef(...) per product; use fetcher.get()/fetch_many()

    def parse_product(
        self, html, ref
    ): ...  # pure: no HTTP; return a Coffee, or None only for non-coffee items
```

`sites/loader.py`'s `load_all()` (re-exported as `sites.load_all`) imports every
module under `sites/` and `platforms/`, so the `@register` decorator is the whole
wiring. Per-shop strings live in that module —
never in a shared constants dump. Adapters must be constructible with no arguments,
and `max_pages` is a *per-run argument*, never written onto the singleton adapter.
A `ProductRef` may carry the product's own source in `payload`; the pipeline then
parses it directly and spends no request, which is what a JSON-API or feed-driven
shop needs.

### Switching a shop off

```toml
disabled = "TLS certificate does not match the domain, 2026-10-02"
```

Discovery skips the shop and says so at INFO; it never reaches the registry,
`list-sites` or a crawl, and it is not a load error, so it does not fail the
run. Use it when a shop becomes unreadable through no fault of the config — its
site moves to a platform the adapters cannot parse, or its certificate stops
matching its domain — because two shops that write nothing every night turn the
nightly exit code into an alarm nobody reads.

The value is the reason, not a boolean. A shop is only ever switched off for
something that will have to be re-read later, and deleting the file instead
would take the label map and ignore list with it.

### (b) A shop on a supported platform — one TOML file

Drop `src/coffee_aggregator/sites/configs/<shop>.toml`:

```toml
platform = "shoptet"
site_id = "myshop"
name = "My Roastery"
country = "CZ"
base_url = "https://myshop.cz"
category_urls = ["https://myshop.cz/zrnkova-kava/"]
currency = "CZK"
max_pages = 50
pagination = "path"                      # "path" -> /strana-2/, "query" -> ?page=2
ignore = ["cascara", "tasting pack"]     # extra non-coffee markers, folded

[label_map]                              # only for labels the built-in map misses
"Bližší určení" = "region"
```

No Python at all. `platforms/loader.py`'s `build_from_config()` resolves `platform`
by convention
to `coffee_aggregator.platforms.<name>:build` and imports it lazily, so adding a
platform is one module and no registration, and a platform nobody configured is
never loaded.

`label_map` values are field names the platform understands — `country`, `region`,
`farm`, `producer`, `washing_station`, `altitude`, `variety`, `harvest`, `process`,
`roast`, `brewing`, `flavor_notes`, `sca_score`, `species`, `decaf`, `weight`,
`certifications`, `body`, `bitterness`, `acidity`, `sweetness` — or `""` to keep a
label out of the typed fields while still recording it in `raw_attributes`. They are
validated against `KNOWN_FIELDS` when the file is loaded, so a typo is an error that
names the label instead of a mapping that silently does nothing. The built-in
`DEFAULT_LABEL_MAP` in `platforms/shoptet.py` already covers the common CZ/SK/EN
parameter names, so most shops need no `label_map` at all.

## Data model and schema

`models.Coffee` is the canonical record (see `src/coffee_aggregator/models.py`):
identity, price and weight, `Origin`, `Processing`, `Roast`, `Species`, `Taste`,
`Popularity`, variants, images, tags, categories, certifications, awards and the
`raw_attributes` catch-all. The model itself stores no serialisation:
`sinks/records.py` flattens it, and `coffee_record()` emits exactly the data
columns of the `coffee` table, so a sink never reshapes anything.

`Processing` keeps a list: a lot marked *"Washed · Natural"* really was processed
both ways, so `processing.methods` holds both and `processing.method` becomes
`ProcessMethod.MIXED`. Page-level metadata (`OG_TITLE`, `OG_DESCRIPTION`,
`META_DESCRIPTION`, `META_KEYWORDS`) and the listing card's own strings (prefixed
`LIST_`) join `raw_attributes`, so nothing a shop showed us is lost.

Five tables:

* `coffee` — one row per `(site, external_id)`, with `first_seen_at`,
  `last_seen_at` and `delisted_at` bookkeeping.
* `coffee_variant` — one row per weight/price variant of a product.
* `price_history` — one row per product per crawl, for price tracking over time.
* `fx_rates` — one EUR/CZK fixing per day (see below).
* `crawl_run` — one row per shop per crawl (see [Run history](#run-history)).

The sink's `COLUMNS` tuple is the single source of truth for the INSERT, and a test
replays every migration to assert the two agree — with a second check against
`information_schema.columns` in the integration suite, so the code, the DDL and the
live database cannot drift apart.

## Migrations

The DDL lives in numbered files under `src/coffee_aggregator/db/migrations/` and is
applied by the dependency-free runner in `src/coffee_aggregator/db/migrate.py`.

**There is exactly one file today, `0001_initial.sql`, and that is deliberate.**
Nothing has ever been deployed — the only databases are a developer's laptop and
the throw-away one the integration tests own, and a full catalogue is re-crawled in
under a minute — so the incremental files describing the road to a shape nobody ran
were collapsed into it. Until the first real deployment, a schema change is an edit
to that file followed by a rebuild:

```bash
dropdb coffee && createdb -O coffee coffee && coffee-aggregator init-db
```

The runner records a checksum, so an edited file is *rejected* on a database that
already applied it (`MigrationChecksumError`) rather than silently skipped — a
rebuild is the only way through, which is exactly the property that makes the
collapse safe to stop doing later. **From the first real deployment onwards** every
change is a new numbered file beside it and nothing is ever edited in place.

The runner itself:

* a `schema_migrations(version, checksum, applied_at)` table records what has run,
  and a session-level advisory lock keeps two Lambdas from applying the same file;
* pending files are applied in lexical order — hence the zero-padded prefix —
  each one in a single transaction together with the row that records it, so a
  crash can never leave a version half applied;
* `apply_migrations(conn)` returns the versions it applied, `pending(conn)` the
  ones it would; `PostgresSink.init_schema()` and `coffee-aggregator init-db` are
  thin wrappers over them.

The files are read with `importlib.resources`, so `init-db` works from an
installed wheel exactly as it does from a checkout. Every statement is still
`IF NOT EXISTS`, so `0001_initial` is a no-op on a database created before the
migrations existed and only records its version.

**Adding one** (once something is deployed): drop `000N_what_it_does.sql` in that
directory and re-run `init-db`. If it adds a column to `coffee`, add it to `Coffee`,
to `coffee_record()` and to `COLUMNS` in the same commit — the drift test fails
otherwise. `init-db --dry-run` prints what is pending without touching anything.

## Run history

Every shop of every crawl appends one row to `crawl_run`: when it started and
finished, how long it took, every counter of its `RunReport` (`discovered`,
`fetched`, `parsed`, `skipped_non_coffee`, `failed`, `disallowed`, `written`,
`delisted`), whether it was `complete`, whether `discovery_ok`, whether it stopped
on its `deadline_reached`, its first fifty errors as `jsonb`, and the command line
it was started with. `src/coffee_aggregator/db/monitoring.py` owns the table;
`NullMonitor` stands in when the run has no database (`--sink jsonl`), so a
debugging crawl into a flat file never needs a DSN.

The monitor keeps its own connection rather than borrowing the sink's: the sink's
is held across a batch upsert and committed with it, and the row saying "this shop
wrote nothing" matters most exactly when the writes are what failed. A failure to
record is logged and swallowed — monitoring that can take the crawl down with it is
worse than no monitoring.

```bash
coffee-aggregator runs                      # the last 20 runs, newest first
coffee-aggregator runs --site coffeein --limit 5
```

**Which shops returned nothing today** — the morning-after query:

```sql
SELECT site,
       max(finished_at) AS last_finished,
       bool_and(discovery_ok) AS discovery_ok,
       sum(written) AS written
FROM crawl_run
WHERE started_at >= date_trunc('day', now())
GROUP BY site
HAVING sum(written) = 0
ORDER BY site
```

`tests/test_monitoring.py` runs that exact text against a live database and asserts
this file still contains it, so the runbook cannot drift from what was tested.

## Sharding the daily run

`--shard I/N` crawls one deterministic slice of the registered shops, so a nightly
job can be spread over N workers with a fifteen-minute ceiling each.

* **Split by host, not by shop.** Two shops on one origin always land in the same
  shard: the rate limiter is keyed by host, so splitting them would have each shard
  pace that origin on its own and double the request rate the shop sees.
* **Balanced by estimated cost, not by count.** A host costs
  `requests × max(--delay, its crawl-delay)`, where `requests` is estimated as
  `max_pages × (1 + 24)` — every listing page the adapter may walk, plus the detail
  pages a page of that size holds. Hosts are then packed longest-first into the
  emptiest shard. Balancing by count would put fifty fast shops in one shard and the
  one slow shop in another, which is the split that misses the deadline.
* **Crawl-delays are known up front.** robots.txt is the authority and the fetcher
  always re-reads it, but a shard has to be weighed *before* the first request, so
  the surveyed values live in `config.KNOWN_CRAWL_DELAYS` and `COFFEE_AGG_CRAWL_DELAYS`
  adds to them.

**caffeoro.sk declares `Crawl-delay: 30`.** At five listing pages that is an
estimated ~3 750 s of request time — on its own, more than four times a Lambda's
fifteen-minute ceiling, so *no* split of any N can make it fit. Sharding therefore
does the one useful thing available: it gives that host a shard to itself, keeps it
out of the others, and `--deadline` makes the shard that holds it stop cleanly and
be reported incomplete, which never delists. Getting its whole catalogue needs a
separate, longer-budgeted schedule for that one shop; nothing here pretends to
solve a thirty-second crawl-delay.

## Price normalisation

Shops price in CZK or in EUR, so nothing is comparable until both are expressed in
one currency. Every crawl resolves **one** fixing and stamps it on every row it
writes:

* **Source:** the Czech National Bank's daily fixing
  (`.../denni_kurz.txt`), the authoritative CZK reference rate. Its decimal comma
  and per-`množství` quoting (JPY and HUF are published per 100) are handled in
  `fx/cnb.py`. On a weekend or a public holiday the file carries the last working
  day's date, and that date is what gets recorded.
* **Fallback:** the ECB's `eurofxref-daily.xml` (`fx/ecb.py`). If neither feed
  answers, the most recent stored rate is used and a warning states its age; if
  the store is empty the crawl proceeds with the six columns left NULL. The FX
  layer never raises into a crawl.
* **Once a day:** `FxService.get_rate()` asks the store first and only then a
  feed, so a second crawl on the same day costs no request — and a Saturday reuses
  Friday's fixing, because the bank publishes nothing over a weekend. Both feeds go
  through the same `PoliteFetcher` as the shops: robots.txt, delay and jitter apply
  to the banks too.
* **Storage:** `fx_rates(date, base, quote, rate, source, fetched_at)` with the
  postgres sink, or a JSON file (`COFFEE_AGG_FX_CACHE`) for `--sink jsonl`.
* **Columns:** `coffee.price_eur`, `price_czk`, `price_per_kg_eur`,
  `price_per_kg_czk`, `fx_rate_eur_czk`, `fx_date`, indexed on
  `price_per_kg_eur`; `price_history` keeps `price_eur`, `price_czk` and
  `fx_rate_eur_czk` so a historical row stays interpretable; each variant in the
  `variants` jsonb gains `price_eur` and `price_czk`.
* **Where it happens:** the pipeline's `derive()` step, next to `price_per_kg`. No
  adapter ever sees a rate, let alone fetches one.

```bash
uv run coffee-aggregator fx --refresh --dsn postgresql://coffee:coffee@localhost:5432/coffee
# {"date": "2026-09-11", "base": "EUR", "quote": "CZK", "rate": "24.260", "source": "cnb"}
```

## Tests

```bash
uv run pytest -q          # offline: no test touches the network
```

No test touches the network: `tests/test_fetcher.py` exposes a `MockHTTP` registry
served through an [`httpx2.MockTransport`](https://www.python-httpx.org/advanced/transports/),
which `install(monkeypatch)` puts under every fetcher the test builds. Real pages
saved under `tests/fixtures/<site>/` are loaded by the `fixture_html` fixture:

```python
def test_something(fixture_html):
    html = fixture_html("coffeein", "detail_151_cuba.html")
```

Capture a new fixture with `curl` (or `crawl --cache-dir`), drop it in that folder,
and iterate with `coffee-aggregator parse --site <id> --file <that file>`.

### Integration tests

`tests/test_postgres_integration.py` is skipped unless `TEST_DATABASE_URL` is set.
It **drops and recreates** `coffee`, `price_history`, `fx_rates` and
`schema_migrations`, so point it at a throw-away database — never at the one you
crawl into:

```bash
createdb -O coffee coffee_test    # once
TEST_DATABASE_URL=postgresql://coffee:coffee@localhost:5432/coffee_test \
    uv run pytest tests/test_postgres_integration.py -q
```

## Module layout

Every package's `__init__.py` is a thin re-export — the logic lives in a named
module next to it: `sites/loader.py` (discovery), `sites/registry.py` (the
`@register` map), `platforms/loader.py` (platform resolution), `sinks/factory.py`
(the sink name → factory map), `http/fetcher.py` (the only place that makes a
request), `db/migrate.py` (the migration runner), `db/monitoring.py` (the `crawl_run`
recorder), `fx/cnb.py` and `fx/ecb.py` (the
rate feeds), `fx/rates.py` (the once-a-day service), `fx/stores.py` and
`fx/convert.py`, `money.py` (the one place a price is rounded or extrapolated to
the kilogram) and `sinks/records.py` (model → column dictionary, for both
sinks). Nothing in the project starts with a module docstring or a licence
header; `D100`, `D104` and `CPY001` are ignored for that reason.

## Development

```bash
uv run ruff check          # every rule ruff has ("ALL"), including ANN and D
uv run ruff format
uv run mypy                # strict
uv run pytest -q
uv run pre-commit install --install-hooks -t pre-commit -t pre-push
uv run pre-commit run --all-files
```

Dependencies are never upper-bounded; refresh them with `uv lock --upgrade && uv sync`.

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:
`lint` (ruff + mypy), `hooks` (`pre-commit run --all-files`), `test` (the whole
suite against a `postgres:18-alpine` service container, so the integration tests
run instead of skipping), `image` (builds `Dockerfile` and checks the image ships
every shop the working tree has) and `diff-summary` (classifies the pull
request's files into the job summary). See [docs/ci.md](docs/ci.md) for the one-command local
equivalent of each job.

`tests/test_repo_hygiene.py` keeps `tests/fixtures/` honest: every file stays
under 300 KB, and every fixture directory must be named by some test module.
