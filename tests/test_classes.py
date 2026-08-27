"""Label classes: who may curate them, and why an index is never reused.

Two rules are being pinned here.

**Creation is the annotator's, curation is the admin's.** SPEC section 2 grants
annotators "label crops, add classes, read" in so many words, so `POST
/api/classes` is open — a taxonomy an annotator cannot extend mid-session gets
worked around by mislabeling, which is far more expensive than a stray class.
Renaming, recoloring, archiving and restoring are not additive: they rewrite
every picker, every legend and every export's `data.yaml` for everyone at once,
so they sit behind the admin gate (SPEC section 2 grants admins "everything:
users, classes, ..."; amendment A4 already put restore there).

**`yolo_index` is forever.** SPEC section 4: 0-based, monotonic, never reused or
renumbered. It is the class's identity in every exported label file, so handing
an archived class's index to a new class would silently relabel every crop in
every dataset exported before the archive.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _names(resp) -> list[str]:
    return [c["name"] for c in resp.json()]


# ------------------------------------------------------------------ permissions
def test_annotator_may_create_a_class(annotator: TestClient):
    """SPEC section 2: annotators add classes. Creation is additive and local —
    it costs a fresh yolo_index and changes nothing anyone already labeled."""
    resp = annotator.post("/api/classes", json={"name": "drone"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "drone"
    assert body["archived"] is False
    assert body["yolo_index"] == 0
    assert body["color"].startswith("#") and len(body["color"]) == 7


def test_annotator_cannot_rename_a_class(annotator: TestClient, bee_class: dict):
    """A rename is exactly as globally visible as an archive: it rewrites every
    picker and every export's data.yaml for every user at once, and the person
    doing it cannot see who is mid-crop against the old name."""
    resp = annotator.patch(
        f"/api/classes/{bee_class['class_id']}", json={"name": "renamed"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin only"


def test_annotator_cannot_archive_a_class(annotator: TestClient, bee_class: dict):
    """Archiving takes a class out of every picker and refuses new masks on it,
    while its existing masks stay in the store as invisible work."""
    resp = annotator.delete(f"/api/classes/{bee_class['class_id']}")
    assert resp.status_code == 403


def test_annotator_cannot_restore_a_class(
    admin: TestClient, annotator: TestClient, bee_class: dict
):
    """A4. Archive and restore are a matched pair — a role that can hide a class
    but not unhide it can only make a mess somebody else has to clean up."""
    assert admin.delete(f"/api/classes/{bee_class['class_id']}").status_code == 200
    resp = annotator.post(f"/api/classes/{bee_class['class_id']}/restore")
    assert resp.status_code == 403


def test_admin_may_rename_recolor_and_describe(admin: TestClient, bee_class: dict):
    resp = admin.patch(
        f"/api/classes/{bee_class['class_id']}",
        json={"name": "worker bee", "color": "#123abc", "description": "on comb"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "worker bee"
    assert body["color"] == "#123abc"
    assert body["description"] == "on comb"
    # The id is what every mask references, so it survives the rename.
    assert body["class_id"] == bee_class["class_id"]
    assert body["yolo_index"] == bee_class["yolo_index"]


def test_a_duplicate_class_name_is_409(admin: TestClient, bee_class: dict):
    """SPEC section 5 names 409 for exactly this case."""
    resp = admin.post("/api/classes", json={"name": "Bee"})
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_an_empty_class_name_is_400(admin: TestClient):
    resp = admin.post("/api/classes", json={"name": "   "})
    assert resp.status_code == 400


def test_patching_an_unknown_class_is_404(admin: TestClient):
    resp = admin.patch("/api/classes/no_such_class", json={"name": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------- archiving
def test_archive_hides_but_never_drops(admin: TestClient, bee_class: dict):
    """Soft delete everywhere (SPEC section 4). The row keeps its id, its name
    and its yolo_index; `?include_archived=` is the only thing that changes."""
    class_id = bee_class["class_id"]
    resp = admin.delete(f"/api/classes/{class_id}")
    assert resp.status_code == 200
    assert resp.json()["archived"] is True

    visible = admin.get("/api/classes")
    assert class_id not in [c["class_id"] for c in visible.json()]

    archived = admin.get("/api/classes?include_archived=true")
    row = next(c for c in archived.json() if c["class_id"] == class_id)
    assert row["archived"] is True
    assert row["name"] == bee_class["name"]
    assert row["yolo_index"] == bee_class["yolo_index"]


def test_restore_brings_a_class_back_unchanged(admin: TestClient, bee_class: dict):
    """A4: without restore, a mis-clicked archive is unfixable from the UI while
    the class's masks sit there as invisible work."""
    class_id = bee_class["class_id"]
    admin.delete(f"/api/classes/{class_id}")

    resp = admin.post(f"/api/classes/{class_id}/restore")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is False
    assert resp.json()["yolo_index"] == bee_class["yolo_index"]
    assert class_id in [c["class_id"] for c in admin.get("/api/classes").json()]


