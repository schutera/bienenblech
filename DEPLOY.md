# Deploying Bienenblech

Production deployment as a Docker Compose stack: the FastAPI app (uvicorn,
serving the built React frontend) behind a **Caddy** reverse proxy that
terminates TLS. There is no model and no GPU anywhere in this — the image is
python:3.11-slim plus Pillow, DuckDB and FastAPI, and it boots in seconds.

```
             :80/:443                     :8000 (internal)
  browser ───────────▶  Caddy  ─────────────────────▶  bienenblech
                        (auto-HTTPS)                    (uvicorn + SPA)
                                                            │
                                                     /app/data volume
                                    (DuckDB, source images, crop cache, backups)
```

## What's in the box

| file | role |
|------|------|
| `Dockerfile` | 2-stage build: Node builds `frontend/dist`, Python runtime serves it. One apt package (`curl`, for the healthcheck). |
| `docker-compose.yml` | `bienenblech` + `caddy`, `./data` bind mount, secrets from `.env`. The standalone story. |
| `docker-compose.shared.yml` | Override for a host that already runs another stack on 80/443: no Caddy, app published on loopback. |
| `Caddyfile` | Reverse proxy, auto-HTTPS, 512 MB request-body ceiling for uploads. |
| `entrypoint.sh` | Re-owns drifted files in `/app/data`, then drops root via `setpriv`. |
| `.env.example` | Secrets template (session key, bootstrap admin, site address, webhook). |
| `config/bienenblech.prod.yaml` | Server config, bind-mounted read-only. |

## This deployment

The concrete target, so nobody has to rediscover it:

