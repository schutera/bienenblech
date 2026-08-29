"""Periodic zips of the stores — one job per store, each an independent DB
snapshot + flat CSVs + the stored images, rotated locally and optionally posted
to a Discord webhook.

Annotator hours are the only thing on this box that cannot be regenerated. The
images can in principle be re-uploaded, but the polygons drawn against their
exact pixel grid cannot, so the backup carries *both*: labels without pixels are
worthless, which is why `images/<image_id>.jpg` is in the zip and why this module
is bulkier than cownting's ancestor. The Age tool's photos matter for the same
reason — an age judgment is only meaningful next to the exact photo it was made
against — but they no longer ride along in the Blech zip: the Age tool keeps its
rows in its OWN DuckDB store (`data/age.duckdb`), so it gets its OWN archive.

TWO STORES, TWO INDEPENDENT JOBS. Everything per-store is described by a
`_Store` descriptor (name, zip prefix, connect fn, CSV set, file enumerator,
watermark query, snapshot source) instantiated exactly twice:

- **blech** — `data/bienenblech.duckdb`: users + all Blech tables. Its zip is
  the pre-age member list (snapshot minus `users` per A11, the four CSVs, the
  frame images, manifest, README), named `bienenblech-<stamp>-<run>.zip`.
- **age** — `data/age.duckdb`: `age_samples` only. Its zip is the age snapshot
  (same `users` exclusion machinery run against it, belt and braces — the store
  carries no users table), `age_samples.csv`, the `age/<sample_id>.<ext>`
  photos, manifest, README, named `bienenblech-age-<stamp>-<run>.zip`.

Each store is SELF-DESCRIBING: its `backup_runs` history, its watermark and its
cooldown all live in that store's own `backup_runs`/`meta` tables, so either
file can be detached, restored or inspected alone and still knows what has been
archived. The consequence this buys, and the reason for the split jobs: due(),
the claim, the cooldown and the rotation are evaluated PER STORE — blech-only
activity never fires an age backup, an age failure never cools down blech, and
`backup.keep` applies to each zip prefix independently.

The schedule is one in-process daemon thread started from `create_app` that
ticks every 15 minutes and asks EACH store's DB *"has the interval elapsed
since the last successful run, and did anything land since its watermark?"* —
never `sleep(interval_days * 86400)`. A sleeping timer restarts from zero on
every process start, so on a box that is redeployed weekly a 7-day sleep is
reset at day 6 forever and the backup silently never fires. Asking the database
instead makes the gate stateless with respect to process lifetime: `due()`
compares persisted timestamps, so a hundred redeploys change nothing.

The failure taxonomy matters more than it looks, it applies to each store
separately, and it is the part to preserve verbatim if this module is ever
rewritten:

- **Contention** — the store is held by another process, its connect fn
  exhausts its retry budget, or the claim is refused — is `status='skipped'`,
  **no row written**, **no cooldown armed**, exit code 0. Without that split,
  an operator who wires `bienenblech backup --force` into a nightly host cron
  converts a transient lock error into a *permanently disabled* backup: every
  nightly failure would re-arm the 6-hour cooldown and the scheduler's tick
  would never find a green window.
- **Genuine failure** — disk full, torn snapshot, webhook unreachable — writes a
  `failed` row, prints a `[bienenblech.alert] BACKUP` line, arms a 6-hour
  cooldown, and does **not** advance the watermark, so nothing is ever silently
  dropped out of the next successful zip. The cooldown row lives in the failing
  store's own `backup_runs`, which is what makes it impossible for one store's
  broken week to suppress the other's.

The webhook URL comes from `BIENENBLECH_DISCORD_WEBHOOK` at the point of use,
never from Config or YAML (`config/` is committed and bind-mounted `:ro`). Both
stores post to the same webhook. Unset is a fully supported state: the jobs
still zip, still rotate and still advance their watermarks. The URL and its
token must never reach a log line, an exception string or a stored column —
`backup_runs.error` travels inside the very zip that gets posted to the channel
— so everything printed, raised or stored goes through `_redact()` first.

Import discipline: this module is opened by the CLI and by the scheduler thread
in processes that never serve HTTP, so it imports `db` and `config` and nothing
else from the package. In particular it must never import `api`, `uploads`,
`crops` or `age`, which drag Pillow in behind them — a `bienenblech backup` on a
box should not need the imaging stack to be importable to rescue the labels.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import duckdb

from . import __version__, db
from .config import BackupCfg, Config

# The env var holding the Discord webhook URL. Read at the point of use, never at
# import and never from YAML: a webhook URL is a bearer credential for posting
# into the channel, and `config/` is committed to the repo.
WEBHOOK_ENV = "BIENENBLECH_DISCORD_WEBHOOK"

# meta key: the latest change covered by the last SUCCESSFUL run — one row per
# store, in THAT STORE'S meta table, so the key name can stay identical in both.
# Kept in the store itself rather than a sidecar file so it travels inside every
# snapshot — a restored backup knows roughly what has already been posted instead
# of re-posting everything. Note that the copy inside a zip is one run stale by
# construction: the snapshot is taken before the run advances the watermark,
# because a run that has not finished must not claim its window. That error is in
# the safe direction — a restore backs up slightly more than it needs to.
META_WATERMARK = "backup_watermark"
META_SCHEMA_VERSION = "schema_version"

# First tick delay. LOAD-BEARING: longer than any test run, so the apps the test
# suite builds in temp dirs never fire a backup and the sleeping thread holds no
# file handle. Lowering it resurrects the Windows TemporaryDirectory-cleanup crash
# that this exact constant was introduced to fix in cownting: a backup firing
# mid-test keeps `data/bienenblech.duckdb` open, and Windows then refuses to
# remove the directory the test is tearing down.
_FIRST_TICK_S = 120
_TICK_S = 15 * 60

# Genuine-failure cooldown, per store. Long enough that a broken webhook does not
# spam the alert feed every 15 minutes, short enough that the weekly cadence is
# not visibly dented.
_COOLDOWN_HOURS = 6

# A 'running' claim older than this is treated as abandoned (runner killed
# mid-run) and closed out as failed, so a crash cannot wedge the job forever.
_CLAIM_LEASE_HOURS = 1

_ALERT = "[bienenblech.alert] BACKUP"
_UA = "bienenblech-backup/1.0"

# Accept only real Discord webhook endpoints. Anything else is refused WITHOUT
# logging the value — a typo'd URL is still someone's URL.
_WEBHOOK_OK_RE = re.compile(
    r"^https://(?:[\w.-]+\.)?discord(?:app)?\.com/api/webhooks/\S+$", re.IGNORECASE
)
# Redaction net for text that merely CONTAINS a webhook URL: urllib puts the full
# URL into HTTPError attributes, and third-party text can embed one anywhere.
_WEBHOOK_ANY_RE = re.compile(
    r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE
)

_TXN_EXC = getattr(duckdb, "TransactionException", duckdb.Error)

# Tables that must never travel inside a zip (A11). SPEC section 8 mandates
# posting these archives to a Discord webhook and section 4 puts usernames and
# scrypt password hashes in `users`, which would make that channel exactly as
# sensitive as the box. The accounts are the one cheap thing in here: a restore
# re-bootstraps the admin from BIENENBLECH_ADMIN_* and the remaining accounts
# are recreated by hand, unlike the annotations. Applied to BOTH stores' snapshots
# even though only the main store carries `users` — the drop is IF EXISTS, and a
# future table that wanders into the age store is then excluded with no edit here.
# See `snapshot_db` for why this is an exclusion from the copy rather than a
# delete afterwards.
_SNAPSHOT_EXCLUDED_TABLES: tuple[str, ...] = ("users",)

# poster(webhook_url, content, file_path_or_None) -> None, raising on failure.
# Injected so the oversize ladder is unit-testable with no network.
Poster = Callable[[str, str, "Path | None"], None]


class _StoreBusy(Exception):
    """The store is held elsewhere — the CONTENTION class, never a failure."""


# ------------------------------------------------------------------- redaction

def _redact(text: str, webhook: str | None = None) -> str:
    """Scrub the webhook URL and its token from any string before it is printed,
    raised onward, or stored.

    `backup_runs.error` travels inside the zip that gets posted to the channel,
    so a raw URL there hands out the ability to post as this box to anyone who
    can read the archive. When `webhook` is None the current env value is
    scrubbed too, so a caller cannot forget to pass it."""
    out = _WEBHOOK_ANY_RE.sub("<discord-webhook>", text or "")
    if webhook is None:
        webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if webhook:
        out = out.replace(webhook, "<discord-webhook>")
        token = webhook.rstrip("/").rsplit("/", 1)[-1]
        # Only a real token: replacing a short trailing segment ("1", "api")
        # would chew holes in unrelated text.
        if len(token) >= 8:
            out = out.replace(token, "<token>")
    return out


def _looks_like_webhook(url: str) -> bool:
    return bool(_WEBHOOK_OK_RE.match(url))


# ------------------------------------------------------------------- store access

# Contention is classified with `db.is_transient_lock_error`, never a private
# substring list. The same collision is worded differently per platform, and a
# wording added to one list and not the other turns a routine contention *skip*
# into a spurious *failure* — which arms the six-hour cooldown, i.e. the
# divergence silently suppresses backups. One definition, in db.py.
_is_lock_error = db.is_transient_lock_error


def _connect_file(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB file by path, retrying past the momentary handle collision.

    The store connect fns take a Config, but `snapshot_db` takes plain paths on
    purpose — it must also work against a restored file or a test fixture with no
    Config in sight — so the bounded retry is repeated here. Read-write, never
    read-only: DuckDB refuses a second connection to one file opened with a
    different mode in the same process, and `_run_store` already holds a
    read-write handle on it while the snapshot runs."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    delay = 0.02
    last: Exception | None = None
    for _ in range(50):
        try:
            return duckdb.connect(str(path))
        except Exception as e:  # noqa: BLE001 — retry only the handle clash
            if _is_lock_error(e):
                last = e
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)
                continue
            raise
    assert last is not None
    raise last  # retries exhausted — surface the real DuckDB error


def ensure_backup_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the run-history and meta tables. Idempotent, additive. Run against
    BOTH stores — each carries its own copy, which is the whole point of the
    per-store split: history, watermark and cooldown travel with their store.

    `db.init_db` declares the same DDL — deliberately, so the watermark and the
    run history travel inside the very snapshot they describe (a snapshot without
    them would restore as "never backed up" and re-post everything). It is
    re-declared here because a backup that dies on its own bookkeeping is the
    silent kind of dead: the CLI must work on a box where the server has never
    booted, and against a store written by a build that predates a column."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_runs (
            run_id TEXT PRIMARY KEY,
            started_at TIMESTAMP, finished_at TIMESTAMP,
            status TEXT,            -- 'running' | 'ok' | 'failed'
            "trigger" TEXT,         -- 'schedule' | 'manual' | 'cli'
            n_masks BIGINT, n_images BIGINT, "bytes" BIGINT,
            zip_path TEXT, delivered BOOLEAN, error TEXT, host TEXT
        );
        """
    )
    # Additive: the SPEC's `delivered BOOLEAN` records THAT the zip arrived but
    # not what happened instead when it did not, and "over the upload cap, summary
    # posted" has to be distinguishable from "no webhook configured" when someone
    # asks why the channel is quiet.
    con.execute("ALTER TABLE backup_runs ADD COLUMN IF NOT EXISTS delivery TEXT;")
    con.execute(
        'CREATE TABLE IF NOT EXISTS meta ("key" TEXT PRIMARY KEY, "value" TEXT);'
    )


