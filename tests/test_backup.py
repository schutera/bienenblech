"""Backup tests — SPEC section 8 and amendments A11, A12, A13, A15.

This archive is *posted to a chat channel*. That single fact is what most of
these tests defend: what goes into the zip, what must never go into the zip, and
what must happen when it cannot be posted. The rest defend the failure taxonomy,
whose whole purpose is that a transient lock can never turn into a permanently
disabled backup.

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
2. **No test may touch the real store.** Every path is under `tmp_path` and
   `_paths_are_sandboxed` asserts it. A run that rotated away real backups would
   be worse than no tests at all.
"""
from __future__ import annotations

import io
import json
import os
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
    """Refuse to run a test whose store or backups directory escapes `tmp_path`."""
    if "cfg" not in request.fixturenames:
        return
    config: Config = request.getfixturevalue("cfg")
    for path in (config.paths.db_path, config.paths.images_dir,
                 config.paths.cache_dir, config.paths.backups_dir):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve()), (
            f"test store escaped tmp_path: {path}"
        )


@pytest.fixture()
def admin_client(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A signed-in admin client against a fresh store.

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

    `run_backup(poster=...)` exists precisely so the delivery ladder is testable
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


@pytest.fixture()
def seeded_store(admin_client: TestClient) -> dict[str, Any]:
    """A seeded store built through the real API: one frame, a 2x2 crop grid, two
    classes, polygons on two crops, one completed crop and one `is_empty` crop.

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


def _extract_snapshot(zip_path: str | Path, dest_dir: Path) -> Path:
    """Pull `bienenblech.duckdb` out of the archive onto its own, and only its
    own, path — a restore has nothing but this member and no WAL sidecar."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "bienenblech.duckdb"
    with zipfile.ZipFile(zip_path) as zf:
        out.write_bytes(zf.read("bienenblech.duckdb"))
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


def _runs(cfg: Config) -> list[dict[str, Any]]:
    """Every `backup_runs` row, oldest first.

    `ensure_backup_tables` is called first because `db.init_db` declares
    `backup_runs` without the additive `delivery` column (A13) — a store whose
    backup module never opened it therefore has no such column, which is exactly
    the situation in the contention tests, where `_open_store` is stubbed out."""
    con = db.connect(cfg)
    try:
        backup.ensure_backup_tables(con)
        cols = ("run_id", "status", "trigger", "delivered", "delivery", "error",
                "zip_path", "started_at", "finished_at")
        rows = con.execute(
            'SELECT run_id, status, "trigger", delivered, delivery, error, zip_path, '
            "started_at, finished_at FROM backup_runs ORDER BY started_at"
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        con.close()


def _watermark(cfg: Config) -> str | None:
    con = db.connect(cfg)
    try:
        return db.get_meta(con, backup.META_WATERMARK)
    finally:
        con.close()


def _zips(cfg: Config) -> list[Path]:
    return sorted(Path(cfg.paths.backups_dir).glob("bienenblech-*.zip"))


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
    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
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
    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    with zipfile.ZipFile(result["zip_path"]) as zf:
        names = zf.namelist()
        assert "users.csv" not in names
        csvs = [n for n in names if n.endswith(".csv")]
        assert sorted(csvs) == ["classes.csv", "crops.csv", "images.csv", "masks.csv"]
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

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
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

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=exploding)

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
    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "ok", result
    assert result["delivery"] == "posted_summary"

    zip_path = Path(result["zip_path"])
    assert zip_path.is_file(), "the oversize archive was not written"
    assert zip_path.stat().st_size == result["zip_bytes"] > 0
    assert zip_path in _zips(cfg), "the oversize archive was not retained locally"
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
    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "ok", result
    assert result["delivery"] == "skipped"
    assert not poster.calls, "nothing may be posted with no webhook configured"
    assert Path(result["zip_path"]).is_file()
    assert _zips(cfg) == [Path(result["zip_path"])]

    watermark = _watermark(cfg)
    assert watermark, "the watermark must advance even with no webhook"

    con = db.connect(cfg)
    try:
        assert datetime.fromisoformat(watermark) == backup._last_change(con)
        is_due, reason = backup.due(con, cfg.backup)
    finally:
        con.close()
    assert is_due is False and "nothing new" in reason


def test_discord_disabled_still_produces_a_local_archive(seeded_store, cfg, poster):
    """`discord=False` is the same supported shape reached a different way."""
    result = backup.run_backup(cfg, trigger="cli", force=True, discord=False,
                               poster=poster)
    assert result["status"] == "ok" and result["delivery"] == "disabled"
    assert not poster.calls
    assert Path(result["zip_path"]).is_file()


# ============================================================== rotation

def test_local_rotation_keeps_exactly_backup_keep_archives(seeded_store, root, poster):
    """Rotation keeps `backup.keep` zips under `backups_dir` — no more (the disk
    is small and the images are the expensive half) and no fewer.

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

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
    assert result["status"] == "ok", result

    remaining = _zips(cfg)
    assert len(remaining) == keep, [p.name for p in remaining]
    assert Path(result["zip_path"]) in remaining, "rotation removed the new archive"
    # The survivors are the newest by name: the last (keep - 1) placeholders.
    assert remaining[:-1] == older[-(keep - 1):]


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
        lambda config: (_ for _ in ()).throw(backup._StoreBusy("locked by the app")),
    )

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "skipped"
    assert result["reason"] == "store busy"
    assert result["error"] is None, "contention is not an error"
    assert result["status"] != "failed", "the CLI exits non-zero only on 'failed'"
    assert not poster.calls
    assert _zips(cfg) == [], "a skipped run must not write an archive"

    monkeypatch.undo()
    assert _runs(cfg) == [], "contention must write no backup_runs row at all"
    con = db.connect(cfg)
    try:
        is_due, _ = backup.due(con, cfg.backup)
    finally:
        con.close()
    assert is_due is True, "contention must not arm the cooldown"


def test_a_refused_claim_writes_no_row_and_arms_no_cooldown(
    seeded_store, cfg, poster, monkeypatch
):
    """The other half of the contention class: the claim mutex refused us because
    another runner (the scheduler thread, a second container on one bind mount)
    holds it. Same rule — no row, no cooldown, exit 0."""
    monkeypatch.setattr(backup, "_claim", lambda con, *, trigger: None)

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)

    assert result["status"] == "skipped"
    assert result["reason"] == "store busy"
    assert result["run_id"] is None
    assert _zips(cfg) == []

    monkeypatch.undo()
    assert _runs(cfg) == []
    con = db.connect(cfg)
    try:
        assert backup.due(con, cfg.backup)[0] is True
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

    result = backup.run_backup(cfg, trigger="cli", force=True, poster=exploding)

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
        is_due, reason = backup.due(con, cfg.backup)
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
    con = backup._open_store(cfg)
    try:
        claim = backup._claim(con, trigger="cli")
        assert claim is not None and claim["run_id"]
        status = con.execute(
            "SELECT status FROM backup_runs WHERE run_id = ?", [claim["run_id"]]
        ).fetchone()[0]
        assert status == "running"

        assert backup._claim(con, trigger="manual") is None, (
            "a second claim inside the lease must be refused"
        )
    finally:
        con.close()

    # And a full run against the held claim is contention, not failure.
    result = backup.run_backup(cfg, trigger="manual", force=True, poster=poster)
    assert result["status"] == "skipped" and result["reason"] == "store busy"
    assert _zips(cfg) == []


def test_skipped_is_unreachable_in_the_backup_runs_table(
    seeded_store, cfg, poster, monkeypatch
):
    """A12: `'skipped'` is a *result* status, never a row status.

    By the contention rule a skip writes no row at all, so a `'skipped'` row in
    `backup_runs` means somebody wrote one — which means a cooldown could be
    armed off a transient lock. This drives one run of each kind and then asserts
    the table's vocabulary."""
    ok = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
    assert ok["status"] == "ok"

    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    failed = backup.run_backup(cfg, trigger="cli", force=True,
                               poster=Recorder(error=RuntimeError("nope")))
    assert failed["status"] == "failed"
    monkeypatch.delenv(backup.WEBHOOK_ENV, raising=False)

    monkeypatch.setattr(backup, "_claim", lambda con, *, trigger: None)
    skipped = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
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
    seen["unset"] = backup.run_backup(cfg, force=True, poster=poster)["delivery"]

    # 2. Discord explicitly disabled for this run
    seen["disabled"] = backup.run_backup(cfg, force=True, discord=False,
                                         poster=poster)["delivery"]

    # 3. a configured URL that is not a Discord webhook -> refused, never fetched
    monkeypatch.setenv(backup.WEBHOOK_ENV, "https://example.invalid/hooks/whatever")
    seen["refused"] = backup.run_backup(cfg, force=True, poster=poster)["delivery"]
    assert not poster.calls, "a non-Discord URL must not even be contacted"

    # 4. posted for real (with an injected poster)
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    seen["posted"] = backup.run_backup(cfg, force=True, poster=poster)["delivery"]
    assert len(poster.calls) == 1 and poster.calls[0]["file_path"] is not None

    # 5. over the cap -> summary only
    seen["oversize"] = backup.run_backup(_config(root, max_upload_mb=0), force=True,
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

def test_last_change_is_the_max_of_masks_crops_and_images(seeded_store, cfg):
    """A15: the watermark is `max(mask created/updated, crop completed_at,
    image uploaded_at)` — three sources, not one."""
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
    first = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
    assert first["status"] == "ok"
    before = _watermark(cfg)
    assert before

    con = db.connect(cfg)
    try:
        masks_before = con.execute(
            "SELECT max(greatest(created_at, coalesce(updated_at, created_at))) FROM masks"
        ).fetchone()[0]
        assert backup.due(con, cfg.backup) == (
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

    second = backup.run_backup(cfg, trigger="cli", force=True, poster=poster)
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
    and it is also what the CLI prints on a shared terminal."""
    monkeypatch.setenv(backup.WEBHOOK_ENV, FAKE_WEBHOOK)
    out = backup.status(cfg)

    assert out["webhook_configured"] is True
    assert out["webhook_valid"] is True
    assert FAKE_WEBHOOK not in json.dumps(out, default=str)
    assert FAKE_WEBHOOK.rsplit("/", 1)[-1] not in json.dumps(out, default=str)
