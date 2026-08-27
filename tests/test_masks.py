"""Polygon CRUD: what is accepted, what is refused, and what survives a delete.

SPEC section 4: **soft delete everywhere.** Annotator hours are the only thing on
this box that cannot be regenerated, so `DELETE /api/masks/{id}` flags the row
and never removes it — an annotator who deletes the wrong polygon after twenty
minutes of tracing wants it back, and the row costs nothing.

SPEC section 3 fixes the rest: minimum three vertices, self-intersecting
polygons accepted (annotators draw them and the exporter does not care), no
holes, and points on the wire are CROP-LOCAL while points in the store are
SOURCE-IMAGE.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

TRIANGLE = [[10.0, 10.0], [60.0, 10.0], [60.0, 60.0]]
# A bow tie: edges cross in the middle. Perfectly ordinary output from a human
# tracing an overlapping pair of bees with a mouse.
BOWTIE = [[0.0, 0.0], [100.0, 100.0], [100.0, 0.0], [0.0, 100.0]]


def _post_mask(client: TestClient, crop_id: str, class_id: str, points) -> dict:
    resp = client.post(
        "/api/masks",
        json={"crop_id": crop_id, "class_id": class_id, "points": points},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ------------------------------------------------------------------------ create
def test_create_returns_the_spec_mask_shape(
    annotator: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """SPEC section 6's `Mask`, and nothing more. A3: the db row carries
    `image_id` and it is dropped here — the crop already identifies the frame."""
    mask = _post_mask(
        annotator, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE
    )
    assert set(mask) == {
        "mask_id", "crop_id", "class_id", "points",
        "created_by", "created_at", "updated_at",
    }
    assert mask["crop_id"] == crop_rows[0]["crop_id"]
    assert mask["class_id"] == bee_class["class_id"]
    assert mask["points"] == TRIANGLE
    assert mask["created_by"] is not None
    assert mask["updated_at"] is None


def test_a_new_mask_shows_up_on_its_crop(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    crop_id = crop_rows[3]["crop_id"]
    mask = _post_mask(admin, crop_id, bee_class["class_id"], TRIANGLE)

    task = admin.get(f"/api/crops/{crop_id}").json()
    assert [m["mask_id"] for m in task["masks"]] == [mask["mask_id"]]
    assert task["crop"]["n_masks"] == 1
    # And not on any other crop of the same frame.
    assert admin.get(f"/api/crops/{crop_rows[0]['crop_id']}").json()["masks"] == []


def test_a_polygon_needs_three_vertices(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """Two points is a line: it has no area, so it cannot describe an instance
    and the exporter's normalisation would emit a degenerate segment."""
    resp = admin.post(
        "/api/masks",
        json={"crop_id": crop_rows[0]["crop_id"], "class_id": bee_class["class_id"],
              "points": [[10.0, 10.0], [60.0, 10.0]]},
    )
    assert resp.status_code == 400, resp.text
    assert "3" in resp.json()["detail"]


def test_a_missing_polygon_is_400_not_422(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """SPEC section 5 says a bad polygon is a 400 with a sentence the annotator
    can act on, which is why `CreateMaskReq.points` is deliberately untyped —
    pydantic would answer 422 with a validation blob instead."""
    resp = admin.post(
        "/api/masks",
        json={"crop_id": crop_rows[0]["crop_id"], "class_id": bee_class["class_id"]},
    )
    assert resp.status_code == 400, resp.text
    assert "points" in resp.json()["detail"]


def test_self_intersecting_polygons_are_accepted(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """SPEC section 3 is explicit: annotators make them and the exporter does not
    care. Rejecting them would block real work over a geometric nicety that
    nothing downstream ever checks."""
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], BOWTIE)
    assert mask["points"] == BOWTIE


def test_a_mask_on_an_unknown_crop_is_404(admin: TestClient, bee_class: dict):
    """404, not a 500. `db.get_crop` answers None and `api._need` is what turns
    that into a status code."""
    resp = admin.post(
        "/api/masks",
        json={"crop_id": "no_such_crop", "class_id": bee_class["class_id"],
              "points": TRIANGLE},
    )
    assert resp.status_code == 404, resp.text
    assert "crop" in resp.json()["detail"]


