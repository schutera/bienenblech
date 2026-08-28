"""The empty-sheet upload flag: crops born done, queue skipped, negatives exported.

The contract: a sheet can be marked EMPTY at upload. The uploader asserts
sheet-level emptiness ONCE, at the moment they are looking at the sheet, and
every crop of that frame is born finished — status='done', is_empty=TRUE,
completed_by=<the uploader>, completed_at=now. From that one write, everything
else follows from machinery that already exists: the queue never shows the
crops (`/api/crops/next` already skips done crops), the export automatically
gains them as hard negatives (0-byte label files, SPEC section 7), the Frames
list shows the frame fully done, and a mis-marked sheet is recoverable per
crop via the ordinary reopen flow.

No schema change: "uploaded as empty" is derivable — a done crop with no masks
is empty by the section-1 guard — and the uploader's assertion is attributed
via `completed_by`, same as any hand-completed crop.

Two hard edges pinned here because they are the ones a refactor would sand off:

*   The duplicate path is UNCHANGED. A sha256 match answers "nothing was
    changed" and that answer must stay true: the flag on a re-upload must never
    retro-complete an existing frame's crops.
*   Without the flag (absent OR explicitly false) an upload behaves exactly as
    before: every crop open. The flag is opt-in per file, per request — the
    frontend sends one file per request, which is also how these tests upload.

Everything goes through the real multipart endpoint against the sandboxed
store from conftest; no rows are fabricated.
"""
from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from bienenblech import db
from conftest import (
    ADMIN_USER,
    GRID_COLS,
    GRID_ROWS,
    N_CROPS,
    POWERUSER_USER,
    frame_bytes,
)

TRIANGLE = [[10, 10], [60, 10], [60, 60]]