def _ensure_age_columns(con: duckdb.DuckDBPyConnection) -> None:
    """The age-store columns THIS module depends on, re-declared additively.

    `db.init_age_db` carries the same `updated_at` migration — same twin-DDL
    discipline as the `delivery` column above, and for the same reason: the CLI
    must be able to rescue the samples from an age store written by a build that
    predates the column, on a box whose server has never booted this code. The
    watermark below reads `updated_at`, so a missing column would turn every
    backup of an older store into a genuine failure. Gated on the table existing
    (ALTER on a missing table raises): a foreign or hand-restored file without
    `age_samples` is handled further down as an empty store, never an error."""
    if _has_table(con, "age_samples"):
        con.execute(
            "ALTER TABLE age_samples ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"
        )


def _quote(identifier: str) -> str:
    """Escape a SQL identifier for interpolation inside double quotes.

    The table names `snapshot_db` interpolates come from the catalog, not from
    a user, so this is belt and braces — but the alternative is an f-string
    dropping an unescaped name straight into DDL, which is not the shape of
    code that should sit in the module holding the only copy of the labels.
    """
    return identifier.replace('"', '""')


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] = ()) -> Any:
    row = con.execute(sql, list(params)).fetchone()
    return row[0] if row else None


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Does the connected catalog carry `name`? The age-store queries are gated
    on this even though `db.init_age_db` creates the table, because this module
    must also work against files no init ever touched (a restored `age.duckdb`,
    a test fixture). It is also why the MAIN store needs no gate any more: since
    the split, `age_samples` is PERMANENTLY absent from a healthy main store —
    the one-time boot migration drops it — so the blech job simply never asks.
    A gate rather than a blanket try/except, so a real error in an age query
    still surfaces as the genuine-failure class instead of quietly emptying the
    archive."""
    return bool(
        _scalar(
            con,
            "SELECT count(*) FROM duckdb_tables() "
            "WHERE database_name = current_database() AND table_name = ?",
            [name],
        )
    )


def _last_change(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """Newest timestamp of anything worth backing up in the BLECH store, or None
    on an empty store.

    Three sources, not one: a mask edited, a crop completed, or an image uploaded
    all represent work that would hurt to lose. Watching only `masks` would leave
    a box that has been uploading frames all week reading as "nothing new", and
    the images are the expensive half of the zip.

    The Age tool (A15) is deliberately NOT a source any more: its rows live in
    their own store with their own watermark (`_age_last_change`). A main store
    that has not yet been through the one-time boot migration may still carry a
    legacy `age_samples` table — those rows are ignored here on purpose (they
    still travel inside the DB snapshot until they move, so nothing is lost) and
    they must never fire a BLECH backup: blech-only activity fires only the
    blech zip and vice versa, which is the contract the per-store split exists
    to keep."""
    stamps = [
        _scalar(con, "SELECT max(greatest(created_at, coalesce(updated_at, created_at))) FROM masks"),
        _scalar(con, "SELECT max(completed_at) FROM crops"),
        _scalar(con, "SELECT max(uploaded_at) FROM images"),
    ]
    live = [s for s in stamps if isinstance(s, datetime)]
    return max(live) if live else None


def _age_last_change(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """Newest timestamp of anything worth backing up in the AGE store.

    `max(uploaded_at, updated_at)` — where `updated_at` is stamped by annotate,
    flag AND reopen. The stamp on flag is the watermark fix this split rode in
    with: a flag used to write no timestamp at all, so a week that produced only
    flags (annotator judgment, unrecoverable) read as "nothing new" and never
    fired a backup. `annotated_at` is no longer consulted: every path that sets
    it now also sets `updated_at`, and the legacy rows migrated from the main
    store are covered anyway — a fresh age store has no watermark, so its first
    run archives everything regardless of stamps."""
    if not _has_table(con, "age_samples"):
        return None
    stamp = _scalar(
        con,
        "SELECT max(greatest(uploaded_at, coalesce(updated_at, uploaded_at))) "
        "FROM age_samples",
    )
    return stamp if isinstance(stamp, datetime) else None


# ------------------------------------------------------------------- flat exports

# One CSV per table, flat and boring on purpose: these are the members that
# outlive DuckDB itself. A future reader with nothing but a text editor can still
# recover who labeled what, and `masks.csv` carries the class name and the crop
# rect alongside the raw JSON points so the polygons can be re-normalized without
# a join against a file format that may no longer open.
_BLECH_CSV_SQL: dict[str, str] = {
    "images.csv": """
        SELECT image_id, filename, sha256, width, height, stored_path, bytes,
               crop_size, crop_overlap, uploaded_by, uploaded_at, note
        FROM images ORDER BY uploaded_at, image_id
    """,
    "crops.csv": """
        SELECT crop_id, image_id, row_idx, col_idx, x, y, w, h, status, is_empty,
               completed_by, completed_at
        FROM crops ORDER BY image_id, row_idx, col_idx
    """,
    "classes.csv": """
        SELECT class_id, name, color, yolo_index, description, archived,
               created_by, created_at
        FROM label_classes ORDER BY yolo_index
    """,
    # Deleted masks are INCLUDED: soft delete is the SPEC's rule everywhere, and a
    # backup that drops them would make the archive the one place an accidental
    # delete becomes permanent.
    "masks.csv": """
        SELECT m.mask_id, m.crop_id, m.image_id, m.class_id, lc.name AS class_name,
               lc.yolo_index, c.x AS crop_x, c.y AS crop_y, c.w AS crop_w, c.h AS crop_h,
               m.points, m.created_by, m.created_at, m.updated_at, m.deleted
        FROM masks m
        LEFT JOIN label_classes lc ON lc.class_id = m.class_id
        LEFT JOIN crops c ON c.crop_id = m.crop_id
        ORDER BY m.image_id, m.crop_id, m.created_at
    """,
}

# Flagged samples are INCLUDED, unlike the training export at /api/age/export: a
# flag and its reason are annotator judgment too, and this archive is the store
# of record, not a training set — dropping them here would make the backup the
# one place a flag disappears. `updated_at` rides in the CSV so the flat rescue
# preserves the same "when did this last change" answer the watermark reads.
_AGE_CSV_SQL: dict[str, str] = {
    "age_samples.csv": """
        SELECT sample_id, filename, sha256, width, height, stored_path, bytes,
               status, age_days, annotated_by, annotated_at, flag_reason,
               uploaded_by, uploaded_at, updated_at
        FROM age_samples ORDER BY uploaded_at, sample_id
    """,
}

# CSVs whose table may legitimately be absent from the connected file (a
# restored or foreign store): emitted only when the table exists, so a run
# still delivers the other members rather than dying. Only tables listed here
# degrade — a typo in a core CSV's SQL must keep failing loudly.
_AGE_CSV_OPTIONAL_TABLE: dict[str, str] = {"age_samples.csv": "age_samples"}


def _export_csvs(
    con: duckdb.DuckDBPyConnection, out_dir: Path, store: "_Store"
) -> list[str]:
    written: list[str] = []
    for name, sql in store.csv_sql.items():
        table = store.csv_optional_table.get(name)
        if table is not None and not _has_table(con, table):
            continue
        safe = str(out_dir / name).replace("'", "''")
        con.execute(f"COPY ({sql}) TO '{safe}' (HEADER, DELIMITER ',')")
        written.append(name)
    return written


def _image_members(con: duckdb.DuckDBPyConnection) -> tuple[list[tuple[str, Path]], list[str]]:
    """(zip members, image_ids whose file is missing) for the stored derivatives.

    Enumerated from the DB, never a directory walk of `images_dir`: a walk sweeps
    up whatever else happens to be sitting there, and this zip gets posted to a
    chat channel. A file the DB does not know about is not part of the dataset.

    A missing file is recorded and alerted, not raised. Losing one derivative to
    filesystem drift must not cost the whole backup — the labels for every other
    image are the expensive thing in here."""
    members: list[tuple[str, Path]] = []
    missing: list[str] = []
    rows = con.execute(
        "SELECT image_id, stored_path FROM images ORDER BY image_id"
    ).fetchall()
    for image_id, stored_path in rows:
        path = Path(str(stored_path))
        if not path.is_file():
            missing.append(str(image_id))
            continue
        # The DDL pins the derivative to .jpg; honour a different suffix if one
        # ever appears rather than silently mislabelling the member.
        members.append((f"images/{image_id}{path.suffix or '.jpg'}", path))
    return members, missing


def _age_members(con: duckdb.DuckDBPyConnection) -> tuple[list[tuple[str, Path]], list[str]]:
    """(zip members, sample_ids whose file is missing) for the Age tool's photos.

    Same rules as `_image_members`, for the same reasons: enumerated from
    `age_samples` — with the stored extension, never an assumed one — and never a
    walk of `data/age/`; a missing file is recorded and alerted rather than
    raised. A connected file without the table at all (a foreign or hand-built
    store) is a working store, not an error, so the answer is an empty member
    list, never a raise that would fail the whole run."""
    if not _has_table(con, "age_samples"):
        return [], []
    members: list[tuple[str, Path]] = []
    missing: list[str] = []
    rows = con.execute(
        "SELECT sample_id, stored_path FROM age_samples ORDER BY sample_id"
    ).fetchall()
    for sample_id, stored_path in rows:
        path = Path(str(stored_path))
        if not path.is_file():
            missing.append(str(sample_id))
            continue
        members.append((f"age/{sample_id}{path.suffix or '.jpg'}", path))
    return members, missing


# ------------------------------------------------------------------- claim counts

def _blech_claim_counts(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """(n_masks, n_images) for the blech run row — the SPEC's original meaning."""
    return (
        int(_scalar(con, "SELECT count(*) FROM masks WHERE NOT deleted") or 0),
        int(_scalar(con, "SELECT count(*) FROM images") or 0),
    )


