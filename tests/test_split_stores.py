"""The per-tool store split — two DuckDB files, one product (owner decision).

`data/bienenblech.duckdb` stays the MAIN store: users plus every Blech table
plus its own `backup_runs`/`meta`, existing file, existing history, zero
migration for Blech. `data/age.duckdb` (config `paths.age_db_path`) is NEW and
holds `age_samples` plus its OWN `backup_runs` and `meta` — each store is
self-describing and detachable. Users stay GLOBAL in the main store: one login,
one role, everywhere. The API surface does not change at all.

Three seams of that contract are pinned here, because each fails silently:

*   **Placement.** A route wired to the wrong `get_con` would happily create
    `age_samples` in the main store again (DuckDB will CREATE-IF-NOT-EXISTS its
    way through anything), and nothing at the HTTP level would look different —
    until the age backup zips an empty store. So both files are opened and the
    table sets asserted BOTH ways, including the headline: no `users` table in
    the age store, ever, because the age zip travels to a chat channel and its
    snapshot must have nothing to exclude in the first place.
*   **The legacy migration.** A pre-split main store still carrying
    `age_samples` is healed exactly once at boot: rows copied into the age
    store (skipping sample_ids already present, so a crash mid-copy resumes),
    the table dropped from the main store, one printed line saying how many
    rows moved. Idempotent, and silent on a store that was never pre-split.
    The rows are annotator hours, so they are compared column by column, not
    counted.
*   **`updated_at`** (the watermark fix riding along): annotate, flag AND
    reopen stamp it. The flag case is the one that was broken before the
    split: a flag wrote no timestamp anywhere, so a flag-only week never
    triggered a backup — the age watermark is `max(uploaded_at, updated_at)`,
    and this column is its second leg. The backup-side consequence (a
    flag-only store actually fires the age job) is pinned in test_backup.py;
    here is the stamp itself.

Everything runs through conftest's sandboxed `store`; no test touches `data/`.
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from bienenblech import api, db
from bienenblech.config import Config

# ------------------------------------------------------------------- helpers

def _bee(seed: int, width: int = 320, height: int = 240) -> bytes:
    """A synthetic PNG bee, seeded so two uploads differ in sha256 (the upload
    dedupes on the sha of the original bytes)."""
    im = Image.new("RGB", (width, height), (12, 12, 12))
    draw = ImageDraw.Draw(im)
    cx, cy = width // 2 + (seed * 7) % 40, height // 2 + (seed * 5) % 30
    draw.ellipse([cx - 70, cy - 35, cx + 70, cy + 35],
                 fill=(212, 160, 60 + seed % 90))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _sample_ids(client: TestClient) -> set[str]:
    resp = client.get("/api/age/samples")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body["samples"] if isinstance(body, dict) else body
    return {r["sample_id"] for r in rows}


def _upload_age(client: TestClient, seed: int) -> str:
    """One age sample through the real route, returning its sample_id."""
    before = _sample_ids(client)
    resp = client.post(
        "/api/age/samples",
        files=[("file", (f"bee{seed}.png", _bee(seed), "image/png"))],
    )
    assert resp.status_code == 200, resp.text
    new = _sample_ids(client) - before
    assert len(new) == 1, f"expected exactly one new sample, got {new}"
    return new.pop()


def _table_names(query) -> set[str]:
    """The table set of whichever store `query`/`age_query` is bound to."""
    return {
        str(row[0]).lower()
        for row in query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        )
    }


def _updated_at(age_query, sample_id: str) -> datetime | None:
    rows = age_query(
        "SELECT updated_at FROM age_samples WHERE sample_id = ?", [sample_id]
    )
    assert len(rows) == 1, f"expected one row for {sample_id}, got {rows}"
    return rows[0][0]


# ============================================================ placement
def test_age_rows_land_in_the_age_store_and_nowhere_near_the_main_store(
    admin: TestClient, image: dict, query, age_query, store: Config
):
    """The load-bearing fact of the split, checked at the FILE level: after one
    Blech frame and one age sample go through the real routes, each row is in
    its own DuckDB file and neither file carries the other tool's tables.

    Asserted both ways because the failure is asymmetric and silent: an age
    route on the wrong connection would re-create `age_samples` in the main
    store without an error anywhere, and a Blech table leaking into the age
    store would drag pixels-adjacent data into the age backup zip. The `users`
    absence is the headline — auth is global and lives in the main store, and
    the age snapshot must have no credentials to exclude in the first place.
    """
    sample_id = _upload_age(admin, 1)

    # Both files genuinely exist on disk — two stores, not one plus an alias.
    assert Path(store.paths.db_path).is_file()
    assert Path(store.paths.age_db_path).is_file()
    assert Path(store.paths.db_path) != Path(store.paths.age_db_path)

    main_tables = _table_names(query)
    age_tables = _table_names(age_query)

    # The main store: users + all Blech tables + its own operational pair,
    # and NO age_samples — the pre-split layout is gone.
    for table in ("users", "images", "crops", "masks", "label_classes",
                  "class_audit", "backup_runs", "meta"):
        assert table in main_tables, f"the main store lost {table}"
    assert "age_samples" not in main_tables, (
        "age_samples is (still) in the main store — an age route is running on "
        "the main-store connection"
    )

    # The age store: age_samples + its OWN operational pair (self-describing,
    # detachable), and none of the main store's tables — users above all.
    assert "age_samples" in age_tables
    for table in ("backup_runs", "meta"):
        assert table in age_tables, (
            f"the age store has no {table} of its own; it must be "
            "self-describing so the age backup never opens the main file"
        )
    assert "users" not in age_tables, (
        "the age store carries a users table; auth is global in the main store, "
        "and the age backup zip must have no credentials anywhere near it"
    )
    for table in ("images", "crops", "masks", "label_classes"):
        assert table not in age_tables, f"{table} leaked into the age store"

    # And the rows themselves are where the tables are.
    assert age_query(
        "SELECT count(*) FROM age_samples WHERE sample_id = ?", [sample_id]
    ) == [(1,)]
    assert query(
        "SELECT count(*) FROM images WHERE image_id = ?", [image["image_id"]]
    ) == [(1,)]
    assert query("SELECT count(*) FROM users") != [(0,)]


def test_a_never_pre_split_store_boots_silently(store: Config, capsys):
    """The migration probe must not announce itself on the common case. A
    fresh install (and every install after its one migration) has no legacy
    table, and a boot line that always says 'moved 0 rows' would train the
    operator to ignore the one boot where the number matters."""
    with TestClient(api.create_app(store)):
        pass
    out = capsys.readouterr().out
    assert "migrat" not in out.lower(), (
        f"a store with no legacy age_samples table printed a migration line:\n{out}"
    )


# ==================================================== the legacy migration
# The pre-split main store's `age_samples`, frozen as text the way test_db.py
# freezes its old shapes: the migration under test exists because stores of
# exactly this layout are on disk in production, and the test must keep
# producing that layout after db.py has moved on. Note there is NO updated_at —
# that column postdates the split, and migrated rows must land with it NULL,
# which the watermark's max(uploaded_at, updated_at) reads as "never touched
# since upload": correct.
PRE_SPLIT_AGE_DDL = """
    CREATE TABLE age_samples (
        sample_id    TEXT PRIMARY KEY,
        filename     TEXT NOT NULL,
        sha256       TEXT NOT NULL UNIQUE,
        stored_path  TEXT NOT NULL,
        width        INTEGER NOT NULL,
        height       INTEGER NOT NULL,
        "bytes"      BIGINT NOT NULL,
        uploaded_by  TEXT,
        uploaded_at  TIMESTAMP NOT NULL,
        status       TEXT NOT NULL DEFAULT 'open',
        age_days     INTEGER CHECK (age_days BETWEEN 0 AND 28),
        annotated_by TEXT,
        annotated_at TIMESTAMP,
        flag_reason  TEXT
    );