def test_restoring_an_unknown_class_is_404(admin: TestClient):
    assert admin.post("/api/classes/no_such_class/restore").status_code == 404


def test_an_archived_name_stays_taken(admin: TestClient, bee_class: dict):
    """Recreating an archived class under its old name would mint a second
    yolo_index for the same concept and split its masks across two ids."""
    admin.delete(f"/api/classes/{bee_class['class_id']}")
    resp = admin.post("/api/classes", json={"name": bee_class["name"]})
    assert resp.status_code == 409


# -------------------------------------------------------------------- yolo_index
def test_yolo_index_is_server_assigned_and_monotonic(admin: TestClient):
    """0-based and assigned by the server — a client that could choose its own
    index could collide with an archived one."""
    indices = [
        admin.post("/api/classes", json={"name": name}).json()["yolo_index"]
        for name in ("bee", "varroa", "cell")
    ]
    assert indices == [0, 1, 2]


def test_yolo_index_is_never_reused_after_an_archive(admin: TestClient):
    """The load-bearing one. An archived class keeps its index reserved, so a
    model trained on an older export keeps matching today's data.yaml. Handing
    index 1 to a new class would silently relabel every varroa in every dataset
    exported before the archive."""
    admin.post("/api/classes", json={"name": "bee"})
    varroa = admin.post("/api/classes", json={"name": "varroa"}).json()
    assert varroa["yolo_index"] == 1

    admin.delete(f"/api/classes/{varroa['class_id']}")
    fresh = admin.post("/api/classes", json={"name": "queen"}).json()
    assert fresh["yolo_index"] == 2, "an archived index must never be handed out again"

    restored = admin.post(f"/api/classes/{varroa['class_id']}/restore").json()
    assert restored["yolo_index"] == 1, "restore must not renumber"


def test_classes_are_ordered_by_yolo_index(admin: TestClient):
    """That order is the order of data.yaml and of the annotator's number keys
    (A18: the ClassPicker's 1..9 hints are positional), so it has to be stable
    and it has to be the index order, not creation-timestamp order."""
    for name in ("bee", "varroa", "cell"):
        admin.post("/api/classes", json={"name": name})
    listed = admin.get("/api/classes").json()
    assert [c["yolo_index"] for c in listed] == sorted(c["yolo_index"] for c in listed)
    assert _names(admin.get("/api/classes")) == ["bee", "varroa", "cell"]


# ------------------------------------------------------- archived classes + masks
def test_a_new_mask_on_an_archived_class_is_refused_with_a_useful_message(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """400, not a silent success. An archived class is out of every picker, so a
    mask created against it would be invisible work — and the annotator needs to
    be told which class, not just "bad request"."""
    admin.delete(f"/api/classes/{bee_class['class_id']}")
    resp = admin.post(
        "/api/masks",
        json={
            "crop_id": crop_rows[0]["crop_id"],
            "class_id": bee_class["class_id"],
            "points": [[10, 10], [40, 10], [40, 40]],
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "archived" in detail
    assert bee_class["name"] in detail


def test_archiving_keeps_existing_masks_countable(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """Its masks stay in the store and stay in the exports, which is why the
    index stays reserved."""
    admin.post(
        "/api/masks",
        json={
            "crop_id": crop_rows[0]["crop_id"],
            "class_id": bee_class["class_id"],
            "points": [[10, 10], [40, 10], [40, 40]],
        },
    )
    admin.delete(f"/api/classes/{bee_class['class_id']}")

    archived = admin.get("/api/classes?include_archived=true").json()
    row = next(c for c in archived if c["class_id"] == bee_class["class_id"])
    assert row["n_masks"] == 1
