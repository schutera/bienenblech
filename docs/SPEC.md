# Bienenblech — build contract

Minimal, single-camera **polygon segmentation labeling tool**. Sibling of
`d:\Projects\cownting` (same stack, same house style) but deliberately leaner:
no video, no detector, no pipeline, no agreement statistics.

> **Read this file before writing any code.** It is the frozen contract between
> the parallel workstreams: the DDL, the HTTP surface, the TypeScript types and
> the file-ownership map are all binding. If you believe a clause is wrong, say
> so in your final report — do not silently deviate, because someone else is
> coding against it right now.

---

## 1. The idea, and why crops

A user uploads a **single full-resolution frame**. The server immediately
**tiles it into fixed-size crops** (default 640x640). The crop — not the frame —
is the unit of work and the unit of training.

That is the load-bearing decision in this project, so it is worth stating why:
we intend to train **YOLO11-seg** on this data later. YOLO-seg training assumes
every instance visible in a training image is labeled; an unlabeled bee on a
sheet is not "missing data", it is an explicit *background* teaching signal that
actively trains the model to suppress true positives. On a 4000x3000 frame with
hundreds of instances, exhaustive labeling is not realistic in one sitting, and a
half-labeled frame is worse than no frame at all. On a 640x640 crop it *is*
realistic — so the invariant becomes enforceable:

> **A crop is `done` only when every instance of every known class inside it has
> a polygon.** Only `done` crops are exported.

The UI must state this to the annotator in plain words, prominently, on the
labeling screen — not buried in a tooltip. Wording along the lines of:
*"Label every instance in this crop before marking it done. A missed instance
teaches the model that it is background."* A crop with genuinely nothing in it is
marked **empty** — that is a valid and valuable negative sample, not a skip.

## 2. Stack (identical to cownting; do not innovate here)

- Python 3.11, **FastAPI** + uvicorn, **DuckDB** (`data/bienenblech.duckdb`),
  **Typer** CLI (`python -m bienenblech.cli`), **Pillow** for image work.
- **React 19 + Vite + Tailwind v4 + react-router-dom v6**, TypeScript.
  FastAPI serves the built SPA from `frontend/dist` at `/`.
- Docker Compose, app behind **Caddy** (auto-HTTPS), one `./data` bind mount
  holding all state.
- Style: EB Garamond (display) + Geist Mono + system sans, warm-paper palette.
  Copy the `@theme` token block from
  `d:\Projects\cownting\frontend\src\index.css` verbatim and keep the names.
- Auth: port `d:\Projects\cownting\cownting\auth.py` (stdlib scrypt, DuckDB
  `users` table, Starlette SessionMiddleware cookie). Roles collapse to two:
  **`admin`** (everything: users, classes, upload, delete, export, backup) and
  **`annotator`** (label crops, add classes, read). No third role.

Env-var prefix is `BIENENBLECH_` throughout (`BIENENBLECH_SECRET`,
`BIENENBLECH_ADMIN_USER`, `BIENENBLECH_ADMIN_PASSWORD`,
`BIENENBLECH_DISCORD_WEBHOOK`). Secrets are read from the environment at the
point of use, **never** from YAML — `config/` is committed and bind-mounted `:ro`.

## 3. Coordinates — read this twice

- **The DB stores polygon points in SOURCE-IMAGE pixel coordinates** (floats,
  origin top-left of the full uploaded frame). This survives a change of tiling
  parameters and lets a future full-frame view render every mask.
- **The HTTP API transmits polygon points in CROP-LOCAL pixel coordinates**
  (origin top-left of the crop, so `0..crop.w`, `0..crop.h`). The frontend never
  performs the offset; the backend adds `crop.x, crop.y` on write and subtracts
  it on read.
- On write the backend **clamps** every vertex into the crop rect. An instance
  clipped by a tile edge is correct and expected for YOLO-seg training.
- Minimum 3 vertices. Self-intersecting polygons are accepted (annotators make
  them; the exporter does not care). No holes — YOLO-seg has no hole concept, so
  do not model them.

## 4. Database — `data/bienenblech.duckdb`