"""

# One row per status, with every nullable column exercised somewhere, and
# explicit timestamps so "rode through intact" is equality, not approximation.
# The columns line up with PRE_SPLIT_AGE_DDL's order.
LEGACY_ROWS: list[tuple] = [
    ("s_done", "done.png", "a" * 64, "data/age/s_done.jpg", 320, 240, 999,
     "admin", datetime(2026, 1, 1, 10, 0, 0), "done", 9,
     "bob", datetime(2026, 1, 2, 11, 30, 0), None),
    ("s_flag", "flag.png", "b" * 64, "data/age/s_flag.jpg", 320, 240, 998,
     "admin", datetime(2026, 1, 1, 10, 5, 0), "flagged", None,
     None, None, "two bees"),
    ("s_open", "open.png", "c" * 64, "data/age/s_open.jpg", 320, 240, 997,
     None, datetime(2026, 1, 1, 10, 10, 0), "open", None,
     None, None, None),
]

_LEGACY_SELECT = (
    'SELECT sample_id, filename, sha256, stored_path, width, height, "bytes", '
    "uploaded_by, uploaded_at, status, age_days, annotated_by, annotated_at, "
    "flag_reason FROM age_samples ORDER BY sample_id"
)


@pytest.fixture
def legacy_store(store: Config) -> Config:
    """The `store` config with a PRE-SPLIT main store already on disk: the
    legacy `age_samples` table and its rows, written by hand before any app
    code runs — exactly what a production box that labeled bees before the
    split has under `data/`."""
    Path(store.paths.db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(store.paths.db_path)
    try:
        con.execute(PRE_SPLIT_AGE_DDL)
        con.executemany(
            "INSERT INTO age_samples VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [list(r) for r in LEGACY_ROWS],
        )
    finally:
        con.close()
    assert not Path(store.paths.age_db_path).exists(), (
        "the fixture premise is a box from before the split: no age store yet"
    )
    return store


def _boot(config: Config) -> None:
    """One app boot, fully torn down — the migration runs in create_app's boot
    block, and the TestClient context manager is what closes the app again."""
    with TestClient(api.create_app(config)):
        pass


def test_boot_moves_legacy_age_rows_drops_the_table_and_says_so(
    legacy_store: Config, query, age_query, capsys
):
    """The migration itself: after one boot the rows are in the age store
    column-for-column intact, the main store no longer has the table, and the
    boot said how many rows moved.

    Compared row by row rather than counted because these rows are the
    annotator hours the SPEC calls irreplaceable — a migration that moved
    three half-NULL rows would pass a count. `updated_at` must be NULL on
    every migrated row: the legacy table never had the column, and inventing a
    stamp here would make a week-old flag look like fresh work to the age
    backup watermark."""
    _boot(legacy_store)

    out = capsys.readouterr().out
    migration_lines = [l for l in out.splitlines() if "migrat" in l.lower()]
    assert len(migration_lines) == 1, (
        f"expected exactly one migration line on a pre-split boot, got:\n{out}"
    )
    assert str(len(LEGACY_ROWS)) in migration_lines[0], (
        f"the migration line must say how many rows moved: {migration_lines[0]!r}"
    )

    assert age_query(_LEGACY_SELECT) == LEGACY_ROWS, (
        "the migrated rows do not match what the pre-split store held"
    )
    assert age_query(
        "SELECT count(*) FROM age_samples WHERE updated_at IS NOT NULL"
    ) == [(0,)], "migrated rows must land with updated_at NULL (never touched)"

    assert "age_samples" not in _table_names(query), (
        "the legacy age_samples table survived in the main store; every future "
        "boot would re-run the migration and the split would never be real"
    )


def test_the_migration_is_idempotent_across_boots(
    legacy_store: Config, query, age_query, capsys
):
    """Boot twice. The second boot finds no legacy table, moves nothing, prints
    nothing — init_db runs on every boot, so the just-migrated store IS the
    common case, and a re-run that duplicated rows (or re-announced itself)
    would page the operator weekly forever."""
    _boot(legacy_store)
    capsys.readouterr()                       # discard the first boot's line

    _boot(legacy_store)

    out = capsys.readouterr().out
    assert "migrat" not in out.lower(), (
        f"the second boot re-announced the migration:\n{out}"
    )
    assert age_query(_LEGACY_SELECT) == LEGACY_ROWS, (
        "a second boot changed the migrated rows"
    )
    assert "age_samples" not in _table_names(query)


def test_the_migration_skips_sample_ids_already_in_the_age_store(
    legacy_store: Config, age_query, capsys
):
    """Resume safety: a boot that crashed between copying rows and dropping
    the table leaves BOTH stores holding some rows. The next boot must copy
    only the remainder — skipping sample_ids already present, and never
    overwriting the age store's version, which by then may carry newer work
    (an annotation landed through the age routes after the crash)."""
    already = ("s_done", "done.png", "a" * 64, "data/age/s_done.jpg", 320, 240,
               999, "admin", datetime(2026, 1, 1, 10, 0, 0), "done", 4,
               "carol", datetime(2026, 2, 1, 9, 0, 0), None)
    con = duckdb.connect(legacy_store.paths.age_db_path)
    try:
        db.init_age_db(con)
        con.execute(
            "INSERT INTO age_samples (sample_id, filename, sha256, stored_path, "
            'width, height, "bytes", uploaded_by, uploaded_at, status, age_days, '
            "annotated_by, annotated_at, flag_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            list(already),
        )
    finally:
        con.close()

    _boot(legacy_store)

    rows = age_query(_LEGACY_SELECT)
    assert len(rows) == len(LEGACY_ROWS), (
        "the resumed migration duplicated or dropped rows"
    )
    by_id = {r[0]: r for r in rows}
    assert by_id["s_done"] == already, (
        "the migration overwrote an age-store row that was already present — "
        "the age store's version is the newer one and must stand"
    )
    for legacy in LEGACY_ROWS[1:]:
        assert by_id[legacy[0]] == legacy, (
            f"the remainder row {legacy[0]} did not ride through intact"
        )
    out = capsys.readouterr().out
    migration_lines = [l for l in out.splitlines() if "migrat" in l.lower()]
    assert len(migration_lines) == 1
    assert str(len(LEGACY_ROWS) - 1) in migration_lines[0], (
        "the printed count must be the rows actually moved, not the table size: "
        f"{migration_lines[0]!r}"
    )


# ========================================================== updated_at
def test_annotate_stamps_updated_at(admin: TestClient, age_query):
    """`updated_at` is the age backup watermark's second leg
    (max(uploaded_at, updated_at)); every state change must move it, and
    annotate is the everyday one."""
    sid = _upload_age(admin, 11)
    resp = admin.post(f"/api/age/samples/{sid}/annotate", json={"age_days": 9})
    assert resp.status_code == 200, resp.text

    stamped = _updated_at(age_query, sid)
    assert stamped is not None, "annotate did not stamp updated_at"
    uploaded = age_query(
        "SELECT uploaded_at FROM age_samples WHERE sample_id = ?", [sid]
    )[0][0]
    assert stamped >= uploaded


def test_flag_stamps_updated_at_closing_the_flag_only_week_gap(
    admin: TestClient, age_query
):
    """THE reason the column exists. Before the split, a flag wrote no
    timestamp at all — `annotated_at` stays NULL by design (a flag is a
    refusal, not an answer) and nothing else moved — so a deployment that
    spent a week flagging unjudgeable samples read as an idle store and never
    triggered a backup, silently leaving that week's judgment unarchived.
    `updated_at` closes the gap: a flag stamps it, and the age watermark's
    max(uploaded_at, updated_at) then sees the week. The backup half of this
    story (a flag-only store actually fires the age job) is pinned in
    test_backup.py."""
    sid = _upload_age(admin, 12)
    resp = admin.post(f"/api/age/samples/{sid}/flag", json={"reason": "blur"})
    assert resp.status_code == 200, resp.text

    row = age_query(
        "SELECT updated_at, annotated_at, uploaded_at FROM age_samples "
        "WHERE sample_id = ?", [sid],
    )[0]
    stamped, annotated_at, uploaded_at = row
    assert stamped is not None, (
        "flag did not stamp updated_at — the flag-only-week backup gap is back"
    )
    assert annotated_at is None, "a flag is a refusal, not an answer"
    assert stamped >= uploaded_at


def test_reopen_stamps_updated_at(admin: TestClient, age_query):
    """Reopen is annotator judgment too ('that answer was wrong'), and it
    CLEARS annotated_at — so without its own stamp, reopening yesterday's
    annotation would move the store's newest visible timestamp backwards and
    the reopened state could miss the next backup entirely."""
    sid = _upload_age(admin, 13)
    resp = admin.post(f"/api/age/samples/{sid}/annotate", json={"age_days": 3})
    assert resp.status_code == 200, resp.text
    after_annotate = _updated_at(age_query, sid)
    assert after_annotate is not None

    resp = admin.post(f"/api/age/samples/{sid}/reopen")
    assert resp.status_code == 200, resp.text

    after_reopen = _updated_at(age_query, sid)
    assert after_reopen is not None, "reopen did not stamp updated_at"
    assert after_reopen >= after_annotate, (
        "reopen moved updated_at backwards; the watermark would un-see work"
    )
    assert age_query(
        "SELECT annotated_at FROM age_samples WHERE sample_id = ?", [sid]
    ) == [(None,)], "reopen must still clear annotated_at"