def test_a_mask_on_an_unknown_class_is_404(admin: TestClient, crop_rows: list[dict]):
    resp = admin.post(
        "/api/masks",
        json={"crop_id": crop_rows[0]["crop_id"], "class_id": "no_such_class",
              "points": TRIANGLE},
    )
    assert resp.status_code == 404, resp.text
    assert "class" in resp.json()["detail"]


def test_masks_on_a_deleted_images_crop_are_refused_cleanly(
    admin: TestClient, bee_class: dict, image: dict, crop_rows: list[dict]
):
    """Deleting an image is the one hard delete in the schema, and it takes the
    crops with it. A stale browser tab still holding a crop id must get a 404,
    not a 500 from a write against a row that is gone."""
    crop_id = crop_rows[0]["crop_id"]
    _post_mask(admin, crop_id, bee_class["class_id"], TRIANGLE)

    deleted = admin.delete(f"/api/images/{image['image_id']}?force=true")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_masks"] == 1

    assert admin.get(f"/api/crops/{crop_id}").status_code == 404
    resp = admin.post(
        "/api/masks",
        json={"crop_id": crop_id, "class_id": bee_class["class_id"],
              "points": TRIANGLE},
    )
    assert resp.status_code == 404, resp.text


def test_deleting_an_image_with_masks_needs_force(
    admin: TestClient, bee_class: dict, image: dict, crop_rows: list[dict]
):
    """A mis-clicked row in an image list is a plausible way to lose a week of
    annotator hours, so the delete refuses until it is asked twice."""
    _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)
    resp = admin.delete(f"/api/images/{image['image_id']}")
    assert resp.status_code == 409, resp.text
    assert "force=true" in resp.json()["detail"]
    assert admin.get(f"/api/images/{image['image_id']}").status_code == 200


