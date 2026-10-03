# Running this on your own server

One host runs both the database and the crawler, in Docker. A systemd timer
starts the crawl every morning; when it finishes, the same run dumps the
database and uploads it off-site with rclone, keeping a week of dumps on the
machine and a locked, server-immutable history on the remote.

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
# hex, not base64: a "/" in the password ends the URL's authority early, and the
# DSN then reads the host as "coffee" and the rest of the password as the port.
sudo sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
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

## Off-site copy: Cloudflare R2

R2 is S3-compatible, so rclone talks to it with a static key pair that does not
expire and needs no browser. That is the whole reason to prefer it here over a
consumer cloud drive: a headless server should not depend on an OAuth refresh
token that a provider can invalidate. The free allowance is 10 GB with no egress
charge and no expiry, which is orders of magnitude more than these dumps need —
but Cloudflare does require a payment method on the account before R2 can be
enabled, even to stay inside it.

### What this is defending against

Anyone who can read the key on the server already has root there, and can
destroy the database and every local dump directly. The copy in R2 is the one
thing that could survive that — so the server is deliberately given credentials
that **cannot** remove it. That shapes three settings:

* the API token may read and write objects in one bucket, and nothing else;
* a **bucket lock** refuses deletes and overwrites for a retention period, and
  removing that lock needs permission to edit bucket configuration, which the
  token does not have;
* `backup.sh` therefore writes a new, timestamped object every night and never
  deletes from the remote. Expiry is the bucket's job, not the server's.

### Bucket

**R2 Object Storage** → create a bucket, location Automatic. The name is what
goes after the colon in `RCLONE_REMOTE`.

Then on that bucket:

* **Settings → Bucket lock** → add a rule, retention **Age**, 7 days. Nothing
  written can be deleted or replaced for a week, by anyone holding the server's
  key.
* **Settings → Object lifecycle rules** → delete objects older than 14 days.
  This has to be longer than the lock, or the lifecycle rule has nothing it is
  allowed to remove.

Fourteen dumps of tens of megabytes is a rounding error against 10 GB.

### Token

**R2 → API → Manage API tokens → Create Account API token**. Permission
**Object Read & Write** — not Admin, which could edit bucket configuration and
therefore lift the lock. Under *Specify bucket(s)* choose **Apply to specific
buckets only** and pick the one bucket. TTL **Forever**: an expiring token would
break the backup silently, which is the failure mode this whole setup exists to
avoid.

Keep the **Access Key ID**, the **Secret Access Key** and the **S3 API**
endpoint, which looks like `https://<account-id>.r2.cloudflarestorage.com`.

Optionally restrict the token to the server's public IP — worth it only if that
address is actually static, since a changed IP stops the upload as surely as a
revoked key.

### rclone

As root on the server, because that is who the timer runs as; a remote
configured under your own user will not be found:

```bash
sudo rclone config create r2 s3 \
    provider=Cloudflare \
    access_key_id=YOUR_ACCESS_KEY_ID \
    secret_access_key=YOUR_SECRET_ACCESS_KEY \
    endpoint=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com \
    region=auto \
    no_check_bucket=true
```

`no_check_bucket=true` matters: a token scoped to one bucket cannot call
HeadBucket, so rclone's normal "does this bucket exist" check fails and takes
every upload down with it.

```bash
sudo rclone ls r2:YOUR_BUCKET
sudo chmod 600 /root/.config/rclone/rclone.conf
```

`RCLONE_REMOTE` in the env file must match the remote and bucket. Leave it empty
to keep backups on this machine only.

Any other rclone remote works the same way — the script only ever uses the name
— so Backblaze B2 or S3 need no change beyond that one variable. A Google Drive
remote also works, but authenticates with an OAuth refresh token rather than a
static key, and unless the Google Cloud consent screen is *published* rather
than left in testing, Google expires that token after seven days and the upload
stops without the crawl noticing.

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

## Updating

```bash
sudo /opt/coffee-aggregator/deploy/update.sh            # pull, rebuild, migrate
sudo /opt/coffee-aggregator/deploy/update.sh --crawl    # …and crawl now
```

Three steps in the one order that works. The middle one is the step that is easy
to forget and silent when forgotten: the containers run an image that was built
once, so `git pull` on its own changes nothing and the crawl keeps running
yesterday's code. The last one has to come before any crawl, because a sink
writing a column the schema does not have yet fails the whole run.

It refuses to do anything if the checkout has uncommitted changes, and pulls
`--ff-only`, so a server someone edited by hand stops with a readable message
instead of conflicting half way through a deployment. Both the rebuild and
`init-db` are idempotent — running it when nothing has changed costs a few
cached seconds and prints that there was nothing to apply.

