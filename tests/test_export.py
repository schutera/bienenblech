"""YOLO11-seg export tests — SPEC section 7, "the whole point".

Everything in here defends a dataset-quality invariant rather than a formatting
detail, so the failure mode of a loosened assertion is a training run that
produces a plausible-looking checkpoint from poisoned data. Each test docstring
records the reason the rule exists; read it before "fixing" a test.

Self-contained on purpose: every fixture is defined at module level, under
names (`seeded_store`, `admin_client`) that cannot collide with the differently
shaped `store` and `client` fixtures in `tests/conftest.py`, so this file never
depends on a fixture it does not define.

Nothing here may touch the real store. `cfg` roots every path under `tmp_path`
and `_paths_are_sandboxed` asserts it on every test.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from bienenblech import db, export
from bienenblech.api import create_app
from bienenblech.config import Config

# A 128x128 frame at crop.size=64 tiles into an exact 2x2 grid with no shifted
# edge tile, so every crop rect is a round (0|64, 0|64, 64, 64) and an expected
# normalized coordinate can be written down by hand in a failure message.
FRAME = 128
CROP = 64

ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-admin-password"


# --------------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every environment variable the app reads at import/boot time.

    `BIENENBLECH_CONFIG` in particular: `load_config()` consults it, and a
    developer with it set in their shell would otherwise have the suite silently
    run against a real config (and therefore a real `data/` directory)."""
    monkeypatch.delenv("BIENENBLECH_CONFIG", raising=False)
    monkeypatch.delenv("BIENENBLECH_DISCORD_WEBHOOK", raising=False)
    monkeypatch.setenv("BIENENBLECH_SECRET", "test-secret-not-a-real-key")
    monkeypatch.setenv("BIENENBLECH_ADMIN_USER", ADMIN_USER)
    monkeypatch.setenv("BIENENBLECH_ADMIN_PASSWORD", ADMIN_PASSWORD)


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """A Config whose every path is inside `tmp_path`.

    The backup scheduler is disabled here: `create_app` would otherwise spawn a
    daemon thread that outlives the test and holds a DuckDB handle open in a
    directory pytest is about to remove (which on Windows is a hard error, not a
    warning)."""
    root = tmp_path / "store"
    return Config(
        project="bienenblech-test",
        paths={
            "db_path": str(root / "bienenblech.duckdb"),
            "images_dir": str(root / "images"),
            "cache_dir": str(root / "cache"),
            "backups_dir": str(root / "backups"),
        },
        crop={"size": CROP, "overlap": 0.0, "min_edge": 16, "jpeg_quality": 80},
        backup={"enabled": False},
    )


@pytest.fixture(autouse=True)
def _paths_are_sandboxed(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """Refuse to run a test whose store is not under `tmp_path`.

    A suite that corrupted `data/bienenblech.duckdb` or rotated away real backups
    would be worse than no suite at all, so the guard is an assertion rather than
    a convention."""
    if "cfg" not in request.fixturenames:
        return
    config: Config = request.getfixturevalue("cfg")
    for path in (config.paths.db_path, config.paths.images_dir,
                 config.paths.cache_dir, config.paths.backups_dir):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve()), (
            f"test store escaped tmp_path: {path}"
        )


@pytest.fixture()
def admin_client(cfg: Config) -> Iterator[TestClient]:
    """A signed-in admin client against a fresh store."""
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        yield c


# ---------------------------------------------------------------------- helpers

