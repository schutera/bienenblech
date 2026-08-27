"""DuckDB store: schema, connection discipline, and row helpers for the API.

Three things in here are load-bearing enough to state up front.

**Coordinates.** `masks.points` is JSON text holding SOURCE-IMAGE pixel
coordinates — `[[x, y], ...]`, floats, origin at the top-left of the full stored
frame. This module stores and returns those coordinates VERBATIM. It never adds
or subtracts `crop.x/crop.y`, never clamps, never normalises. The crop-local
transform the HTTP API speaks (SPEC section 3) belongs to `api.py`, which
subtracts the crop origin on read and adds it back on write. Source coordinates
survive a change of tiling parameters and let a future full-frame view draw every
mask on one canvas; a store of crop-local points would be silently wrong the
first time anyone re-tiles.

**Soft delete.** Annotator hours are the only thing on this box that cannot be
regenerated. Masks are flagged `deleted`, classes are flagged `archived`, and
`label_classes.yolo_index` is never reused or renumbered so a model trained on an
older export keeps matching indices. The single hard delete in the whole schema
is `delete_image`, which is admin-only and explicit.

**Row helpers return JSON-ready dicts** whose keys are exactly the TypeScript
types in SPEC section 6, so `api.py` can return them straight out of a route:
timestamps are already ISO-8601 strings and `points` is already a parsed list of
pairs. Image, crop and mask dicts carry a few server-side extras beyond the TS
type (`stored_path`, `sha256`, `bytes`, `image_id` on crops and masks) because
the API needs them to serve files and delete them; they are additive, so the
frontend types stay accurate about what they read.

Timestamps are written with DuckDB's `now()` everywhere — never Python's clock.
Mixing the two would leave the backup watermark comparing a local wall clock
against a UTC one, which fails only on machines whose TZ is not UTC, i.e. every
developer laptop and no CI runner.
"""
from __future__ import annotations

import json
import math
import re
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb

from . import auth
from .config import Config

# Bumped whenever the DDL below changes in a way a running deployment must notice.
# Written to meta['schema_version'] by init_db and reported by GET /api/health, so
# an operator can tell at a glance whether the container matches its data dir.
SCHEMA_VERSION: int = 1

# Retry budget for `connect`. 50 attempts with the backoff below sums to roughly
# nine seconds. It is deliberately longer than any single request on this box:
# the collision it absorbs clears in milliseconds, so exhausting the budget means
# something is genuinely holding the file (a CLI session, a stuck export) and the
# caller wants a 503 rather than a wait.
_CONNECT_ATTEMPTS = 50
_CONNECT_DELAY = 0.02
_CONNECT_DELAY_MAX = 0.2

# Class colours are picked from this fixed list so two classes created a minute
# apart never come out near-identical on a photograph. Tailwind's 700 ramp: dark
# enough to read as an outline over a bright sheet, distinct at a 20 px polygon.
# Order is stable — changing it recolours nothing already stored, but the next
# class created lands somewhere new.
_PALETTE: tuple[str, ...] = (
    "#c2410c", "#1d4ed8", "#15803d", "#a21caf",
    "#0e7490", "#b45309", "#4d7c0f", "#be123c",
    "#6d28d9", "#0f766e", "#a16207", "#334155",
)

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

CROP_STATUSES: tuple[str, ...] = ("open", "done")


class NotFound(Exception):
    """An id that does not exist. `api.py` maps this to 404."""


class DuplicateClass(Exception):
    """A class name that already exists. `api.py` maps this to 409."""


class DbBusy(Exception):
    """The store stayed locked for the whole retry budget. `api.py` maps this to
    503 — it is transient by definition, so the client should retry, not report a
    bug."""


# ----------------------------------------------------------------------- connect
def is_transient_lock_error(exc: Exception) -> bool:
    """True when `exc` is DuckDB's momentary file-handle/lock collision.

    Two short-lived connections to one file coincide for a few milliseconds — an
    upload overlapping a crop render, or half a dozen mask writes arriving from
    one page — and DuckDB refuses the second. It clears on its own, so callers
    retry rather than fail.

    The predicate lives here, once, because the same collision is worded
    differently per platform: "Unique file handle conflict ... already attached"
    on both, POSIX's "Conflicting lock is held", and Windows' "The process cannot
    access the file because it is being used by another process". In the sibling
    project this test was copy-pasted at three call sites and adding the Windows
    wording to one of them left the other two returning 500s.

    Note what it does NOT cover: opening a file read-only while this process
    already holds it read-write raises a *different* message, and retrying that
    just burns the budget. That is why `connect` defaults to read-write.
    """
    msg = str(exc).lower()
    return ("already attached" in msg
            or "unique file handle" in msg
            or "conflict" in msg                        # POSIX lock + catalog write-write
            or "being used by another process" in msg)  # Windows sharing violation