DuckDB DDL. Follow cownting's `db.init_db` ordering discipline: sequences first,
then `CREATE TABLE IF NOT EXISTS`, then indexes; every migration is additive and
idempotent (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`). Ids are uuid4 hex TEXT
unless stated otherwise.

```sql
users (                              -- ported from cownting/auth.py
  username       TEXT PRIMARY KEY,
  password_hash  TEXT NOT NULL,      -- scrypt$N$r$p$salt$hash
  role           TEXT NOT NULL,      -- 'admin' | 'annotator'
  created_at     TIMESTAMP NOT NULL
)

images (
  image_id     TEXT PRIMARY KEY,
  filename     TEXT NOT NULL,        -- sanitised original upload name
  sha256       TEXT NOT NULL,        -- of the ORIGINAL bytes; re-upload dedupe key
  width        INTEGER NOT NULL,     -- of the STORED derivative
  height       INTEGER NOT NULL,
  stored_path  TEXT NOT NULL,        -- data/images/<image_id>.jpg
  bytes        INTEGER NOT NULL,
  crop_size    INTEGER NOT NULL,     -- tiling parameters frozen at upload time,
  crop_overlap DOUBLE  NOT NULL,     -- so changing the config never re-tiles old work
  uploaded_by  TEXT,
  uploaded_at  TIMESTAMP NOT NULL,
  note         TEXT
)

crops (
  crop_id      TEXT PRIMARY KEY,     -- '<image_id>_r<row>c<col>'  (deterministic)
  image_id     TEXT NOT NULL,
  row_idx      INTEGER NOT NULL,     -- 'row'/'column' are reserved-ish; use row_idx/col_idx
  col_idx      INTEGER NOT NULL,
  x INTEGER NOT NULL, y INTEGER NOT NULL,   -- rect in SOURCE-image px
  w INTEGER NOT NULL, h INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'done'
  is_empty     BOOLEAN NOT NULL DEFAULT FALSE,-- marked as containing no instances
  completed_by TEXT,
  completed_at TIMESTAMP,
  UNIQUE (image_id, row_idx, col_idx)
)

label_classes (
  class_id    TEXT PRIMARY KEY,      -- slug of name at creation, stable forever
  name        TEXT NOT NULL UNIQUE,
  color       TEXT NOT NULL,         -- '#rrggbb'
  yolo_index  INTEGER NOT NULL UNIQUE,  -- 0-based, monotonic, NEVER reused or
                                        -- renumbered: an archived class keeps its
                                        -- index so old exports stay readable
  description TEXT,
  archived    BOOLEAN NOT NULL DEFAULT FALSE,  -- soft delete only
  created_by  TEXT,
  created_at  TIMESTAMP NOT NULL
)

masks (
  mask_id    TEXT PRIMARY KEY,
  crop_id    TEXT NOT NULL,
  image_id   TEXT NOT NULL,          -- denormalised for cheap per-image queries
  class_id   TEXT NOT NULL,
  points     JSON NOT NULL,          -- [[x,y],...] SOURCE-IMAGE px, >= 3 pairs
  created_by TEXT,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP,
  deleted    BOOLEAN NOT NULL DEFAULT FALSE   -- soft delete; never hard-DELETE a mask
)

class_audit (                        -- who created/renamed/recolored/archived a class
  audit_id TEXT PRIMARY KEY, class_id TEXT, action TEXT, detail JSON,
  actor TEXT, at TIMESTAMP NOT NULL
)

backup_runs (                        -- shape mirrors cownting/labels_backup.py
  run_id TEXT PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP,
  status TEXT,                       -- 'ok' | 'failed' | 'skipped'
  trigger TEXT,                      -- 'schedule' | 'manual' | 'cli'
  n_masks BIGINT, n_images BIGINT, bytes BIGINT, zip_path TEXT,
  delivered BOOLEAN, error TEXT, host TEXT
)

meta ( key TEXT PRIMARY KEY, value TEXT )   -- 'schema_version', 'backup_watermark'
```

Indexes: `masks(crop_id)`, `masks(image_id)`, `masks(class_id)`,
`crops(image_id)`, `crops(status)`, `images(sha256)`.

**Soft delete everywhere.** Annotator hours are the only thing on this box that
cannot be regenerated. Masks and classes are archived, never dropped. Deleting an
*image* (admin-only, explicit) is the one hard delete, and it must refuse unless
`?force=true` when the image has masks.

## 5. HTTP API

All under `/api`. JSON in, JSON out. Auth via session cookie; every route except
`/api/health` and `/api/login` requires a session. `admin` marked where relevant.

```
GET    /api/health                       -> {ok, version, schema_version}
POST   /api/login        {username,password} -> {username, role}
POST   /api/logout
GET    /api/me                           -> {username, role} | 401

GET    /api/users                        (admin)
POST   /api/users        {username,password,role}          (admin)
DELETE /api/users/{username}             (admin, cannot delete last admin)
POST   /api/users/{username}/password  {password}          (admin, or self)

POST   /api/images       multipart 'file' (repeatable) -> {images:[ImageSummary], duplicates:[...]}
GET    /api/images                       -> [ImageSummary]   (with crop progress)
GET    /api/images/{image_id}            -> {image: ImageSummary, crops: [CropSummary]}
GET    /api/images/{image_id}/file       -> image/jpeg (the stored derivative)
DELETE /api/images/{image_id}?force=     (admin)

GET    /api/crops/next?image_id=         -> CropTask | 204   (queue: oldest open crop)
GET    /api/crops/{crop_id}              -> CropTask
GET    /api/crops/{crop_id}/image        -> image/jpeg, rendered on demand from the
                                            stored source, disk-cached under
                                            data/cache/crops/<crop_id>.jpg
POST   /api/crops/{crop_id}/complete  {is_empty:boolean} -> CropTask
POST   /api/crops/{crop_id}/reopen       -> CropTask

GET    /api/classes                      -> [LabelClass]   (?include_archived=)
POST   /api/classes      {name,color?,description?} -> LabelClass
PATCH  /api/classes/{class_id}  {name?,color?,description?} -> LabelClass
DELETE /api/classes/{class_id}           -> LabelClass (archived=true)

POST   /api/masks        {crop_id,class_id,points}  -> Mask
PATCH  /api/masks/{mask_id}  {class_id?,points?}    -> Mask
DELETE /api/masks/{mask_id}                          -> {ok:true}

GET    /api/stats                        -> {n_images,n_crops,n_done,n_masks,per_class:[...]}
GET    /api/export/yolo?val_fraction=0.2&seed=0  -> application/zip  (admin)
GET    /api/backup/status                -> {last_run, next_due, enabled, runs:[...]}
POST   /api/backup/run                   (admin) -> run summary
```

Errors: `{"detail": "..."}` with a real status code. 400 for a bad polygon,
404 for an unknown id, 409 for a duplicate class name, 413 for an oversized
upload.

## 6. TypeScript types (`frontend/src/lib/types.ts` — binding)

```ts
export type Role = "admin" | "annotator";
export type Me = { username: string; role: Role };

export type LabelClass = {
  class_id: string; name: string; color: string; yolo_index: number;
  description: string | null; archived: boolean; n_masks: number;
};

export type ImageSummary = {
  image_id: string; filename: string; width: number; height: number;
  crop_size: number; crop_overlap: number;
  n_crops: number; n_done: number; n_masks: number;
  uploaded_by: string | null; uploaded_at: string; note: string | null;
};

export type CropSummary = {
  crop_id: string; row_idx: number; col_idx: number;
  x: number; y: number; w: number; h: number;
  status: "open" | "done"; is_empty: boolean;
  n_masks: number; completed_by: string | null; completed_at: string | null;
};

/** Points are CROP-LOCAL pixels: [[x,y], ...] within 0..crop.w / 0..crop.h. */
export type Mask = {
  mask_id: string; crop_id: string; class_id: string;
  points: [number, number][];
  created_by: string | null; created_at: string; updated_at: string | null;
};

export type CropTask = {
  crop: CropSummary;
  image: { image_id: string; filename: string; width: number; height: number };
  masks: Mask[];
  /** position in this image's crop grid, for the "3 of 24" progress line */
  index: number; total: number;
};
```

## 7. YOLO-seg export (the whole point)

`GET /api/export/yolo` streams a zip:

```
data.yaml            # path/train/val + names: {0: bee, 1: ...} from yolo_index
images/train/<crop_id>.jpg
images/val/<crop_id>.jpg
labels/train/<crop_id>.txt
labels/val/<crop_id>.txt
README.txt           # export stamp, counts, the completeness invariant
```

- **Only `status='done'` crops are exported.** Crops still `open` are omitted
  entirely; exporting them would inject the exact unlabeled-instance poison the
  crop design exists to prevent.
- Label line: `<yolo_index> x1 y1 x2 y2 ... xn yn`, coordinates **normalized to
  the crop** (`(px - crop.x) / crop.w`, `(py - crop.y) / crop.h`), clamped to
  `[0,1]`, 6 decimal places.
- An `is_empty` crop exports an image plus an **empty** `.txt` — a legitimate
  negative sample.
- **The train/val split is deterministic and grouped by `image_id`**, not by
  crop: `sha256(f"{seed}:{image_id}")` -> fraction. Two tiles of the same frame
  landing on opposite sides of the split is textbook leakage and would make the
  val metric a lie.
- Class names in `data.yaml` are keyed by `yolo_index` including archived
  classes (their index is reserved), so a model trained on an older export keeps
  matching indices.

## 8. Backup

Port `d:\Projects\cownting\cownting\labels_backup.py` and keep its structure and
its failure taxonomy — that module already encodes several hard-won lessons:

- In-process daemon thread started from `create_app`, ticks every 15 min and asks
  the DB *"has the interval elapsed since the last successful run, and did
  anything land since its watermark?"* — never `sleep(interval)`, which resets on
  every redeploy and therefore never fires on a box redeployed weekly.
- **Contention** (store locked, claim refused) -> `status='skipped'`, no row, **no
  cooldown armed**, exit 0. **Genuine failure** (disk full, torn snapshot,
  webhook unreachable) -> `failed` row, `[bienenblech.alert] BACKUP` line, 6 h
  cooldown, watermark NOT advanced.
- Webhook URL comes from `BIENENBLECH_DISCORD_WEBHOOK` at the point of use, never
  from Config or YAML, and everything printed or stored passes through
  `_redact()` first — `backup_runs.error` ends up inside the very zip that gets
  posted to the channel.
- Unset webhook is a **supported state**: still zips, still rotates locally,
  still advances the watermark. So it must not appear as `${VAR:?}` in
  docker-compose.

**What goes in the zip** (this is the one real difference from cownting):
1. `bienenblech.duckdb` — a consistent snapshot, not a live-file copy.
2. `masks.csv`, `classes.csv`, `crops.csv` — flat exports that outlive DuckDB.
3. `images/<image_id>.jpg` — **the compressed derivatives**, i.e. every stored
   source image. Labels without pixels are worthless.
4. `manifest.json` + `README.txt`.

Discord's per-file limit is ~8-25 MB depending on the server tier. So: if the zip
exceeds `backup.max_upload_mb` (default 8), **still write and rotate it locally**
and post only a text summary naming the local path — a silently dropped backup is
the failure mode to avoid. Local rotation keeps `backup.keep` (default 8) zips
under `data/backups/`.

## 9. Config (`config/bienenblech.example.yaml` -> `config/bienenblech.yaml`)

```yaml
project: bienenblech
paths:
  db_path: data/bienenblech.duckdb
  images_dir: data/images
  cache_dir: data/cache
  backups_dir: data/backups
upload:
  max_mb: 200                 # per file
  allowed: [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]
  store_format: jpeg          # the derivative written to images_dir
  store_quality: 92           # high: this IS the archival copy, masks refer to it
  max_edge: 8000              # downscale only if larger; masks are stored against
                              # the DERIVATIVE, so this must never change later
crop:
  size: 640                   # YOLO-seg native tile
  overlap: 0.0                # fraction; 0 = clean partition
  min_edge: 160               # edge tiles smaller than this are shifted back to
                              # full size rather than emitted undersized
  jpeg_quality: 92
auth:
  https_only: false           # true behind the real domain
  session_days: 14
backup:
  enabled: true
  interval_days: 7
  keep: 8
  max_upload_mb: 8
```

## 10. Deployment

Standalone stack on its **own domain**, but it will later share a server with
cownting — which already binds :80/:443. So:

- `docker-compose.yml` — app + its own Caddy, the default standalone story.
- `docker-compose.shared.yml` — an **override** that removes the Caddy service
  and publishes the app on `127.0.0.1:${BIENENBLECH_PORT:-8001}` instead, for the
  day it moves next to cownting behind one proxy:
  `docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d`.
- `DEPLOY.md` documents both paths, including the Caddyfile site block to paste
  into cownting's Caddyfile for the shared case.
- Entrypoint mirrors cownting's: start as root, re-own drifted files in the
  `/app/data` bind mount, `setpriv` down to an unprivileged `bienenblech` user.
- No torch, no ultralytics, no CUDA — this tool never runs a model. The image is
  python:3.11-slim + Pillow + FastAPI + DuckDB, and should stay small.

## 11. File ownership (do not write outside your list)

| Workstream | Owns |
|---|---|
| **core**     | `bienenblech/__init__.py` `config.py` `db.py` `auth.py`, `config/*.yaml` |
| **api**      | `bienenblech/api.py` `uploads.py` `crops.py` `cli.py` |
| **export**   | `bienenblech/export.py` `backup.py` |
| **editor**   | `frontend/src/components/PolygonCanvas.tsx` `ClassPicker.tsx` `CropProgress.tsx`, `frontend/src/lib/geometry.ts` |
| **app**      | `frontend/{index.html,package.json,vite.config.ts,tsconfig.json}`, `frontend/src/{main.tsx,App.tsx,index.css}`, `frontend/src/lib/{api.ts,types.ts,auth.tsx}`, `frontend/src/components/ui.tsx`, `frontend/src/pages/*` |
| **deploy**   | `Dockerfile` `docker-compose.yml` `docker-compose.shared.yml` `Caddyfile` `entrypoint.sh` `.env.example` `.dockerignore` `.gitignore` `requirements.txt` `pyproject.toml` `README.md` `DEPLOY.md` |
| **tests**    | `tests/*` |

Everyone may **read** anything, in this repo and in `d:\Projects\cownting`.
Nobody edits `docs/SPEC.md`.

## 12. House style (from cownting — match it)

- Module docstrings explain **why**, not what: the constraint that forced the
  design, the bug that would come back if it changed. Same for the load-bearing
  constants.
- Type hints everywhere, `from __future__ import annotations`.
- No emoji in code or UI copy. No `console.log` left behind.
- Tailwind utility classes inline; design tokens via the `@theme` block, never
  hard-coded hex in components.
- Prefer boring, readable code over clever code. This tool must still be
  obvious to read in two years.

---

## 13. Amendments (post-fan-out)

Findings from the workstreams that contradict or extend the sections above. These
supersede the earlier text where they conflict.

**A1 — `done` with zero masks must be refused (amends §1, §5).** `db.set_crop_status`
deliberately stores what it is told; it has no business vetoing. So
`POST /api/crops/{crop_id}/complete` is the ONLY place the completeness invariant
can be enforced: `is_empty=false` together with zero non-deleted masks is a **400**,
with a message naming the fix. Without this the exact poison §1 exists to prevent
reaches the export silently.

**A2 — `images.bytes` should be `BIGINT` (amends §4).** DuckDB `INTEGER` is 32-bit
(~2.147 GB). Harmless at `upload.max_mb: 200`, but a config bump past ~2048 would
overflow silently. `backup_runs.bytes` is already `BIGINT`.

**A3 — server-side extras are not browser-facing (amends §5, §6).** `db` row dicts
carry more than the TS types: images add `sha256`, `stored_path`, `bytes`; crops and
masks add `image_id`. `api.py` needs them internally but must strip `stored_path`
and `sha256` from responses — a filesystem path in JSON is free reconnaissance.

**A4 — class restore (amends §5).** `DELETE /api/classes/{id}` archives, and nothing
un-archives, so an accidental archive was unfixable from the UI. Add
`POST /api/classes/{class_id}/restore` (admin). `db.update_class(..., archived=False)`
already supports it.

**A5 — timestamps are SQL `now()`, never Python's clock.** Any comparison against a
stored timestamp must also use SQL `now()`. Mixing a local wall clock with UTC
breaks only on non-UTC machines, which is the worst kind of bug to find later.

**A6 — `init_db` brings up the users table too.** It calls `auth.ensure_user_table`,
so no startup path can forget it. `ensure_user_table` stays public for CLI-only paths.

**A7 — co-tenancy needs a shared docker network, not host loopback (amends §10).**
§10 prescribed publishing on `127.0.0.1:${BIENENBLECH_PORT:-8001}` for the day this
lands beside cownting. That is correct and safe, but it does not actually work for
cownting's Caddy, which is itself a container: a port bound to the host's loopback is
not reachable from the docker bridge, and `host.docker.internal` does not close the
gap on Linux. The working route, documented in DEPLOY.md, is to join cownting's Caddy
to the external `bienenblech_default` network and `reverse_proxy bienenblech:8000`.
The loopback publish stays, for a host-run proxy and for host-side curl debugging.

**A8 — the Caddy profile trick belongs in the OVERRIDE, not the base (amends §10).**
Putting `profiles:` on the caddy service in `docker-compose.yml` would break the
standalone default (it would need `COMPOSE_PROFILES` set just to boot). The override
adds the profile instead; compose merges, and a service carrying an unenabled profile
is dropped from the resolved model entirely. Verified against Compose v2.39.2.

**A9 — `auth.https_only: true` ships in the prod config.** Right behind a real domain,
but it makes the plain-HTTP path a login loop. So the docs invert the usual
instruction: HTTPS needs no edit; plain HTTP requires setting it to `false`.

**A10 — `upload.store_format` is effectively a constant.** §4 pins `stored_path` to
`.jpg` and the crop cache is JPEG, so only `jpeg` can work today.

**A11 — the backup zip carries password hashes into a Discord channel (amends §8).**
§8 mandates a full DuckDB snapshot posted to a webhook; §4 puts `users` — usernames
and scrypt hashes — in that database. Nobody wrote down the consequence: the channel
becomes as sensitive as the box. scrypt is salted and expensive, so this is not a
catastrophe, but it is not a decision anyone made either. **Resolution: exclude the
`users` table from the backup snapshot.** A restore re-bootstraps the admin from
`BIENENBLECH_ADMIN_*` and annotator accounts are recreated by hand — cheap, unlike
the annotations. Usernames still appear in `created_by` columns, which is fine;
usernames are not secrets, hashes are.

**A12 — `backup_runs.status` needs `'running'` (amends §4, §8).** The claim mutex that
stops the scheduler thread and a manual run from zipping simultaneously needs a
transient in-flight status, bounded by a lease. Conversely `'skipped'` is unreachable
*in the table* — by the contention rule a skip writes no row at all.

**A13 — `delivered BOOLEAN` loses the reason (amends §4).** "no webhook", "URL
refused" and "over the cap, summary posted" all collapse to `false`, which is exactly
the ambiguity an operator hits when asking why the channel is quiet. A `delivery TEXT`
column carries the reason.

**A14 — `data.yaml`'s `path:` is an Ultralytics footgun (amends §7).** Ultralytics
resolves a *relative* `path` against its own `settings['datasets_dir']` (`~/datasets`),
NOT against the yaml's directory — so `path: .` silently points training at the wrong
tree. Omitting `path` entirely is more correct, since it then defaults to the yaml's
parent.

**A15 — the backup watermark watches more than masks.** Defined as
`max(mask created/updated, crop completed_at, image uploaded_at)`. Watching only
`masks` would make a week of uploads read as "nothing new", and the images are the
expensive half of the zip.

**A16 — reserved for prelabeling: `masks.source` and `masks.confidence`.** Not built,
but the columns are cheap now and expensive later. See [PRELABELING.md](PRELABELING.md):
if model-assisted labeling ever ships, a model-authored mask must never be silently
indistinguishable from a human one — automation bias is a direct threat to the
completeness invariant of §1. `source TEXT NOT NULL DEFAULT 'human'` (`'human' | 'model'`)
and `confidence DOUBLE` (NULL for human work), both additive.

**A17 — `CropTask.index` is 1-based (amends §6).** §6 said only "position in this
image's crop grid", which was ambiguous. Resolved: 1-based, ordered by `row_idx` then
`col_idx`. The frontend renders it directly as "Crop 3 of 24"; 0-based would print
"Crop 0 of 24" on the first screen an annotator ever sees.

**A18 — re-classing a mask is the app's job, not the canvas's (amends §6).**
`PolygonCanvas.onUpdate` carries points only. So when a mask is **selected** and a
digit key is pressed, the Label page must `PATCH /api/masks/{id} {class_id}` rather
than only moving `activeClassId` — otherwise a mis-classed polygon can only be
deleted and redrawn. Relatedly, the `1..9` hints in `ClassPicker` are **positional**
(they label `classes[0..8]` as passed), so the page must bind its digit keys to the
same filtered, same-ordered array it hands the picker, or the hints lie.