Parsing fixes do not touch rows already in the database; they take effect on the
next crawl, which upserts. So the numbers move the morning after, or straight
away with `--crawl`.

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
shop did what, and the report below says what it means.

To be told about it, add a handler:

```ini
# /etc/systemd/system/coffee-aggregator.service.d/notify.conf
[Unit]
OnFailure=your-notify@%n.service
```

## The morning report

`run.sh` runs `coffee-aggregator report --format text` between the crawl and the
backup, so the night's summary is in the journal without anyone asking:

```bash
journalctl -u coffee-aggregator.service -b --since today | grep '^report'
```

A clean night is one line — `report 2026-10-02: nothing to report`. Otherwise it
leads with the verdict, lists every serious finding and caps the rest.

**It cannot fail the run and cannot change the exit code.** The crawl's own exit
1 is the alarm; the report is the explanation, and a night that already went
badly must not be failed twice. Both invocations end in a way that leaves `$?`
alone, which is the same reasoning that keeps the backup out of an
`ExecStartPost=`: a step that exists to describe the data should never be able
to take down the unit that stored it. Nothing there can hang the run either —
every connection carries a 60-second `statement_timeout`.

The fuller formats are run by hand against the same day:

```bash
docker compose -f deploy/compose.yml run --rm -T crawler \
    report --day 2026-10-02 --format markdown     # to paste into a chat
```

### Serving it as a page

one standalone HTML file — styles inline, no CDN, no fonts, no images, so it
renders from a `file://` path with no network at all:

```ini
```

whole, so a failed render leaves yesterday's page readable instead of truncating
it to nothing. Unset the variable and no page is written.

To serve it, how it is served, the same proxy
that already fronts pgweb. **It is a static file on purpose.** The database
publishes no port and nothing outside the compose network can query it, which is
the reason the data is kept on this machine at all; a web tier would need
credentials and would be one more thing listening. A post-crawl report is a
snapshot by nature, and pgweb is what ad-hoc querying is for.

```bash
mkdir -p /var/www/coffee && chown root:root /var/www/coffee
```

## Backups

The timer starts the crawl at 05:00; the backup runs when the crawl finishes,
so the dump contains that morning's data. Dumps are named by UTC timestamp
(`coffee-20260930T050412Z.dump`), which makes them sort chronologically by
name — the pruning relies on that.

One dump per day, `pg_dump --format=custom --compress=9`, verified with
`pg_restore --list` before it is trusted — a dump cut off half-way still looks
like a file, and that check is the difference between having a backup and
believing you have one.

On the remote, one new object per night, never an overwrite and never a delete
from this side — see the R2 section above for why. Old ones are expired by the
bucket's own lifecycle rule. A half-finished upload leaves one bad object that
the next night supersedes, and every good one before it is untouched.

Locally, `KEEP` dumps are kept (seven by default), newest first, and the
pruning runs only after the upload succeeded — a failed upload never deletes
the copies you still have.

`BACKUP_DIR` defaults to `/var/backups/coffee-aggregator`, which on a stock
Ubuntu is the same filesystem as `/var/lib/docker` — so the database and its
backups would share a disk, and one failure would take both. If the host has
more than one disk, point it at the other one. Which disk is which:

```bash
findmnt -no SOURCE,TARGET /var/lib/docker /var/backups
lsblk -o NAME,ROTA,SIZE,TYPE,MOUNTPOINT,MODEL     # ROTA=1 is spinning, 0 is SSD
```

Size is not the deciding factor: seven dumps are tens of megabytes at a
realistic catalogue size. Separation is.

Restoring, into this database or any other Postgres anywhere:

```bash
sudo docker compose -f /opt/coffee-aggregator/deploy/compose.yml exec -T db \
    pg_restore --clean --if-exists --no-owner --no-privileges \
    -U coffee -d coffee < /var/backups/coffee-aggregator/coffee-<stamp>.dump
```

Nothing in the schema is vendor-specific, so the same dump restores onto RDS,
Neon, Aiven or a laptop.

From the remote, when the machine itself is what you lost:

```bash
rclone lsf r2:YOUR_BUCKET                       # newest name sorts last
rclone copyto r2:YOUR_BUCKET/coffee-<stamp>.dump ./restore.dump
```

**How far back you can go is two weeks**, and only one of those is safe from
the server itself: a week of local dumps, and up to fourteen days on the remote
of which the newest seven cannot be deleted or replaced by anything holding the
server's key. Raise the lock retention and the lifecycle age together if you
want longer — they are two numbers in the bucket settings and the script does
not need to change.
