"""Backup tests — SPEC section 8 and amendments A11, A12, A13, A15, updated
for the per-tool store split.

This archive is *posted to a chat channel*. That single fact is what most of
these tests defend: what goes into each zip, what must never go into any zip,
and what must happen when one cannot be posted. The rest defend the failure
taxonomy, whose whole purpose is that a transient lock can never turn into a
permanently disabled backup.

Since the split there are TWO independent weekly jobs, one per store, and the
independence is itself contract: own watermark (in each store's own `meta`),
own 6h failure cooldown, own claim row, own rotation (`backup.keep` applies
per store), posting to the same webhook. `bienenblech-<stamp>-<run>.zip` is
the Blech archive — its member list back to exactly its pre-age shape — and
`bienenblech-age-<stamp>-<run>.zip` is the Age archive with the `age.duckdb`
snapshot. Blech-only activity must never fire an age backup and vice versa;
section 8's failure taxonomy applies per store. The blech-side tests therefore
drive `backup._run_store(backup._BLECH, ...)` — the per-store job the
scheduler runs — and the handful of tests about the combined `run_backup`
say so explicitly.

Self-contained on purpose: every fixture is defined at module level, under
names (`seeded_store`, `admin_client`) that cannot collide with the differently
shaped `store` and `client` fixtures in `tests/conftest.py`, so this file never
depends on a fixture it does not define.

TWO SAFETY RULES, both enforced by autouse fixtures:

1. **No test may reach a real webhook.** `_no_network` unsets
   `BIENENBLECH_DISCORD_WEBHOOK` and replaces both `backup._discord_poster` and
   `urllib.request.urlopen` with functions that fail the test. Any test that
   needs a configured webhook sets an obviously fake one and passes an explicit
   recording `poster`. That applies to the tests that "cannot possibly post" too:
   the point of the guard is the day one of them can.
2. **No test may touch the real store.** Every path — both DuckDB files
   included — is under `tmp_path` and `_paths_are_sandboxed` asserts it. A run
   that rotated away real backups would be worse than no tests at all.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bienenblech import backup, db
from bienenblech.api import create_app
from bienenblech.config import Config

FRAME = 128
CROP = 64

ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-admin-password"

# Shaped like a real Discord webhook so `_looks_like_webhook` accepts it and the
# delivery ladder is actually exercised, but it points at an id and token that
# cannot exist. Nothing ever opens a socket to it: the poster is always injected.
FAKE_WEBHOOK = (
    "https://discord.com/api/webhooks/000000000000000000/"
    "TEST-TOKEN-THIS-IS-NOT-A-REAL-WEBHOOK-0123456789"
)

# The self-describing prefix of every stored password (`auth.hash_password`
# writes `scrypt$<N>$<r>$<p>$<salt>$<hash>`). Searching the raw archive bytes for
# it is the strongest available statement of A11: not "the table was dropped"
# but "the hash was never written into this file".
SCRYPT_MARKER = b"scrypt$"

# The two archive name shapes, restated here from the contract rather than read
# out of backup.py — the file names are an interface (operators fetch them by
# hand, rotation keys on them), so a drift must fail a test, not follow it.
# 'bienenblech' is a PREFIX of 'bienenblech-age', which is exactly why rotation
# and these helpers match the full shape and never a bare prefix glob.
BLECH_ZIP_RE = re.compile(r"^bienenblech-\d{8}T\d{6}Z-[0-9a-f]{8}\.zip$")
AGE_ZIP_RE = re.compile(r"^bienenblech-age-\d{8}T\d{6}Z-[0-9a-f]{8}\.zip$")


# --------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIENENBLECH_CONFIG", raising=False)
    monkeypatch.setenv("BIENENBLECH_SECRET", "test-secret-not-a-real-key")
    monkeypatch.setenv("BIENENBLECH_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("BIENENBLECH_ADMIN_PASSWORD", ADMIN_PASSWORD)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test, ever, reaches a real webhook.

    SPEC section 8 reads the URL from the environment *at the point of use*, so
    a developer with `BIENENBLECH_DISCORD_WEBHOOK` exported in their shell would
    otherwise have this suite post real archives of synthetic data into a real
    channel — and a post cannot be un-posted. Both the module's default poster
    and the underlying `urlopen` are replaced, so a future test that forgets to
    inject a poster fails loudly instead of quietly succeeding over the wire."""
    monkeypatch.delenv(backup.WEBHOOK_ENV, raising=False)

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "a test tried to reach the network / the real Discord poster"
        )

    monkeypatch.setattr(backup, "_discord_poster", _refuse)
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "store"


def _config(root: Path, **backup_kw: Any) -> Config:
    """A Config rooted at `root`. `backup_kw` overrides the backup block, so a
    test can change `keep` or `max_upload_mb` without re-seeding the store."""
    settings = {"enabled": True, "interval_days": 7, "keep": 8, "max_upload_mb": 8}
    settings.update(backup_kw)
    return Config(
        project="bienenblech-test",
        paths={
            "db_path": str(root / "bienenblech.duckdb"),
            "age_db_path": str(root / "age.duckdb"),
            "images_dir": str(root / "images"),
            "cache_dir": str(root / "cache"),
            "backups_dir": str(root / "backups"),
        },
        crop={"size": CROP, "overlap": 0.0, "min_edge": 16, "jpeg_quality": 80},
        backup=settings,
    )


@pytest.fixture()
def cfg(root: Path) -> Config:
    return _config(root)


