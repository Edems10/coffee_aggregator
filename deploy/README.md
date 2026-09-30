# Running this on your own server

One host runs both the database and the crawler, in Docker. A systemd timer
starts the crawl every morning; when it finishes, the same run dumps the
database, uploads the dump to Google Drive with rclone, and keeps the two
newest dumps in both places.

The database publishes no port. Both containers share one compose network, so
the crawler reaches it as `db:5432` and nothing outside the host can reach it at
all.

## What the host needs

Docker with the compose plugin, git, systemd and rclone. It does **not** need
`postgresql-client`: `pg_dump` runs inside the database container, so the dump
is always taken by the same major version that wrote the data.

```bash
sudo apt install rclone            # the only missing piece on a stock Ubuntu 24.04
```

## Install

```bash
sudo git clone https://github.com/Edems10/coffee_aggregator /opt/coffee-aggregator

sudo install -d -m 700 /etc/coffee-aggregator
sudo install -m 600 /opt/coffee-aggregator/deploy/env.example /etc/coffee-aggregator/env
sudo sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -base64 24)|" \
    /etc/coffee-aggregator/env
sudo nano /etc/coffee-aggregator/env          # set COFFEE_AGG_CONTACT and RCLONE_REMOTE

sudo install -d -m 700 /var/backups/coffee-aggregator
```

The table browser sits behind your existing nginx-proxy-manager, which runs in
another compose stack, so the two need one shared network. Create it once:

```bash
docker network create proxy
```

Then add that network to the proxy in `/opt/stacks/services/compose.yml`:

```yaml
  nginx-proxy-manager:
    # ... everything already there ...
    networks:
      - default
      - proxy

networks:                 # at the bottom of that file, if it has no networks: yet
  proxy:
    external: true
```

Bring the database up and build the crawler image. The build reads only four
paths from the repo, because `.dockerignore` is an allowlist:

```bash
cd /opt/coffee-aggregator/deploy
sudo docker compose --env-file /etc/coffee-aggregator/env up -d
sudo docker compose --env-file /etc/coffee-aggregator/env --profile crawler build
sudo docker compose --env-file /etc/coffee-aggregator/env run --rm crawler init-db
```

Then check one shop end to end before trusting the schedule:

```bash
sudo docker compose --env-file /etc/coffee-aggregator/env run --rm crawler \
    crawl --site kafista --sink postgres
sudo docker compose --env-file /etc/coffee-aggregator/env run --rm crawler runs
```

## The table browser

`pgweb` runs on the shared `proxy` network and publishes no host port, so the
only way to it is through nginx-proxy-manager. In the NPM admin UI add a proxy
host pointing at `coffee-pgweb` port `8081`, give it a certificate, and turn on
websocket support.

It is locked to one database — `--lock-session` means the connection cannot be
pointed anywhere else from the browser — and it asks for the basic-auth
credentials from the env file on top of whatever the proxy requires. It is
read-write: rows can be edited from there, which is the point, but it is also
why it should never be exposed without both locks.

## Google Drive

`rclone config` needs a browser once, which a server does not have. Do the
consent step on a machine that has one:

```bash
rclone authorize "drive"          # on your laptop; copy the token it prints
```

Then on the server, `sudo rclone config` → `n` → name it `gdrive` → `drive` →
leave client id and secret empty → scope `drive.file` (it may only touch files
it created) → answer **no** to "Use auto config" → paste the token.

Check it before relying on it:

```bash
sudo rclone mkdir gdrive:coffee-aggregator
sudo rclone lsd gdrive:
```

`RCLONE_REMOTE` in the env file must match that remote and folder. Leave it
empty to keep backups on this machine only.

Note that rclone stores the token in root's `~/.config/rclone/rclone.conf`,
because the timer runs as root. A token configured under your own user will not
be found.

## Schedule it

```bash
sudo cp /opt/coffee-aggregator/deploy/coffee-aggregator.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coffee-aggregator.timer
systemctl list-timers coffee-aggregator.timer
```

Run it once by hand rather than waiting for 05:00:

```bash
sudo systemctl start coffee-aggregator.service
journalctl -u coffee-aggregator.service -f
```

## How long it takes, and what the exit code means

`--site-workers` is how many shops run at once; `--workers` is threads inside
one shop, where two is the measured optimum because the per-host crawl delay is
the real bound. Eight shops at once puts the estimated worst case under two
hours. The floor is caffeoro.sk, which declares `Crawl-delay: 30` in its
robots.txt and takes about an hour on its own — every other shop is long
finished by then.

`--deadline` is a ceiling, not a duration: the run ends when the last shop
finishes. A run that does hit the deadline is recorded as incomplete and
**delists nothing**, so a truncated night can never make products look
discontinued.

The unit fails when the crawl reports that some shop wrote nothing (exit 1) or
when the backup failed. The backup runs either way — a night where five shops
rotted still produced data worth keeping. `coffee-aggregator runs` says which
shop did what.

To be told about it, add a handler:

```ini
# /etc/systemd/system/coffee-aggregator.service.d/notify.conf
[Unit]
OnFailure=your-notify@%n.service
```

## Backups

The timer starts the crawl at 05:00; the backup runs when the crawl finishes,
so the dump contains that morning's data. Dumps are named by UTC timestamp
(`coffee-20260930T050412Z.dump`), which makes them sort chronologically by
name — the pruning relies on that.

One dump per day, `pg_dump --format=custom --compress=9`, verified with
`pg_restore --list` before it is trusted — a dump cut off half-way still looks
like a file, and that check is the difference between having a backup and
believing you have one. Pruning happens only after a successful upload, so a
failed upload never deletes the copies you still have.

Restoring, into this database or any other Postgres anywhere:

```bash
sudo docker compose -f /opt/coffee-aggregator/deploy/compose.yml exec -T db \
    pg_restore --clean --if-exists --no-owner --no-privileges \
    -U coffee -d coffee < /var/backups/coffee-aggregator/coffee-<stamp>.dump
```

Nothing in the schema is vendor-specific, so the same dump restores onto RDS,
Neon, Aiven or a laptop.

**Two dumps is a two-day window.** Break something and notice it on the third
day and both copies already contain the breakage. For this project that is a
reasonable trade: the catalogue rebuilds itself from one crawl, and the only
irreplaceable tables are `price_history` and `crawl_run`, which are also the
small ones. Raise `KEEP`, or add a weekly copy, if that window feels short.
