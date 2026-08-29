"""Shared fixtures. Every test in this suite runs against a throwaway store.

**The one rule that matters here: no test ever touches `data/`.** The real
`data/bienenblech.duckdb` holds labeling hours, which SPEC section 4 calls the
only thing on this box that cannot be regenerated — a test run that corrupted it
would be worse than having no tests at all. So the `store` fixture builds a
`Config` whose four paths all live under pytest's `tmp_path`, asserts that they
do, and also points `$BIENENBLECH_CONFIG` at a matching YAML file so that any
code path which reaches for `load_config()` on its own still lands in the
sandbox rather than in the repo. The dev server on :8001 is a different process
with a different store and is never contacted.

Everything else is built through the real front door: sessions come from
`POST /api/login` against the admin `auth.bootstrap_admin` seeds from the
`BIENENBLECH_ADMIN_*` env vars, and the seeded image is POSTed to
`POST /api/images` so its crop grid is created exactly the way production
creates it. Fixtures that fabricate rows directly would happily keep passing
after the route that builds them broke.
"""
from __future__ import annotations

import io
import random
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from bienenblech import api, db
from bienenblech.config import BackupCfg, Config, PathsCfg

# The seeded frame. 1920x1280 at the default crop size of 640 and overlap 0
# tiles into exactly 3 columns x 2 rows of full-size tiles with no shift-back
# and no dropped edge tile, so the grid is easy to reason about in a failure
# message. `test_crops.test_default_grid_is_three_by_two` pins this against
# crops.tile rather than trusting the arithmetic here.
FRAME_W, FRAME_H = 1920, 1280
GRID_COLS, GRID_ROWS = 3, 2
N_CROPS = GRID_COLS * GRID_ROWS

ADMIN_USER = "admin_test"
ADMIN_PASSWORD = "admin-pw-not-a-secret"
POWERUSER_USER = "power_test"
POWERUSER_PASSWORD = "power-pw-not-a-secret"