@pytest.fixture(autouse=True)
def _paths_are_sandboxed(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Refuse to run a test whose stores or backups directory escape `tmp_path`."""
    if "cfg" not in request.fixturenames:
        return
    config: Config = request.getfixturevalue("cfg")
    for path in (config.paths.db_path, config.paths.age_db_path,
                 config.paths.images_dir, config.paths.cache_dir,
                 config.paths.backups_dir):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve()), (
            f"test store escaped tmp_path: {path}"
        )


@pytest.fixture()
def admin_client(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A signed-in admin client against a fresh pair of stores.

    The scheduler thread is stubbed out rather than merely disabled: it is a
    process-global daemon that would outlive the test holding a DuckDB handle in
    a directory pytest is about to remove."""
    monkeypatch.setattr(backup, "start_scheduler", lambda config: None)
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        yield c


class Recorder:
    """A `Poster` that records instead of posting, and optionally fails.

    `_run_store(poster=...)` exists precisely so the delivery ladder is testable
    with no network — see `backup.Poster`."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def __call__(self, webhook: str, content: str, file_path: Path | None) -> None:
        self.calls.append({"webhook": webhook, "content": content,
                           "file_path": file_path})
        if self.error is not None:
            raise self.error


@pytest.fixture()
def poster() -> Recorder:
    return Recorder()


# ---------------------------------------------------------------------- helpers

def _run_blech(config: Config, **kw: Any) -> dict[str, Any]:
    """One attempt of the BLECH job — the per-store run the scheduler makes."""
    return backup._run_store(backup._BLECH, config, **kw)


def _run_age(config: Config, **kw: Any) -> dict[str, Any]:
    """One attempt of the AGE job."""
    return backup._run_store(backup._AGE, config, **kw)


def _png(seed: int, size: int = FRAME) -> bytes:
    im = Image.new("RGB", (size, size))
    im.putdata([
        ((x * 7 + seed * 31) % 256, (y * 5 + seed * 13) % 256, (x * y + seed) % 256)
        for y in range(size) for x in range(size)
    ])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _upload(client: TestClient, seed: int) -> str:
    r = client.post(
        "/api/images",
        files=[("file", (f"frame{seed}.png", io.BytesIO(_png(seed)), "image/png"))],
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["images"], f"upload {seed} was deduped: {payload}"
    return payload["images"][0]["image_id"]


def _upload_age(client: TestClient, seed: int) -> str:
    """One age sample through the real route, returning its sample_id."""
    r = client.post(
        "/api/age/samples",
        files=[("file", (f"bee{seed}.png", io.BytesIO(_png(seed)), "image/png"))],
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["samples"], f"age upload {seed} was deduped: {payload}"
    return payload["samples"][0]["sample_id"]


@pytest.fixture()
def seeded_store(admin_client: TestClient) -> dict[str, Any]:
    """A seeded BLECH store built through the real API: one frame, a 2x2 crop
    grid, two classes, polygons on two crops, one completed crop and one
    `is_empty` crop. The AGE store is deliberately left empty — several tests
    below lean on exactly that asymmetry.

    Built through HTTP rather than by inserting rows so the crop grid, the
    coordinate offsets and the completion guards are exactly what production
    writes — the backup's job is to preserve *that*."""
    classes = {}
    for name in ("Wax", "Mite"):
        r = admin_client.post("/api/classes", json={"name": name})
        assert r.status_code == 200, r.text
        classes[name] = r.json()

    image_id = _upload(admin_client, 1)
    c00 = db.crop_id_for(image_id, 0, 0)
    c01 = db.crop_id_for(image_id, 0, 1)
    c10 = db.crop_id_for(image_id, 1, 0)

    for crop_id, name, points in (
        (c00, "Wax", [[4, 4], [40, 4], [40, 32]]),
        (c00, "Mite", [[8, 8], [56, 56], [56, 8], [8, 56]]),
        (c10, "Wax", [[2, 2], [60, 2], [60, 60]]),
    ):
        r = admin_client.post("/api/masks", json={
            "crop_id": crop_id, "class_id": classes[name]["class_id"], "points": points,
        })
        assert r.status_code == 200, r.text

    r = admin_client.post(f"/api/crops/{c00}/complete", json={"is_empty": False})
    assert r.status_code == 200, r.text
    r = admin_client.post(f"/api/crops/{c01}/complete", json={"is_empty": True})
    assert r.status_code == 200, r.text

    return {"image_id": image_id, "c00": c00, "c01": c01, "c10": c10,
            "classes": classes}


def _members(zip_path: str | Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(zf.namelist())


def _extract_snapshot(
    zip_path: str | Path, dest_dir: Path, member: str = "bienenblech.duckdb"
) -> Path:
    """Pull the DB snapshot out of the archive onto its own, and only its
    own, path — a restore has nothing but this member and no WAL sidecar."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / member
    with zipfile.ZipFile(zip_path) as zf:
        out.write_bytes(zf.read(member))
    return out


def _tables(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0]).lower()
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return int(con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _store_runs(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    backup.ensure_backup_tables(con)
    cols = ("run_id", "status", "trigger", "delivered", "delivery", "error",
            "zip_path", "started_at", "finished_at")
    rows = con.execute(
        'SELECT run_id, status, "trigger", delivered, delivery, error, zip_path, '
        "started_at, finished_at FROM backup_runs ORDER BY started_at"
    ).fetchall()
    return [dict(zip(cols, row)) for row in rows]


def _runs(cfg: Config) -> list[dict[str, Any]]:
    """Every `backup_runs` row of the MAIN store, oldest first.

    `ensure_backup_tables` is called first so this works against a store whose
    backup machinery never ran — exactly the situation in the contention tests,
    where `_open_store` is stubbed out."""
    con = db.connect(cfg)
    try:
        return _store_runs(con)
    finally:
        con.close()


def _age_runs(cfg: Config) -> list[dict[str, Any]]:
    """Every `backup_runs` row of the AGE store — its own history, its own file."""
    con = db.connect_age(cfg)
    try:
        return _store_runs(con)
    finally:
        con.close()


def _watermark(cfg: Config) -> str | None:
    con = db.connect(cfg)
    try:
        return db.get_meta(con, backup.META_WATERMARK)
    finally:
        con.close()


def _age_watermark(cfg: Config) -> str | None:
    con = db.connect_age(cfg)
    try:
        backup.ensure_backup_tables(con)
        return db.get_meta(con, backup.META_WATERMARK)
    finally:
        con.close()


def _blech_zips(cfg: Config) -> list[Path]:
    return sorted(
        p for p in Path(cfg.paths.backups_dir).glob("*.zip")
        if BLECH_ZIP_RE.match(p.name)
    )


def _age_zips(cfg: Config) -> list[Path]:
    return sorted(
        p for p in Path(cfg.paths.backups_dir).glob("*.zip")
        if AGE_ZIP_RE.match(p.name)
    )


# ============================================================== A11: the headline

def test_users_table_is_not_in_the_backup_snapshot(seeded_store, cfg, poster, tmp_path):
    """A11: the `users` table must NOT be in the DuckDB snapshot inside the zip.

    SPEC section 8 mandates posting this archive to a Discord webhook and SPEC
    section 4 puts usernames and scrypt password hashes in that same database.
    Nobody wrote down the consequence: the channel becomes exactly as sensitive
    as the box, and a post cannot be un-posted. scrypt is salted and expensive so
    this is not a catastrophe, but it was never a decision anyone made. The
    resolution is to exclude `users` from the snapshot — accounts are cheap
    (a restore re-bootstraps the admin from `BIENENBLECH_ADMIN_*` and poweruser
    accounts are recreated by hand), annotations are not.

    The absence check alone is not enough: an empty or half-copied snapshot would
    pass it while quietly destroying the backup. So this also asserts the tables
    that matter are present AND populated, and that the raw snapshot bytes
    contain no scrypt hash anywhere — a table copied and then dropped can still
    leave its blocks readable in the file, so "never copied" is the property
    under test, not "deleted afterwards"."""
    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result
    assert not poster.calls, "no webhook is configured, so nothing may be posted"

    zip_path = Path(result["zip_path"])
    assert "bienenblech.duckdb" in _members(zip_path)

    snapshot = _extract_snapshot(zip_path, tmp_path / "restore")
    con = duckdb.connect(str(snapshot))
    try:
        tables = _tables(con)
        assert "users" not in tables, (
            "the backup snapshot carries the `users` table; posting it to a "
            "Discord webhook would put every scrypt password hash in the channel "
            "(A11)"
        )
        for table in ("images", "crops", "masks", "label_classes"):
            assert table in tables, f"the snapshot lost {table}"
            assert _count(con, table) > 0, (
                f"{table} is empty in the snapshot — an empty snapshot would pass "
                "a naive 'users is absent' check while losing the entire backup"
            )
        assert _count(con, "images") == 1
        assert _count(con, "crops") == 4
        assert _count(con, "masks") == 3
        assert _count(con, "label_classes") == 2
    finally:
        con.close()

    assert SCRYPT_MARKER not in snapshot.read_bytes(), (
        "a scrypt password hash is still present in the snapshot bytes"
    )

    # The live store still has its users, of course — this is an exclusion from
    # the archive, never a deletion from the box.
    live = db.connect(cfg)
    try:
        assert _count(live, "users") >= 1
    finally:
        live.close()


def test_no_zip_member_carries_a_password_hash(seeded_store, cfg, poster, tmp_path):
    """A11, the other half: the flat CSVs must not reintroduce what the snapshot
    excludes.

    `masks.csv` and friends exist so the archive outlives DuckDB, and a
    well-meaning "add users.csv so a restore keeps its accounts" would undo the
    exclusion in a form that is even easier to read. Usernames themselves are
    fine and expected — `created_by`, `completed_by`, `uploaded_by` are
    provenance, and a dataset that cannot say who labeled what is a worse
    dataset. Hashes are the secret, so the assertion is about hashes."""
    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    with zipfile.ZipFile(result["zip_path"]) as zf:
        names = zf.namelist()
        assert "users.csv" not in names
        csvs = [n for n in names if n.endswith(".csv")]
        # Back to the PRE-AGE four since the store split: age_samples.csv now
        # travels in the age archive, whose store this zip never opens.
        assert sorted(csvs) == ["classes.csv", "crops.csv", "images.csv",
                                "masks.csv"]
        for name in csvs:
            text = zf.read(name).decode("utf-8")
            header = text.splitlines()[0].split(",")
            offenders = [h for h in header if "password" in h.lower()]
            assert not offenders, f"{name} has a password column: {offenders}"
        for name in names:
            assert SCRYPT_MARKER not in zf.read(name), (
                f"zip member {name} contains a scrypt password hash"
            )

        # And the manifest says what was left out, so a restorer never has to
        # infer "no accounts" from an absence.
        man = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert "users" in man.get("snapshot_excluded_tables", [])


# ============================================== enumerated members, not a walk

def test_blech_zip_member_list_is_exactly_its_pre_age_shape(
    seeded_store, cfg, admin_client, poster
):
    """The Blech archive's member list, pinned exactly — and pinned to its
    PRE-AGE shape (owner decision, the store split): DB snapshot, the four flat
    CSVs, the frame derivatives, manifest, README. Nothing age-shaped, even
    while the age store right next to it holds samples: those now travel in
    `bienenblech-age-*.zip`, and a blech zip that quietly re-grew age members
    would mean the blech job opened the wrong store."""
    sample_id = _upload_age(admin_client, 90)   # age data EXISTS, elsewhere
    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result
    assert BLECH_ZIP_RE.match(Path(result["zip_path"]).name), result["zip_path"]

    names = _members(result["zip_path"])
    assert names == sorted([
        "bienenblech.duckdb",
        "classes.csv", "crops.csv", "images.csv", "masks.csv",
        f"images/{seeded_store['image_id']}.jpg",
        "manifest.json", "README.txt",
    ]), f"the blech member list drifted from its pre-age shape: {names}"
    assert not any(n.startswith("age/") for n in names)
    assert "age_samples.csv" not in names
    assert sample_id not in str(names)


def test_a_stray_file_beside_the_images_never_reaches_the_archive(
    seeded_store, cfg, poster, tmp_path
):
    """The zip's member list is ENUMERATED from the database, never a directory
    walk (`_image_members` and `_write_zip` both say so).

    This archive is posted to a chat channel. A glob-based backup that one day
    sweeps up an adjacent secret — an operator's `.env` copied into `data/images`
    while debugging, a stray dump — cannot be un-posted. So the rule is that a
    file the DB does not know about is not part of the dataset and is not in the
    zip, however conveniently it happens to be sitting there."""
    stray_names = ("SECRET.env", "notes.txt", "operator-dump.sql")
    for name in stray_names:
        (Path(cfg.paths.images_dir) / name).write_text(
            "BIENENBLECH_SECRET=super-secret", encoding="utf-8"
        )
    Path(cfg.paths.backups_dir).mkdir(parents=True, exist_ok=True)
    (Path(cfg.paths.backups_dir) / "leftover.txt").write_text("x", encoding="utf-8")

    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    names = _members(result["zip_path"])
    for stray in (*stray_names, "leftover.txt"):
        assert not any(stray in n for n in names), f"{stray} was swept into the archive"
    # The DB-known derivative IS there — proving the absence above is selection,
    # not a broken image step.
    assert f"images/{seeded_store['image_id']}.jpg" in names


def test_write_zip_takes_only_the_members_it_was_given(tmp_path):
    """`_write_zip` at the unit level: a stray dropped into the staging directory
    it is zipping FROM must not appear in the archive.

    The staging directory is a temp dir inside `backups_dir`, so anything a
    concurrent process writes there would be swept up by a `glob('*')`
    implementation. The enumeration is the whole defence."""
    staging = tmp_path / "staging"
    staging.mkdir()
    wanted = staging / "bienenblech.duckdb"
    wanted.write_bytes(b"not really a database")
    (staging / "masks.csv").write_text("mask_id\n", encoding="utf-8")
    (staging / "STRAY-SECRET.env").write_text("token=hunter2", encoding="utf-8")

    dest = tmp_path / "out.zip"
    size = backup._write_zip(dest, [
        ("bienenblech.duckdb", wanted),
        ("masks.csv", staging / "masks.csv"),
    ])

    assert size == dest.stat().st_size > 0
    assert _members(dest) == ["bienenblech.duckdb", "masks.csv"]
    assert not (tmp_path / "out.zip.part").exists(), "the .part file was left behind"


# ============================================================== redaction

def test_redact_scrubs_the_webhook_url_and_its_token():
    """`_redact` is the last thing between the webhook URL and a place it can be
    read: a printed log line, an exception chain, and `backup_runs.error` — which
    travels inside the very zip that gets posted to the channel. A URL in there
    hands the ability to post as this box to anyone who can read the archive."""
    token = FAKE_WEBHOOK.rsplit("/", 1)[-1]
    text = f"HTTP 401 from {FAKE_WEBHOOK} while posting"

    scrubbed = backup._redact(text, FAKE_WEBHOOK)
    assert FAKE_WEBHOOK not in scrubbed
    assert token not in scrubbed
    assert "<discord-webhook>" in scrubbed
    assert "HTTP 401" in scrubbed, "redaction must not eat the diagnosis"

    # A URL that merely appears in third-party text, with no webhook passed in.
    assert FAKE_WEBHOOK not in backup._redact(f"urllib: <{FAKE_WEBHOOK}>", "")
    # And an unrelated string is untouched.
    assert backup._redact("disk full writing /data/backups", "") == (
        "disk full writing /data/backups"
    )


def test_a_failure_naming_the_webhook_is_redacted_in_backup_runs_error(
    seeded_store, cfg, monkeypatch
):
    """The stored `backup_runs.error` must be redacted, not just the printed line.

    That column is inside the DuckDB snapshot of the NEXT archive, which is
    posted to the channel — so an un-redacted error is a credential leak on a
    one-run delay, and the failing run is exactly the one whose exception carries
    the URL (urllib puts the full URL into its HTTPError attributes)."""
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    token = FAKE_WEBHOOK.rsplit("/", 1)[-1]
    exploding = Recorder(error=RuntimeError(f"HTTP 401 Unauthorized for {FAKE_WEBHOOK}"))

    result = _run_blech(cfg, trigger="cli", force=True, poster=exploding)

    assert result["status"] == "failed"
    assert exploding.calls, "the poster should have been reached"
    assert FAKE_WEBHOOK not in result["error"] and token not in result["error"]
    assert "<discord-webhook>" in result["error"]

    rows = _runs(cfg)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    stored = rows[0]["error"] or ""
    assert FAKE_WEBHOOK not in stored, "the raw webhook URL is stored in backup_runs"
    assert token not in stored, "the webhook token is stored in backup_runs"
    assert "HTTP 401" in stored, "the operator still needs to know what happened"


# ============================================================== the size ladder

def test_oversize_zip_is_still_written_rotated_and_summarised(
    seeded_store, root, poster, monkeypatch
):
    """Over `backup.max_upload_mb`: the zip is STILL written and STILL rotated
    locally, and only a text summary naming the local path is posted.

    Discord's per-file cap is 8-25 MB depending on server tier and it rejects
    rather than truncating. A silently dropped backup is the exact failure mode
    section 8 exists to avoid: the operator reads a quiet channel and concludes
    nothing needs doing. So the message must both prove the backup exists and say
    where to fetch it."""
    cfg = _config(root, max_upload_mb=0)   # every archive is over a 0 MB cap
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "ok", result
    assert result["delivery"] == "posted_summary"

    zip_path = Path(result["zip_path"])
    assert zip_path.is_file(), "the oversize archive was not written"
    assert zip_path.stat().st_size == result["zip_bytes"] > 0
    assert zip_path in _blech_zips(cfg), "the oversize archive was not retained locally"
    assert "bienenblech.duckdb" in _members(zip_path)

    assert len(poster.calls) == 1
    call = poster.calls[0]
    assert call["file_path"] is None, "an over-cap archive must not be attached"
    assert str(zip_path) in call["content"], (
        "the summary must name the local path, or the operator cannot fetch it"
    )

    row = _runs(cfg)[-1]
    assert row["status"] == "ok"
    assert row["delivery"] == "posted_summary"
    assert row["delivered"] is False, (
        "a summary-only post is a delivered message, not a delivered backup"
    )


# ============================================================== unset webhook

def test_unset_webhook_still_zips_rotates_and_advances_the_watermark(
    seeded_store, cfg, poster
):
    """An unset webhook is a FULLY SUPPORTED state (SPEC section 8), not a
    degraded one — which is also why it must not appear as `${VAR:?}` in
    docker-compose.

    The local archive is the backup; the Discord post is a convenience. Treating
    "no webhook" as a failure would arm the 6-hour cooldown on every run and
    silently disable backups on exactly the deployments that never configured
    one."""
    assert backup.WEBHOOK_ENV not in os.environ

    assert _watermark(cfg) is None
    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "ok", result
    assert result["delivery"] == "skipped"
    assert not poster.calls, "nothing may be posted with no webhook configured"
    assert Path(result["zip_path"]).is_file()
    assert _blech_zips(cfg) == [Path(result["zip_path"])]

    watermark = _watermark(cfg)
    assert watermark, "the watermark must advance even with no webhook"

    con = db.connect(cfg)
    try:
        assert datetime.fromisoformat(watermark) == backup._last_change(con)
        is_due, reason = backup.due(con, cfg.backup, store=backup._BLECH)
    finally:
        con.close()
    assert is_due is False and "nothing new" in reason


def test_discord_disabled_still_produces_a_local_archive(seeded_store, cfg, poster):
    """`discord=False` is the same supported shape reached a different way."""
    result = _run_blech(cfg, trigger="cli", force=True, discord=False,
                        poster=poster)
    assert result["status"] == "ok" and result["delivery"] == "disabled"
    assert not poster.calls
    assert Path(result["zip_path"]).is_file()


# ============================================================== rotation

def test_local_rotation_keeps_exactly_backup_keep_archives(seeded_store, root, poster):
    """Rotation keeps `backup.keep` zips OF THIS STORE under `backups_dir` — no
    more (the disk is small and the images are the expensive half) and no fewer.

    Pruned BY NAME, not by mtime: the names lead with a UTC stamp and therefore
    sort chronologically, while an archive restored onto the box carries an
    unrelated mtime that would evict the wrong file. The run that just succeeded
    is protected whatever `keep` says, so a misconfigured `keep: 0` can never
    delete the backup it just made."""
    keep = 3
    cfg = _config(root, keep=keep)
    backups = Path(cfg.paths.backups_dir)
    backups.mkdir(parents=True, exist_ok=True)
    older = [backups / f"bienenblech-2020010{i}T000000Z-aaaaaaa{i}.zip"
             for i in range(1, 6)]
    for path in older:
        path.write_bytes(b"an older archive")

    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    remaining = _blech_zips(cfg)
    assert len(remaining) == keep, [p.name for p in remaining]
    assert Path(result["zip_path"]) in remaining, "rotation removed the new archive"
    # The survivors are the newest by name: the last (keep - 1) placeholders.
    assert remaining[:-1] == older[-(keep - 1):]


def test_rotation_is_per_prefix_and_blind_to_the_other_stores_zips(
    seeded_store, admin_client, root, poster
):
    """`backup.keep` applies PER STORE, and each store's rotation must be blind
    to the other's archives. The trap is textual: 'bienenblech' is a prefix of
    'bienenblech-age', so the pre-split glob `bienenblech-*.zip` matches BOTH
    names — a blech rotation still using it would count the age archives
    against the blech budget and, given enough of them, delete the newest age
    zips as 'oldest blech'. Rotation must key on the full name shape."""
    cfg = _config(root, keep=3)
    sample_id = _upload_age(admin_client, 91)
    r = admin_client.post(f"/api/age/samples/{sample_id}/annotate",
                          json={"age_days": 9})
    assert r.status_code == 200, r.text

    backups = Path(cfg.paths.backups_dir)
    backups.mkdir(parents=True, exist_ok=True)
    old_blech = [backups / f"bienenblech-2020010{i}T000000Z-aaaaaaa{i}.zip"
                 for i in range(1, 6)]
    old_age = [backups / f"bienenblech-age-2020010{i}T000000Z-bbbbbbb{i}.zip"
               for i in range(1, 6)]
    for path in (*old_blech, *old_age):
        path.write_bytes(b"an older archive")

    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result
    assert len(_blech_zips(cfg)) == 3, [p.name for p in _blech_zips(cfg)]
    assert _age_zips(cfg) == old_age, (
        "the blech rotation touched the age archives — 'bienenblech' is a "
        "prefix of 'bienenblech-age', and the rotation matched on the prefix"
    )

    result = _run_age(cfg, trigger="cli", force=True, keep=2, poster=poster)
    assert result["status"] == "ok", result
    age_remaining = _age_zips(cfg)
    assert len(age_remaining) == 2, [p.name for p in age_remaining]
    assert Path(result["zip_path"]) in age_remaining
    assert len(_blech_zips(cfg)) == 3, (
        "the age rotation deleted blech archives"
    )


# ============================================================== failure taxonomy

def test_contention_on_open_writes_no_row_arms_no_cooldown_and_does_not_fail(
    seeded_store, cfg, poster, monkeypatch
):
    """CONTENTION — the store is held elsewhere — is `skipped`: no row, no
    cooldown, and a zero exit status.

    Without that split, an operator who wires `bienenblech backup --force` into a
    nightly host cron converts a transient DuckDB lock into a *permanently
    disabled* backup: every nightly collision would re-arm the six-hour cooldown
    and the scheduler's tick would never find a green window again. The bug would
    present months later as "the backups just stopped"."""
    monkeypatch.setattr(
        backup, "_open_store",
        lambda store, config: (_ for _ in ()).throw(
            backup._StoreBusy("locked by the app")
        ),
    )

    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "skipped"
    assert result["reason"] == "store busy"
    assert result["error"] is None, "contention is not an error"
    assert result["status"] != "failed", "the CLI exits non-zero only on 'failed'"
    assert not poster.calls
    assert _blech_zips(cfg) == [], "a skipped run must not write an archive"

    monkeypatch.undo()
    assert _runs(cfg) == [], "contention must write no backup_runs row at all"
    con = db.connect(cfg)
    try:
        is_due, _ = backup.due(con, cfg.backup, store=backup._BLECH)
    finally:
        con.close()
    assert is_due is True, "contention must not arm the cooldown"


def test_a_refused_claim_writes_no_row_and_arms_no_cooldown(
    seeded_store, cfg, poster, monkeypatch
):
    """The other half of the contention class: the claim mutex refused us because
    another runner (the scheduler thread, a second container on one bind mount)
    holds it. Same rule — no row, no cooldown, exit 0."""
    monkeypatch.setattr(backup, "_claim",
                        lambda con, *, trigger, store=None: None)

    result = _run_blech(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "skipped"
    assert result["reason"] == "store busy"
    assert result["run_id"] is None
    assert _blech_zips(cfg) == []

    monkeypatch.undo()
    assert _runs(cfg) == []
    con = db.connect(cfg)
    try:
        assert backup.due(con, cfg.backup, store=backup._BLECH)[0] is True
    finally:
        con.close()


def test_genuine_failure_writes_a_failed_row_arms_the_cooldown_and_holds_the_watermark(
    seeded_store, cfg, monkeypatch
):
    """GENUINE FAILURE — disk full, torn snapshot, webhook unreachable — writes a
    `failed` row, arms the six-hour cooldown, and does NOT advance the watermark.

    The held watermark is the load-bearing half: the next successful run must
    re-cover everything this run failed to archive, so nothing is ever silently
    dropped out of the archive. The cooldown is what stops a broken webhook
    spamming the alert feed every fifteen minutes."""
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    exploding = Recorder(error=RuntimeError("connection refused"))

    result = _run_blech(cfg, trigger="cli", force=True, poster=exploding)

    assert result["status"] == "failed"
    assert result["error"]

    rows = _runs(cfg)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["finished_at"] is not None, "a failed run must be closed out"
    assert rows[0]["delivered"] is False

    assert _watermark(cfg) is None, (
        "a failed run advanced the watermark; the work it did not archive would "
        "never be archived"
    )
    con = db.connect(cfg)
    try:
        is_due, reason = backup.due(con, cfg.backup, store=backup._BLECH)
    finally:
        con.close()
    assert is_due is False and "cooling down" in reason


def test_claim_is_a_mutex_using_the_transient_running_status(seeded_store, cfg, poster):
    """A12: `backup_runs.status` needs a transient `'running'`.

    The claim is a compare-and-set inside an explicit transaction, so the loser
    sees the winner's committed row — a real mutex between the scheduler thread
    and a manual run, which is what stops two processes zipping the same store at
    once. `'running'` is never a final state; a lease closes out an abandoned
    claim so a runner killed mid-run cannot wedge the job forever."""
    con = backup._open_store(backup._BLECH, cfg)
    try:
        claim = backup._claim(con, trigger="cli", store=backup._BLECH)
        assert claim is not None and claim["run_id"]
        status = con.execute(
            "SELECT status FROM backup_runs WHERE run_id = ?", [claim["run_id"]]
        ).fetchone()[0]
        assert status == "running"

        assert backup._claim(con, trigger="manual", store=backup._BLECH) is None, (
            "a second claim inside the lease must be refused"
        )
    finally:
        con.close()

    # And a full run against the held claim is contention, not failure.
    result = _run_blech(cfg, trigger="manual", force=True, poster=poster)
    assert result["status"] == "skipped" and result["reason"] == "store busy"
    assert _blech_zips(cfg) == []


def test_skipped_is_unreachable_in_the_backup_runs_table(
    seeded_store, cfg, poster, monkeypatch
):
    """A12: `'skipped'` is a *result* status, never a row status.

    By the contention rule a skip writes no row at all, so a `'skipped'` row in
    `backup_runs` means somebody wrote one — which means a cooldown could be
    armed off a transient lock. This drives one run of each kind and then asserts
    the table's vocabulary."""
    ok = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert ok["status"] == "ok"

    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    failed = _run_blech(cfg, trigger="cli", force=True,
                        poster=Recorder(error=RuntimeError("nope")))
    assert failed["status"] == "failed"
    monkeypatch.delenv(backup.WEBHOOK_ENV, raising=False)

    monkeypatch.setattr(backup, "_claim",
                        lambda con, *, trigger, store=None: None)
    skipped = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert skipped["status"] == "skipped"
    monkeypatch.undo()

    statuses = {row["status"] for row in _runs(cfg)}
    assert statuses <= {"running", "ok", "failed"}
    assert "skipped" not in statuses
    assert statuses == {"ok", "failed"}, "one row per non-contended run, and only those"


def test_delivery_is_text_carrying_a_reason_not_a_boolean(seeded_store, root, monkeypatch):
    """A13: `delivered BOOLEAN` loses the reason, so `delivery TEXT` carries it.

    "no webhook configured", "the URL was refused", "over the cap so only a
    summary went out" and "posted" all collapse to `false` in a boolean — which
    is precisely the ambiguity an operator hits when they ask why the channel is
    quiet and the table answers "not delivered" four different ways. Each reason
    must be distinguishable from the others."""
    cfg = _config(root)
    seen: dict[str, str] = {}

    # 1. no webhook at all
    poster = Recorder()
    seen["unset"] = _run_blech(cfg, force=True, poster=poster)["delivery"]

    # 2. Discord explicitly disabled for this run
    seen["disabled"] = _run_blech(cfg, force=True, discord=False,
                                  poster=poster)["delivery"]

    # 3. a configured URL that is not a Discord webhook -> refused, never fetched
    monkeypatch.setenv(backup.WEBHOOK_ENV, "https://example.invalid/hooks/whatever")
    seen["refused"] = _run_blech(cfg, force=True, poster=poster)["delivery"]
    assert not poster.calls, "a non-Discord URL must not even be contacted"

    # 4. posted for real (with an injected poster)
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    seen["posted"] = _run_blech(cfg, force=True, poster=poster)["delivery"]
    assert len(poster.calls) == 1 and poster.calls[0]["file_path"] is not None

    # 5. over the cap -> summary only
    seen["oversize"] = _run_blech(_config(root, max_upload_mb=0), force=True,
                                  poster=poster)["delivery"]

    assert len(set(seen.values())) == 5, f"reasons collapsed together: {seen}"
    assert all(isinstance(v, str) for v in seen.values())
    assert seen == {"unset": "skipped", "disabled": "disabled", "refused": "refused",
                    "posted": "posted", "oversize": "posted_summary"}

    rows = _runs(cfg)
    assert len(rows) == 5
    assert [r["status"] for r in rows] == ["ok"] * 5
    assert [r["delivery"] for r in rows] == [
        "skipped", "disabled", "refused", "posted", "posted_summary"
    ]
    # `delivered` stays a strict statement about the ZIP arriving, so exactly one
    # of these five is true.
    assert [bool(r["delivered"]) for r in rows] == [False, False, False, True, False]

    con = db.connect(cfg)
    try:
        data_type = con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'backup_runs' AND column_name = 'delivery'"
        ).fetchone()
    finally:
        con.close()
    assert data_type is not None and "CHAR" in str(data_type[0]).upper(), (
        f"delivery must be a TEXT column, got {data_type}"
    )


# ============================================================== A15: watermark

def test_blech_last_change_is_the_max_of_masks_crops_and_images(seeded_store, cfg):
    """A15, post-split: the BLECH watermark is `max(mask created/updated, crop
    completed_at, image uploaded_at)` — three sources, and no longer a fourth:
    the age rows live in their own store with their own watermark now."""
    con = db.connect(cfg)
    try:
        masks = con.execute(
            "SELECT max(greatest(created_at, coalesce(updated_at, created_at))) FROM masks"
        ).fetchone()[0]
        completed = con.execute("SELECT max(completed_at) FROM crops").fetchone()[0]
        uploaded = con.execute("SELECT max(uploaded_at) FROM images").fetchone()[0]
        assert backup._last_change(con) == max(masks, completed, uploaded)
    finally:
        con.close()


def test_a_new_image_with_no_new_masks_still_moves_the_watermark(
    seeded_store, cfg, admin_client, poster
):
    """A15: watching only `masks` would make a week of uploads read as
    "nothing new".

    That is not a small miss: the derivative JPEGs are the expensive half of the
    archive and the only pixels the polygons will ever refer to, so a store that
    gained fifty frames and no polygons is precisely the store that most needs
    backing up. This uploads a frame with no masks and no completions and proves
    the watermark moves anyway."""
    first = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert first["status"] == "ok"
    before = _watermark(cfg)
    assert before

    con = db.connect(cfg)
    try:
        masks_before = con.execute(
            "SELECT max(greatest(created_at, coalesce(updated_at, created_at))) FROM masks"
        ).fetchone()[0]
        assert backup.due(con, cfg.backup, store=backup._BLECH) == (
            False, f"nothing new since the last backup (watermark {before})"
        )
    finally:
        con.close()

    _upload(admin_client, 2)      # a frame only: no masks, no completions

    con = db.connect(cfg)
    try:
        masks_after = con.execute(
            "SELECT max(greatest(created_at, coalesce(updated_at, created_at))) FROM masks"
        ).fetchone()[0]
        assert masks_after == masks_before, "the fixture accidentally added a mask"
        assert backup._last_change(con) > datetime.fromisoformat(before), (
            "an image-only change did not register as new work — a masks-only "
            "watermark would read a week of uploads as 'nothing new' (A15)"
        )
        assert backup._last_change(con) > masks_after
    finally:
        con.close()

    second = _run_blech(cfg, trigger="cli", force=True, poster=poster)
    assert second["status"] == "ok"
    after = _watermark(cfg)
    assert datetime.fromisoformat(after) > datetime.fromisoformat(before)


# ============================================================== the snapshot

def test_snapshot_db_is_readable_and_transactionally_whole(seeded_store, cfg, tmp_path):
    """`snapshot_db` must produce a database, not a torn file.

    DuckDB keeps unflushed pages in a `.duckdb.wal` sidecar, so `shutil.copy` of
    the `.duckdb` alone while a mask POST is in flight yields a file that only
    fails at RESTORE time — the worst possible moment to find out. Letting the
    engine do the copying reads one committed MVCC snapshot instead. This opens
    the result on its own, with no sidecar in sight, and checks it agrees with
    itself: every mask resolves to a crop, every crop to an image."""
    dest = tmp_path / "snap" / "bienenblech.duckdb"
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup.snapshot_db(cfg.paths.db_path, dest)

    assert dest.is_file()
    assert not dest.with_name(dest.name + ".wal").exists(), (
        "the snapshot left an unfolded WAL sidecar; the file alone is not a store"
    )

    # Move it somewhere else entirely: a restore has this one file and nothing
    # that was next to it.
    alone = tmp_path / "restore-elsewhere" / "bienenblech.duckdb"
    alone.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dest, alone)

    source = db.connect(cfg)
    snap = duckdb.connect(str(alone))
    try:
        for table in ("images", "crops", "masks", "label_classes", "meta",
                      "class_audit", "backup_runs"):
            assert table in _tables(snap), f"the snapshot lost {table}"
            assert _count(snap, table) == _count(source, table), (
                f"{table} row count diverged between the store and the snapshot"
            )
        assert _count(snap, "masks") == 3

        orphan_masks = snap.execute(
            "SELECT count(*) FROM masks m "
            "LEFT JOIN crops c ON c.crop_id = m.crop_id WHERE c.crop_id IS NULL"
        ).fetchone()[0]
        orphan_crops = snap.execute(
            "SELECT count(*) FROM crops c "
            "LEFT JOIN images i ON i.image_id = c.image_id WHERE i.image_id IS NULL"
        ).fetchone()[0]
        assert orphan_masks == 0 and orphan_crops == 0, (
            "the snapshot disagrees with itself — a torn read across tables"
        )
        assert db.get_meta(snap, backup.META_SCHEMA_VERSION) == db.get_meta(
            source, backup.META_SCHEMA_VERSION
        )
    finally:
        snap.close()
        source.close()

    # Idempotent: a leftover destination is replaced, not appended to.
    backup.snapshot_db(cfg.paths.db_path, dest)
    again = duckdb.connect(str(dest))
    try:
        assert _count(again, "masks") == 3
    finally:
        again.close()