def _age_claim_counts(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """(n_masks, n_images) for the age run row. The columns are the shared
    `backup_runs` DDL's, re-purposed per store rather than renamed: here
    `n_images` is samples on the box and `n_masks` is how many carry an answer —
    the two numbers an operator reading the age store's history actually wants.
    Guarded on the table for the same foreign-file reason as everything else."""
    if not _has_table(con, "age_samples"):
        return 0, 0
    return (
        int(_scalar(con, "SELECT count(*) FROM age_samples WHERE status = 'done'") or 0),
        int(_scalar(con, "SELECT count(*) FROM age_samples") or 0),
    )


# ------------------------------------------------------------------- bundle text

# Per-store manifest counts. Every aggregate is individually guarded at build
# time: a future schema change must degrade the manifest, not raise a KeyError
# that escapes the bundle build, stamps the run failed, holds the watermark and
# silently disables the job behind a six-hour cooldown.
#
# Every count is of the LIVE store, which for `users` is the one row that
# describes something the zip does NOT contain — hence the key name. It is kept
# rather than dropped because a bare integer is not a secret and it is the
# number the restorer needs: how many accounts to recreate by hand. The age
# store carries no users at all (they are GLOBAL, in the main store), so its
# list simply does not ask.
_BLECH_MANIFEST_COUNTS: tuple[tuple[str, str], ...] = (
    ("images", "SELECT count(*) FROM images"),
    ("crops", "SELECT count(*) FROM crops"),
    ("crops_done", "SELECT count(*) FROM crops WHERE status = 'done'"),
    ("crops_empty", "SELECT count(*) FROM crops WHERE is_empty"),
    ("masks", "SELECT count(*) FROM masks WHERE NOT deleted"),
    ("masks_deleted", "SELECT count(*) FROM masks WHERE deleted"),
    ("classes", "SELECT count(*) FROM label_classes"),
    ("users_excluded", "SELECT count(*) FROM users"),
)

_AGE_MANIFEST_COUNTS: tuple[tuple[str, str], ...] = (
    ("age_samples", "SELECT count(*) FROM age_samples"),
    ("age_open", "SELECT count(*) FROM age_samples WHERE status = 'open'"),
    ("age_done", "SELECT count(*) FROM age_samples WHERE status = 'done'"),
    ("age_flagged", "SELECT count(*) FROM age_samples WHERE status = 'flagged'"),
)


def _blech_manifest_extras(con: duckdb.DuckDBPyConnection, man: dict[str, Any]) -> None:
    """The per-class mask histogram — blech-only, guarded like every count."""
    try:
        man["per_class"] = {
            str(name): int(n)
            for name, n in con.execute(
                "SELECT lc.name, count(*) FROM masks m "
                "JOIN label_classes lc ON lc.class_id = m.class_id "
                "WHERE NOT m.deleted GROUP BY lc.name ORDER BY lc.name"
            ).fetchall()
        }
    except Exception:  # noqa: BLE001 — degrade, never fail the bundle
        pass


def _manifest(
    store: "_Store",
    con: duckdb.DuckDBPyConnection,
    *,
    run: Mapping[str, Any],
    trigger: str,
    members: Sequence[str],
    missing: Sequence[str],
) -> dict[str, Any]:
    """Bundle metadata for one store's zip. `missing_images` is generic on
    purpose — for the age zip it lists sample_ids — so a reader of either
    manifest checks the same key to learn what filesystem drift cost."""
    man: dict[str, Any] = {
        "kind": store.manifest_kind,
        "store": store.name,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app_version": __version__,
        "duckdb_version": getattr(duckdb, "__version__", None),
        "host": run.get("host"),
        "run_id": run.get("run_id"),
        "trigger": trigger,
        "watermark_from": str(run["watermark_from"]) if run.get("watermark_from") else None,
        "watermark_to": str(run["watermark_to"]) if run.get("watermark_to") else None,
        "members": list(members),
        "missing_images": list(missing),
        # Machine-readable statement of what the snapshot deliberately omits, so a
        # restorer never has to infer "no users table" from an absence (A11).
        "snapshot_excluded_tables": list(_SNAPSHOT_EXCLUDED_TABLES),
    }
    try:
        man["schema_version"] = db.get_meta(con, META_SCHEMA_VERSION)
    except Exception:  # noqa: BLE001 — degrade, never fail the bundle
        pass
    counts: dict[str, Any] = {}
    for key, sql in store.manifest_counts:
        try:
            counts[key] = int(_scalar(con, sql) or 0)
        except Exception:  # noqa: BLE001
            pass
    man["counts"] = counts
    if store.manifest_extras is not None:
        store.manifest_extras(con, man)
    return man


def _blech_readme(man: Mapping[str, Any], *, stamp: str, trigger: str) -> str:
    """The Blech zip's README — the pre-age member list, restored: since the
    split, nothing of the Age tool is in this archive, and a README describing
    members a zip does not contain would send a restorer chasing phantoms."""
    counts = man.get("counts") or {}
    return f"""bienenblech backup
==================

Created {stamp} (UTC) by run {man.get('run_id')} (trigger: {trigger}) on
{man.get('host')}, bienenblech {man.get('app_version')}.

{counts.get('masks')} masks over {counts.get('crops_done')} completed crops,
{counts.get('images')} images, {counts.get('classes')} classes.

Members
-------
    bienenblech.duckdb   engine-consistent snapshot of the store, minus the
                         `users` table (see Privacy); not a live-file copy
    images.csv           one row per uploaded frame
    crops.csv            the crop grid, its status and is_empty flags
    classes.csv          label classes including archived ones, with yolo_index
    masks.csv            one row per polygon, points as JSON in SOURCE-IMAGE px,
                         with the crop rect alongside; soft-deleted rows included
    images/<id>.jpg      every stored derivative
    manifest.json        counts, versions, host, watermark
    README.txt           this file

The images are in here because labels without pixels are worthless: every polygon
is stored in the coordinate space of its derivative, so a restored DB next to
different pixels is not a dataset.

The Age tool is NOT in here: its samples live in their own store
(`data/age.duckdb`) with their own archive (`bienenblech-age-<stamp>-<run>.zip`).

Restore
-------
1. Stop the app:  docker compose down
2. Copy `bienenblech.duckdb` over `data/bienenblech.duckdb` on the host.
   THIS REPLACES EVERY ACCOUNT. The snapshot carries no `users` table, so the
   restored store has no logins at all. That is the accepted trade: accounts are
   cheap to recreate, annotations are not. On the next boot the app recreates the
   empty table and seeds ONE admin from BIENENBLECH_ADMIN_USER /
   BIENENBLECH_ADMIN_PASSWORD — falling back to admin / admin if they are unset,
   and saying so on stdout — so set them before step 4 unless you want the
   default. Every other account has to be recreated by hand on the Admin page:
   there were {counts.get('users_excluded')} account(s) on this box in total when
   the archive was written, and their passwords are not in here and cannot be
   recovered from it.
3. Unpack `images/` into `data/images/` (the DB's stored_path points there).
4. docker compose up -d  — the entrypoint re-owns drifted files on boot.

`data/cache/` is not in this archive on purpose: crop JPEGs are regenerated on
demand from the derivatives, and shipping them would double the size for nothing.

Privacy
-------
The DuckDB snapshot deliberately EXCLUDES the `users` table, so there is no
scrypt password hash anywhere in this archive. The table is never copied in the
first place rather than copied and then dropped, because a dropped table's blocks
go back on DuckDB's free list without being zeroed and the hashes would still be
readable in the file. The reason for the exclusion is this archive's destination:
it is posted to a chat channel, and hashes in the channel would make the channel
as sensitive as the box itself.

Usernames DO appear, and that is intended: `images.uploaded_by`,
`crops.completed_by`, `masks.created_by`, `label_classes.created_by`,
`class_audit.actor`, and the same columns in the CSVs. A dataset
that cannot say who labeled what is a worse dataset. Usernames are not secrets;
password hashes are.
"""


def _age_readme(man: Mapping[str, Any], *, stamp: str, trigger: str) -> str:
    """The Age zip's README — written for a reader holding ONLY this zip: what
    it is, how to put the Age tool back, and where the things it deliberately
    does not contain (accounts, the Blech labels) actually live."""
    counts = man.get("counts") or {}
    return f"""bienenblech age backup
======================

Created {stamp} (UTC) by run {man.get('run_id')} (trigger: {trigger}) on
{man.get('host')}, bienenblech {man.get('app_version')}.

{counts.get('age_samples')} bee photos for the Age tool,
{counts.get('age_done')} of them age-annotated, {counts.get('age_flagged')} flagged.

This archive is the Age tool's COMPLETE state. The Age tool keeps its rows in
its own DuckDB store (`data/age.duckdb`), separate from the main Blech store,
so this zip restores on its own — you do not need the Blech archive
(`bienenblech-<stamp>-<run>.zip`) to bring the Age tool back, nor this one to
bring Blech back.

Members
-------
    age.duckdb           engine-consistent snapshot of the Age store; not a
                         live-file copy
    age_samples.csv      one row per sample: status, age_days (28 meaning 28+),
                         who judged it and when, flag reason, updated_at;
                         flagged rows included — a flag and its reason are
                         annotator judgment too, and this archive is the store
                         of record, not a training set
    age/<id>.jpg         every stored bee photo (the stored extension is kept,
                         whatever it is)
    manifest.json        counts, versions, host, watermark
    README.txt           this file

The photos are in here because an age judgment is only meaningful next to the
exact photo it was made against — a restored DB next to different pixels is not
a dataset.

Restore
-------
1. Stop the app:  docker compose down
2. Copy `age.duckdb` over `data/age.duckdb` on the host.
3. Unpack `age/` into `data/age/` (the DB's stored_path points there).
4. docker compose up -d  — the entrypoint re-owns drifted files on boot.

Accounts
--------
There are NO user accounts in this archive, and none in the Age store at all:
accounts are global and live in the MAIN store (`data/bienenblech.duckdb`),
which has its own archive. Restoring this zip alone restores every sample and
every judgment; logins come from the main store, or on a bare box the app seeds
one admin from BIENENBLECH_ADMIN_USER / BIENENBLECH_ADMIN_PASSWORD at boot.

Usernames DO appear (`age_samples.uploaded_by`, `age_samples.annotated_by`, and
the same columns in the CSV), and that is intended: a dataset that cannot say
who judged what is a worse dataset. Usernames are not secrets; password hashes
are — and there is no password hash anywhere in this archive.
"""


def _blech_post_line(man: Mapping[str, Any], zip_bytes: int, stamp: str) -> str:
    counts = man.get("counts") or {}
    return (
        f"bienenblech backup {stamp}: {counts.get('masks')} masks, "
        f"{counts.get('crops_done')} completed crops, {counts.get('images')} images "
        f"({zip_bytes / 1e6:.1f} MB)."
    )


def _age_post_line(man: Mapping[str, Any], zip_bytes: int, stamp: str) -> str:
    counts = man.get("counts") or {}
    return (
        f"bienenblech age backup {stamp}: {counts.get('age_samples')} bee photos, "
        f"{counts.get('age_done')} age-annotated, {counts.get('age_flagged')} flagged "
        f"({zip_bytes / 1e6:.1f} MB)."
    )


# ------------------------------------------------------------------- descriptors

@dataclass(frozen=True)
class _Store:
    """Everything that differs between the two backup jobs, in one place.

    The run/claim/deliver/prune machinery below is written once against this
    shape; the two instances at the bottom of this section are the only spots
    that know what "blech" and "age" concretely are. Adding a third store —
    should another tool ever grow its own file — is one more instance, not a
    third copy of the taxonomy.

    Every callable that touches `db` is a lambda, not a bound reference: the
    core module and this one land changes in parallel, and late binding means a
    monkeypatched or freshly-reloaded `db` is picked up at call time."""

    name: str                       # 'blech' | 'age' — tags results, runs, alerts
    zip_prefix: str                 # leads every archive name; rotation keys on it
    snapshot_member: str            # the snapshot's member name inside the zip
    manifest_kind: str
    db_path: Callable[[Config], str]
    connect: Callable[[Config], duckdb.DuckDBPyConnection]
    init: Callable[[duckdb.DuckDBPyConnection], None]
    csv_sql: Mapping[str, str]
    csv_optional_table: Mapping[str, str]
    file_members: Callable[
        [duckdb.DuckDBPyConnection], tuple[list[tuple[str, Path]], list[str]]
    ]
    missing_noun: str               # for the missing-file alert line
    last_change: Callable[[duckdb.DuckDBPyConnection], "datetime | None"]
    claim_counts: Callable[[duckdb.DuckDBPyConnection], tuple[int, int]]
    manifest_counts: Sequence[tuple[str, str]]
    manifest_extras: "Callable[[duckdb.DuckDBPyConnection, dict[str, Any]], None] | None"
    readme: Callable[..., str]
    post_line: Callable[[Mapping[str, Any], int, str], str]
    ensure: "Callable[[duckdb.DuckDBPyConnection], None] | None" = None

    def zip_name(self, stamp: str, run_id: str) -> str:
        return f"{self.zip_prefix}-{stamp}-{run_id[:8]}.zip"

    @property
    def zip_re(self) -> re.Pattern[str]:
        """Exact-shape matcher for THIS store's archives. A prefix glob alone
        cannot do rotation any more: 'bienenblech' is a prefix of
        'bienenblech-age', so a glob-based blech prune would count — and could
        delete — the age archives. The full-shape match keeps each prefix's
        rotation blind to the other's zips (and to any operator stray)."""
        return re.compile(
            rf"^{re.escape(self.zip_prefix)}-\d{{8}}T\d{{6}}Z-[0-9a-f]{{8}}\.zip$"
        )


_BLECH = _Store(
    name="blech",
    zip_prefix="bienenblech",
    snapshot_member="bienenblech.duckdb",
    manifest_kind="bienenblech-backup",
    db_path=lambda config: config.paths.db_path,
    connect=lambda config: db.connect(config),
    init=lambda con: db.init_db(con),        # idempotent by contract (SPEC section 4)
    csv_sql=_BLECH_CSV_SQL,
    csv_optional_table={},
    file_members=_image_members,
    missing_noun="image file(s)",
    last_change=_last_change,
    claim_counts=_blech_claim_counts,
    manifest_counts=_BLECH_MANIFEST_COUNTS,
    manifest_extras=_blech_manifest_extras,
    readme=_blech_readme,
    post_line=_blech_post_line,
)

_AGE = _Store(
    name="age",
    zip_prefix="bienenblech-age",
    snapshot_member="age.duckdb",
    manifest_kind="bienenblech-age-backup",
    db_path=lambda config: config.paths.age_db_path,
    connect=lambda config: db.connect_age(config),
    init=lambda con: db.init_age_db(con),    # idempotent, same contract as init_db
    csv_sql=_AGE_CSV_SQL,
    csv_optional_table=_AGE_CSV_OPTIONAL_TABLE,
    file_members=_age_members,
    missing_noun="age sample file(s)",
    last_change=_age_last_change,
    claim_counts=_age_claim_counts,
    manifest_counts=_AGE_MANIFEST_COUNTS,
    manifest_extras=None,
    readme=_age_readme,
    post_line=_age_post_line,
    ensure=_ensure_age_columns,
)

# Blech first: it is the store with years of history and the one the legacy
# top-level result/status fields mirror (see `run_backup` and `status`).
_STORES: tuple[_Store, ...] = (_BLECH, _AGE)


def _open_store(store: _Store, config: Config) -> duckdb.DuckDBPyConnection:
    """Open one store read-write and make sure this module's schema exists in it.

    Read-write even though a backup mostly reads: DuckDB refuses a second
    connection to one file with a different mode in the same process, and the
    snapshot below opens the same file again. A connect failure that is the
    cross-process lock is re-raised as `_StoreBusy` — the contention class,
    reported as `skipped` rather than `failed` — because the server in the
    container legitimately holds the file whenever a write is in flight."""
    try:
        con = store.connect(config)
    except db.DbBusy as e:
        raise _StoreBusy(str(e)) from e
    except Exception as e:  # noqa: BLE001 — classify, then re-raise
        if _is_lock_error(e):
            raise _StoreBusy(str(e)) from e
        raise
    try:
        store.init(con)
        ensure_backup_tables(con)
        if store.ensure is not None:
            store.ensure(con)
    except Exception as e:  # noqa: BLE001 — a concurrent init is contention
        if _is_lock_error(e):
            con.close()
            raise _StoreBusy(str(e)) from e
        raise
    return con


# ------------------------------------------------------------------- gate + claim

def due(
    con: duckdb.DuckDBPyConnection, backup: BackupCfg, *, store: _Store | None = None
) -> tuple[bool, str]:
    """Is a scheduled run of ONE store warranted? Returns (is_due, human reason).
    `con` must be a connection to that store; `store` defaults to blech, so
    pre-split callers keep their meaning.

    The checks are ordered cheap-first but the semantics are AND: something landed
    since the watermark, no successful run within `interval_days`, and no genuine
    failure within the cooldown — all read from THIS store's own tables, which is
    what makes the two schedules independent. An empty store is never due — there
    is nothing to lose yet, and a zip of nothing would still rotate a real one
    out."""
    store = store or _BLECH
    last = store.last_change(con)
    if last is None:
        return False, "empty store: nothing to back up yet"
    watermark = db.get_meta(con, META_WATERMARK)
    if watermark:
        try:
            if last <= datetime.fromisoformat(str(watermark)):
                return False, f"nothing new since the last backup (watermark {watermark})"
        except ValueError:
            # An unparseable watermark must not wedge the schedule forever; treat
            # it as absent and let the run rewrite it.
            pass
    # Cooldown: only the MOST RECENT finished run counts. A failed run followed by
    # a successful retry must not keep suppressing the schedule for six hours.
    row = con.execute(
        "SELECT status, finished_at > now() - to_hours(CAST(? AS INTEGER)) "
        "FROM backup_runs WHERE finished_at IS NOT NULL "
        "ORDER BY finished_at DESC LIMIT 1",
        [_COOLDOWN_HOURS],
    ).fetchone()
    if row is not None and row[0] == "failed" and bool(row[1]):
        return False, f"cooling down: the last run failed within {_COOLDOWN_HOURS}h"
    recent = _scalar(
        con,
        "SELECT count(*) FROM backup_runs WHERE status = 'ok' "
        "AND finished_at > now() - to_days(CAST(? AS INTEGER))",
        [int(backup.interval_days)],
    )
    if recent:
        return False, f"already backed up within the last {backup.interval_days} days"
    return True, "due: new work since the watermark"


def _claim(
    con: duckdb.DuckDBPyConnection, *, trigger: str, store: _Store | None = None
) -> dict[str, Any] | None:
    """Claim one store's run via compare-and-set inside an explicit transaction.

    DuckDB grants one writer at a time, so the loser's transaction sees the
    winner's committed 'running' row — a real mutex covering a manual CLI run
    racing the scheduler thread, and a second container against one bind mount.
    The claim rows live in the store being claimed, so the two stores' claims
    never contend with each other — a long age zip does not block a blech run.
    Returns None when the claim is refused: that is CONTENTION, not failure, and
    the caller must write no row and arm no cooldown. A stale claim past the
    lease is closed out as failed and reclaimed, so a runner killed mid-run
    cannot wedge the job forever.

    'running' is a transient fourth status the SPEC's enum does not list; it is
    never a final state, and the lease guarantees it cannot become one."""
    store = store or _BLECH
    host = f"{socket.gethostname()}:{os.getpid()}"
    con.execute("BEGIN")
    try:
        fresh = con.execute(
            "SELECT count(*) FROM backup_runs WHERE status = 'running' "
            "AND started_at > now() - to_hours(CAST(? AS INTEGER))",
            [_CLAIM_LEASE_HOURS],
        ).fetchone()[0]
        if fresh:
            con.execute("ROLLBACK")
            return None
        con.execute(
            "UPDATE backup_runs SET status = 'failed', finished_at = now(), "
            "error = 'abandoned: claim held past the lease (runner killed mid-run?)' "
            "WHERE status = 'running'"
        )
        raw = db.get_meta(con, META_WATERMARK)
        try:
            wm_from = datetime.fromisoformat(str(raw)) if raw else None
        except ValueError:
            wm_from = None
        wm_to = store.last_change(con)
        n_masks, n_images = store.claim_counts(con)
        run_id = uuid.uuid4().hex
        con.execute(
            'INSERT INTO backup_runs (run_id, started_at, status, "trigger", host, '
            "n_masks, n_images) VALUES (?, now(), 'running', ?, ?, ?, ?)",
            [run_id, trigger, host, n_masks, n_images],
        )
        con.execute("COMMIT")
    except _TXN_EXC:
        # A concurrent writer beat us between BEGIN and COMMIT: contention.
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        return None
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    return {
        "run_id": run_id, "host": host,
        "watermark_from": wm_from, "watermark_to": wm_to,
        "n_masks": n_masks, "n_images": n_images,
    }


def _finish(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    *,
    status: str,
    delivery: str | None = None,
    zip_path: str | None = None,
    zip_bytes: int | None = None,
    error: str | None = None,
) -> None:
    """Close a claimed row. `delivered` means the zip itself reached the channel —
    a summary-only post is a delivered *message*, not a delivered backup, and
    conflating them hides the oversize case from anyone reading the table."""
    con.execute(
        "UPDATE backup_runs SET status = ?, finished_at = now(), delivery = ?, "
        'delivered = ?, zip_path = ?, "bytes" = ?, error = ? WHERE run_id = ?',
        [status, delivery, delivery == "posted", zip_path, zip_bytes, error, run_id],
    )


# ------------------------------------------------------------------- snapshot

def snapshot_db(src: str | Path, dest: str | Path) -> None:
    """Copy a store with the engine itself: CHECKPOINT -> ATTACH -> copy the
    catalog -> insert the rows table by table -> CHECKPOINT. Never `shutil.copy`.

    DuckDB keeps unflushed pages in a `.duckdb.wal` sidecar, so copying the
    `.duckdb` alone while a write is in flight yields a torn file that only
    fails at RESTORE time — the worst possible moment to discover it. Letting the
    engine do the copying reads the current committed MVCC snapshot instead:
    writers are never blocked and the result (tables, constraints, indexes, data)
    is transactionally whole. The inserts share ONE explicit transaction for that
    last word — statement per transaction would let `crops` be read before a
    write and `masks` after it, and a snapshot that disagrees with itself is
    precisely the torn file this function exists to prevent.

    Why `(SCHEMA)` plus explicit inserts rather than a plain `COPY FROM DATABASE`
    (A11): these archives are posted to a Discord webhook and `users` carries
    scrypt password hashes, so that table is excluded. Copying everything and
    dropping `users` afterwards is NOT equivalent, and the gap is measured, not
    theorised — on DuckDB 1.4.4 against a 43 MB store the copy trips the 16 MB
    `checkpoint_threshold` mid-flight, so the hashes are already written to the
    file by the time a DROP could run; DROP returns those blocks to the free list
    without zeroing them and the hash is still findable in the finished snapshot
    with a plain `grep`. Bytes that were never written cannot leak, so the
    excluded tables are dropped from the freshly copied *schema*, before a single
    row moves. The age store carries no `users` table, but it goes through this
    same machinery on purpose — the drop is IF EXISTS and costs nothing, and one
    code path means the exclusion can never quietly apply to only one zip."""
    dest_path = Path(dest)
    if dest_path.exists():
        # ATTACH onto a leftover file would copy into a non-empty catalog.
        dest_path.unlink()
    con = _connect_file(src)
    try:
        try:
            con.execute("CHECKPOINT")
        except duckdb.Error:
            # A concurrent write transaction blocks a plain CHECKPOINT. Folding
            # the WAL first is a size optimisation, not correctness — the copy
            # below reads a consistent MVCC snapshot either way.
            pass
        catalog = con.execute("SELECT current_database()").fetchone()[0]
        safe_dest = str(dest_path).replace("'", "''")
        safe_cat = _quote(str(catalog))
        con.execute(f"ATTACH '{safe_dest}' AS snap")
        # No rollback handler: the connection is function-local and the `finally`
        # closes it, which discards any transaction still open. A half-written
        # `dest` is harmless too — the caller treats a raise from here as the
        # genuine-failure class and deletes the staging directory it lives in.
        con.execute("BEGIN")
        con.execute(f'COPY FROM DATABASE "{safe_cat}" TO snap (SCHEMA)')
        for excluded in _SNAPSHOT_EXCLUDED_TABLES:
            # IF EXISTS: a store old enough or fresh enough not to carry the table
            # is still a working store, and must not cost the whole backup.
            con.execute(f'DROP TABLE IF EXISTS snap.main."{_quote(excluded)}"')
        # Enumerated from the SNAPSHOT catalog, after the drops. A table added to
        # the store's init later is then carried with no edit here, and there is
        # no second list of names that could drift out of step with the
        # exclusions.
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE database_name = 'snap' ORDER BY table_name"
            ).fetchall()
        ]
        for table in tables:
            name = _quote(table)
            con.execute(
                f'INSERT INTO snap.main."{name}" '
                f'SELECT * FROM "{safe_cat}".main."{name}"'
            )
        con.execute("COMMIT")
        con.execute("CHECKPOINT snap")
        con.execute("DETACH snap")
    finally:
        con.close()