def _png(seed: int, size: int = FRAME) -> bytes:
    """A deterministic synthetic frame. Distinct `seed` -> distinct sha256, which
    is what stops the second upload being swallowed by the dedupe path."""
    im = Image.new("RGB", (size, size))
    im.putdata([
        ((x * 7 + seed * 31) % 256, (y * 5 + seed * 13) % 256, (x * y + seed) % 256)
        for y in range(size) for x in range(size)
    ])
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _upload(client: TestClient, seed: int) -> str:
    body = _png(seed)
    r = client.post(
        "/api/images",
        files=[("file", (f"frame{seed}.png", io.BytesIO(body), "image/png"))],
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["images"], f"upload {seed} was deduped: {payload}"
    return payload["images"][0]["image_id"]


def _class(client: TestClient, name: str) -> dict[str, Any]:
    r = client.post("/api/classes", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _mask(client: TestClient, crop_id: str, class_id: str,
          points: list[list[float]]) -> dict[str, Any]:
    r = client.post(
        "/api/masks",
        json={"crop_id": crop_id, "class_id": class_id, "points": points},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _complete(client: TestClient, crop_id: str, *, is_empty: bool = False) -> None:
    r = client.post(f"/api/crops/{crop_id}/complete", json={"is_empty": is_empty})
    assert r.status_code == 200, r.text


@pytest.fixture()
def seeded_store(admin_client: TestClient, cfg: Config) -> dict[str, Any]:
    """A seeded store built through the real API, so the crop grid, the coordinate
    offsets and the completion guards are all exactly what production produces.

    Layout (two frames, 2x2 crops each):

        frame A  r0c0  done, 2 masks (wax + a self-intersecting mite bowtie)
                 r0c1  done, is_empty  -> must export a 0-byte label
                 r1c0  OPEN, 1 mask    -> must contribute nothing at all
                 r1c1  OPEN, no masks
        frame B  r0c0  done, 1 wax mask + one polygon inserted straight into the
                       DB with vertices outside the crop rect, to exercise the
                       export clamp (the API clamps on write, so an out-of-rect
                       polygon cannot be created through HTTP)
                 rest  OPEN

    The 'mite' class is archived last, AFTER its masks exist: an archived class
    keeps its reserved `yolo_index` forever (SPEC section 4), which is the
    property `data.yaml` and the label lines are checked against.
    """
    wax = _class(admin_client, "Wax")
    mite = _class(admin_client, "Mite")
    pollen = _class(admin_client, "Pollen")
    assert [wax["yolo_index"], mite["yolo_index"], pollen["yolo_index"]] == [0, 1, 2]

    image_a = _upload(admin_client, 1)
    image_b = _upload(admin_client, 2)

    a00 = db.crop_id_for(image_a, 0, 0)
    a01 = db.crop_id_for(image_a, 0, 1)
    a10 = db.crop_id_for(image_a, 1, 0)
    b00 = db.crop_id_for(image_b, 0, 0)

    wax_poly = [[4, 4], [40, 4], [40, 32], [4, 32]]
    # A bowtie: edges cross. SPEC section 3 says users draw these and the
    # exporter does not care, so it must survive with its vertex order intact.
    bowtie = [[8, 8], [56, 56], [56, 8], [8, 56]]
    _mask(admin_client, a00, wax["class_id"], wax_poly)
    _mask(admin_client, a00, mite["class_id"], bowtie)
    _complete(admin_client, a00)

    _complete(admin_client, a01, is_empty=True)

    _mask(admin_client, a10, pollen["class_id"], [[10, 10], [50, 10], [30, 50]])
    # deliberately NOT completed: leaving a crop open IS the skip (SPEC section 1)

    _mask(admin_client, b00, wax["class_id"], [[2, 2], [60, 2], [60, 60]])
    _complete(admin_client, b00)

    # Straight into the DB, in SOURCE-IMAGE pixels, bypassing the write clamp.
    con = db.connect(cfg)
    try:
        out_of_rect = db.create_mask(
            con,
            crop_id=b00,
            image_id=image_b,
            class_id=pollen["class_id"],
            points=[[-40.0, -40.0], [100.0, 20.0], [30.0, 120.0]],
            actor="fixture",
        )
    finally:
        con.close()

    r = admin_client.delete(f"/api/classes/{mite['class_id']}")
    assert r.status_code == 200 and r.json()["archived"] is True, r.text

    return {
        "image_a": image_a, "image_b": image_b,
        "a00": a00, "a01": a01, "a10": a10, "b00": b00,
        "wax": wax, "mite": mite, "pollen": pollen,
        "out_of_rect_mask_id": out_of_rect["mask_id"],
        "done_crops": {a00, a01, b00},
        "open_crops": {a10, db.crop_id_for(image_a, 1, 1),
                       db.crop_id_for(image_b, 0, 1),
                       db.crop_id_for(image_b, 1, 0),
                       db.crop_id_for(image_b, 1, 1)},
    }


def _build(cfg: Config, tmp_path: Path, *, name: str = "ds.zip",
           val_fraction: float = 0.2, seed: int = 0) -> tuple[Path, dict[str, Any]]:
    out = tmp_path / "out" / name
    con = db.connect(cfg)
    try:
        counts = export.build_yolo_zip(
            cfg, con, val_fraction=val_fraction, seed=seed, out_path=out
        )
    finally:
        con.close()
    return out, counts


def _members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(zf.namelist())


def _label(zip_path: Path, crop_id: str) -> bytes:
    with zipfile.ZipFile(zip_path) as zf:
        hits = [n for n in zf.namelist() if n.endswith(f"labels/{crop_id}.txt".rsplit("/", 1)[-1])
                and n.startswith("labels/") and n.endswith(f"/{crop_id}.txt")]
        assert len(hits) == 1, f"expected exactly one label for {crop_id}, got {hits}"
        return zf.read(hits[0])


def _crop_rect(cfg: Config, crop_id: str) -> dict[str, Any]:
    con = db.connect(cfg)
    try:
        row = db.get_crop(con, crop_id)
    finally:
        con.close()
    assert row is not None
    return row


# ------------------------------------------------------------- zip structure

def test_zip_layout_matches_the_spec(seeded_store, cfg, tmp_path):
    """SPEC section 7 freezes the member layout, and Ultralytics reads it
    positionally: `labels/<split>/<stem>.txt` is found by string-substituting
    `images` -> `labels` in the image path. A member named anything else is
    invisible to training, which surfaces as a model that learns nothing rather
    than as an error."""
    zip_path, counts = _build(cfg, tmp_path)
    names = _members(zip_path)

    assert "data.yaml" in names
    assert "README.txt" in names

    images = [n for n in names if n.startswith("images/")]
    labels = [n for n in names if n.startswith("labels/")]
    assert images and labels
    assert set(names) == {"data.yaml", "README.txt", *images, *labels}, (
        "the zip carries a member outside the SPEC section 7 layout"
    )
    for name in images:
        split, stem = name.split("/")[1], Path(name).stem
        assert split in ("train", "val")
        assert name.endswith(".jpg")
        assert f"labels/{split}/{stem}.txt" in labels, f"{name} has no label file"
    for name in labels:
        split, stem = name.split("/")[1], Path(name).stem
        assert split in ("train", "val")
        assert f"images/{split}/{stem}.jpg" in images, f"{name} has no image"

    assert counts["n_crops"] == len(images) == len(seeded_store["done_crops"])
    assert counts["n_images"] == 2


def test_exported_crop_images_are_the_crop_pixels(seeded_store, cfg, tmp_path):
    """The image member must be the tile, not the frame: every polygon in the
    label file is normalized against `crop.w`/`crop.h`, so an image of a
    different size silently rescales every mask."""
    zip_path, _ = _build(cfg, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        for name in [n for n in zf.namelist() if n.startswith("images/")]:
            with Image.open(io.BytesIO(zf.read(name))) as im:
                rect = _crop_rect(cfg, Path(name).stem)
                assert im.size == (rect["w"], rect["h"])


# ------------------------------------------------------------- the done gate

def test_only_done_crops_are_exported_even_when_an_open_crop_has_masks(
    seeded_store, cfg, tmp_path
):
    """SPEC section 1: leaving a crop `open` IS the skip.

    An open crop may already carry polygons — a user half way through one —
    and exporting it would ship a tile whose remaining instances are unlabeled.
    YOLO-seg reads every unlabeled instance as an explicit *background* example,
    so a half-labeled tile does not contribute less, it actively trains the model
    to suppress true positives. `seeded_store` deliberately leaves one open crop with a
    mask on it precisely so a "but it has labels, ship it" change fails here."""
    zip_path, counts = _build(cfg, tmp_path)
    names = _members(zip_path)
    stems = {Path(n).stem for n in names if n.startswith(("images/", "labels/"))}

    assert stems == seeded_store["done_crops"]
    for crop_id in seeded_store["open_crops"]:
        assert crop_id not in stems, f"open crop {crop_id} reached the export"

    # And the masks on the open crop are not counted anywhere either.
    con = db.connect(cfg)
    try:
        assert len(db.list_masks(con, crop_id=seeded_store["a10"])) == 1
    finally:
        con.close()
    assert counts["n_masks"] == 4  # 2 on a00, 2 on b00; none from the open crop


def test_empty_crop_exports_a_zero_byte_label_file(seeded_store, cfg, tmp_path):
    """An `is_empty` crop is a HARD NEGATIVE: present, with an empty label file.

    Present, not absent — dropping it throws away the most valuable thing a
    reviewed-and-genuinely-empty tile can contribute, which is the explicit
    statement that this patch of sheet contains nothing. Empty, not missing — a
    training image with no `.txt` at all is treated by Ultralytics as unlabeled
    rather than as a negative, and older versions warn and skip it. This is a
    dataset-quality invariant, not a formatting detail: a change that "tidies up"
    zero-byte files silently removes every negative sample from the dataset."""
    zip_path, _ = _build(cfg, tmp_path)
    empty = seeded_store["a01"]
    names = _members(zip_path)

    label = [n for n in names if n.startswith("labels/") and n.endswith(f"/{empty}.txt")]
    image = [n for n in names if n.startswith("images/") and n.endswith(f"/{empty}.jpg")]
    assert len(label) == 1 and len(image) == 1, "the empty crop must still be exported"

    with zipfile.ZipFile(zip_path) as zf:
        info = zf.getinfo(label[0])
        assert info.file_size == 0, f"{label[0]} is {info.file_size} B, expected 0"
        assert zf.read(label[0]) == b""


# ------------------------------------------------------------- coordinates

def test_every_coordinate_is_normalized_into_the_unit_interval(seeded_store, cfg, tmp_path):
    """Ultralytics reads label coordinates as fractions of the tile. A value
    outside [0,1] is not rejected — it is consumed, and produces a mask that
    extends past the image, a negative area and a NaN segmentation loss partway
    into a training run. `seeded_store` includes a polygon inserted with vertices well
    outside the crop rect so the clamp is genuinely exercised."""
    zip_path, _ = _build(cfg, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        checked = 0
        for name in [n for n in zf.namelist() if n.startswith("labels/")]:
            for line in zf.read(name).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split(" ")
                assert len(parts) >= 1 + 2 * 3, f"{name}: fewer than 3 vertices: {line}"
                assert (len(parts) - 1) % 2 == 0, f"{name}: odd coordinate count: {line}"
                for token in parts[1:]:
                    value = float(token)
                    assert 0.0 <= value <= 1.0, f"{name}: {value} escapes [0,1]"
                    checked += 1
        assert checked > 0, "no coordinates were checked — the fixture stopped seeding masks"


def test_normalization_is_crop_relative_and_clamped(seeded_store, cfg, tmp_path):
    """`(px - crop.x) / crop.w`, clamped, at 6 decimals (SPEC section 7).

    Checked against the DB rather than against literals so it also proves the
    SOURCE-pixel storage of SPEC section 3 is being converted rather than
    written through: a crop at x=64 whose polygon is emitted un-offset lands
    entirely at the right edge of the tile and every mask in the dataset is
    wrong by exactly one tile."""
    zip_path, _ = _build(cfg, tmp_path)
    con = db.connect(cfg)
    try:
        classes = {c["class_id"]: c["yolo_index"]
                   for c in db.list_classes(con, include_archived=True)}
        for crop_id in sorted(seeded_store["done_crops"]):
            crop = db.get_crop(con, crop_id)
            assert crop is not None
            expected = []
            for mask in db.list_masks(con, crop_id=crop_id):
                coords = []
                for px, py in mask["points"]:
                    coords.append(min(1.0, max(0.0, (px - crop["x"]) / crop["w"])))
                    coords.append(min(1.0, max(0.0, (py - crop["y"]) / crop["h"])))
                expected.append(
                    f"{classes[mask['class_id']]} " + " ".join("%.6f" % v for v in coords)
                )
            body = _label(zip_path, crop_id).decode("utf-8")
            actual = [l for l in body.splitlines() if l.strip()]
            assert actual == expected, f"{crop_id}: label lines diverged"
    finally:
        con.close()

    # The clamped polygon specifically: -40 and 120 px on a 64 px tile.
    b00 = _label(zip_path, seeded_store["b00"]).decode("utf-8").splitlines()
    clamped = [l for l in b00 if l.startswith(f"{seeded_store['pollen']['yolo_index']} ")]
    assert len(clamped) == 1
    values = [float(v) for v in clamped[0].split(" ")[1:]]
    assert values == pytest.approx(
        [0.0, 0.0, 1.0, 20 / 64, 30 / 64, 1.0], abs=5e-7
    )


def test_self_intersecting_polygon_survives_export(seeded_store, cfg, tmp_path):
    """SPEC section 3 accepts self-intersecting polygons: users draw them,
    and the exporter explicitly does not care.

    Vertex ORDER is part of the polygon, so this also pins that the exporter does
    not quietly reorder or convex-hull the ring — a "repair" that would change the
    shape the user actually traced, without ever failing loudly."""
    zip_path, _ = _build(cfg, tmp_path)
    lines = _label(zip_path, seeded_store["a00"]).decode("utf-8").splitlines()
    mite_index = seeded_store["mite"]["yolo_index"]
    bowtie = [l for l in lines if l.startswith(f"{mite_index} ")]
    assert len(bowtie) == 1, "the self-intersecting polygon was dropped"
    values = [float(v) for v in bowtie[0].split(" ")[1:]]
    assert values == pytest.approx(
        [8 / 64, 8 / 64, 56 / 64, 56 / 64, 56 / 64, 8 / 64, 8 / 64, 56 / 64], abs=5e-7
    )


# ------------------------------------------------------------- class indices

def test_class_index_written_is_the_yolo_index(seeded_store, cfg, tmp_path):
    """The integer at the head of a label line is `label_classes.yolo_index`, not
    a position in some filtered list. The index IS the class's identity in every
    file that was ever exported."""
    zip_path, _ = _build(cfg, tmp_path)
    lines = _label(zip_path, seeded_store["a00"]).decode("utf-8").splitlines()
    assert sorted(int(l.split(" ", 1)[0]) for l in lines if l.strip()) == [
        seeded_store["wax"]["yolo_index"], seeded_store["mite"]["yolo_index"]
    ]
    assert seeded_store["wax"]["yolo_index"] == 0 and seeded_store["mite"]["yolo_index"] == 1


def test_archived_class_keeps_its_index_in_the_export(seeded_store, cfg, tmp_path):
    """An archived class keeps its `yolo_index` and its name in `data.yaml`
    (SPEC sections 4 and 7).

    'Mite' is archived in the fixture and still owns index 1. If archiving
    renumbered — or dropped the name from `names:` and let 'Pollen' slide from 2
    to 1 — then every checkpoint trained against an older export would keep
    predicting index 1 and every one of those predictions would silently become
    'Pollen'. There is no error anywhere in that chain; the model simply reports
    the wrong class forever."""
    zip_path, _ = _build(cfg, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        doc = yaml.safe_load(zf.read("data.yaml").decode("utf-8"))

    names = doc["names"]
    assert names[seeded_store["mite"]["yolo_index"]] == "Mite"
    assert names[seeded_store["wax"]["yolo_index"]] == "Wax"
    assert names[seeded_store["pollen"]["yolo_index"]] == "Pollen"
    assert doc["nc"] == len(names) == 3
    assert sorted(names) == [0, 1, 2], "an archived class must not open a gap"

    # And the masks drawn under the archived class still export under index 1.
    lines = _label(zip_path, seeded_store["a00"]).decode("utf-8").splitlines()
    assert any(l.startswith("1 ") for l in lines)


def test_data_yaml_has_no_path_key(seeded_store, cfg, tmp_path):
    """Amendment A14: `data.yaml` must NOT carry a `path:` key.

    This looks like an omission and it is not. Ultralytics resolves a RELATIVE
    `path` against its own `settings['datasets_dir']` (`~/datasets`), NOT against
    the directory the yaml lives in — so `path: .` does not mean "this folder",
    it means `~/datasets`, and training either dies on a missing directory or,
    much worse, silently trains against a different dataset that happens to be
    sitting there. With `path` omitted, Ultralytics defaults it to the yaml's own
    parent, which is what everyone reading `path: .` believed it already said.

    Do not "fix" this test by allowing `path: .`."""
    zip_path, _ = _build(cfg, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read("data.yaml").decode("utf-8")
    doc = yaml.safe_load(raw)

    assert "path" not in doc, (
        "data.yaml carries a 'path' key; Ultralytics resolves a relative path "
        "against settings['datasets_dir'], not against the yaml's directory (A14)"
    )
    assert doc["train"] == "images/train"
    assert doc["val"] == "images/val"


# ------------------------------------------------------------- the split

def test_split_is_deterministic_for_a_given_seed(seeded_store, cfg, tmp_path):
    """The same store and the same seed must reproduce the same dataset.

    `split_for_image` hashes with sha256 rather than `random` or `hash()` so it
    is stable across processes, Python versions and PYTHONHASHSEED. Without that,
    re-exporting to add ten new frames reshuffles every old frame across the
    split, and every metric ever measured against the previous export becomes
    incomparable — including the one that justified shipping a model."""
    first, _ = _build(cfg, tmp_path, name="a.zip", seed=7, val_fraction=0.5)
    second, _ = _build(cfg, tmp_path, name="b.zip", seed=7, val_fraction=0.5)
    assert _members(first) == _members(second)

    # A different seed is allowed to (and here does) move something.
    ids = [f"image{i:04d}" for i in range(400)]
    a = [export.split_for_image(i, seed=7, val_fraction=0.5) for i in ids]
    b = [export.split_for_image(i, seed=8, val_fraction=0.5) for i in ids]
    assert a != b, "the seed does not affect the split at all"


def test_every_exported_crop_lands_in_exactly_one_side(seeded_store, cfg, tmp_path):
    """Train and val must partition the dataset: no crop in both (which trains on
    the val set and makes the metric meaningless), none in neither (which drops
    labeled work on the floor without saying so)."""
    zip_path, counts = _build(cfg, tmp_path, val_fraction=0.5, seed=3)
    names = _members(zip_path)
    train = {Path(n).stem for n in names if n.startswith("images/train/")}
    val = {Path(n).stem for n in names if n.startswith("images/val/")}

    assert not (train & val)
    assert train | val == seeded_store["done_crops"]
    assert counts["n_train"] == len(train)
    assert counts["n_val"] == len(val)
    assert counts["n_train"] + counts["n_val"] == counts["n_crops"]


def test_crops_of_one_frame_never_straddle_the_split(seeded_store, cfg, tmp_path):
    """SPEC section 7: the split is grouped by `image_id`, never by crop.

    Two tiles of one 4000x3000 frame share lighting, sheet furniture and often
    overlapping pixels at the seam. One in train and one in val is textbook
    leakage: the val loss drops, the metric stops measuring generalisation, and
    the model looks ready when it is not."""
    for seed in range(6):
        zip_path, _ = _build(cfg, tmp_path, name=f"s{seed}.zip",
                             val_fraction=0.5, seed=seed)
        sides: dict[str, set[str]] = {}
        for name in [n for n in _members(zip_path) if n.startswith("images/")]:
            split = name.split("/")[1]
            image_id = Path(name).stem.rsplit("_r", 1)[0]
            sides.setdefault(image_id, set()).add(split)
        assert sides, "nothing was exported"
        for image_id, split_set in sides.items():
            assert len(split_set) == 1, f"seed {seed}: {image_id} straddles {split_set}"


def test_val_fraction_is_honoured(seeded_store, cfg, tmp_path):
    """`val_fraction` is the expected share of IMAGES, and the two degenerate
    ends must be exact: 0.0 puts nothing in val (a caller asking for a
    train-only dump must not get a stray val image), 1.0 puts everything there."""
    ids = [f"frame-{i}" for i in range(4000)]
    for fraction in (0.1, 0.25, 0.5, 0.8):
        observed = sum(
            export.split_for_image(i, seed=0, val_fraction=fraction) == "val"
            for i in ids
        ) / len(ids)
        assert observed == pytest.approx(fraction, abs=0.03), (
            f"val_fraction={fraction} produced {observed}"
        )
    assert all(export.split_for_image(i, seed=0, val_fraction=0.0) == "train" for i in ids)
    assert all(export.split_for_image(i, seed=0, val_fraction=1.0) == "val" for i in ids)

    # And end to end, through the zip.
    zip_path, counts = _build(cfg, tmp_path, name="allt.zip", val_fraction=0.0)
    assert counts["n_val"] == 0
    assert not [n for n in _members(zip_path) if "/val/" in n]

    zip_path, counts = _build(cfg, tmp_path, name="allv.zip", val_fraction=1.0)
    assert counts["n_train"] == 0
    assert not [n for n in _members(zip_path) if "/train/" in n]


# ------------------------------------------------------------- refusals + HTTP

def test_export_refuses_a_store_with_no_done_crop(admin_client, cfg, tmp_path):
    """An empty dataset is a request that cannot be honoured yet, not an empty
    zip. Ultralytics happily accepts a dataset with zero images and burns a full
    training run producing a checkpoint that predicts nothing."""
    wax = _class(admin_client, "Wax")
    image_id = _upload(admin_client, 9)
    _mask(admin_client, db.crop_id_for(image_id, 0, 0), wax["class_id"],
          [[1, 1], [20, 1], [20, 20]])  # drawn, but the crop is left open

    with pytest.raises(export.EmptyExport):
        _build(cfg, tmp_path)
    assert not (tmp_path / "out" / "ds.zip").exists()
    assert not (tmp_path / "out" / "ds.zip.part").exists()


def test_http_export_route_streams_the_same_zip(seeded_store, admin_client):
    """The route is the surface an admin actually uses; it must hand back a real
    zip (not a JSON error body with a 200) and it is admin-gated."""
    r = admin_client.get("/api/export/yolo", params={"val_fraction": 0.5, "seed": 1})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        assert "data.yaml" in names and "README.txt" in names
        assert zf.testzip() is None
        # data.yaml's contents (including the absence of `path`, A14) are pinned
        # by test_data_yaml_has_no_path_key; asserted once, not twice.
        assert any(n.startswith("images/") for n in names)
        assert any(n.startswith("labels/") for n in names)


def test_readme_states_the_completeness_invariant(seeded_store, cfg, tmp_path):
    """SPEC section 7 puts the invariant in README.txt because the person who
    unzips this months from now is the person most likely to "helpfully" delete
    the zero-byte labels or re-split the dataset by crop."""
    zip_path, _ = _build(cfg, tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        readme = zf.read("README.txt").decode("utf-8").lower()
    assert "done" in readme
    assert "background" in readme
    assert "zero-byte" in readme or "empty" in readme
    assert "image_id" in readme
