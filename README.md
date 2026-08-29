# Bienenblech

A minimal polygon **segmentation labeler** for single-camera still frames. You
upload a full-resolution frame; the server tiles it into fixed-size crops
(640x640 by default) and hands out one crop at a time. You draw polygons, assign
a class, and mark the crop done. Out the other end comes a **YOLO11-seg**
dataset.

```
frame ─▶ tile into 640px crops ─▶ label every instance in a crop ─▶ export
(upload)     (server, on ingest)      (browser: zoom/pan/polygon)   (YOLO-seg zip)
```

## Why crops, and not whole frames

This is the one load-bearing decision in the project, so it is worth the
paragraph.

YOLO-seg training assumes **every instance visible in a training image is
labeled**. An unlabeled instance on a sheet is not "missing data" — it is an explicit
*background* teaching signal that actively trains the model to suppress true
positives. A half-labeled frame is therefore worse than no frame at all.

On a 4000x3000 frame with hundreds of instances, exhaustive labeling in one
sitting is not realistic. On a 640x640 crop it is. That makes the invariant
enforceable, and the whole tool is built around it:

> **A crop is `done` only when every instance of every known class inside it has
> a polygon. Only `done` crops are exported.**

Consequences that follow from that rule:

- A crop with nothing in it is marked **empty** — a valid and valuable *negative*
  sample, exported as an image plus an empty label file. It is not a skip.
- Crops still `open` are omitted from the export entirely, rather than exported
  with whatever labels they happen to have.
- An instance clipped by a tile edge is correct and expected; the polygon is
  clamped to the crop rect.
- 640 is the tile size because it is YOLO-seg's native input: the crop you label
  is pixel-for-pixel the image the model trains on. No resampling in between.

## Install

```bash
pip install -r requirements.txt        # fastapi, duckdb, pillow, typer — no torch
cd frontend && npm ci && cd ..
```

The runtime has no model in it: no torch, no ultralytics, no OpenCV, no ffmpeg.
Bienenblech *produces* training data, it does not consume it — training happens
elsewhere, on a GPU.

## Configure

```bash
cp config/bienenblech.example.yaml config/bienenblech.yaml
```

The parts worth knowing:

| key | meaning |
|---|---|
| `crop.size` / `crop.overlap` | tiling; 640 / 0.0 is a clean partition. Frozen per image at upload time, so changing this never re-tiles existing work. |
| `upload.max_edge` | downscale-if-larger for the stored derivative. Masks are stored against that derivative, so **do not change it once anything is labeled**. |
| `upload.store_quality` | 92 — the stored JPEG *is* the archival copy. |
| `auth.https_only` | `true` behind a real domain (HTTPS-only session cookie); `bienenblech.prod.yaml` already ships `true`. On plain HTTP it must be `false` or the login loops. |
| `backup.*` | interval, how many zips to keep (per store), the Discord upload ceiling. |
| `paths.age_db_path` | the Age tool's own store, default `data/age.duckdb`. Existing configs need no edit. |

Secrets are never in YAML. `BIENENBLECH_SECRET`, `BIENENBLECH_ADMIN_USER`,
`BIENENBLECH_ADMIN_PASSWORD` and the optional `BIENENBLECH_DISCORD_WEBHOOK` are
read from the environment at the point of use — `config/` is committed, and in
production it is bind-mounted read-only.

## Run

Development, two terminals:

```bash
python -m bienenblech.cli serve                 # API on :8000
cd frontend && npm run dev                      # UI on :5173 (proxies /api -> :8000)
```

Production, one command (full walkthrough in **[DEPLOY.md](DEPLOY.md)**):

```bash
cp .env.example .env            # BIENENBLECH_SECRET, admin password, your domain
docker compose up -d --build    # builds the SPA + image, boots the app behind Caddy
```

To exercise the prod path locally, build the SPA once and let FastAPI serve it
from `frontend/dist`:

```bash
cd frontend && npm run build && cd ..
python -m bienenblech.cli serve --config config/bienenblech.yaml
```

## The labeling flow

1. **Upload** one or more frames. Any signed-in user can upload — there are
   exactly two roles, **admin** and **poweruser**, and feeding the queue is
   part of both jobs. The server hashes the original bytes
   (re-uploading the same file is a no-op, reported as a duplicate), writes a
   quality-92 JPEG derivative, and immediately tiles it into crops. Nothing is
   pre-labeled — there is no detector in this tool.