def _upload_one(client: TestClient, *, seed: int, is_empty: bool | None = None) -> dict:
    """One frame, one request — the way the frontend uploads (one file per
    request for honest progress bars). `is_empty=None` omits the field
    entirely, which must be indistinguishable from sending false."""
    data = {} if is_empty is None else {"is_empty": "true" if is_empty else "false"}
    resp = client.post(
        "/api/images",
        files={"file": (f"sheet{seed}.png", frame_bytes(seed=seed), "image/png")},
        data=data,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _image_detail(client: TestClient, image_id: str) -> dict:
    resp = client.get(f"/api/images/{image_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _empty_frame_crop_ids(image_id: str) -> list[str]:
    return [
        db.crop_id_for(image_id, r, c)
        for r in range(GRID_ROWS)
        for c in range(GRID_COLS)
    ]


# ------------------------------------------------------------- born finished (1)
def test_empty_upload_births_every_crop_done(admin: TestClient):
    """The whole contract in one write: the uploader asserted emptiness for the
    sheet, so every crop is born done+is_empty with the uploader's name on it.
    Stored and tiled exactly like any other frame — same grid, same N_CROPS —
    only the crops' lifecycle starts at the end."""
    body = _upload_one(admin, seed=11, is_empty=True)
    assert body["duplicates"] == []
    summary = body["images"][0]
    assert summary["n_crops"] == N_CROPS
    assert summary["n_done"] == N_CROPS, (
        "born finished must already show in the upload response, not after a refresh"
    )
    assert summary["n_masks"] == 0
    assert summary["uploaded_by"] == ADMIN_USER

    detail = _image_detail(admin, summary["image_id"])
    rows = detail["crops"]
    assert len(rows) == N_CROPS
    for row in rows:
        assert row["status"] == "done", row
        assert row["is_empty"] is True, row
        assert row["completed_by"] == ADMIN_USER, (
            "the uploader's assertion is attributed via completed_by"
        )
        assert row["completed_at"] is not None, row
        assert row["n_masks"] == 0, row

    # The Frames list derives its "empty" pill from exactly this triple; if the
    # summary stops satisfying it, the pill silently disappears.
    img = detail["image"]
    assert img["n_crops"] > 0
    assert img["n_done"] == img["n_crops"]
    assert img["n_masks"] == 0


# ---------------------------------------------------------------- the queue (2)
def test_empty_crops_never_enter_the_queue(admin: TestClient):
    """Born done means the queue never shows them — that is the point of the
    flag: nobody should page through 6 crops of a sheet the uploader already
    judged. The empty frame is uploaded FIRST, so if its crops were open the
    oldest-first queue would have to serve them before the plain frame's."""
    empty = _upload_one(admin, seed=21, is_empty=True)["images"][0]
    plain = _upload_one(admin, seed=22)["images"][0]

    task = admin.get("/api/crops/next")
    assert task.status_code == 200
    body = task.json()
    assert body["image"]["image_id"] == plain["image_id"], (
        "the queue served an auto-done crop of the empty-marked frame"
    )
    assert body["crop"]["crop_id"] == db.crop_id_for(plain["image_id"], 0, 0)

    scoped = admin.get(f"/api/crops/next?image_id={empty['image_id']}")
    assert scoped.status_code == 204, (
        "scoped to the empty frame the queue must be exhausted, not serving"
    )


# --------------------------------------------------------------- the export (3)
def test_export_ships_empty_frame_crops_as_hard_negatives(
    admin: TestClient, mite_class: dict
):
    """Mirrors `test_export.test_empty_crop_exports_a_zero_byte_label_file` for
    the upload flag: an empty-marked frame's crops are hard negatives — present
    in the zip, each with a 0-byte label file. Present, not absent; empty, not
    missing (a training image with no .txt at all is unlabeled to Ultralytics,
    not a negative). A labeled positive from another frame rides along to prove
    the zero bytes are the flag's doing, not a dead label writer."""
    empty = _upload_one(admin, seed=31, is_empty=True)["images"][0]
    plain = _upload_one(admin, seed=32)["images"][0]
    labeled = db.crop_id_for(plain["image_id"], 0, 0)
    r = admin.post(
        "/api/masks",
        json={"crop_id": labeled, "class_id": mite_class["class_id"],
              "points": TRIANGLE},
    )
    assert r.status_code == 200, r.text
    r = admin.post(f"/api/crops/{labeled}/complete", json={"is_empty": False})
    assert r.status_code == 200, r.text

    resp = admin.get("/api/export/yolo", params={"val_fraction": 0.0, "seed": 0})
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        for cid in _empty_frame_crop_ids(empty["image_id"]):
            label = [n for n in names
                     if n.startswith("labels/") and n.endswith(f"/{cid}.txt")]
            image = [n for n in names
                     if n.startswith("images/") and n.endswith(f"/{cid}.jpg")]
            assert len(label) == 1 and len(image) == 1, (
                f"empty-marked crop {cid} must still be exported: {label} {image}"
            )
            assert zf.getinfo(label[0]).file_size == 0, (
                f"{label[0]} must be a 0-byte hard negative"
            )
            assert zf.read(label[0]) == b""
        positive = [n for n in names
                    if n.startswith("labels/") and n.endswith(f"/{labeled}.txt")]
        assert len(positive) == 1
        assert zf.read(positive[0]) != b"", (
            "the labeled crop's label file is empty too - the label writer is "
            "broken, and the zero-byte negatives above prove nothing"
        )


# ------------------------------------------------------------------ dedupe (4)
def test_dedupe_with_the_flag_changes_nothing(admin: TestClient):
    """The duplicate path is UNCHANGED. The dedupe answer is "nothing was
    changed", and the flag must not make that a lie: a re-upload of known bytes
    with is_empty=true must never retro-complete the existing frame's crops —
    someone may be half way through labeling them."""
    first = _upload_one(admin, seed=41)
    image_id = first["images"][0]["image_id"]
    # label nothing — the crops sit open, exactly as uploaded

    again = _upload_one(admin, seed=41, is_empty=True)
    assert again["images"] == []
    assert [d["image_id"] for d in again["duplicates"]] == [image_id]
    assert again["duplicates"][0]["n_done"] == 0, (
        "the dedupe answer must stay true: nothing was changed"
    )

    for row in _image_detail(admin, image_id)["crops"]:
        assert row["status"] == "open", row
        assert row["is_empty"] is False, row
        assert row["completed_by"] is None, row
        assert row["completed_at"] is None, row

    scoped = admin.get(f"/api/crops/next?image_id={image_id}")
    assert scoped.status_code == 200, "the frame's crops must still be labelable"


# ------------------------------------------------------------------ reopen (5)
def test_reopen_recovers_a_mis_marked_crop(admin: TestClient):
    """A mis-marked sheet is recoverable per crop through the existing reopen
    flow — an auto-done crop is an ordinary done crop, so reopening returns it
    to the queue and clears is_empty and the provenance, same as undoing a
    hand completion. The other five crops keep the uploader's assertion."""
    empty = _upload_one(admin, seed=51, is_empty=True)["images"][0]
    cid = db.crop_id_for(empty["image_id"], 0, 1)

    resp = admin.post(f"/api/crops/{cid}/reopen")
    assert resp.status_code == 200, resp.text
    crop = resp.json()["crop"]
    assert crop["status"] == "open"
    assert crop["is_empty"] is False
    assert crop["completed_by"] is None
    assert crop["completed_at"] is None

    nxt = admin.get("/api/crops/next")
    assert nxt.status_code == 200
    assert nxt.json()["crop"]["crop_id"] == cid, (
        "the reopened crop must be back in the queue"
    )

    others = [r for r in _image_detail(admin, empty["image_id"])["crops"]
              if r["crop_id"] != cid]
    assert len(others) == N_CROPS - 1
    assert all(r["status"] == "done" and r["is_empty"] is True for r in others), (
        "reopen is per crop; the rest of the sheet keeps the uploader's assertion"
    )


# -------------------------------------------------------- regression guard (6)
def test_upload_without_the_flag_is_unchanged(admin: TestClient):
    """The flag is opt-in: absent and explicitly-false must both produce the
    upload behaviour that existed before the flag — every crop open, nothing
    attributed. Explicit false is tested too so 'field present' can never
    become the accidental trigger."""
    absent = _upload_one(admin, seed=61)["images"][0]
    explicit = _upload_one(admin, seed=62, is_empty=False)["images"][0]

    for summary in (absent, explicit):
        assert summary["n_done"] == 0, summary
        for row in _image_detail(admin, summary["image_id"])["crops"]:
            assert row["status"] == "open", row
            assert row["is_empty"] is False, row
            assert row["completed_by"] is None, row
            assert row["completed_at"] is None, row


# ---------------------------------------------------------------- poweruser (7)
def test_poweruser_can_mark_a_sheet_empty(poweruser: TestClient):
    """The flag rides the same open upload right as POST /api/images itself:
    powerusers upload the sheets they label, so they can assert emptiness at
    upload too, and the attribution is THEIR name, not an admin's."""
    body = _upload_one(poweruser, seed=71, is_empty=True)
    summary = body["images"][0]
    assert summary["n_done"] == N_CROPS, summary
    assert summary["uploaded_by"] == POWERUSER_USER

    for row in _image_detail(poweruser, summary["image_id"])["crops"]:
        assert row["status"] == "done", row
        assert row["is_empty"] is True, row
        assert row["completed_by"] == POWERUSER_USER, (
            "the uploader's assertion is attributed to the uploader"
        )
        assert row["completed_at"] is not None, row