# ------------------------------------------------------------------------ update
def test_reshaping_a_mask_stamps_updated_at(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    crop = crop_rows[-1]
    mask = _post_mask(admin, crop["crop_id"], bee_class["class_id"], TRIANGLE)
    moved = [[100.0, 100.0], [200.0, 100.0], [200.0, 200.0], [150.0, 180.5]]

    resp = admin.patch(f"/api/masks/{mask['mask_id']}", json={"points": moved})
    assert resp.status_code == 200, resp.text
    assert resp.json()["points"] == moved
    assert resp.json()["updated_at"] is not None

    reloaded = admin.get(f"/api/crops/{crop['crop_id']}").json()["masks"][0]
    assert reloaded["points"] == moved


def test_re_classing_a_mask_works(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """A18 depends on this endpoint existing and being usable: `PolygonCanvas`
    only carries points, so when a selected mask is re-classed with a digit key
    the Label page must PATCH `{class_id}` here. Without it a mis-classed
    polygon can only be deleted and redrawn."""
    varroa = admin.post("/api/classes", json={"name": "varroa"}).json()
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)

    resp = admin.patch(
        f"/api/masks/{mask['mask_id']}", json={"class_id": varroa["class_id"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["class_id"] == varroa["class_id"]
    # The points are untouched by a re-class.
    assert resp.json()["points"] == TRIANGLE

    counts = {c["class_id"]: c["n_masks"] for c in admin.get("/api/classes").json()}
    assert counts[varroa["class_id"]] == 1
    assert counts[bee_class["class_id"]] == 0


def test_re_classing_onto_an_archived_class_is_refused(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """Same rule as creation: an archived class is out of every picker, so a
    mask moved onto it becomes invisible work."""
    varroa = admin.post("/api/classes", json={"name": "varroa"}).json()
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)
    admin.delete(f"/api/classes/{varroa['class_id']}")

    resp = admin.patch(
        f"/api/masks/{mask['mask_id']}", json={"class_id": varroa["class_id"]}
    )
    assert resp.status_code == 400, resp.text
    assert "archived" in resp.json()["detail"]


def test_patching_an_unknown_mask_is_404(admin: TestClient):
    resp = admin.patch("/api/masks/no_such_mask", json={"points": TRIANGLE})
    assert resp.status_code == 404


def test_patching_with_a_bad_polygon_is_400(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)
    resp = admin.patch(
        f"/api/masks/{mask['mask_id']}", json={"points": [[1.0, 1.0], [2.0, 2.0]]}
    )
    assert resp.status_code == 400
    # The old shape is still there: a rejected edit must not blank the mask.
    assert admin.get(
        f"/api/crops/{crop_rows[0]['crop_id']}"
    ).json()["masks"][0]["points"] == TRIANGLE


# ------------------------------------------------------------------ delete (soft)
def test_delete_is_soft_and_the_row_survives(
    admin: TestClient, bee_class: dict, crop_rows: list[dict], query
):
    """SPEC section 4: never hard-DELETE a mask. Every read route agrees it is
    gone; only the store knows it is recoverable, which is the entire point —
    an operator can un-flag it, and nobody has to retrace the polygon."""
    crop_id = crop_rows[0]["crop_id"]
    mask = _post_mask(admin, crop_id, bee_class["class_id"], TRIANGLE)

    resp = admin.delete(f"/api/masks/{mask['mask_id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert admin.get(f"/api/crops/{crop_id}").json()["masks"] == []
    assert admin.get(f"/api/crops/{crop_id}").json()["crop"]["n_masks"] == 0

    rows = query(
        "SELECT deleted, points FROM masks WHERE mask_id = ?", [mask["mask_id"]]
    )
    assert len(rows) == 1, "the mask row must survive a delete"
    assert rows[0][0] is True


def test_deleting_twice_is_a_no_op(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)
    assert admin.delete(f"/api/masks/{mask['mask_id']}").status_code == 200
    assert admin.delete(f"/api/masks/{mask['mask_id']}").status_code == 200


def test_deleting_an_unknown_mask_is_404(admin: TestClient):
    assert admin.delete("/api/masks/no_such_mask").status_code == 404


def test_a_deleted_mask_cannot_be_edited_back_to_life(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """An edit that silently resurrected a soft-deleted mask would make the
    delete look like it never happened."""
    mask = _post_mask(admin, crop_rows[0]["crop_id"], bee_class["class_id"], TRIANGLE)
    admin.delete(f"/api/masks/{mask['mask_id']}")
    resp = admin.patch(f"/api/masks/{mask['mask_id']}", json={"points": BOWTIE})
    assert resp.status_code == 404


def test_a_deleted_mask_stops_blocking_an_empty_completion(
    admin: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """The completeness guard counts live masks only, so the fix its own error
    message suggests ("delete them first") actually works."""
    crop_id = crop_rows[0]["crop_id"]
    mask = _post_mask(admin, crop_id, bee_class["class_id"], TRIANGLE)
    assert admin.post(
        f"/api/crops/{crop_id}/complete", json={"is_empty": True}
    ).status_code == 400

    admin.delete(f"/api/masks/{mask['mask_id']}")
    resp = admin.post(f"/api/crops/{crop_id}/complete", json={"is_empty": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["crop"]["is_empty"] is True


def test_an_annotator_may_label(
    annotator: TestClient, bee_class: dict, crop_rows: list[dict]
):
    """Labeling crops is the annotator's whole job (SPEC section 2), so create,
    edit, delete and complete all have to work without an admin."""
    crop_id = crop_rows[0]["crop_id"]
    mask = _post_mask(annotator, crop_id, bee_class["class_id"], TRIANGLE)
    assert annotator.patch(
        f"/api/masks/{mask['mask_id']}", json={"points": BOWTIE}
    ).status_code == 200
    assert annotator.post(
        f"/api/crops/{crop_id}/complete", json={"is_empty": False}
    ).status_code == 200
    assert annotator.delete(f"/api/masks/{mask['mask_id']}").status_code == 200