def connect(config: Config, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the store, retrying past the transient file-handle conflict.

    The app opens a short-lived connection per request and closes it right after,
    rather than holding a process-wide singleton: that keeps the file free
    between operations for the CLI and for the backup thread, which open it from
    their own connections. The cost is that concurrent requests occasionally
    collide on open, which the bounded retry turns into a sub-second wait instead
    of a hard 500.

    Raises `DbBusy` when the budget is exhausted, chaining the real DuckDB error
    so the log still shows which lock message was hit.
    """
    db_path = config.paths.db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    delay = _CONNECT_DELAY
    last: Exception | None = None
    for _ in range(_CONNECT_ATTEMPTS):
        try:
            return duckdb.connect(db_path, read_only=read_only)
        except Exception as e:  # noqa: BLE001 — retry only the file-handle clash
            if is_transient_lock_error(e):
                last = e
                time.sleep(delay)
                delay = min(delay * 1.5, _CONNECT_DELAY_MAX)
                continue
            raise
    raise DbBusy(
        f"{db_path} stayed locked for ~{_CONNECT_ATTEMPTS * _CONNECT_DELAY_MAX:.0f}s: {last}"
    ) from last


# --------------------------------------------------------------------------- DDL
def init_db(con: duckdb.DuckDBPyConnection) -> None:
    """Create or upgrade the whole schema. Idempotent; safe on every boot.

    Ordering is a discipline, not a preference: sequences, then `CREATE TABLE IF
    NOT EXISTS`, then the additive-migration block, then indexes, then meta. A
    boot that runs these out of order fails against a store created by an older
    build, and a DDL error here takes the whole app down rather than one page.
    The migration block runs BEFORE the index block on purpose: a column retype
    must drop an index to get past DuckDB's dependency check, and the index
    block re-creating it on the same boot is what makes that safe.

    No foreign keys anywhere. DuckDB's FK restrictions fight the soft-delete flow
    (and its `ON CONFLICT` handling), and the referential rules that matter here
    are enforced by the helpers below, which are the only writers.
    """
    # Sequences: none. Every id in this schema is either a uuid4 hex string or a
    # deterministic composite ('<image_id>_r<row>c<col>'), so nothing needs a
    # counter. The step is kept as a comment so the ordering above stays readable
    # when the first sequence does arrive.

    # The users table belongs to auth.py; created here too so a single init_db
    # call brings up the complete schema and no startup path can forget it.
    auth.ensure_user_table(con)

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS images (
            image_id     TEXT PRIMARY KEY,
            filename     TEXT NOT NULL,        -- sanitised original upload name
            sha256       TEXT NOT NULL,        -- of the ORIGINAL bytes; re-upload dedupe key
            width        INTEGER NOT NULL,     -- of the STORED derivative, not the upload
            height       INTEGER NOT NULL,
            stored_path  TEXT NOT NULL,        -- data/images/<image_id>.jpg
            "bytes"      BIGINT NOT NULL,      -- BIGINT, not INTEGER (A2): INTEGER is 32-bit
                                               -- (~2.147 GB) and upload.max_mb is one config
                                               -- edit away from overflowing it silently.
                                               -- Quoted: unambiguous next to the type name.
            crop_size    INTEGER NOT NULL,     -- tiling parameters frozen at upload time,
            crop_overlap DOUBLE  NOT NULL,     -- so changing the config never re-tiles old work
            uploaded_by  TEXT,
            uploaded_at  TIMESTAMP NOT NULL,
            note         TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS crops (
            crop_id      TEXT PRIMARY KEY,     -- '<image_id>_r<row>c<col>' (deterministic)
            image_id     TEXT NOT NULL,
            row_idx      INTEGER NOT NULL,     -- 'row'/'column' are reserved-ish in SQL
            col_idx      INTEGER NOT NULL,
            x INTEGER NOT NULL, y INTEGER NOT NULL,   -- rect in SOURCE-image px
            w INTEGER NOT NULL, h INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'done'
            is_empty     BOOLEAN NOT NULL DEFAULT FALSE, -- a valid negative sample, not a skip
            completed_by TEXT,
            completed_at TIMESTAMP,
            UNIQUE (image_id, row_idx, col_idx)
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS label_classes (
            class_id    TEXT PRIMARY KEY,      -- slug of the name at creation, stable forever
            name        TEXT NOT NULL UNIQUE,
            color       TEXT NOT NULL,         -- '#rrggbb'
            yolo_index  INTEGER NOT NULL UNIQUE,  -- 0-based, monotonic, NEVER reused or
                                                  -- renumbered: an archived class keeps its
                                                  -- index so old exports stay readable
            description TEXT,
            archived    BOOLEAN NOT NULL DEFAULT FALSE,  -- soft delete only
            created_by  TEXT,
            created_at  TIMESTAMP NOT NULL
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS masks (
            mask_id    TEXT PRIMARY KEY,
            crop_id    TEXT NOT NULL,
            image_id   TEXT NOT NULL,          -- denormalised for cheap per-image queries
            class_id   TEXT NOT NULL,
            points     JSON NOT NULL,          -- [[x,y],...] SOURCE-IMAGE px, >= 3 pairs
            created_by TEXT,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP,
            deleted    BOOLEAN NOT NULL DEFAULT FALSE  -- soft delete; never hard-DELETE a mask
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS class_audit (
            audit_id TEXT PRIMARY KEY,
            class_id TEXT,
            action   TEXT,
            detail   JSON,
            actor    TEXT,
            "at"     TIMESTAMP NOT NULL        -- quoted: AT is a DuckDB keyword
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_runs (
            run_id      TEXT PRIMARY KEY,
            started_at  TIMESTAMP,
            finished_at TIMESTAMP,
            status      TEXT,                  -- 'running' | 'ok' | 'failed' (A12). Never
                                               -- 'skipped': by the contention rule a skip
                                               -- writes no row at all, and 'running' is the
                                               -- in-flight claim the mutex lease is built on
            trigger     TEXT,                  -- 'schedule' | 'manual' | 'cli'
            n_masks     BIGINT,
            n_images    BIGINT,
            "bytes"     BIGINT,
            zip_path    TEXT,
            delivered   BOOLEAN,
            delivery    TEXT,                  -- WHY the channel is or is not showing a zip
                                               -- (A13): 'posted' | 'posted_summary' |
                                               -- 'disabled' | 'skipped'; delivered alone
                                               -- collapses all the quiet cases to false
            error       TEXT,                  -- redacted before it is stored: it ends up
                                               -- inside the zip that is posted to Discord
            host        TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY, value TEXT   -- 'schema_version', 'backup_watermark'
        );
        """
    )

    # --- additive migrations -------------------------------------------------
    # Every schema change edits the CREATE TABLE above (a fresh store is born in
    # the current shape) AND adds an idempotent statement here, left in place
    # forever: a store created by an older build reaches the same shape only by
    # replaying this block on boot. Everything below must therefore be a no-op
    # against all three store ages — fresh, already-migrated, and old.

    # A13: `delivered BOOLEAN` records THAT the zip arrived but not what
    # happened instead when it did not, so `delivery TEXT` carries the reason.
    # backup.ensure_backup_tables carries this same ALTER on purpose, not by
    # accident: the backup CLI must be able to rescue the labels from a box
    # whose server has never booted against this store, so it cannot assume this
    # block already ran. Both copies are ADD COLUMN IF NOT EXISTS — whichever
    # runs second is a no-op. Keep them textually in step.
    con.execute("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS delivery TEXT")

    # A2: images."bytes" was INTEGER in the original DDL — 32-bit, ~2.147 GB,
    # one `upload.max_mb` config bump away from silently overflowing. Retype to
    # BIGINT. Guarded by a type probe rather than run unconditionally because
    # DuckDB (1.4.4) refuses ALTER COLUMN on a table carrying ANY index with a
    # DependencyException — and idx_images_sha exists on every store that has
    # booted before. When the retype is genuinely needed the index is dropped
    # first; the index block below re-creates it later in this same call.
    bytes_type = con.execute(
        "SELECT type FROM pragma_table_info('images') WHERE name = 'bytes'"
    ).fetchone()
    if bytes_type is not None and bytes_type[0] != "BIGINT":
        con.execute("DROP INDEX IF EXISTS idx_images_sha")
        con.execute('ALTER TABLE images ALTER COLUMN "bytes" SET DATA TYPE BIGINT')

    # --- indexes -------------------------------------------------------------
    # These three mask indexes are what keep the labeling screen honest: every
    # crop load counts masks by crop_id, the image list counts by image_id, and
    # the stats page groups by class_id.
    con.execute("CREATE INDEX IF NOT EXISTS idx_masks_crop ON masks(crop_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_masks_image ON masks(image_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_masks_class ON masks(class_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_crops_image ON crops(image_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_crops_status ON crops(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256)")

    set_meta(con, "schema_version", str(SCHEMA_VERSION))


