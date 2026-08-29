"""Schema-alignment regression tests for `db.init_db` — amendments A2, A12, A13.

These pin the property that makes "additive and idempotent" (SPEC section 4)
real rather than aspirational: **`init_db` ALONE brings any store — brand new or
created by an older build — to the current schema.** No other module may be a
hidden prerequisite. The bug class this catches is a column that only exists
because `backup.ensure_backup_tables` happened to run first: such a store works
on the box where backups ran and fails on the box where they never did, which is
exactly the deployment nobody tests.

Three concrete shapes are defended:

- `backup_runs.delivery` (A13) must come from `init_db` itself, so the delivery
  reason is recordable even on a store whose backup module never opened it.
- `images."bytes"` must be `BIGINT` (A2): DuckDB `INTEGER` is 32-bit and a
  config bump past `upload.max_mb: ~2048` would overflow it silently. New
  stores must be born BIGINT, and a store created back when the column was
  INTEGER must be migrated up by the next `init_db` — with its rows intact,
  because those rows describe images that labeling hours refer to.
- `users.role` rows saying 'annotator' must come up as 'poweruser' (the role
  rename amends SPEC section 2; the SPEC is frozen, so the record lives here).
  Older builds wrote 'annotator' rows and production holds them, so the flip
  must run inside the boot path — `init_db` brings up the users table (A6) —
  and replay as a no-op on every subsequent boot.

Everything runs against throwaway DuckDB files under `tmp_path`; no Config, no
HTTP, no network, and never `data/`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import duckdb
import pytest

from bienenblech import db

# One byte more than INT32 can hold. A migrated column must accept it; the old
# INTEGER column could not, which is the whole point of A2.
OVER_INT32 = 2**31

# `images` exactly as SCHEMA_VERSION 1 originally created it, `"bytes" INTEGER`
# included. Frozen here as text, NOT imported from db.py: the migration under
# test only exists because old stores have this shape on disk, so the test must
# keep producing it after db.py stops being able to.
OLD_IMAGES_DDL = """
    CREATE TABLE images (
        image_id     TEXT PRIMARY KEY,
        filename     TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        width        INTEGER NOT NULL,
        height       INTEGER NOT NULL,
        stored_path  TEXT NOT NULL,
        "bytes"      INTEGER NOT NULL,
        crop_size    INTEGER NOT NULL,
        crop_overlap DOUBLE  NOT NULL,
        uploaded_by  TEXT,
        uploaded_at  TIMESTAMP NOT NULL,
        note         TEXT
    );
"""

# `backup_runs` as originally created: no `delivery` column (A13 added it).
OLD_BACKUP_RUNS_DDL = """
    CREATE TABLE backup_runs (
        run_id      TEXT PRIMARY KEY,
        started_at  TIMESTAMP,
        finished_at TIMESTAMP,
        status      TEXT,
        trigger     TEXT,
        n_masks     BIGINT,
        n_images    BIGINT,
        "bytes"     BIGINT,
        zip_path    TEXT,
        delivered   BOOLEAN,
        error       TEXT,
        host        TEXT
    );
"""


@pytest.fixture()
def con(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A connection to a brand-new DuckDB file under `tmp_path`.

    A real file rather than `:memory:` because the migration scenario is "a
    store on disk that an older build wrote"; keeping every test on the same
    substrate means a failure reproduces with the CLI against the same file.
    """
    handle = duckdb.connect(str(tmp_path / "store.duckdb"))
    try:
        yield handle
    finally:
        handle.close()


def _column_type(con: duckdb.DuckDBPyConnection, table: str, column: str) -> str | None:
    row = con.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = ? AND column_name = ?",
        [table, column],
    ).fetchone()
    return None if row is None else str(row[0]).upper()