def test_backup_status_never_reports_the_webhook_url(seeded_store, cfg, monkeypatch):
    """`status()` reports WHETHER a webhook is configured, never the URL — it
    feeds `GET /api/backup/status`, which SPEC section 5 does not gate to admins,
    and it is also what the CLI prints on a shared terminal. Since the split it
    reports both stores, so the whole payload is searched, per-store entries
    included."""
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    out = backup.status(cfg)

    assert out["webhook_configured"] is True
    assert out["webhook_valid"] is True
    assert FAKE_WEBHOOK not in json.dumps(out, default=str)
    assert FAKE_WEBHOOK.rsplit("/", 1)[-1] not in json.dumps(out, default=str)


# ======================================================== the age store's job
# Since the split the Age tool has its OWN weekly job against its own store:
# own watermark in its own meta, own cooldown, own claim, own rotation, same
# webhook, `bienenblech-age-<stamp>-<run>.zip`. SPEC section 8's rules extend
# to it unchanged — files enumerated from the DB, never a directory walk; the
# failure taxonomy applies per store — and the A11 exclusion machinery runs on
# the age snapshot too, belt and braces, even though the age store carries no
# users table to exclude. The HTTP-level age contract is pinned in
# tests/test_age.py; the store split itself in tests/test_split_stores.py.

