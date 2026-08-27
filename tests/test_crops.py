"""Tiling, coordinates, the labeling queue, and the completeness invariant.

SPEC section 1 is the heart of this tool: *a crop is `done` only when every
instance of every known class inside it has a polygon, and only `done` crops are
exported.* An unlabeled instance in a `done` crop is not missing data — it is an
explicit background teaching signal that trains the model to suppress true
positives. Amendment A1 makes `POST /api/crops/{id}/complete` the one place that
invariant can be enforced, because `db.set_crop_status` deliberately stores what
it is told. The guards in `test_completing_*` are that enforcement. Loosening any
of them puts the exact poison the crop design exists to prevent back into the
export, silently.

SPEC section 3 is the other half: the DB stores SOURCE-image pixels, the HTTP
API transmits CROP-LOCAL pixels, and `crops.to_crop_local` / `crops.to_source`
are the only two functions allowed to know that. A polygon that reloads shifted
by one crop origin is the single most likely bug in this codebase, so the
round-trip is tested against the helpers directly and again through the API.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bienenblech import crops
from conftest import FRAME_H, FRAME_W, GRID_COLS, GRID_ROWS, N_CROPS

CROP_SIZE = 640
TRIANGLE = [[10, 10], [60, 10], [60, 60]]


def _axis_starts(rects: list[dict], axis: str) -> list[int]:
    return sorted({r[axis] for r in rects})


def _assert_axis_covers(starts: list[int], extent: int, size: int, axis: str) -> None:
    """Every pixel of `0..extent` is inside at least one tile, and no tile hangs
    off the end. `crops.tile` promises total coverage; a gap would be a strip of
    the frame no user is ever shown, which is unlabeled data that still
    looks complete on the progress bar."""
    assert starts[0] == 0, f"{axis} tiling does not start at 0: {starts}"
    assert starts[-1] + size == extent or extent <= size, (
        f"{axis} tiling ends at {starts[-1] + size}, not {extent}"
    )
    for prev, nxt in zip(starts, starts[1:]):
        assert nxt <= prev + size, f"gap in {axis} tiling between {prev} and {nxt}"
    assert all(0 <= s and s + size <= extent for s in starts), (
        f"{axis} tile escapes the frame: {starts}"
    )


# --------------------------------------------------------------- tiling geometry
def test_default_grid_is_three_by_two():
    """1920x1280 at the shipped crop config. Every fixture in this suite leans
    on this grid, so it is pinned against `crops.tile` rather than assumed."""
    rects = crops.tile(FRAME_W, FRAME_H, size=CROP_SIZE, overlap=0.0, min_edge=160)
    assert len(rects) == N_CROPS == 6
    assert _axis_starts(rects, "x") == [0, 640, 1280]
    assert _axis_starts(rects, "y") == [0, 640]
    assert {(r["row_idx"], r["col_idx"]) for r in rects} == {
        (r, c) for r in range(GRID_ROWS) for c in range(GRID_COLS)
    }


def test_tiles_are_emitted_in_row_major_order():
    """Reading order. A17 pins `CropTask.index` to this same order, and
    `db.list_crops` re-sorts by (row_idx, col_idx) to match."""
    rects = crops.tile(FRAME_W, FRAME_H, size=CROP_SIZE, overlap=0.0, min_edge=160)
    assert [(r["row_idx"], r["col_idx"]) for r in rects] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)
    ]


def test_every_tile_is_exactly_the_crop_size():
    """An edge tile is shifted back into the image, never shrunk: a shrunk tile
    would be letterboxed by the trainer, changing the effective object scale for
    exactly the tiles at the frame border, and would resize the user's
    canvas between crops for no visible reason."""
    rects = crops.tile(1000, 700, size=CROP_SIZE, overlap=0.0, min_edge=160)
    assert all(r["w"] == CROP_SIZE and r["h"] == CROP_SIZE for r in rects)
    _assert_axis_covers(_axis_starts(rects, "x"), 1000, CROP_SIZE, "x")
    _assert_axis_covers(_axis_starts(rects, "y"), 700, CROP_SIZE, "y")


@pytest.mark.parametrize("overlap", [0.0, 0.25, 0.5])
def test_tiling_covers_the_frame_at_any_overlap(overlap: float):
    rects = crops.tile(FRAME_W, FRAME_H, size=CROP_SIZE, overlap=overlap, min_edge=160)
    _assert_axis_covers(_axis_starts(rects, "x"), FRAME_W, CROP_SIZE, "x")
    _assert_axis_covers(_axis_starts(rects, "y"), FRAME_H, CROP_SIZE, "y")


def test_overlap_sets_the_stride():
    """`overlap` is a fraction of `size`, so 0.25 means a 480 px stride and a
    160 px shared band between neighbours."""
    rects = crops.tile(FRAME_W, FRAME_H, size=CROP_SIZE, overlap=0.25, min_edge=160)
    xs = _axis_starts(rects, "x")
    assert xs[1] - xs[0] == 480
    assert xs[0] + CROP_SIZE - xs[1] == 160


def test_a_frame_smaller_than_one_tile_is_a_single_crop():
    """Nothing to shift back into, so the tile is the frame."""
    rects = crops.tile(300, 200, size=CROP_SIZE, overlap=0.0, min_edge=160)
    assert rects == [{"row_idx": 0, "col_idx": 0, "x": 0, "y": 0, "w": 300, "h": 200}]


def test_tiling_rejects_a_nonsense_overlap():
    """An overlap of 1.0 would make the stride 0 and hang the tiling loop."""
    with pytest.raises(ValueError):
        crops.tile(FRAME_W, FRAME_H, size=CROP_SIZE, overlap=1.0, min_edge=160)


# ------------------------------------------------------------ coordinate helpers
def test_source_to_crop_local_and_back_is_lossless():
    """SPEC section 3. `crop.x/y` are integers, so the subtract-then-add is exact
    in float64 and a reloaded polygon compares equal to the one that was sent."""
    crop = {"x": 1280, "y": 640, "w": 640, "h": 640}
    source = [[1290.5, 660.25], [1900.0, 700.75], [1500.125, 1279.5]]
    local = crops.to_crop_local(source, crop)
    assert local == [[10.5, 20.25], [620.0, 60.75], [220.125, 639.5]]
    assert crops.to_source(local, crop) == source


def test_crop_local_to_source_and_back_is_lossless():
    """The direction the API actually runs: in from the wire, out to the wire."""
    crop = {"x": 1280, "y": 640, "w": 640, "h": 640}
    local = [[0.0, 0.0], [10.5, 20.25], [639.75, 1.5], [320.0, 639.0]]
    assert crops.to_crop_local(crops.to_source(local, crop), crop) == local


def test_writing_clamps_a_vertex_into_the_crop_rect():
    """An instance clipped by a tile edge is correct and expected for YOLO-seg;
    a vertex the user dragged past the canvas must not be stored as if it
    described pixels of the neighbouring tile."""
    crop = {"x": 1280, "y": 640, "w": 640, "h": 640}
    stored = crops.to_source([[-50.0, -10.0], [700.0, 20.0], [300.0, 999.0]], crop)
    assert stored == [[1280.0, 640.0], [1920.0, 660.0], [1580.0, 1280.0]]


def test_clamping_is_idempotent():
    """Saving a polygon, reloading it and saving it again is a no-op — otherwise
    a clipped mask would creep across the frame one edit at a time."""
    crop = {"x": 1280, "y": 640, "w": 640, "h": 640}
    once = crops.to_source([[-50.0, -10.0], [700.0, 20.0], [300.0, 999.0]], crop)
    twice = crops.to_source(crops.to_crop_local(once, crop), crop)
    assert twice == once


# ---------------------------------------------------------- the grid via the API
def test_upload_creates_the_whole_grid(image: dict, crop_rows: list[dict]):
    """An image row and its crop rows are created together or not at all: a
    frame with no crops is invisible to the queue and looks, to the user,
    exactly like a frame nobody has started."""
    assert image["width"] == FRAME_W and image["height"] == FRAME_H
    assert image["crop_size"] == CROP_SIZE
    assert len(crop_rows) == N_CROPS
    assert all(r["status"] == "open" and r["is_empty"] is False for r in crop_rows)
    assert [(r["row_idx"], r["col_idx"]) for r in crop_rows] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)
    ]


def test_crop_summaries_carry_no_filesystem_path(crop_rows: list[dict]):
    """A3: db row dicts carry server-side extras. A path in a JSON response is
    free reconnaissance, and `image_id` is not part of a CropSummary."""
    for row in crop_rows:
        assert "stored_path" not in row
        assert "sha256" not in row
        assert "image_id" not in row


def test_crop_image_renders_and_caches(admin: TestClient, crop_rows: list[dict]):
    resp = admin.get(f"/api/crops/{crop_rows[4]['crop_id']}/image")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:2] == b"\xff\xd8"      # JPEG SOI
    etag = resp.headers["etag"]

    again = admin.get(
        f"/api/crops/{crop_rows[4]['crop_id']}/image", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304


def test_unknown_crop_is_404_not_500(admin: TestClient):
    assert admin.get("/api/crops/no_such_crop").status_code == 404
    assert admin.post("/api/crops/no_such_crop/complete", json={}).status_code == 404
    assert admin.post("/api/crops/no_such_crop/reopen").status_code == 404


# ------------------------------------------------------- coordinates via the API
def test_mask_points_round_trip_through_the_api(
    admin: TestClient, mite_class: dict, crop_rows: list[dict], query
):
    """The wire speaks crop-local, the store speaks source-image, and neither
    side may drift. The crop chosen here is the bottom-right one, so both offsets
    are non-zero — a test on crop r0c0 would pass with the transform deleted."""
    crop = crop_rows[-1]
    assert (crop["x"], crop["y"]) == (1280, 640)
    local = [[10.5, 20.25], [600.0, 30.0], [300.0, 620.75]]

    created = admin.post(
        "/api/masks",
        json={"crop_id": crop["crop_id"], "class_id": mite_class["class_id"],
              "points": local},
    )
    assert created.status_code == 200, created.text
    assert created.json()["points"] == local

    reloaded = admin.get(f"/api/crops/{crop['crop_id']}").json()
    assert reloaded["masks"][0]["points"] == local

    raw = query("SELECT points FROM masks WHERE mask_id = ?",
                [created.json()["mask_id"]])[0][0]
    stored = json.loads(raw) if isinstance(raw, str) else raw
    assert stored == [[1290.5, 660.25], [1880.0, 670.0], [1580.0, 1260.75]], (
        f"the DB must hold SOURCE-image pixels (crop origin added), got {stored}"
    )


def test_points_are_clamped_to_the_crop_through_the_api(
    admin: TestClient, mite_class: dict, crop_rows: list[dict]
):
    crop = crop_rows[0]
    created = admin.post(
        "/api/masks",
        json={"crop_id": crop["crop_id"], "class_id": mite_class["class_id"],
              "points": [[-20.0, 5.0], [700.0, 5.0], [300.0, 900.0]]},
    )
    assert created.status_code == 200, created.text
    assert created.json()["points"] == [[0.0, 5.0], [640.0, 5.0], [300.0, 640.0]]


# ----------------------------------------------------------- CropTask index (A17)
def test_crop_index_is_one_based_and_in_reading_order(
    admin: TestClient, crop_rows: list[dict]
):
    """A17. The frontend prints `index` verbatim as "Crop 3 of 6", so 0-based
    would greet every user with "Crop 0 of 6" on the first screen they ever
    see. Ordered `row_idx` then `col_idx`, which is also the order
    `db.next_open_crop` walks, so "next" always moves forward."""
    seen = []
    for expected, row in enumerate(crop_rows, start=1):
        task = admin.get(f"/api/crops/{row['crop_id']}").json()
        assert task["index"] == expected, (
            f"{row['crop_id']} (r{row['row_idx']}c{row['col_idx']}) reported index "
            f"{task['index']}, expected {expected}"
        )
        assert task["total"] == N_CROPS
        seen.append(task["index"])
    assert seen == [1, 2, 3, 4, 5, 6]
    assert 0 not in seen, "the first crop a user sees must say 1, never 0"


def test_next_walks_the_grid_forward(admin: TestClient, crop_rows: list[dict]):
    """The queue order and the progress line are the same order, so completing a
    crop always advances the number rather than jumping backwards."""
    for expected in range(1, N_CROPS + 1):
        task = admin.get("/api/crops/next").json()
        assert task["index"] == expected
        assert task["crop"]["crop_id"] == crop_rows[expected - 1]["crop_id"]
        done = admin.post(
            f"/api/crops/{task['crop']['crop_id']}/complete", json={"is_empty": True}
        )
        assert done.status_code == 200, done.text

    empty = admin.get("/api/crops/next")
    assert empty.status_code == 204, "an exhausted queue is a success, not a 404"


def test_next_can_be_scoped_to_one_image(admin: TestClient, image: dict):
    resp = admin.get(f"/api/crops/next?image_id={image['image_id']}")
    assert resp.status_code == 200
    assert resp.json()["image"]["image_id"] == image["image_id"]
    assert resp.json()["index"] == 1


# ------------------------------------------- the completeness invariant (1, A1)
def test_completing_with_masks_and_is_empty_is_refused(
    admin: TestClient, mite_class: dict, crop_rows: list[dict]
):
    """"Empty" is a claim about the pixels, not a shortcut. A crop marked empty
    exports an image with an EMPTY label file (SPEC section 7), so a crop that
    both carries masks and claims to be empty would ship those instances to the
    trainer as background — the precise failure SPEC section 1 exists to
    prevent. 400, server-side, with a message naming the fix."""
    crop_id = crop_rows[0]["crop_id"]
    admin.post(
        "/api/masks",
        json={"crop_id": crop_id, "class_id": mite_class["class_id"],
              "points": TRIANGLE},
    )
    resp = admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": True})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "not empty" in detail
    assert "delete them" in detail, "the message must name the fix, not just refuse"
    assert admin.get(f"/api/crops/{crop_id}").json()["crop"]["status"] == "open"


def test_completing_with_no_masks_and_not_empty_is_refused(
    admin: TestClient, crop_rows: list[dict]
):
    """A1. Without this, a crop full of unlabeled instances reaches the export as an
    all-background image and actively teaches the model to suppress true
    positives. It is the one failure this tool exists to prevent, so it is a 400
    and not a tooltip."""
    crop_id = crop_rows[0]["crop_id"]
    resp = admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": False})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "no masks" in detail
    assert "empty" in detail, "the message must offer the honest alternative"
    assert admin.get(f"/api/crops/{crop_id}").json()["crop"]["status"] == "open"


def test_completing_an_empty_crop_succeeds(admin: TestClient, crop_rows: list[dict]):
    """A crop with genuinely nothing in it is a valid and valuable negative
    sample (SPEC section 1), not a skip."""
    crop_id = crop_rows[0]["crop_id"]
    resp = admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": True})
    assert resp.status_code == 200, resp.text
    crop = resp.json()["crop"]
    assert crop["status"] == "done"
    assert crop["is_empty"] is True
    assert crop["completed_by"] is not None
    assert crop["completed_at"] is not None


def test_completing_a_labeled_crop_succeeds(
    admin: TestClient, mite_class: dict, crop_rows: list[dict]
):
    crop_id = crop_rows[0]["crop_id"]
    admin.post(
        "/api/masks",
        json={"crop_id": crop_id, "class_id": mite_class["class_id"],
              "points": TRIANGLE},
    )
    resp = admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["crop"]["status"] == "done"
    assert resp.json()["crop"]["is_empty"] is False


def test_reopening_clears_is_empty_and_requeues(
    admin: TestClient, crop_rows: list[dict]
):
    """Reopening means the completion no longer stands: the provenance is
    cleared so it never describes a completion that was undone, and `is_empty`
    goes with it — a still-empty crop is one click away."""
    crop_id = crop_rows[0]["crop_id"]
    admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": True})

    resp = admin.post(f"/api/crops/{crop_id}/reopen")
    assert resp.status_code == 200, resp.text
    crop = resp.json()["crop"]
    assert crop["status"] == "open"
    assert crop["is_empty"] is False
    assert crop["completed_by"] is None
    assert crop["completed_at"] is None

    assert admin.get("/api/crops/next").json()["crop"]["crop_id"] == crop_id


# --------------------------------------------------------------- CropTask n_done
CROP_TASK_KEYS = {"crop", "image", "masks", "index", "total", "n_done"}


def _assert_crop_task(body: dict, *, n_done: int) -> None:
    """The CropTask shape. `n_done` is top level beside `index` and `total`
    because those three are one thought — the progress line — and it saves the
    Label page a second `GET /api/images/{id}` after every completed crop purely
    to read one number back. `n_crops` is deliberately absent: `total` is
    already that count, and two fields obliged to agree will eventually
    disagree."""
    assert set(body) == CROP_TASK_KEYS, f"unexpected CropTask keys: {sorted(body)}"
    assert "n_crops" not in body
    assert isinstance(body["n_done"], int) and not isinstance(body["n_done"], bool)
    assert body["n_done"] == n_done


def test_next_carries_n_done(admin: TestClient, crop_rows: list[dict]):
    _assert_crop_task(admin.get("/api/crops/next").json(), n_done=0)


def test_get_crop_carries_n_done(admin: TestClient, crop_rows: list[dict]):
    _assert_crop_task(
        admin.get(f"/api/crops/{crop_rows[2]['crop_id']}").json(), n_done=0
    )


def test_complete_carries_n_done_and_increments_it(
    admin: TestClient, crop_rows: list[dict]
):
    """The count is of THIS image's crops with status='done', so the response to
    the completion already reflects it — that is the whole reason it is here."""
    first = admin.post(
        f"/api/crops/{crop_rows[0]['crop_id']}/complete", json={"is_empty": True}
    )
    assert first.status_code == 200, first.text
    _assert_crop_task(first.json(), n_done=1)

    second = admin.post(
        f"/api/crops/{crop_rows[1]['crop_id']}/complete", json={"is_empty": True}
    )
    _assert_crop_task(second.json(), n_done=2)

    # And every other CropTask route agrees with it.
    _assert_crop_task(admin.get("/api/crops/next").json(), n_done=2)
    _assert_crop_task(
        admin.get(f"/api/crops/{crop_rows[0]['crop_id']}").json(), n_done=2
    )


def test_reopen_carries_n_done_and_decrements_it(
    admin: TestClient, crop_rows: list[dict]
):
    admin.post(f"/api/crops/{crop_rows[0]['crop_id']}/complete",
               json={"is_empty": True})
    admin.post(f"/api/crops/{crop_rows[1]['crop_id']}/complete",
               json={"is_empty": True})

    reopened = admin.post(f"/api/crops/{crop_rows[1]['crop_id']}/reopen")
    assert reopened.status_code == 200, reopened.text
    _assert_crop_task(reopened.json(), n_done=1)


def test_n_done_matches_the_image_summary(
    admin: TestClient, image: dict, crop_rows: list[dict]
):
    """`ImageSummary.n_done` and `CropTask.n_done` are the same number computed
    from the same query; if they ever disagree, the progress bar is lying."""
    admin.post(f"/api/crops/{crop_rows[0]['crop_id']}/complete",
               json={"is_empty": True})
    task = admin.get(f"/api/crops/{crop_rows[3]['crop_id']}").json()
    summary = admin.get(f"/api/images/{image['image_id']}").json()["image"]
    assert task["n_done"] == summary["n_done"] == 1
    assert task["total"] == summary["n_crops"] == N_CROPS