# ------------------------------------------------------------------- zip + rotate

def _write_zip(dest: Path, members: Sequence[tuple[str, Path]]) -> int:
    """Zip an ENUMERATED member list into `dest` (via `.part` + atomic replace).

    Never a directory walk. This zip is posted to a chat channel, and a
    glob-based backup that one day sweeps up an adjacent secret or an unrelated
    operator file cannot be un-posted. Returns the final size in bytes."""
    part = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for arcname, path in members:
                # JPEGs are already compressed; the DB and CSVs deflate well.
                compress = (
                    zipfile.ZIP_STORED
                    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                    else zipfile.ZIP_DEFLATED
                )
                z.write(path, arcname, compress_type=compress)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return dest.stat().st_size


def _prune(
    out_dir: Path, keep: int, *, store: _Store, protect: Path | None = None
) -> list[str]:
    """Keep the newest `keep` zips OF THIS STORE'S PREFIX, pruned BY NAME. Never
    fails a run.

    Names lead with a UTC timestamp and therefore sort chronologically; a zip
    restored onto the box carries an unrelated mtime that would evict the wrong
    file. Membership is the store's exact name shape (`zip_re`), not a glob:
    'bienenblech' is a prefix of 'bienenblech-age', so a glob would let the
    blech rotation count — and delete — the age archives, and `backup.keep`
    applies to each store independently. `protect` is the run that just
    succeeded — it can never be the file rotation removes, whatever `keep` is
    set to. Per-file errors are swallowed with a log line: a root-owned stray
    from a `docker compose exec` without `-u` raises PermissionError here, and
    losing a rotation beats losing the backup that just succeeded."""
    deleted: list[str] = []
    keep = max(1, int(keep))
    try:
        zips = sorted(
            p for p in out_dir.glob(f"{store.zip_prefix}-*.zip")
            if store.zip_re.match(p.name)
        )
    except OSError:
        return deleted
    for path in (zips[:-keep] if len(zips) > keep else []):
        if protect is not None and path.resolve() == protect.resolve():
            continue
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError as e:
            print(f"{_ALERT} prune: could not remove {path.name}: {_redact(str(e))}")
    return deleted