def frame_bytes(width: int = FRAME_W, height: int = FRAME_H, *, seed: int = 0) -> bytes:
    """A synthetic PNG frame with some structure in it.

    Shapes rather than a flat fill so the JPEG derivative the upload writes is a
    realistic size, and `seed` so two frames differ in their sha256 — the upload
    path dedupes on the sha of the original bytes, and two "different" test
    images that hashed alike would silently become one.
    """
    im = Image.new("RGB", (width, height), (232, 226, 214))
    draw = ImageDraw.Draw(im)
    rng = random.Random(seed)
    for _ in range(60):
        x = rng.randrange(0, max(1, width - 60))
        y = rng.randrange(0, max(1, height - 60))
        fill = (rng.randrange(60, 200), rng.randrange(40, 160), rng.randrange(20, 120))
        draw.ellipse([x, y, x + 50, y + 34], fill=fill)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A Config pointed entirely at `tmp_path`, plus the env this app reads.

    `backup.enabled` is False on purpose: `create_app` starts the backup
    scheduler as a process-wide daemon thread with a module-global config, so a
    test that started it would outlive its own tmp_path and hand the next test's
    store to a thread that writes zips.
    """
    root = tmp_path / "store"
    paths = PathsCfg(
        db_path=str(root / "bienenblech.duckdb"),
        images_dir=str(root / "images"),
        cache_dir=str(root / "cache"),
        backups_dir=str(root / "backups"),
    )
    config = Config(paths=paths, backup=BackupCfg(enabled=False))

    # PathsCfg may grow fields this fixture does not know by name yet (the Age
    # tool's sample directory is the live case). A new field's default is a
    # RELATIVE path under the real `data/`, so leaving it at its default would
    # aim the very first age upload of the suite at the production store.
    # Repoint every unrecognised string field under `root` before anything can
    # read it, keeping only the default's basename.
    for name in type(config.paths).model_fields:
        if name in ("db_path", "images_dir", "cache_dir", "backups_dir"):
            continue
        default = getattr(config.paths, name)
        if isinstance(default, str):
            setattr(config.paths, name, str(root / (Path(default).name or name)))

    # The guard. Cheap, and it fires before a single byte is written. Every
    # string field of PathsCfg is checked, not just the four named ones, so the
    # repointing loop above cannot silently miss a new path.
    for name in type(config.paths).model_fields:
        raw = getattr(config.paths, name)
        if not isinstance(raw, str):
            continue
        value = Path(raw).resolve()
        assert tmp_path.resolve() in value.parents, (
            f"config.paths.{name} = {value} escapes tmp_path; tests must never "
            f"touch the real store under data/"
        )

    # A second line of defence: anything that calls load_config() itself — the
    # CLI, a route that forgot its injected config — reads this file, not the
    # committed config/ that points at data/.
    config_file = tmp_path / "bienenblech.yaml"
    config_file.write_text(yaml.safe_dump(config.model_dump()), encoding="utf-8")
    monkeypatch.setenv("BIENENBLECH_CONFIG", str(config_file))

    monkeypatch.setenv("BIENENBLECH_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("BIENENBLECH_ADMIN_PASSWORD", ADMIN_PASSWORD)
    # Fixed so a session cookie survives building a second TestClient on the
    # same app; without it every client would sign requests with its own key.
    monkeypatch.setenv("BIENENBLECH_SECRET", "test-session-secret")
    # Never let a test reach a real webhook, whatever this machine has set.
    monkeypatch.delenv("BIENENBLECH_DISCORD_WEBHOOK", raising=False)
    monkeypatch.delenv("BIENENBLECH_IN_CONTAINER", raising=False)
    return config


@pytest.fixture
def app(store: Config) -> FastAPI:
    """The real application, boot block and all, on the sandboxed store."""
    return api.create_app(store)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client with no session — the anonymous caller."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(app: FastAPI) -> Iterator[TestClient]:
    """A client logged in as the bootstrap admin."""
    with TestClient(app) as c:
        resp = c.post(
            "/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "admin"
        yield c


@pytest.fixture
def poweruser(app: FastAPI, admin: TestClient) -> Iterator[TestClient]:
    """A client logged in as a poweruser, created by the admin through the API.

    'poweruser' is the second of the two roles; it replaces the role SPEC
    section 2 called 'annotator' (an amendment recorded here because the SPEC
    is frozen). Same two-role model, admin unchanged — but powerusers may
    additionally upload frames, which is why `POST /api/images` is open to any
    signed-in user."""
    resp = admin.post(
        "/api/users",
        json={
            "username": POWERUSER_USER,
            "password": POWERUSER_PASSWORD,
            "role": "poweruser",
        },
    )
    assert resp.status_code == 200, resp.text
    with TestClient(app) as c:
        resp = c.post(
            "/api/login",
            json={"username": POWERUSER_USER, "password": POWERUSER_PASSWORD},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["role"] == "poweruser"
        yield c


@pytest.fixture
def image(admin: TestClient) -> dict:
    """One 1920x1280 frame, uploaded through `POST /api/images`.

    Through the real endpoint rather than by inserting rows, so the crop grid
    under test is the one production builds.
    """
    resp = admin.post(
        "/api/images",
        files={"file": ("frame.png", frame_bytes(), "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicates"] == []
    summary = body["images"][0]
    assert summary["n_crops"] == N_CROPS, summary
    return summary


@pytest.fixture
def crop_rows(admin: TestClient, image: dict) -> list[dict]:
    """The seeded image's crops in grid order (`row_idx`, then `col_idx`)."""
    resp = admin.get(f"/api/images/{image['image_id']}")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["crops"]
    assert len(rows) == N_CROPS
    return rows


@pytest.fixture
def mite_class(admin: TestClient) -> dict:
    """One label class to hang masks off."""
    resp = admin.post("/api/classes", json={"name": "mite"})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def query(store: Config) -> Callable[..., list[tuple]]:
    """Read the store directly, for facts the HTTP API deliberately hides.

    Soft delete is the case that needs it: `DELETE /api/masks/{id}` and every
    read route agree the mask is gone, and the whole point of SPEC section 4 is
    that the row is still there. Only a SQL read can tell those two apart.

    Opens and closes a connection per call rather than holding one, because a
    held connection would lock the file against the app's own per-request
    connections.
    """

    def run(sql: str, params: Any = ()) -> list[tuple]:
        con = db.connect(store)
        try:
            return con.execute(sql, list(params)).fetchall()
        finally:
            con.close()

    return run