# ----------------------------------------------------------------------- helpers
def _uid() -> str:
    return uuid.uuid4().hex


def _jsonable(v: Any) -> Any:
    """Datetimes to ISO-8601 strings; everything else untouched.

    Applied to every fetched row so the dicts these helpers return can go
    straight into a JSON response without leaning on FastAPI's encoder — which
    matters for the export and backup paths, where the same dicts are written to
    CSV and to a manifest by plain `json.dumps`."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _rows(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Fetch a cursor as a list of column-name -> value dicts."""
    cols = [d[0] for d in cur.description]
    return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def _one(cur: duckdb.DuckDBPyConnection) -> dict[str, Any] | None:
    rows = _rows(cur)
    return rows[0] if rows else None


def _slug(name: str) -> str:
    """Lowercase, underscore-joined id derived from a class name.

    Computed once at creation and then stable forever, even if the name is later
    changed: the id is what masks reference and what a URL carries, so
    re-slugging on rename would orphan every mask of that class."""
    return _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")[:64]


def _valid_color(color: str) -> str:
    """'#rrggbb', lowercased. Rejected rather than stored when malformed: this
    string is written into an inline SVG fill in the editor, so anything that is
    not six hex digits is an injection vector, and classes are user-writable
    at runtime."""
    if not isinstance(color, str) or not _COLOR_RE.fullmatch(color.strip()):
        raise ValueError(f"color must be '#rrggbb', got {color!r}")
    return color.strip().lower()