# ------------------------------------------------------------------- discord

def _discord_poster(webhook: str, content: str, file_path: Path | None) -> None:
    """Default poster: stdlib urllib plus a hand-rolled multipart encoder.

    `requests` is not a dependency and will not become one for this. Not `curl`
    via subprocess either — the webhook URL would land in the process argv table,
    visible to anyone who can run `docker top`. Raises on any non-2xx (urllib
    does that for us), which is the genuine-failure class."""
    if file_path is None:
        body = json.dumps({"content": content[:1900]}).encode()
        req = urllib.request.Request(
            webhook, data=body,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return
    boundary = "bienenblech" + uuid.uuid4().hex
    payload = json.dumps({"content": content[:1900]}).encode()
    crlf = b"\r\n"
    body = b"".join([
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="payload_json"', crlf,
        b"Content-Type: application/json", crlf, crlf,
        payload, crlf,
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="files[0]"; filename="',
        file_path.name.encode(), b'"', crlf,
        b"Content-Type: application/zip", crlf, crlf,
        file_path.read_bytes(), crlf,
        b"--", boundary.encode(), b"--", crlf,
    ])
    req = urllib.request.Request(
        webhook, data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def _deliver(
    *,
    webhook: str,
    enabled: bool,
    poster: Poster,
    budget: int,
    zip_path: Path,
    zip_bytes: int,
    base: str,
) -> str:
    """Post one store's bundle, walking the oversize ladder. Returns the recorded
    mode. `base` is the store-flavoured summary line, built by the caller —
    both stores post through here, to the same webhook, so the sentence is the
    only thing telling the channel's reader which archive this is.

    Discord's per-file cap is 8-25 MB depending on the server tier and it rejects
    rather than truncating, so above `backup.max_upload_mb` the zip is NOT posted
    — but it has already been written and is about to be rotated locally, and the
    message names its path. A silently dropped backup is the failure mode to
    avoid: the operator must be able to read the channel and know both that the
    backup exists and exactly where to fetch it.

    Unset or blank webhook is a clean no-op (a supported deployment, not an
    error); a non-Discord URL is refused without logging its value. Poster
    exceptions propagate — an unreachable webhook is the genuine-failure class."""
    if not enabled:
        return "disabled"
    if not webhook:
        return "skipped"
    if not _looks_like_webhook(webhook):
        print(
            f"{_ALERT} webhook refused: {WEBHOOK_ENV} is not a "
            "discord.com/api/webhooks URL (value not logged)"
        )
        return "refused"
    if zip_bytes <= budget:
        poster(webhook, base, zip_path)
        return "posted"
    print(f"{_ALERT} oversize: zip is {zip_bytes} B (cap {budget} B) — posting a summary only")
    poster(
        webhook,
        base + " Over the upload cap, so the archive is NOT attached. It was "
        f"written and retained on the box at {zip_path} — fetch it from there.",
        None,
    )
    return "posted_summary"


# ------------------------------------------------------------------- the run

def _run_store(
    store: _Store,
    config: Config,
    *,
    trigger: str = "cli",
    force: bool = False,
    keep: int | None = None,
    discord: bool = True,
    poster: Poster | None = None,
) -> dict[str, Any]:
    """One backup attempt of ONE store, end to end. Never raises; returns:

        {store, status: 'ok'|'skipped'|'failed', reason, trigger, run_id,
         zip_path, zip_bytes, delivery, n_masks, n_images, error}

    `status='skipped'` covers both "not due" and the whole contention class
    (store held elsewhere, claim refused) — no row is written and no cooldown is
    armed, so wiring this into a host cron cannot disable the scheduler.
    `force=True` bypasses the due-gate but NOT the claim: a run already in flight
    still wins. `discord=False` zips and rotates locally and still advances the
    watermark. A failed run holds the watermark, so nothing is silently dropped
    from the next successful archive. Every one of those rules is evaluated
    against THIS store's own tables and this store's own zips: nothing here can
    read or write the other store, which is the isolation the split promises.

    Files land owned by whoever runs this; inside the container that is the
    unprivileged user by construction, because the scheduler thread lives past
    the entrypoint's privilege drop. Hand-runs must use the same `-u`."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(config.paths.backups_dir)
    keep_n = config.backup.keep if keep is None else keep
    budget = int(config.backup.max_upload_mb) * 1024 * 1024
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    post = poster or _discord_poster
    result: dict[str, Any] = {
        "store": store.name,
        "status": "skipped", "reason": None, "trigger": trigger, "run_id": None,
        "zip_path": None, "zip_bytes": None, "delivery": None,
        "n_masks": None, "n_images": None, "error": None,
    }
    try:
        con = _open_store(store, config)
    except _StoreBusy:
        result["reason"] = "store busy"
        _note_store(result)
        return result
    except Exception as e:  # noqa: BLE001 — genuine failure, but nowhere to write a row
        result.update(status="failed", error=_redact(str(e) or repr(e), webhook))
        print(f"{_ALERT} {store.name} failed: {result['error']}")
        _note_store(result)
        return result

    staging: str | None = None
    try:
        if trigger == "schedule":
            _stale_alert(con, config.backup, store)
        if not force:
            ok, reason = due(con, config.backup, store=store)
            if not ok:
                result["reason"] = reason
                return result
        run = _claim(con, trigger=trigger, store=store)
        if run is None:
            # The frozen reason string for the whole contention class; here it
            # means another runner holds this store's claim.
            result["reason"] = "store busy"
            return result
        _note_store({**result, "status": "running"})
        result.update(
            run_id=run["run_id"], n_masks=run["n_masks"], n_images=run["n_images"]
        )
        zip_path = out_dir / store.zip_name(stamp, run["run_id"])
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            # Staged on the same filesystem as the final zip, so os.replace stays
            # atomic.
            staging = tempfile.mkdtemp(prefix=".staging-", dir=out_dir)
            sdir = Path(staging)
            snapshot_db(store.db_path(config), sdir / store.snapshot_member)
            csv_names = _export_csvs(con, sdir, store)
            file_members, missing = store.file_members(con)
            if missing:
                print(
                    f"{_ALERT} {len(missing)} {store.missing_noun} referenced by the "
                    f"DB are missing from disk and are not in this archive: {missing[:5]}"
                )
            members: list[tuple[str, Path]] = [
                (store.snapshot_member, sdir / store.snapshot_member),
                *((name, sdir / name) for name in csv_names),
                *file_members,
                ("manifest.json", sdir / "manifest.json"),
                ("README.txt", sdir / "README.txt"),
            ]
            man = _manifest(
                store, con, run=run, trigger=trigger,
                members=[m[0] for m in members], missing=missing,
            )
            (sdir / "manifest.json").write_text(
                json.dumps(man, indent=2, default=str), encoding="utf-8"
            )
            (sdir / "README.txt").write_text(
                store.readme(man, stamp=stamp, trigger=trigger), encoding="utf-8"
            )
            zip_bytes = _write_zip(zip_path, members)
            result.update(zip_path=str(zip_path), zip_bytes=zip_bytes)
            delivery = _deliver(
                webhook=webhook, enabled=discord, poster=post, budget=budget,
                zip_path=zip_path, zip_bytes=zip_bytes,
                base=store.post_line(man, zip_bytes, stamp),
            )
            result["delivery"] = delivery
        except Exception as e:  # noqa: BLE001 — the genuine-failure class
            err = _redact(str(e) or repr(e), webhook)
            _finish(
                con, run["run_id"], status="failed", zip_path=result["zip_path"],
                zip_bytes=result["zip_bytes"], error=err,
            )
            print(f"{_ALERT} {store.name} failed run {run['run_id']}: {err}")
            result.update(status="failed", error=err)
            return result
        # Success: close the row, THEN advance the watermark, THEN rotate. In that
        # order, because a crash between any two of them must leave the next run
        # doing more work, never less.
        _finish(
            con, run["run_id"], status="ok", delivery=delivery,
            zip_path=str(zip_path), zip_bytes=zip_bytes,
        )
        if run["watermark_to"] is not None:
            db.set_meta(con, META_WATERMARK, str(run["watermark_to"]))
        _prune(out_dir, keep_n, store=store, protect=zip_path)
        result["status"] = "ok"
        return result
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        con.close()
        _note_store(result)


def run_backup(
    config: Config,
    *,
    trigger: str = "cli",
    force: bool = False,
    keep: int | None = None,
    discord: bool = True,
    poster: Poster | None = None,
) -> dict[str, Any]:
    """One backup attempt of EVERY store, sequentially. Never raises; returns a
    combined dict shaped so `POST /api/backup/run` and the CLI keep working with
    no edits:

        {status, trigger, stores: [per-store result, ...],
         reason, run_id, zip_path, zip_bytes, delivery, n_masks, n_images, error}

    `stores` is the authoritative per-store summary list, in `_STORES` order.
    The scalar keys are the legacy mirror for pre-split readers: `status` is the
    worst across stores ('failed' beats 'ok' beats 'skipped', so a CLI cron's
    exit code still notices either store breaking), `reason`/`error` are the
    per-store strings joined and labelled by store name, and the zip/run fields
    mirror the blech store — falling back to the age store's zip when blech
    produced none, so a result that says 'ok' always names an archive that
    exists. Everything richer lives in `stores`.

    Sequential rather than parallel on purpose: both jobs stage into the same
    `backups_dir` and post to the same webhook, the whole pair takes seconds,
    and two interleaved alert streams would be strictly harder to read. The
    isolation that matters — due, claim, cooldown, watermark, rotation — comes
    from each store's own tables, not from scheduling."""
    with _LOCK:
        _state["status"] = "running"
    results: list[dict[str, Any]] = []
    for store in _STORES:
        try:
            results.append(_run_store(
                store, config, trigger=trigger, force=force, keep=keep,
                discord=discord, poster=poster,
            ))
        except Exception as e:  # noqa: BLE001 — _run_store shouldn't raise; isolate anyway
            # Belt and braces around the per-store isolation promise: a surprise
            # from one store (say, a build where its connect fn has not landed
            # yet) must not cancel the other store's run. Message only, never a
            # traceback — an exception chain can embed the webhook URL.
            err = _redact(str(e) or repr(e))
            print(f"{_ALERT} {store.name} run crashed: {err}")
            failed = {
                "store": store.name, "status": "failed", "reason": None,
                "trigger": trigger, "run_id": None, "zip_path": None,
                "zip_bytes": None, "delivery": None, "n_masks": None,
                "n_images": None, "error": err,
            }
            _note_store(failed)
            results.append(failed)
    combined = _combine(results, trigger)
    _note(combined)
    return combined


def _combine(results: Sequence[dict[str, Any]], trigger: str) -> dict[str, Any]:
    """Fold the per-store results into the legacy-shaped combined dict."""
    statuses = [r["status"] for r in results]
    status = "failed" if "failed" in statuses else ("ok" if "ok" in statuses else "skipped")

    def _joined(key: str) -> str | None:
        parts = [f"{r['store']}: {r[key]}" for r in results if r.get(key)]
        return "; ".join(parts) or None

    blech = next((r for r in results if r["store"] == _BLECH.name), results[0])
    # The zip fields travel as a set from ONE store — mixing blech's path with
    # age's byte count would describe an archive that does not exist.
    zipped = blech if blech.get("zip_path") else next(
        (r for r in results if r.get("zip_path")), blech
    )
    return {
        "status": status, "trigger": trigger,
        "stores": [dict(r) for r in results],
        "reason": _joined("reason"),
        "error": _joined("error"),
        "run_id": blech.get("run_id"),
        "zip_path": zipped.get("zip_path"),
        "zip_bytes": zipped.get("zip_bytes"),
        "delivery": zipped.get("delivery"),
        "n_masks": blech.get("n_masks"),
        "n_images": blech.get("n_images"),
    }


def _stale_alert(con: duckdb.DuckDBPyConnection, backup: BackupCfg, store: _Store) -> None:
    """Print the staleness alarm when one store's last success is over
    2*interval_days old.

    Printed on every scheduler tick while it is true, because this is what closes
    the loop when the gate itself is wedged — a persistent failure inside the
    cooldown window produces no other output at all. Per store, naming the
    store: 'the backups stopped' and 'the AGE backups stopped' are different
    pages of the same runbook."""
    row = con.execute(
        "SELECT max(finished_at), max(finished_at) < now() - to_days(CAST(? AS INTEGER)) "
        "FROM backup_runs WHERE status = 'ok'",
        [2 * int(backup.interval_days)],
    ).fetchone()
    if row is not None and row[0] is not None and bool(row[1]):
        print(
            f"{_ALERT} {store.name} stale: last successful backup {row[0]} — over "
            f"{2 * backup.interval_days} days ago"
        )


# ------------------------------------------------------------------- scheduler

_LOCK = threading.Lock()
_config: Config | None = None
_running = False
_thread: threading.Thread | None = None
_state: dict[str, Any] = {
    "status": "idle",   # idle | running | ok | skipped | failed  (worst-of, legacy)
    "at": None,         # epoch secs of the last run_backup completion
    "last": None,       # the last COMBINED run_backup result dict
    "stores": {},       # per-store: name -> {status, at, last}
}


def _note(result: dict[str, Any]) -> None:
    """Record a combined run's outcome — the legacy top-level scheduler fields."""
    with _LOCK:
        _state.update(status=result["status"], at=time.time(), last=dict(result))


def _note_store(result: dict[str, Any]) -> None:
    """Record one store's outcome under its own key, leaving the legacy fields to
    the combined note — so `status()` can say 'age failed, blech fine' instead of
    whichever store happened to finish last."""
    with _LOCK:
        _state["stores"][result.get("store")] = {
            "status": result["status"], "at": time.time(), "last": dict(result),
        }


def status(config: Config, limit: int = 5) -> dict[str, Any]:
    """Backup health for the CLI and `GET /api/backup/status`.

    Per-store truth lives in `stores` (one entry per store: due, watermark,
    runs, next_due, error — each read from that store's own tables). The
    top-level scalars are kept for pre-split readers: `runs` is the two
    histories merged newest-first with a `store` tag on every row (so the route
    reports both stores' runs with no api.py edit), `due` is true when EITHER
    store is due, `due_reason`/`error` are labelled joins, `next_due` is the
    earlier of the two, and `watermark` mirrors the blech store, whose meaning
    it always had.

    Reports WHETHER a webhook is configured, never the URL. Degrades to an
    `error` field instead of raising when a store is held elsewhere: a status
    probe that 500s during a routine write turns contention into what looks like
    an outage — and one store's contention must not blank the other's health."""
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    with _LOCK:
        scheduler = {
            "status": _state["status"],
            "at": _state["at"],
            "last": _state["last"],
            "stores": {k: dict(v) for k, v in _state["stores"].items()},
            "thread_alive": bool(_thread is not None and _thread.is_alive()),
        }
    out: dict[str, Any] = {
        "enabled": config.backup.enabled,
        "interval_days": config.backup.interval_days,
        "keep": config.backup.keep,
        "max_upload_mb": config.backup.max_upload_mb,
        "webhook_configured": bool(webhook),
        "webhook_valid": bool(webhook) and _looks_like_webhook(webhook),
        "scheduler": scheduler,
        "stores": [],
        "due": None, "due_reason": None, "watermark": None,
        "last_run": None, "next_due": None, "runs": [], "error": None,
    }
    cols = ("run_id", "started_at", "finished_at", "status", "trigger", "host",
            "n_masks", "n_images", "zip_path", "bytes", "delivered", "delivery",
            "error")
    for store in _STORES:
        entry: dict[str, Any] = {
            "store": store.name, "due": None, "due_reason": None,
            "watermark": None, "last_run": None, "next_due": None,
            "runs": [], "error": None,
        }
        out["stores"].append(entry)
        try:
            con = _open_store(store, config)
        except _StoreBusy:
            entry["error"] = "store busy"
            continue
        except Exception as e:  # noqa: BLE001 — degrade, never 500 a status probe
            entry["error"] = _redact(str(e) or repr(e), webhook)
            continue
        try:
            is_due, reason = due(con, config.backup, store=store)
            entry["due"], entry["due_reason"] = is_due, reason
            entry["watermark"] = db.get_meta(con, META_WATERMARK)
            rows = con.execute(
                'SELECT run_id, started_at, finished_at, status, "trigger", host, '
                'n_masks, n_images, zip_path, "bytes", delivered, delivery, error '
                "FROM backup_runs ORDER BY started_at DESC LIMIT ?",
                [max(1, int(limit))],
            ).fetchall()
            entry["runs"] = [
                {
                    "store": store.name,
                    **{
                        k: (_redact(v, webhook) if k == "error" and isinstance(v, str)
                            else str(v) if isinstance(v, datetime) else v)
                        for k, v in zip(cols, row)
                    },
                }
                for row in rows
            ]
            entry["last_run"] = entry["runs"][0] if entry["runs"] else None
            last_ok = _scalar(
                con, "SELECT max(finished_at) FROM backup_runs WHERE status = 'ok'"
            )
            if isinstance(last_ok, datetime):
                # An estimate, not a promise: the run still has to be due
                # (something new must have landed in THIS store), which is what
                # `due_reason` explains.
                entry["next_due"] = str(
                    last_ok + timedelta(days=int(config.backup.interval_days))
                )
        finally:
            con.close()

    entries = out["stores"]
    merged = sorted(
        (r for e in entries for r in e["runs"]),
        key=lambda r: str(r.get("started_at") or ""), reverse=True,
    )
    out["runs"] = merged[: max(1, int(limit))]
    out["last_run"] = out["runs"][0] if out["runs"] else None
    dues = [e["due"] for e in entries if e["due"] is not None]
    out["due"] = any(dues) if dues else None
    reasons = [f"{e['store']}: {e['due_reason']}" for e in entries if e["due_reason"]]
    out["due_reason"] = "; ".join(reasons) or None
    errors = [f"{e['store']}: {e['error']}" for e in entries if e["error"]]
    out["error"] = "; ".join(errors) or None
    out["watermark"] = entries[0]["watermark"] if entries else None
    upcoming = [e["next_due"] for e in entries if e["next_due"]]
    out["next_due"] = min(upcoming) if upcoming else None
    return out


def start_scheduler(config: Config) -> None:
    """Spawn the tick thread. Called from `create_app`'s boot block; idempotent.

    ONE thread for both stores, not one each: the tick body is seconds of work
    every 15 minutes, and two threads would double the lock traffic against the
    stores for nothing — the independence the contract demands is in the data
    (each store's own due gate, claim, cooldown and watermark), not in the
    threading. In-process rather than host cron or a compose sidecar: zero
    cross-process DuckDB lock contention, files land owned by the unprivileged
    user by construction (the thread lives past the entrypoint's privilege drop,
    so nothing ever needs re-owning), and it ships inside the image, so a
    `git pull && docker compose up -d --build` carries it with no host-side
    setup. `backup.enabled` is read on every tick rather than here, so the thread
    starts unconditionally and a disabled config costs one sleeping daemon thread
    that touches no file."""
    global _config, _running, _thread
    with _LOCK:
        _config = config
        if _running:
            return
        _running = True
        try:
            _thread = threading.Thread(target=_ticker, name="bienenblech-backup", daemon=True)
            _thread.start()
        except BaseException:
            # Spawn failed ("can't start new thread" under load). Roll back, so a
            # dead scheduler cannot report itself as running forever.
            _running = False
            raise


def _ticker() -> None:
    time.sleep(_FIRST_TICK_S)
    while True:
        with _LOCK:
            cfg = _config
        if cfg is not None and cfg.backup.enabled:
            # Each store is evaluated INDEPENDENTLY, each inside its own
            # try/except: blech-only activity must never fire an age backup and
            # vice versa (their own due gates see to that), and a crash in one
            # store's run must never cost the other store its tick.
            for store in _STORES:
                try:
                    _run_store(store, cfg, trigger="schedule")
                except Exception as e:  # noqa: BLE001 — _run_store shouldn't raise; keep ticking
                    # Message only, never a traceback: an exception chain can
                    # embed the webhook URL (urllib puts it in HTTPError
                    # attributes).
                    print(f"{_ALERT} {store.name} tick crashed: {_redact(str(e) or repr(e))}")
        time.sleep(_TICK_S)