def _schema_snapshot(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Every (table, column, type, nullability), ordered — the whole DDL surface
    a second `init_db` run could possibly disturb."""
    return con.execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns ORDER BY table_name, ordinal_position"
    ).fetchall()


def _seed_old_store(con: duckdb.DuckDBPyConnection) -> None:
    """A store as an older build left it: old-shape `images` and `backup_runs`,
    each holding one row that the migration must carry through unchanged."""
    con.execute(OLD_IMAGES_DDL)
    con.execute(OLD_BACKUP_RUNS_DDL)
    con.execute(
        "INSERT INTO images VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)",
        ["img1", "frame.png", "a" * 64, 1920, 1280,
         "data/images/img1.jpg", 2**31 - 1, 640, 0.0, "alice", None],
    )
    con.execute(
        "INSERT INTO backup_runs VALUES (?, now(), now(), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["run1", "ok", "cli", 3, 1, 12345, "data/backups/x.zip", True, None, "box"],
    )


# ------------------------------------------------------- init_db alone suffices

def test_init_db_alone_creates_backup_runs_with_a_delivery_column(con):
    """A13 through `init_db` ALONE — `backup.ensure_backup_tables` is never
    called here, deliberately.

    If `delivery` only existed because the backup module patched it in, then
    `GET /api/backup/status` (which reads runs, and reports each run's delivery
    reason) would 500 on any store whose backup never ran — precisely the fresh
    deployment an operator checks first."""
    db.init_db(con)

    delivery = _column_type(con, "backup_runs", "delivery")
    assert delivery is not None, (
        "init_db alone did not give backup_runs a `delivery` column (A13); "
        "the column must not depend on backup.py having run"
    )
    assert "CHAR" in delivery, f"delivery must be TEXT, got {delivery}"
    # And the reason ladder is storable: text, not something boolean-shaped.
    con.execute(
        "INSERT INTO backup_runs (run_id, status, delivery) VALUES (?, ?, ?)",
        ["r1", "ok", "posted_summary"],
    )
    assert con.execute(
        "SELECT delivery FROM backup_runs WHERE run_id = 'r1'"
    ).fetchone()[0] == "posted_summary"


def test_init_db_alone_makes_images_bytes_bigint(con):
    """A2: a fresh store is born with `images."bytes"` as BIGINT.

    DuckDB INTEGER is 32-bit. `upload.max_mb: 200` keeps every real value far
    below the cliff, which is exactly why the overflow would only surface years
    from now, after a config bump — as a failed upload of a legitimate file."""
    db.init_db(con)

    assert _column_type(con, "images", "bytes") == "BIGINT", (
        'images."bytes" must be BIGINT (A2); INTEGER overflows at ~2.147 GB'
    )
    # Prove the headroom is real, not just declared.
    con.execute(
        "INSERT INTO images (image_id, filename, sha256, width, height, "
        'stored_path, "bytes", crop_size, crop_overlap, uploaded_at) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        ["big", "huge.tif", "b" * 64, 8000, 8000, "data/images/big.jpg",
         3 * OVER_INT32, 640, 0.0],
    )
    assert con.execute(
        "SELECT \"bytes\" FROM images WHERE image_id = 'big'"
    ).fetchone()[0] == 3 * OVER_INT32


# ------------------------------------------------------------ the old store

def test_init_db_migrates_old_integer_bytes_to_bigint_with_data_intact(con):
    """An existing store created with `"bytes" INTEGER` is migrated up by the
    next `init_db` run — and its rows survive.

    The rows are the load-bearing half: `images` rows are what every mask's
    SOURCE-image coordinates refer to, so a migration that reached BIGINT by
    dropping and recreating the table would "pass" a type check while orphaning
    every polygon in the store."""
    _seed_old_store(con)
    assert _column_type(con, "images", "bytes") == "INTEGER"  # the old shape is real

    db.init_db(con)

    assert _column_type(con, "images", "bytes") == "BIGINT", (
        "init_db left an old store's images.bytes as INTEGER (A2)"
    )
    row = con.execute(
        'SELECT image_id, filename, sha256, width, height, stored_path, "bytes", '
        "crop_size, crop_overlap, uploaded_by, note FROM images"
    ).fetchall()
    assert row == [("img1", "frame.png", "a" * 64, 1920, 1280,
                    "data/images/img1.jpg", 2**31 - 1, 640, 0.0, "alice", None)], (
        "the migration did not carry the existing images row through intact"
    )
    # The migrated column has BIGINT capacity, not merely the BIGINT name.
    con.execute(
        "INSERT INTO images (image_id, filename, sha256, width, height, "
        'stored_path, "bytes", crop_size, crop_overlap, uploaded_at) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, now())",
        ["big", "huge.tif", "c" * 64, 4000, 3000, "data/images/big.jpg",
         OVER_INT32, 640, 0.0],
    )


def test_init_db_adds_delivery_to_an_old_backup_runs_with_data_intact(con):
    """The same replay discipline for A13: an old store's `backup_runs` gains
    `delivery`, existing run rows keep their values, and the new column reads
    NULL for runs recorded before the reason existed — NULL is the honest value
    there, not a back-filled guess."""
    _seed_old_store(con)
    assert _column_type(con, "backup_runs", "delivery") is None  # genuinely old

    db.init_db(con)

    assert _column_type(con, "backup_runs", "delivery") is not None, (
        "init_db did not add `delivery` to an old backup_runs table (A13)"
    )
    row = con.execute(
        'SELECT run_id, status, "trigger", delivered, delivery FROM backup_runs'
    ).fetchall()
    assert row == [("run1", "ok", "cli", True, None)]


# ------------------------------------------------------------- idempotency

def test_init_db_twice_on_a_fresh_store_changes_nothing_further(con):
    """SPEC section 4: every migration is additive and idempotent. `init_db`
    runs on every boot, so the second run IS the common case — a second run that
    altered anything would make every redeploy a schema event."""
    db.init_db(con)
    first = _schema_snapshot(con)
    counts_first = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                    for (t,) in con.execute(
                        "SELECT table_name FROM information_schema.tables").fetchall()}

    db.init_db(con)

    assert _schema_snapshot(con) == first, "a second init_db changed the schema"
    counts_second = {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
                     for (t,) in con.execute(
                         "SELECT table_name FROM information_schema.tables").fetchall()}
    assert counts_second == counts_first, "a second init_db changed table contents"


def test_init_db_twice_on_a_migrated_old_store_changes_nothing_further(con):
    """Idempotency must hold on the migrated shape too: the INTEGER->BIGINT step
    and the `delivery` add both replay on every boot of an old store, so
    replaying them against their own output has to be a no-op — otherwise the
    store that most needs the migration is the one that cannot restart."""
    _seed_old_store(con)
    db.init_db(con)
    migrated = _schema_snapshot(con)
    rows = con.execute('SELECT image_id, "bytes" FROM images ORDER BY image_id').fetchall()

    db.init_db(con)

    assert _schema_snapshot(con) == migrated
    assert con.execute(
        'SELECT image_id, "bytes" FROM images ORDER BY image_id'
    ).fetchall() == rows
    assert con.execute("SELECT count(*) FROM backup_runs").fetchone()[0] == 1


# --------------------------------------------------------- the users role flip

# `users` exactly as older builds created it — the second role was 'annotator'
# then, so the DEFAULT says so. Frozen as text, NOT imported from auth.py, for
# the same reason as the DDL blocks above: the migration under test only exists
# because stores with this shape are on disk, so the test must keep producing
# it after auth.py stops being able to.
OLD_USERS_DDL = """
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'annotator',
        created_at    TIMESTAMP NOT NULL DEFAULT now()
    );