def _pick_color(con: duckdb.DuckDBPyConnection, yolo_index: int) -> str:
    """First palette entry no existing class uses; once the palette is exhausted,
    cycle it by index so the choice stays deterministic."""
    used = {
        (r[0] or "").lower()
        for r in con.execute("SELECT color FROM label_classes").fetchall()
    }
    for c in _PALETTE:
        if c not in used:
            return c
    return _PALETTE[yolo_index % len(_PALETTE)]


def _points_json(points: Sequence[Sequence[float]]) -> str:
    """Validate a polygon and serialise it for the JSON column.

    SOURCE-IMAGE coordinates in, verbatim: no clamping and no crop offset happen
    here (see the module docstring). Minimum three vertices; self-intersecting
    polygons are accepted because users draw them and the exporter does not
    care. Non-finite values are refused because `json.dumps` would emit bare
    `NaN`, which is not JSON and which DuckDB would hand back unparseable."""
    pts: list[list[float]] = []
    for p in points or []:
        if len(p) != 2:
            raise ValueError(f"each point must be an [x, y] pair, got {p!r}")
        x, y = float(p[0]), float(p[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("polygon points must be finite numbers")
        pts.append([x, y])
    if len(pts) < 3:
        raise ValueError(f"a polygon needs at least 3 points, got {len(pts)}")
    return json.dumps(pts)


def _audit_class(
    con: duckdb.DuckDBPyConnection,
    *,
    class_id: str,
    action: str,
    detail: Mapping[str, Any] | None,
    actor: str | None,
) -> None:
    """Record who created/renamed/recoloured/archived a class. Cheap, and the
    only way to answer "when did this class stop meaning what I think it meant?"
    after a rename lands in the middle of a labeling week."""
    con.execute(
        'INSERT INTO class_audit (audit_id, class_id, action, detail, actor, "at") '
        "VALUES (?, ?, ?, ?, ?, now())",
        [_uid(), class_id, action, json.dumps(detail or {}, default=str), actor],
    )


# ------------------------------------------------------------------------ images
# ImageSummary (SPEC section 6) plus the three server-side columns the API needs:
# `sha256` for upload dedupe, `stored_path` and `bytes` for serving and deleting
# the file. `n_crops`/`n_done`/`n_masks` are computed, never stored — a cached
# progress counter that disagrees with the crops table is the classic bug here.
_IMAGE_COLS = """
    i.image_id, i.filename, i.width, i.height, i.crop_size, i.crop_overlap,
    (SELECT count(*) FROM crops c WHERE c.image_id = i.image_id) AS n_crops,
    (SELECT count(*) FROM crops c WHERE c.image_id = i.image_id AND c.status = 'done') AS n_done,
    (SELECT count(*) FROM masks m WHERE m.image_id = i.image_id AND NOT m.deleted) AS n_masks,
    i.uploaded_by, i.uploaded_at, i.note,
    i.sha256, i.stored_path, i."bytes"
"""

_IMAGE_FIELDS: tuple[str, ...] = (
    "image_id", "filename", "sha256", "width", "height", "stored_path", "bytes",
    "crop_size", "crop_overlap", "uploaded_by", "uploaded_at", "note",
)


def insert_image(con: duckdb.DuckDBPyConnection, **fields: Any) -> dict[str, Any]:
    """Record one uploaded frame and return its ImageSummary dict.

    `image_id` defaults to a fresh uuid4 hex and `uploaded_at` to `now()`;
    everything else in `_IMAGE_FIELDS` is required. `crop_size`/`crop_overlap`
    are the values in force at upload time and are stored per image on purpose —
    editing `crop.size` in the YAML must never re-tile or invalidate a frame
    somebody has already half-labeled."""
    unknown = sorted(set(fields) - set(_IMAGE_FIELDS))
    if unknown:
        raise ValueError(f"unknown image field(s): {', '.join(unknown)}")
    image_id = fields.get("image_id") or _uid()
    missing = [
        k for k in ("filename", "sha256", "width", "height", "stored_path", "bytes",
                    "crop_size", "crop_overlap")
        if fields.get(k) is None
    ]
    if missing:
        raise ValueError(f"missing image field(s): {', '.join(missing)}")
    con.execute(
        'INSERT INTO images (image_id, filename, sha256, width, height, stored_path, '
        '"bytes", crop_size, crop_overlap, uploaded_by, uploaded_at, note) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, coalesce(CAST(? AS TIMESTAMP), now()), ?)",
        [
            image_id, fields["filename"], fields["sha256"],
            int(fields["width"]), int(fields["height"]), fields["stored_path"],
            int(fields["bytes"]), int(fields["crop_size"]), float(fields["crop_overlap"]),
            fields.get("uploaded_by"), fields.get("uploaded_at"), fields.get("note"),
        ],
    )
    row = get_image(con, image_id)
    assert row is not None
    return row


def list_images(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Every image, newest upload first — the order the Images page renders."""
    return _rows(con.execute(
        f"SELECT {_IMAGE_COLS} FROM images i ORDER BY i.uploaded_at DESC, i.image_id"
    ))


def get_image(con: duckdb.DuckDBPyConnection, image_id: str) -> dict[str, Any] | None:
    return _one(con.execute(
        f"SELECT {_IMAGE_COLS} FROM images i WHERE i.image_id = ?", [image_id]
    ))


def find_image_by_sha(con: duckdb.DuckDBPyConnection, sha256: str) -> dict[str, Any] | None:
    """The already-stored image with these original bytes, if any.

    Re-upload dedupe: the same frame dragged in twice must not produce a second
    tiling with a second, independently half-finished set of crops."""
    return _one(con.execute(
        f"SELECT {_IMAGE_COLS} FROM images i WHERE i.sha256 = ? "
        "ORDER BY i.uploaded_at LIMIT 1",
        [sha256],
    ))


def delete_image(con: duckdb.DuckDBPyConnection, image_id: str) -> dict[str, Any]:
    """Hard-delete an image and everything hanging off it. Returns the row as it
    was, so the caller can unlink `stored_path` and the cached crop renders.

    The one hard delete in the schema, and the one place masks are dropped rather
    than flagged — deleting the frame removes the pixels those polygons refer to,
    so keeping them would leave rows that can never be drawn or exported. It is
    admin-only, and the API refuses it without `?force=true` when
    `n_masks > 0`; that check lives there because it needs to answer 409, but the
    counts it needs are in the dict this returns."""
    row = get_image(con, image_id)
    if row is None:
        raise NotFound(f"unknown image {image_id!r}")
    con.execute("DELETE FROM masks WHERE image_id = ?", [image_id])
    con.execute("DELETE FROM crops WHERE image_id = ?", [image_id])
    con.execute("DELETE FROM images WHERE image_id = ?", [image_id])
    return row


# ------------------------------------------------------------------------- crops
# CropSummary (SPEC section 6) plus `image_id`, which the API needs to build a
# CropTask's `image` block and to render the crop from the stored source.
_CROP_COLS = """
    c.crop_id, c.image_id, c.row_idx, c.col_idx, c.x, c.y, c.w, c.h,
    c.status, c.is_empty,
    (SELECT count(*) FROM masks m WHERE m.crop_id = c.crop_id AND NOT m.deleted) AS n_masks,
    c.completed_by, c.completed_at
"""


def crop_id_for(image_id: str, row_idx: int, col_idx: int) -> str:
    """The deterministic crop id. Deterministic so a re-tile of the same image
    with the same parameters lands on the same ids, and so a crop id in a log
    line or an export filename says which frame and which tile it was."""
    return f"{image_id}_r{int(row_idx)}c{int(col_idx)}"


def insert_crops(con: duckdb.DuckDBPyConnection, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert a whole tiling in one statement. Returns the number of rows written.

    Each mapping needs `image_id, row_idx, col_idx, x, y, w, h`; `crop_id`
    defaults to `crop_id_for(...)`. Deliberately NOT upserting: a duplicate here
    means an image was tiled twice, which would double every crop in the queue,
    and the PRIMARY KEY violation is how we find out immediately."""
    params: list[list[Any]] = []
    for r in rows:
        missing = [k for k in ("image_id", "row_idx", "col_idx", "x", "y", "w", "h")
                   if r.get(k) is None]
        if missing:
            raise ValueError(f"missing crop field(s): {', '.join(missing)}")
        params.append([
            r.get("crop_id") or crop_id_for(r["image_id"], r["row_idx"], r["col_idx"]),
            r["image_id"], int(r["row_idx"]), int(r["col_idx"]),
            int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"]),
        ])
    if not params:
        return 0
    con.executemany(
        "INSERT INTO crops (crop_id, image_id, row_idx, col_idx, x, y, w, h) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    return len(params)


def list_crops(con: duckdb.DuckDBPyConnection, image_id: str) -> list[dict[str, Any]]:
    """One image's crops in grid order — the order the crop strip renders and the
    order `index`/`total` in a CropTask are counted in."""
    return _rows(con.execute(
        f"SELECT {_CROP_COLS} FROM crops c WHERE c.image_id = ? "
        "ORDER BY c.row_idx, c.col_idx",
        [image_id],
    ))


def get_crop(con: duckdb.DuckDBPyConnection, crop_id: str) -> dict[str, Any] | None:
    return _one(con.execute(
        f"SELECT {_CROP_COLS} FROM crops c WHERE c.crop_id = ?", [crop_id]
    ))


def next_open_crop(
    con: duckdb.DuckDBPyConnection, image_id: str | None = None
) -> dict[str, Any] | None:
    """The next crop to label: the oldest still-open one, optionally within one
    image. None when the queue is empty, which the API answers as 204.

    Oldest-first by upload, then in grid order — so a user walks a frame to
    completion instead of scattering done crops across every image on the box,
    and a frame that is nearly finished gets finished."""
    where = "c.status = 'open'"
    params: list[Any] = []
    if image_id is not None:
        where += " AND c.image_id = ?"
        params.append(image_id)
    return _one(con.execute(
        f"SELECT {_CROP_COLS} FROM crops c JOIN images i ON i.image_id = c.image_id "
        f"WHERE {where} ORDER BY i.uploaded_at, c.image_id, c.row_idx, c.col_idx LIMIT 1",
        params,
    ))


def set_crop_status(
    con: duckdb.DuckDBPyConnection,
    crop_id: str,
    *,
    status: str,
    is_empty: bool,
    actor: str | None,
) -> dict[str, Any]:
    """Complete or reopen a crop.

    `status='done'` stamps `completed_by`/`completed_at`; reopening clears both,
    so the provenance always describes the completion that currently stands
    rather than one that was undone. `is_empty` is passed explicitly (not
    inferred from `n_masks == 0`) because the two mean different things: an empty
    crop is a deliberate, valuable negative sample, and 'nobody has drawn
    anything yet' is not."""
    if status not in CROP_STATUSES:
        raise ValueError(f"status must be one of {CROP_STATUSES}, got {status!r}")
    if get_crop(con, crop_id) is None:
        raise NotFound(f"unknown crop {crop_id!r}")
    done = status == "done"
    con.execute(
        "UPDATE crops SET status = ?, is_empty = ?, "
        "completed_by = CASE WHEN ? THEN ? ELSE NULL END, "
        "completed_at = CASE WHEN ? THEN now() ELSE NULL END "
        "WHERE crop_id = ?",
        [status, bool(is_empty), done, actor, done, crop_id],
    )
    row = get_crop(con, crop_id)
    assert row is not None
    return row


# ----------------------------------------------------------------------- classes
# LabelClass (SPEC section 6). `n_masks` counts live masks only.
_CLASS_COLS = """
    lc.class_id, lc.name, lc.color, lc.yolo_index, lc.description, lc.archived,
    (SELECT count(*) FROM masks m WHERE m.class_id = lc.class_id AND NOT m.deleted) AS n_masks
"""


def list_classes(
    con: duckdb.DuckDBPyConnection, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Classes ordered by `yolo_index`, i.e. creation order, i.e. the order they
    appear in an export's `data.yaml` and under the user's number keys."""
    where = "" if include_archived else "WHERE NOT lc.archived "
    return _rows(con.execute(
        f"SELECT {_CLASS_COLS} FROM label_classes lc {where}ORDER BY lc.yolo_index"
    ))


def get_class(con: duckdb.DuckDBPyConnection, class_id: str) -> dict[str, Any] | None:
    return _one(con.execute(
        f"SELECT {_CLASS_COLS} FROM label_classes lc WHERE lc.class_id = ?", [class_id]
    ))


def create_class(
    con: duckdb.DuckDBPyConnection,
    *,
    name: str,
    color: str | None = None,
    description: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Create a class. Annotators may do this — a taxonomy they cannot extend
    mid-session gets worked around by mislabeling.

    `yolo_index` is `max(yolo_index) + 1` over ALL classes, archived included.
    Never a gap-filler and never a renumber: the index is the class's identity in
    every exported label file, so reusing an archived class's index would
    silently relabel every crop in every dataset exported before it was archived.

    `color` is optional and defaults to the first unused palette entry. Raises
    `DuplicateClass` when the name (case-insensitively) or its slug is taken."""
    label = (name or "").strip()
    if not label:
        raise ValueError("a class needs a name")
    class_id = _slug(label)
    if not class_id:
        raise ValueError(f"class name {name!r} has no letters or digits to make an id from")
    clash = con.execute(
        "SELECT class_id, name FROM label_classes WHERE class_id = ? OR lower(name) = lower(?)",
        [class_id, label],
    ).fetchone()
    if clash:
        raise DuplicateClass(
            f"class {clash[1]!r} already exists (id {clash[0]!r}) — archived classes "
            "still hold their name and index"
        )
    yolo_index = int(con.execute(
        "SELECT coalesce(max(yolo_index), -1) + 1 FROM label_classes"
    ).fetchone()[0])
    hex_color = _valid_color(color) if color else _pick_color(con, yolo_index)
    desc = description.strip() if isinstance(description, str) else None
    con.execute(
        "INSERT INTO label_classes (class_id, name, color, yolo_index, description, "
        "archived, created_by, created_at) VALUES (?, ?, ?, ?, ?, FALSE, ?, now())",
        [class_id, label, hex_color, yolo_index, desc or None, actor],
    )
    _audit_class(
        con, class_id=class_id, action="create",
        detail={"name": label, "color": hex_color, "yolo_index": yolo_index},
        actor=actor,
    )
    row = get_class(con, class_id)
    assert row is not None
    return row


_CLASS_UPDATABLE: tuple[str, ...] = ("name", "color", "description", "archived")


def update_class(con: duckdb.DuckDBPyConnection, class_id: str, **fields: Any) -> dict[str, Any]:
    """Edit a class. Accepts `name`, `color`, `description`, `archived` and an
    optional `actor` for the audit row.

    A field is changed only when its key is PRESENT, so `description=None` clears
    the description while omitting it leaves it alone. `class_id` never changes,
    even when the name does: it is what every mask references.

    Renaming is safe — exports key `data.yaml` by `yolo_index`, and the index is
    untouched here — so a class whose definition drifts should be renamed, not
    archived-and-recreated, which would burn an index and split its masks."""
    actor = fields.pop("actor", None)
    unknown = sorted(set(fields) - set(_CLASS_UPDATABLE))
    if unknown:
        raise ValueError(f"unknown class field(s): {', '.join(unknown)}")
    before = get_class(con, class_id)
    if before is None:
        raise NotFound(f"unknown class {class_id!r}")

    sets: list[str] = []
    params: list[Any] = []
    if "name" in fields:
        label = (fields["name"] or "").strip()
        if not label:
            raise ValueError("a class name cannot be blanked")
        clash = con.execute(
            "SELECT name FROM label_classes WHERE lower(name) = lower(?) AND class_id != ?",
            [label, class_id],
        ).fetchone()
        if clash:
            raise DuplicateClass(f"class {clash[0]!r} already exists")
        sets.append("name = ?")
        params.append(label)
    if "color" in fields:
        sets.append("color = ?")
        params.append(_valid_color(fields["color"]))
    if "description" in fields:
        d = fields["description"]
        sets.append("description = ?")
        params.append(d.strip() or None if isinstance(d, str) else None)
    if "archived" in fields:
        sets.append("archived = ?")
        params.append(bool(fields["archived"]))

    if sets:
        con.execute(
            f"UPDATE label_classes SET {', '.join(sets)} WHERE class_id = ?",
            [*params, class_id],
        )
        _audit_class(con, class_id=class_id, action="update",
                     detail={"before": before, "changed": dict(fields)}, actor=actor)
    row = get_class(con, class_id)
    assert row is not None
    return row


def archive_class(
    con: duckdb.DuckDBPyConnection, class_id: str, *, actor: str | None
) -> dict[str, Any]:
    """Soft-delete a class: it leaves the picker, keeps its masks, and keeps its
    `yolo_index` reserved forever (see `create_class`). There is no hard delete —
    dropping a class would strand every polygon drawn under it, and those are
    labeling hours."""
    before = get_class(con, class_id)
    if before is None:
        raise NotFound(f"unknown class {class_id!r}")
    con.execute("UPDATE label_classes SET archived = TRUE WHERE class_id = ?", [class_id])
    _audit_class(con, class_id=class_id, action="archive",
                 detail={"name": before["name"], "n_masks": before["n_masks"]}, actor=actor)
    row = get_class(con, class_id)
    assert row is not None
    return row


# ------------------------------------------------------------------------- masks
# Mask (SPEC section 6) plus `image_id`. NOTE: `points` here is SOURCE-IMAGE
# coordinates; the TS type documents crop-local, and api.py is what converts.
_MASK_COLS = """
    m.mask_id, m.crop_id, m.image_id, m.class_id, m.points,
    m.created_by, m.created_at, m.updated_at
"""


def _mask_rows(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Rows with `points` parsed from the JSON column into a list of pairs."""
    rows = _rows(cur)
    for r in rows:
        r["points"] = json.loads(r["points"]) if isinstance(r["points"], str) else r["points"]
    return rows


def list_masks(
    con: duckdb.DuckDBPyConnection,
    *,
    crop_id: str | None = None,
    image_id: str | None = None,
) -> list[dict[str, Any]]:
    """Live masks for a crop or a whole image, oldest first. Soft-deleted rows
    are excluded here and in every count — `deleted` masks exist only so an
    accidental delete is recoverable by an operator, never to be rendered."""
    where = ["NOT m.deleted"]
    params: list[Any] = []
    if crop_id is not None:
        where.append("m.crop_id = ?")
        params.append(crop_id)
    if image_id is not None:
        where.append("m.image_id = ?")
        params.append(image_id)
    return _mask_rows(con.execute(
        f"SELECT {_MASK_COLS} FROM masks m WHERE {' AND '.join(where)} "
        "ORDER BY m.created_at, m.mask_id",
        params,
    ))


def get_mask(con: duckdb.DuckDBPyConnection, mask_id: str) -> dict[str, Any] | None:
    """One mask, deleted or not — the API needs the deleted case to answer 404
    rather than 500 when a stale client PATCHes a mask somebody just removed."""
    rows = _mask_rows(con.execute(
        f"SELECT {_MASK_COLS} FROM masks m WHERE m.mask_id = ?", [mask_id]
    ))
    return rows[0] if rows else None


def create_mask(
    con: duckdb.DuckDBPyConnection,
    *,
    crop_id: str,
    image_id: str,
    class_id: str,
    points: Sequence[Sequence[float]],
    actor: str | None,
) -> dict[str, Any]:
    """Store one polygon. `points` are SOURCE-IMAGE pixels, stored verbatim —
    `api.py` has already added `crop.x/crop.y` and clamped to the crop rect.

    `image_id` is passed rather than derived from `crop_id` so this stays a pure
    write; it is denormalised on the row to keep per-image queries cheap."""
    mask_id = _uid()
    con.execute(
        "INSERT INTO masks (mask_id, crop_id, image_id, class_id, points, created_by, "
        "created_at, deleted) VALUES (?, ?, ?, ?, ?, ?, now(), FALSE)",
        [mask_id, crop_id, image_id, class_id, _points_json(points), actor],
    )
    row = get_mask(con, mask_id)
    assert row is not None
    return row


def update_mask(
    con: duckdb.DuckDBPyConnection,
    mask_id: str,
    *,
    class_id: str | None = None,
    points: Sequence[Sequence[float]] | None = None,
) -> dict[str, Any]:
    """Reclassify and/or reshape a mask. None means "leave unchanged".

    Refuses a mask that is already soft-deleted: an edit that silently
    resurrected one would make the delete look like it never happened."""
    row = _one(con.execute(
        "SELECT mask_id, deleted FROM masks WHERE mask_id = ?", [mask_id]
    ))
    if row is None or row["deleted"]:
        raise NotFound(f"unknown mask {mask_id!r}")
    sets: list[str] = []
    params: list[Any] = []
    if class_id is not None:
        sets.append("class_id = ?")
        params.append(class_id)
    if points is not None:
        sets.append("points = ?")
        params.append(_points_json(points))
    if sets:
        sets.append("updated_at = now()")
        con.execute(f"UPDATE masks SET {', '.join(sets)} WHERE mask_id = ?",
                    [*params, mask_id])
    out = get_mask(con, mask_id)
    assert out is not None
    return out


def soft_delete_mask(con: duckdb.DuckDBPyConnection, mask_id: str) -> None:
    """Flag a mask deleted. Never a DELETE: a user who removes the wrong
    polygon after twenty minutes of tracing wants it back, and the row costs
    nothing. Raises NotFound for an unknown id; deleting twice is a no-op."""
    if con.execute("SELECT 1 FROM masks WHERE mask_id = ?", [mask_id]).fetchone() is None:
        raise NotFound(f"unknown mask {mask_id!r}")
    con.execute(
        "UPDATE masks SET deleted = TRUE, updated_at = now() WHERE mask_id = ?", [mask_id]
    )


# ------------------------------------------------------------------------- stats
def stats(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Store-wide counters for GET /api/stats.

    `n_done` counts crops with `status='done'` — the only crops an export will
    ever contain — so the ratio `n_done / n_crops` is literally "how much of this
    dataset is usable", which is the number the landing page should lead with.

    `per_class` carries full LabelClass dicts so the frontend can reuse its own
    type. Archived classes appear only while they still hold masks: their masks
    remain in the exports, so hiding them entirely would make the totals
    disagree."""
    n_images, n_crops, n_done, n_masks = con.execute(
        """
        SELECT (SELECT count(*) FROM images),
               (SELECT count(*) FROM crops),
               (SELECT count(*) FROM crops WHERE status = 'done'),
               (SELECT count(*) FROM masks WHERE NOT deleted)
        """
    ).fetchone()
    per_class = _rows(con.execute(
        f"SELECT {_CLASS_COLS} FROM label_classes lc "
        "WHERE NOT lc.archived OR (SELECT count(*) FROM masks m "
        "                          WHERE m.class_id = lc.class_id AND NOT m.deleted) > 0 "
        "ORDER BY lc.yolo_index"
    ))
    return {
        "n_images": int(n_images),
        "n_crops": int(n_crops),
        "n_done": int(n_done),
        "n_masks": int(n_masks),
        "per_class": per_class,
    }


# -------------------------------------------------------------------------- meta
def get_meta(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_meta(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    """Upsert one meta row. Used for 'schema_version' and the backup watermark —
    both of which must survive a crash mid-write, hence one statement."""
    con.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [key, str(value)],
    )