def test_age_zip_carries_exactly_the_age_members_and_its_snapshot_opens(
    cfg, admin_client, poster, tmp_path
):
    """The age archive, pinned exactly: the `age.duckdb` snapshot, one flat
    `age_samples.csv` (FLAGGED rows included — a flag and its reason are
    annotator judgment, and this archive is the store of record, not a training
    set), the sample photos enumerated from the table, manifest, README. The
    snapshot must actually OPEN and hold the rows — an empty or torn member
    would pass any name-list check while losing the entire backup — and the
    A11 machinery must have run on it: no users table and no scrypt bytes,
    belt and braces for a store that never carries either."""
    done_id = _upload_age(admin_client, 71)
    flagged_id = _upload_age(admin_client, 72)
    r = admin_client.post(f"/api/age/samples/{done_id}/annotate",
                          json={"age_days": 9})
    assert r.status_code == 200, r.text
    r = admin_client.post(f"/api/age/samples/{flagged_id}/flag",
                          json={"reason": "two bees"})
    assert r.status_code == 200, r.text

    result = _run_age(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result
    assert result["store"] == "age"
    zip_path = Path(result["zip_path"])
    assert AGE_ZIP_RE.match(zip_path.name), (
        f"the age archive must be named bienenblech-age-<stamp>-<run>.zip, "
        f"got {zip_path.name}"
    )

    names = _members(zip_path)
    assert names == sorted([
        "age.duckdb",
        "age_samples.csv",
        f"age/{done_id}.jpg",
        f"age/{flagged_id}.jpg",
        "manifest.json", "README.txt",
    ]), f"the age member list drifted from the contract: {names}"

    with zipfile.ZipFile(zip_path) as zf:
        rows = zf.read("age_samples.csv").decode("utf-8").splitlines()
        man = json.loads(zf.read("manifest.json").decode("utf-8"))
        for name in zf.namelist():
            assert SCRYPT_MARKER not in zf.read(name), (
                f"age zip member {name} contains a scrypt password hash"
            )

    header = rows[0].split(",")
    for col in ("sample_id", "status", "age_days", "annotated_by",
                "flag_reason", "updated_at"):
        assert col in header, f"age_samples.csv lost the {col} column"
    by_id = {r.split(",")[header.index("sample_id")]: r for r in rows[1:]}
    assert set(by_id) == {done_id, flagged_id}
    assert "9" in by_id[done_id].split(",")
    assert "two bees" in by_id[flagged_id], (
        "the flagged row (or its reason) is missing — the backup would be the "
        "one place a flag disappears"
    )

    # The exclusion machinery ran, and said so.
    assert "users" in man.get("snapshot_excluded_tables", [])

    # The snapshot member is a database, not bytes that happen to be named one.
    snapshot = _extract_snapshot(zip_path, tmp_path / "restore-age",
                                 member="age.duckdb")
    con = duckdb.connect(str(snapshot))
    try:
        tables = _tables(con)
        assert "age_samples" in tables
        assert "users" not in tables
        assert _count(con, "age_samples") == 2
        statuses = {
            row[0] for row in
            con.execute("SELECT status FROM age_samples").fetchall()
        }
        assert statuses == {"done", "flagged"}
    finally:
        con.close()
    assert SCRYPT_MARKER not in snapshot.read_bytes()


def test_a_stray_file_beside_the_age_samples_never_reaches_the_archive(
    cfg, admin_client, poster
):
    """Enumerated, never walked — the same rule as `data/images`, for the same
    reason: this archive is posted to a chat channel, and a glob that one day
    sweeps up an adjacent secret cannot be un-posted."""
    sample_id = _upload_age(admin_client, 73)
    age_dir = Path(cfg.paths.images_dir).parent / "age"
    (age_dir / "SECRET.env").write_text("BIENENBLECH_SECRET=oops", encoding="utf-8")

    result = _run_age(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    names = _members(result["zip_path"])
    assert not any("SECRET.env" in n for n in names), "stray swept into the zip"
    # The DB-known sample IS there — the absence above is selection, not a
    # broken age step.
    assert f"age/{sample_id}.jpg" in names


# ------------------------------------------------------------- independence

def test_blech_only_activity_never_fires_the_age_job(seeded_store, cfg, poster):
    """One scheduler pass over a box that did nothing but Blech work: the blech
    job fires, the age job does not — not as 'skipped after due said no' in
    some shared gate, but because the age store's OWN tables answered. No age
    zip, no age run row, no age watermark: the age store must look untouched,
    or a week of blech uploads would burn the age schedule's interval on empty
    archives."""
    result = backup.run_backup(cfg, trigger="schedule", poster=poster)
    by_store = {r["store"]: r for r in result["stores"]}

    assert by_store["blech"]["status"] == "ok", by_store
    assert by_store["age"]["status"] == "skipped", by_store
    assert "empty store" in (by_store["age"]["reason"] or "")

    assert len(_blech_zips(cfg)) == 1
    assert _age_zips(cfg) == [], "blech-only activity produced an age archive"
    assert _age_runs(cfg) == [], "blech-only activity wrote an age run row"
    assert _age_watermark(cfg) is None
    assert _watermark(cfg) is not None

    # The combined result mirrors the interesting store for legacy readers.
    assert result["status"] == "ok"


def test_age_only_activity_fires_the_age_job_only(cfg, admin_client, poster):
    """The mirror image, and deliberately through a FLAG-ONLY store — the old
    watermark gap, worth its own docstring: before the split a flag wrote no
    timestamp anywhere (`annotated_at` stays NULL by design — a flag is a
    refusal, not an answer), so a store whose only activity was flags read as
    idle and never fired a backup, silently leaving that judgment unarchived.
    `updated_at` (stamped by annotate, flag AND reopen) closes the gap, and
    the age watermark is max(uploaded_at, updated_at). Here the flag-only age
    store fires; the empty blech store stays silent, rowless and unwatermarked."""
    sid = _upload_age(admin_client, 81)
    r = admin_client.post(f"/api/age/samples/{sid}/flag", json={"reason": "blur"})
    assert r.status_code == 200, r.text

    result = backup.run_backup(cfg, trigger="schedule", poster=poster)
    by_store = {r["store"]: r for r in result["stores"]}

    assert by_store["age"]["status"] == "ok", by_store
    assert by_store["blech"]["status"] == "skipped", by_store
    assert "empty store" in (by_store["blech"]["reason"] or "")

    assert len(_age_zips(cfg)) == 1
    assert _blech_zips(cfg) == [], "age-only activity produced a blech archive"
    assert _runs(cfg) == [], "age-only activity wrote a blech run row"
    assert _watermark(cfg) is None
    assert _age_watermark(cfg) is not None

    # And the zip actually carries the flagged judgment it exists to save.
    with zipfile.ZipFile(_age_zips(cfg)[0]) as zf:
        assert "blur" in zf.read("age_samples.csv").decode("utf-8")


def test_a_flag_after_a_backup_makes_the_age_job_due_again(
    root, admin_client, poster
):
    """The flag-only WEEK, end to end: back up the age store, then do nothing
    but flag — and the next scheduled evaluation must find the age job due and
    fire it. This is the regression pin for the watermark fix: with the old
    `max(uploaded_at, annotated_at)`-shaped gate the flag moved neither leg,
    `due()` answered 'nothing new' forever, and the flag was never archived.
    `interval_days=0` so the interval gate stays out of the picture — the
    watermark comparison is the thing under test."""
    cfg = _config(root, interval_days=0)
    judged = _upload_age(admin_client, 82)
    unjudged = _upload_age(admin_client, 83)
    r = admin_client.post(f"/api/age/samples/{judged}/annotate",
                          json={"age_days": 3})
    assert r.status_code == 200, r.text

    first = _run_age(cfg, trigger="schedule", poster=poster)
    assert first["status"] == "ok", first
    before = _age_watermark(cfg)
    assert before

    con = db.connect_age(cfg)
    try:
        is_due, reason = backup.due(con, cfg.backup, store=backup._AGE)
        assert is_due is False and "nothing new" in reason
    finally:
        con.close()

    # The week's only activity: one flag. No upload, no annotation.
    r = admin_client.post(f"/api/age/samples/{unjudged}/flag",
                          json={"reason": "two bees"})
    assert r.status_code == 200, r.text

    con = db.connect_age(cfg)
    try:
        assert backup._age_last_change(con) > datetime.fromisoformat(before), (
            "a flag did not register as new age work — the flag-only-week "
            "backup gap is back"
        )
        is_due, reason = backup.due(con, cfg.backup, store=backup._AGE)
    finally:
        con.close()
    assert is_due is True, f"a flag-only week must make the age job due: {reason}"

    second = _run_age(cfg, trigger="schedule", poster=poster)
    assert second["status"] == "ok", second
    assert len(_age_zips(cfg)) == 2
    assert datetime.fromisoformat(_age_watermark(cfg)) > datetime.fromisoformat(before)


# ------------------------------------------------------ per-store isolation

def test_an_age_failure_cools_down_only_the_age_job(
    seeded_store, cfg, admin_client, poster, monkeypatch
):
    """The 6-hour failure cooldown is PER STORE: a broken age run must not
    suppress the blech schedule. Before the split one shared `backup_runs`
    table meant one shared cooldown; now each store reads only its own history,
    and a failure in one is invisible to the other's gate."""
    sid = _upload_age(admin_client, 84)
    r = admin_client.post(f"/api/age/samples/{sid}/annotate", json={"age_days": 7})
    assert r.status_code == 200, r.text

    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    failed = _run_age(cfg, trigger="cli", force=True,
                      poster=Recorder(error=RuntimeError("connection refused")))
    assert failed["status"] == "failed", failed
    monkeypatch.delenv(backup.WEBHOOK_ENV, raising=False)

    age_con = db.connect_age(cfg)
    try:
        is_due, reason = backup.due(age_con, cfg.backup, store=backup._AGE)
    finally:
        age_con.close()
    assert is_due is False and "cooling down" in reason

    con = db.connect(cfg)
    try:
        is_due, reason = backup.due(con, cfg.backup, store=backup._BLECH)
    finally:
        con.close()
    assert is_due is True, (
        f"an age failure cooled down the blech job too: {reason}"
    )

    # The failed delivery leaves its zip behind (written before the poster
    # exploded — that is the taxonomy working, not a leak); what matters here
    # is that the blech run adds a blech archive and no age one.
    age_zips_after_failure = _age_zips(cfg)
    ok = _run_blech(cfg, trigger="schedule", poster=poster)
    assert ok["status"] == "ok", (
        "the blech job did not fire while the age job was cooling down"
    )
    assert _age_zips(cfg) == age_zips_after_failure
    assert len(_blech_zips(cfg)) == 1


def test_a_blech_failure_cools_down_only_the_blech_job(
    seeded_store, cfg, admin_client, poster, monkeypatch
):
    """The same isolation, the other way round — the direction that bites in
    production, because blech is the store with years of history and the
    likelier one to be mid-write when the job fires."""
    sid = _upload_age(admin_client, 85)
    r = admin_client.post(f"/api/age/samples/{sid}/annotate", json={"age_days": 21})
    assert r.status_code == 200, r.text

    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    failed = _run_blech(cfg, trigger="cli", force=True,
                        poster=Recorder(error=RuntimeError("connection refused")))
    assert failed["status"] == "failed", failed
    monkeypatch.delenv(backup.WEBHOOK_ENV, raising=False)

    con = db.connect(cfg)
    try:
        is_due, reason = backup.due(con, cfg.backup, store=backup._BLECH)
    finally:
        con.close()
    assert is_due is False and "cooling down" in reason

    age_con = db.connect_age(cfg)
    try:
        is_due, reason = backup.due(age_con, cfg.backup, store=backup._AGE)
    finally:
        age_con.close()
    assert is_due is True, (
        f"a blech failure cooled down the age job too: {reason}"
    )

    # As above: the failed run's zip stays on disk by design. The pin is that
    # the age run adds an age archive and no blech one.
    blech_zips_after_failure = _blech_zips(cfg)
    ok = _run_age(cfg, trigger="schedule", poster=poster)
    assert ok["status"] == "ok", (
        "the age job did not fire while the blech job was cooling down"
    )
    assert _blech_zips(cfg) == blech_zips_after_failure
    assert len(_age_zips(cfg)) == 1


def test_a_held_age_claim_does_not_block_the_blech_job(
    seeded_store, cfg, admin_client, poster
):
    """Claims live in the store being claimed, so the two jobs never contend
    with each other — a long age zip must not turn a due blech run into a
    'store busy' skip. The held claim still refuses ITS OWN store's run, which
    is the mutex doing its normal job."""
    sid = _upload_age(admin_client, 86)
    r = admin_client.post(f"/api/age/samples/{sid}/annotate", json={"age_days": 2})
    assert r.status_code == 200, r.text

    age_con = backup._open_store(backup._AGE, cfg)
    try:
        claim = backup._claim(age_con, trigger="cli", store=backup._AGE)
        assert claim is not None and claim["run_id"]

        blocked = _run_age(cfg, trigger="manual", force=True, poster=poster)
        assert blocked["status"] == "skipped"
        assert blocked["reason"] == "store busy"

        ok = _run_blech(cfg, trigger="manual", force=True, poster=poster)
        assert ok["status"] == "ok", (
            "a held AGE claim blocked the BLECH job — the claims are supposed "
            "to live in their own stores"
        )
    finally:
        age_con.close()


# --------------------------------------------------------------- odd stores

def test_an_age_store_without_the_table_is_empty_not_broken(cfg, poster):
    """A hand-restored or foreign `age.duckdb` with no `age_samples` table is
    an EMPTY store, never a failed run: the scheduled evaluation answers 'not
    due' instead of raising — a raise would write a failed row, arm the age
    cooldown, and quietly stop archiving a store that was never broken."""
    Path(cfg.paths.age_db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(cfg.paths.age_db_path)
    try:
        con.execute("CREATE TABLE unrelated (x INTEGER)")
    finally:
        con.close()

    result = _run_age(cfg, trigger="schedule", poster=poster)
    assert result["status"] == "skipped", result
    assert "empty store" in (result["reason"] or "")
    assert _age_zips(cfg) == []


def test_legacy_age_rows_in_an_unmigrated_main_store_never_fire_the_blech_job(cfg):
    """The transition case: a main store from before the split that has not
    yet been through its one-time boot migration (the CLI on a box whose
    server never booted this build) still carries a legacy `age_samples`
    table. Those rows must NOT count as blech work — age activity firing the
    blech job is exactly the cross-talk the split removed — and they are not
    lost either: until the migration moves them they still travel inside the
    blech DB snapshot."""
    con = db.connect(cfg)
    try:
        db.init_db(con)
        con.execute("""
            CREATE TABLE age_samples (
                sample_id   TEXT PRIMARY KEY,
                filename    TEXT NOT NULL,
                sha256      TEXT NOT NULL UNIQUE,
                stored_path TEXT NOT NULL,
                width INTEGER NOT NULL, height INTEGER NOT NULL,
                "bytes" BIGINT NOT NULL,
                uploaded_by TEXT, uploaded_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                age_days INTEGER, annotated_by TEXT, annotated_at TIMESTAMP,
                flag_reason TEXT
            );
        """)
        con.execute(
            "INSERT INTO age_samples (sample_id, filename, sha256, stored_path, "
            'width, height, "bytes", uploaded_at) '
            "VALUES ('legacy1', 'bee.png', ?, 'data/age/legacy1.jpg', 320, 240, "
            "999, now())",
            ["f" * 64],
        )
        assert backup._last_change(con) is None, (
            "legacy age rows in the main store counted as blech work; a week "
            "of pre-migration age uploads would fire blech backups"
        )
    finally:
        con.close()

    result = _run_blech(cfg, trigger="schedule")
    assert result["status"] == "skipped", result
    assert "empty store" in (result["reason"] or "")