"""

# A literal hash in the self-describing `scrypt$N$r$p$salt$hash` shape. Not a
# real credential and never verified here — what matters is byte-identity: the
# role flip must not touch the hash column, because a migration that rewrote it
# would lock every existing user out on the first boot after the upgrade.
FROZEN_HASH = "scrypt$16384$8$1$" + "ab" * 16 + "$" + "cd" * 32


def _seed_old_users(con: duckdb.DuckDBPyConnection) -> None:
    """Users as an older build left them: one of each role, by hand."""
    con.execute(OLD_USERS_DDL)
    con.execute(
        "INSERT INTO users VALUES ('alice', ?, 'annotator', now())", [FROZEN_HASH]
    )
    con.execute(
        "INSERT INTO users VALUES ('root', ?, 'admin', now())", [FROZEN_HASH]
    )


def test_init_db_flips_annotator_rows_to_poweruser(con):
    """The role rename, at the store level: 'annotator' rows become 'poweruser'
    through `init_db` ALONE (A6 routes the users table through it, so every
    boot path inherits the flip). Without this, existing accounts would hold a
    role no API gate recognises — not read-only, but locked out of everything —
    and the admin rows must ride through untouched, because a flip that widened
    its WHERE clause would silently promote or demote the wrong rows."""
    _seed_old_users(con)

    db.init_db(con)

    rows = dict(con.execute("SELECT username, role FROM users").fetchall())
    assert rows == {"alice": "poweruser", "root": "admin"}, (
        "init_db must flip exactly the 'annotator' rows to 'poweruser'"
    )
    hashes = con.execute("SELECT password_hash FROM users").fetchall()
    assert all(h == (FROZEN_HASH,) for h in hashes), (
        "the role flip rewrote password hashes; every user is now locked out"
    )


def test_the_role_flip_replays_as_a_no_op(con):
    """The boot block runs on every start, so the flip's second run IS the
    common case: same schema, same rows, byte for byte — including timestamps,
    which is what catches a flip implemented as delete-and-reinsert."""
    _seed_old_users(con)
    db.init_db(con)
    first = con.execute(
        "SELECT username, password_hash, role, created_at "
        "FROM users ORDER BY username"
    ).fetchall()
    assert {r[2] for r in first} == {"admin", "poweruser"}  # the flip happened

    db.init_db(con)

    second = con.execute(
        "SELECT username, password_hash, role, created_at "
        "FROM users ORDER BY username"
    ).fetchall()
    assert second == first, "replaying the boot path disturbed migrated users"


def test_same_path_for_both_stores_is_a_boot_error(tmp_path, monkeypatch):
    """paths.age_db_path == paths.db_path defeats the split and would let the
    legacy-table drop destroy live age data - loud and fatal at boot, because
    it is a config error, not a runtime state."""
    import pytest as _pytest
    from bienenblech.api import create_app
    from bienenblech.config import BackupCfg, Config, PathsCfg

    root = tmp_path / 'store'
    same = str(root / 'one.duckdb')
    cfg = Config(
        paths=PathsCfg(db_path=same, age_db_path=same,
                       images_dir=str(root / 'images'),
                       cache_dir=str(root / 'cache'),
                       backups_dir=str(root / 'backups')),
        backup=BackupCfg(enabled=False),
    )
    with _pytest.raises(ValueError, match="age_db_path must differ"):
        create_app(cfg)