| | |
|---|---|
| Domain | `bienenblech.schutera.com` |
| Host | `87.106.3.239` (reachable from VS Code's Remote-SSH) |
| Mode | see below — this host **already runs cownting on :80/:443** |

Because a **host-level Caddy** (a systemd service, not a container — verified on
the box 2026-08-27; it serves both `cownting.schutera.com` and
`highfive.schutera.com`) already owns 80 and 443 there, the standalone
`docker-compose.yml` cannot be used there as-is: the second Caddy would fail to
bind. Use the **co-tenancy** path below, and add the
`bienenblech.schutera.com` site block to the host's `/etc/caddy/Caddyfile`. Point the
domain's A record at `87.106.3.239` before the first boot — Caddy needs the DNS
to resolve to this host in order to complete the ACME challenge, and a premature
attempt burns Let's Encrypt rate limit.

## Prerequisites

- A Linux server with **Docker** and the **Docker Compose plugin**
  (`docker compose version`).
- For HTTPS: a **domain name** with a DNS `A`/`AAAA` record pointing at the
  server, and ports **80** and **443** open. (Plain HTTP works for an internal
  test — see below.)
- `frontend/package-lock.json` must be committed: both the image build and CI use
  `npm ci`, which refuses to run without it.

## Mode 1 — standalone (its own domain, its own Caddy)

```bash
# 1. Get the code onto the server
git clone <your-repo-url> bienenblech && cd bienenblech

# 2. Secrets
cp .env.example .env
#    - BIENENBLECH_SECRET:          openssl rand -hex 32
#    - BIENENBLECH_ADMIN_PASSWORD:  choose a strong password
#    - CADDY_SITE_ADDRESS:          your domain (e.g. bienenblech.schutera.com)
#    - BIENENBLECH_DISCORD_WEBHOOK: optional; blank is fine
nano .env

# 3. Only if you are NOT using HTTPS: config/bienenblech.prod.yaml ships
#    auth.https_only: true, which is right behind a domain but makes the browser
#    refuse to send the cookie back over plain HTTP — an endless login loop. Set
#    it to false for the plain-HTTP case, and leave it alone otherwise.
nano config/bienenblech.prod.yaml

# 4. Build + start (the build compiles the SPA; there are no model weights to
#    download, so this is a couple of minutes, not twenty)
docker compose up -d --build

# 5. Watch it come up
docker compose logs -f bienenblech
```

### First boot and the bootstrap admin

On the very first boot the users table is empty, and only then does the app
create `BIENENBLECH_ADMIN_USER` / `BIENENBLECH_ADMIN_PASSWORD` as an `admin`.
Log in with those, then change the password. Once any user exists the bootstrap
is skipped, so editing those variables later has no effect — accounts are managed
in the Admin page from then on.

Roles are two: **admin** (users, classes, upload, delete, export, backup) and
**annotator** (label crops, add classes, read). There is no third role.

Check it is really up:

```bash
curl -fsS http://localhost/api/health      # {"ok":true,"version":...,"schema_version":...}
docker compose ps                          # the healthcheck hits /api/health too
```

### Plain HTTP (no domain, quick test)

Leave `CADDY_SITE_ADDRESS=:80` in `.env` **and set `auth.https_only: false` in
`config/bienenblech.prod.yaml`** — it ships `true`, and a Secure cookie over
plain HTTP is never sent back by the browser, so every login appears to succeed
and bounces straight back to the login page. The app is then reachable at
`http://<server-ip>/`. Do not run a public instance this way: the login cookie
travels unencrypted.

## Mode 2 — sharing a server with cownting

`cownting` already binds :80 and :443 on that box, and two Caddies cannot both
have port 80. So bienenblech runs without its own proxy and lets cownting's Caddy
serve its domain:

```bash
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d
```

The override does exactly two things: it puts the `caddy` service into a profile
nobody ever enables (compose cannot delete a service from an override, but a
service carrying an unselected profile is dropped from the resolved model
entirely), and it publishes the app on `127.0.0.1:${BIENENBLECH_PORT:-8001}`.
Confirm before starting:

```bash
docker compose -f docker-compose.yml -f docker-compose.shared.yml config --services
# -> bienenblech          (caddy is gone, and so are the caddy_* volumes)
```

`CADDY_SITE_ADDRESS` and this repo's `Caddyfile` are unused in this mode.

### Wiring cownting's Caddy to it

**As found on 87.106.3.239 (2026-08-27): cownting's Caddy is *not* a container.**
Caddy v2.11.4 runs on the host as a systemd service (`/usr/bin/caddy`, config
`/etc/caddy/Caddyfile`), cownting's bundled caddy service is disabled by a
server-side `docker-compose.override.yml` (profile `disabled`), and the cownting
app is itself published on `127.0.0.1:8090` for the host Caddy to reach. On that
box, therefore, **skip the network join below** and use the host-proxy variant at
the end of this section: `reverse_proxy 127.0.0.1:8001` in `/etc/caddy/Caddyfile`.

What follows is the generic recipe for a box where the fronting Caddy *is* a
container: `127.0.0.1` inside it would then be the container, not the host — and
the loopback publish above is deliberately *not* reachable from the docker
bridge, so the two containers must share a network instead. In cownting's
directory, in `docker-compose.override.yml` (on 87.106.3.239 that file already
exists at `/opt/cownting/docker-compose.override.yml` and carries cownting's
port publish and resource limits — append, never replace):

```yaml
services:
  caddy:
    networks: [default, bienenblech]

networks:
  bienenblech:
    external: true
    # `docker network ls` — compose names it <project>_default, and the project
    # defaults to the directory name of this repo.
    name: bienenblech_default
```

Then add this site block to whichever Caddyfile is actually in front — on
87.106.3.239 that is the host's `/etc/caddy/Caddyfile`, with
`reverse_proxy 127.0.0.1:8001` in place of `reverse_proxy bienenblech:8000` —
and reload it. Host Caddy:
`caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`.
Containerized Caddy:
`docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile`.

```
bienenblech.schutera.com {
	# Same reasoning as this repo's Caddyfile: frames, not video. The app enforces
	# the real per-file limit (upload.max_mb) and answers 413 itself.
	request_body {
		max_size 512MB
	}

	encode zstd gzip
	reverse_proxy bienenblech:8000
}
```

Point the domain's DNS at the same server and Caddy issues the certificate on
first request, exactly as it does for cownting's own domain.

When the fronting proxy runs **on the host** (host nginx, host Caddy) rather
than in a container — **which is the case on 87.106.3.239** — skip the network
join: `reverse_proxy 127.0.0.1:8001` reaches the published port directly, exactly
as the host Caddy already reaches cownting at `127.0.0.1:8090`. That is what the
loopback publish is for; it is also how you `curl` the app from the host to debug.

### Switching between the two modes

Bring the stack down with the *same* file combination you brought it up with, or
with the base file alone if you need Caddy removed too:

```bash
docker compose down                      # base file: stops app + caddy
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d
```

Data survives either way — it is a bind mount, not a compose volume.

## Operating it

```bash
docker compose ps                       # status + health
docker compose logs -f bienenblech      # app logs (uploads, tiling, backup runs)
docker compose restart bienenblech      # restart just the app
docker compose down                     # stop (./data is untouched)
```

Upgrading:

```bash
git pull
docker compose up -d --build            # rebuilds the image, recreates containers
```

The schema migrations are additive and idempotent (`ADD COLUMN IF NOT EXISTS`),
so a new version opens the existing DuckDB file in place. Add the shared-mode
`-f` flags to every one of these commands if you deployed in mode 2.

### Managing accounts from the host

Normally: the **Admin page** in the app (create/delete users, reset passwords,
change roles). The API refuses to delete the last admin, so you cannot lock
yourself out that way.

If you *are* locked out, the CLI runs inside the container:

```bash
# -u bienenblech: exec sessions default to root (the image boots as root so the
# entrypoint can heal data/ ownership); run maintenance as the app user instead,
# or you will leave root-owned files in the bind mount.
docker compose exec -u bienenblech bienenblech \
  python -m bienenblech.cli --help
```

Last resort: the bootstrap in `.env` only fires when the users table is empty, so
recovering an admin means going through the CLI, not through a restart.

## Data & backups

**All persistent state is the `./data` directory** (bind-mounted to `/app/data`):

| path | what it is | regenerable? |
|---|---|---|
| `data/bienenblech.duckdb` | users, images, crops, classes, masks, audit | **no** |
| `data/images/<image_id>.jpg` | the stored derivative every mask refers to | **no** |
| `data/backups/*.zip` | rotated backup zips | no (but they are copies) |
| `data/cache/crops/<crop_id>.jpg` | crop tiles rendered on demand | yes — safe to delete |

The DuckDB file and `data/images/` are the only irreplaceable things on the box.
Everything else is either a copy or a cache.

The in-app backup zips both of them on a schedule (`backup.interval_days`) into
`data/backups/`, keeps the last `backup.keep`, and posts to Discord if
`BIENENBLECH_DISCORD_WEBHOOK` is set. It is not a substitute for having the
directory itself backed up off-box:

```bash
docker compose stop bienenblech
tar czf bienenblech-data-$(date +%Y%m%d).tgz data/
docker compose start bienenblech
```

`.env` holds the session secret and the admin bootstrap — back it up out of band.
Neither `.env` nor `data/` is in git.

### Manual backup and status

```
POST /api/backup/run        (admin)  -> runs one now, returns the run summary
GET  /api/backup/status              -> last run, next due, recent history
```

A run that finds the store busy records `skipped` and arms no cooldown; a genuine
failure (disk full, webhook unreachable) records `failed`, prints a
`[bienenblech.alert] BACKUP` line and holds off for six hours without advancing
the watermark. So `failed` in `/api/backup/status` means look at the logs;
`skipped` means it will try again shortly.

### Restoring from a backup zip

The zip contains `bienenblech.duckdb`, the flat `masks.csv` / `classes.csv` /
`crops.csv` exports, `images/`, and a manifest.

```bash
docker compose stop bienenblech

unzip bienenblech-backup-YYYYMMDD.zip -d /tmp/restore
cp /tmp/restore/bienenblech.duckdb data/bienenblech.duckdb
cp -r /tmp/restore/images/. data/images/
rm -rf data/cache/crops                  # stale tiles; re-rendered on demand

docker compose start bienenblech         # entrypoint re-owns whatever root just wrote
```

The final line matters: those `cp`s ran as root on the host, so the restored files
are root-owned inside a directory the app writes to as uid 10001. The entrypoint
fixes that on the next boot, which is precisely why it exists — but only on a
boot, so restore by stopping and starting, never by copying into a running
container.

The CSVs are there for the day DuckDB cannot open the file at all: they carry
every mask, class and crop in a format that outlives the database.

### File ownership in `data/`

The app runs as uid **10001** inside the container and `data/` is a host bind
mount, so a root-run host tool that writes into it leaves files the app cannot
overwrite, which surfaces later as a 500 (`PermissionError`) on some unrelated
save. Two layers of defence:

- **Prefer `docker compose exec -u bienenblech bienenblech …`** for anything that
  touches `data/` — a plain `exec` enters as root, which is exactly the mistake
  this section is about.
- **The entrypoint self-heals on boot**: anything in `/app/data` not owned by the
  app user is re-owned before the server starts, then privileges drop. A
  root-owned stray at worst breaks things until the next
  `docker compose restart bienenblech`.

## Notes

- **Upload size.** Caddy caps a request body at 512 MB (this repo's `Caddyfile`,
  or the site block above in shared mode); the app caps each *file* at
  `upload.max_mb` (default 200) and answers 413. If large multi-file uploads get
  cut off, it is the Caddy number you need to raise, and you must raise it in
  whichever Caddyfile is actually in front of the app.
- **`upload.max_edge` is frozen after first use.** Masks are stored in the
  coordinate space of the *stored derivative*. Changing that value later would
  silently re-scale where every existing polygon lands. Decide it before the
  first upload.
- **Crop parameters are per image.** `crop.size` / `crop.overlap` are recorded on
  each image at upload time, so changing the config affects only new uploads;
  existing work is never re-tiled.
- **The image ships no tests.** The container runs `serve`; CI
  (`.github/workflows/tests.yml`) is the gate that runs pytest and the frontend
  typecheck/build. A green build is what makes `docker compose up -d --build`
  safe.