2. **Take the next crop.** The queue hands out the oldest `open` crop, optionally
   restricted to a single image.
3. **Zoom and pan** the crop, click a polygon around an instance (3+ vertices, no
   holes — YOLO-seg has no hole concept), pick its class, repeat.
4. **Mark done** — only once every instance in the crop has a polygon — or
   **mark empty** if there is genuinely nothing in it. Either way the crop leaves
   the queue and becomes exportable. Reopening puts it back.

Coordinates: the API speaks **crop-local** pixels, the database stores
**source-image** pixels. The backend does the offset in both directions, so a
future full-frame view can render every mask, and re-tiling parameters later
does not orphan old work.

## Classes

Classes are created in the app (any signed-in user may add one; only admins archive).
Each gets a permanent `yolo_index`, assigned monotonically and **never reused or
renumbered** — an archived class keeps its index, so a model trained on an older
export still matches the names in a newer one. Deleting a class archives it; its
masks stay. Nothing here hard-deletes labeling work, with one exception: an
explicit admin image delete, which refuses unless forced when masks exist.

## Export, and training on it

```
GET /api/export/yolo?val_fraction=0.2&seed=0     (admin) -> a zip
```

```
data.yaml                 # names: {0: mite, 1: ...}, keyed by yolo_index
images/train/<crop_id>.jpg    labels/train/<crop_id>.txt
images/val/<crop_id>.jpg      labels/val/<crop_id>.txt
README.txt                # export stamp, counts, the completeness invariant
```

Label lines are `<yolo_index> x1 y1 ... xn yn`, normalized to the crop and
clamped to `[0,1]`. The **train/val split is deterministic and grouped by source
image**, never by crop: two tiles of the same frame landing on opposite sides of
the split is textbook leakage, and the val metric would be a lie.

Then, on a machine with a GPU:

```bash
unzip bienenblech-yolo.zip -d dataset
yolo segment train model=yolo11n-seg.pt data=dataset/data.yaml imgsz=640 epochs=100 batch=8
```

Keep `imgsz` equal to `crop.size`, so the model trains on exactly the pixels that
were labeled. `data.yaml`'s `path` is relative to the unzipped directory, so run
the command from next to it (or edit it to an absolute path).

## Storage and backups

Persistent state is **two DuckDB stores**. `data/bienenblech.duckdb` holds the
users plus everything above — images, crops, classes, masks, audit.
`data/age.duckdb` (`paths.age_db_path`) holds the samples of the Age tool, the
second labeler behind the same login. Accounts are global and live only in the
main store; roles mean the same in both tools. Each store carries its own
backup bookkeeping, so either file can be moved or restored on its own.

A daemon thread inside the app backs up each store on its own weekly watermark
(`backup.interval_days`, default 7) into `data/backups/`, keeps the last
`backup.keep` (default 8) per store and — if `BIENENBLECH_DISCORD_WEBHOOK` is
set — posts to a Discord channel. Blech-only activity never fires an Age
backup, or vice versa. The Blech zip (`bienenblech-<stamp>-<run>.zip`) holds
the DuckDB snapshot (minus `users`), flat `images.csv` / `crops.csv` /
`classes.csv` / `masks.csv` exports that outlive DuckDB itself, and **the
source images**: labels without pixels are worthless. The Age zip
(`bienenblech-age-<stamp>-<run>.zip`) holds the age store's snapshot,
`age_samples.csv`, and the stored bee photos from `data/age/`.

If a zip exceeds `backup.max_upload_mb` (Discord's per-file limit) it is still
written and rotated locally, and only a text summary naming the local path is
posted — a silently dropped backup is the failure mode this design exists to
avoid. An unset webhook is a supported state, not an error.

## What's deliberately not here

- **No video.** Frames only. Turning footage into frames is a different job (the
  sibling project, `cownting`, does that).
- **No model inference.** No pre-annotation, no active learning, no torch in the
  image. Every polygon here was drawn by a person.
- **No multi-labeler agreement.** No double-labeling, no inter-labeler IoU,
  no adjudication queue. One crop, one labeler, done.
- **No instance tracking or identity** across crops or frames. A mask belongs to
  a crop and a class, and that is all it means.
- **No polygon holes**, no boxes-only mode, no keypoints. YOLO-seg polygons are
  the only output shape.
