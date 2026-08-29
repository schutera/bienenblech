"""Contract tests for the AGE tool — the second labeling tool behind the login.

Age is single-image annotation: each sample is a photo of one instance-masked
honeybee and the annotation is one integer, days 0..28, where 28 is
RIGHT-CENSORED (displays "28+", means four weeks or older). The cap is biology:
appearance-based age judgment is only meaningful across the temporal-polyethism
window (cleaning 0-3d, nursing 4-12d, maintenance 12-20d, foraging 21d+); past
~4 weeks a bee just looks "old forager", so the honest label is the censored
bucket, never a guessed day count. Several tests below pin edges of exactly
that scale, so the reasoning lives here once.

Two rules of the shared product these tests defend:

*   **Roles are global, upload is per-tool.** admin and poweruser mean the same
    everywhere, and a poweruser may annotate/flag/reopen age samples exactly as
    they label Blech crops — but `POST /api/age/samples` is ADMIN-ONLY, the one
    per-tool permission divergence in the product. Blech frames come off
    anyone's sticky sheet; age samples come out of a curated instance-masking
    pipeline, and a mislabeled-provenance sample poisons the age dataset in a
    way no annotator can see or fix from the labeling screen.
*   **Single-annotator model, like Blech crops:** one sample, one answer, done.
    A second answer must be refused, not averaged, because silently
    overwriting the first annotator's judgment destroys attribution.

Where the HTTP contract leaves a shape open, the pin follows the frontend
types in `frontend/src/lib/types.ts` (AgeSample, AgeStats) — the two sides are
compiled against each other, so those types ARE the wire contract.

Everything runs through conftest's sandboxed `store`; no test touches `data/`.
The backend lands in parallel with this file, so a failure here is first
reconciled against the contract before either side is "fixed".
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Any, Iterator

import duckdb
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from bienenblech import db
from conftest import ADMIN_USER, POWERUSER_USER

# Small on purpose: well under upload.max_edge, so the stored derivative keeps
# the source dimensions and "correct dims" is checkable by equality.
BEE_W, BEE_H = 320, 240

# The scale, restated as constants so a failure message names the contract.
AGE_MIN, AGE_MAX = 0, 28

# Exact labels.csv column order — a retraining script indexes these by name
# AND position, so the header is part of the export contract, not a nicety.
CSV_COLUMNS = ["sample_id", "filename", "age_days", "annotated_by", "annotated_at"]


# ------------------------------------------------------------------- helpers
def bee_bytes(seed: int = 0, width: int = BEE_W, height: int = BEE_H) -> bytes:
    """A synthetic PNG "bee on black" — one bright ellipse on a dark ground.

    Structured rather than flat-filled so the JPEG re-encode produces a
    realistic byte count, and seeded so two samples differ in sha256 — the
    upload dedupes on the sha of the original bytes, and two "different" test
    bees that hashed alike would silently become one sample.
    """
    im = Image.new("RGB", (width, height), (12, 12, 12))
    draw = ImageDraw.Draw(im)
    cx, cy = width // 2 + (seed * 7) % 40, height // 2 + (seed * 5) % 30
    draw.ellipse([cx - 70, cy - 35, cx + 70, cy + 35], fill=(212, 160, 60 + seed % 90))
    draw.ellipse([cx - 20, cy - 30, cx + 25, cy + 30], fill=(40, 30, 20))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def bee_cutout_bytes(seed: int = 0, width: int = BEE_W, height: int = BEE_H) -> bytes:
    """What the masking pipeline actually produces: an RGBA PNG whose alpha IS
    the instance mask — an opaque bee ellipse on a fully-transparent ground.
    The upload path must store this as PNG with the alpha intact; flattening it
    to JPEG would re-attach a background the masking removed."""
    im = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    cx, cy = width // 2 + (seed * 7) % 40, height // 2 + (seed * 5) % 30
    draw.ellipse([cx - 70, cy - 35, cx + 70, cy + 35],
                 fill=(212, 160, 60 + seed % 90, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def bee_jpeg_bytes(seed: int = 0) -> bytes:
    """The same synthetic bee as `bee_bytes`, encoded as JPEG at the source —
    an opaque upload, which must keep storing as JPEG exactly as before."""
    im = Image.open(io.BytesIO(bee_bytes(seed))).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _upload(client: TestClient, blobs: list[tuple[str, bytes]]):
    """POST the given (filename, bytes) pairs as one multipart request.

    Field name `file`, repeated — the same convention `POST /api/images` uses,
    which the Age upload card is explicitly modeled on."""
    return client.post(
        "/api/age/samples",
        files=[("file", (name, data, "image/png")) for name, data in blobs],
    )


def _samples(client: TestClient, status: str | None = None) -> list[dict]:
    url = "/api/age/samples" + (f"?status={status}" if status else "")
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # GET /api/images answers a bare array; tolerate a {"samples": [...]}
    # envelope but nothing else.
    rows = body["samples"] if isinstance(body, dict) else body
    assert isinstance(rows, list), f"expected a list of samples, got {body!r}"
    return rows


def _upload_one_blob(admin: TestClient, name: str, blob: bytes) -> str:
    """Upload one payload and return its sample_id.

    Ids are recovered by set difference against the list endpoint rather than
    parsed out of the upload answer, so these helpers do not double as a pin on
    the upload response shape — that pin lives in exactly one test below.
    """
    before = {r["sample_id"] for r in _samples(admin)}
    resp = _upload(admin, [(name, blob)])
    assert resp.status_code == 200, resp.text
    new = {r["sample_id"] for r in _samples(admin)} - before
    assert len(new) == 1, f"expected exactly one new sample, got {new}"
    return new.pop()


def _upload_one(admin: TestClient, seed: int, name: str | None = None) -> str:
    """Upload one opaque bee (RGB PNG source -> stored JPEG derivative)."""
    return _upload_one_blob(admin, name or f"bee{seed}.png", bee_bytes(seed))


def _next(client: TestClient) -> dict | None:
    resp = client.get("/api/age/samples/next")
    if resp.status_code == 204:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()


def _annotate(client: TestClient, sample_id: str, age_days: int):
    return client.post(f"/api/age/samples/{sample_id}/annotate",
                       json={"age_days": age_days})


def _flag(client: TestClient, sample_id: str, reason: str | None = None):
    body: dict = {} if reason is None else {"reason": reason}
    return client.post(f"/api/age/samples/{sample_id}/flag", json=body)


def _reopen(client: TestClient, sample_id: str):
    return client.post(f"/api/age/samples/{sample_id}/reopen")


def _ok(resp) -> None:
    assert 200 <= resp.status_code < 300, f"{resp.status_code}: {resp.text}"


def _db_row(query, sample_id: str) -> dict:
    """The sample straight from the store, for facts the API hides
    (stored_path) or that only SQL can pin (NULLed-out columns)."""
    rows = query(
        'SELECT status, age_days, annotated_by, annotated_at, flag_reason, '
        'stored_path, width, height, "bytes", uploaded_by, filename, sha256 '
        "FROM age_samples WHERE sample_id = ?",
        [sample_id],
    )
    assert len(rows) == 1, f"expected one row for {sample_id}, got {rows}"
    keys = ["status", "age_days", "annotated_by", "annotated_at", "flag_reason",
            "stored_path", "width", "height", "bytes", "uploaded_by",
            "filename", "sha256"]
    return dict(zip(keys, rows[0]))


@pytest.fixture
def trio(admin: TestClient) -> list[str]:
    """Three open samples, oldest first. Uploaded one request at a time so
    their `uploaded_at` stamps are strictly ordered — the queue tests below
    depend on "oldest" being unambiguous."""
    return [_upload_one(admin, seed) for seed in (1, 2, 3)]


# ------------------------------------------------------------------- upload
def test_admin_upload_lands_open_samples_with_true_dims_and_bytes(
    admin: TestClient, query, tmp_path: Path
):
    """Uploads are re-encoded like Blech frames: for an OPAQUE source the
    stored file is the JPEG derivative, and width/height/bytes describe THAT
    file — the pixels the annotator will actually judge — not the upload.
    (Sources carrying alpha store as PNG instead; pinned further down.)"""
    resp = _upload(admin, [("bee_a.png", bee_bytes(101)),
                           ("bee_b.png", bee_bytes(102))])
    assert resp.status_code == 200, resp.text

    rows = _samples(admin)
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "open"
        assert row["width"] == BEE_W and row["height"] == BEE_H
        assert row["uploaded_by"] == ADMIN_USER

        stored = _db_row(query, row["sample_id"])
        path = Path(stored["stored_path"])
        # Sandbox alarm first: a stored_path outside tmp_path means the route
        # wrote into the real data/ despite the sandboxed config.
        assert tmp_path.resolve() in path.resolve().parents, (
            f"age sample stored at {path}, outside the test sandbox"
        )
        assert path.suffix == ".jpg", (
            "an opaque source stores as JPEG, like Blech uploads"
        )
        assert path.is_file()
        assert stored["bytes"] == path.stat().st_size > 0
        assert row["bytes"] == stored["bytes"]


def test_poweruser_upload_is_403(poweruser: TestClient, admin: TestClient):
    """THE one per-tool permission divergence: Blech upload is open to any
    signed-in user, age upload is admin-only. Age samples come out of a curated
    instance-masking pipeline — a stray phone photo uploaded by a well-meaning
    poweruser is not a valid sample and no annotator downstream can tell.
    Roles stay global; the gate is on the route, not on a new role."""
    resp = _upload(poweruser, [("bee.png", bee_bytes(7))])
    assert resp.status_code == 403, resp.text
    assert _samples(admin) == [], "a refused upload must not land a row"


def test_age_routes_refuse_anonymous(client: TestClient):
    """The app-wide session gate covers /api/age like everything else; a new
    router mounted outside it would be a silently public endpoint."""
    assert client.get("/api/age/samples").status_code == 401
    assert client.get("/api/age/samples/next").status_code == 401
    assert client.get("/api/picker/examples").status_code == 401


def test_reupload_of_the_same_bytes_is_information_not_an_error(
    admin: TestClient,
):
    """Dedupe answers like the Blech uploader: HTTP 200 with the duplicate
    reported in a `duplicates` array (the shape the shared UploadCard already
    renders as an info line), and nothing written — an admin re-uploading a
    folder must learn which files were already in, not be shown a failure."""
    first = _upload(admin, [("bee.png", bee_bytes(50))])
    assert first.status_code == 200, first.text
    assert first.json()["duplicates"] == []
    existing = _samples(admin)[0]["sample_id"]

    again = _upload(admin, [("bee_copy.png", bee_bytes(50))])
    assert again.status_code == 200, (
        f"a duplicate upload must be information, not an error: {again.text}"
    )
    dupes = again.json()["duplicates"]
    assert len(dupes) == 1
    assert existing in str(dupes), (
        "the duplicate answer must name the existing sample so the uploader "
        "can find it"
    )
    assert len(_samples(admin)) == 1, "the re-upload forked the sample"


def test_upload_refuses_an_undecodable_payload(admin: TestClient):
    """Re-encoding implies decoding, so a corrupt file fails at upload time
    with a 400 the uploader can act on — not at annotation time in front of a
    poweruser who cannot fix it."""
    resp = _upload(admin, [("bee.png", b"not an image at all")])
    assert resp.status_code == 400, resp.text
    assert _samples(admin) == []


# ------------------------------------------------------------------ listing
def test_list_is_newest_first_and_filters_by_status(admin: TestClient):
    a = _upload_one(admin, 11)
    b = _upload_one(admin, 12)

    rows = _samples(admin)
    assert [r["sample_id"] for r in rows] == [b, a], "list must be newest first"

    _ok(_annotate(admin, b, 5))
    assert [r["sample_id"] for r in _samples(admin, status="open")] == [a]
    assert [r["sample_id"] for r in _samples(admin, status="done")] == [b]
    _ok(_flag(admin, a, "blurred"))
    assert [r["sample_id"] for r in _samples(admin, status="flagged")] == [a]
    assert _samples(admin, status="open") == []


def test_sample_rows_carry_no_filesystem_path(admin: TestClient):
    """House rule (api.py module docstring): nothing that leaves the API
    carries a filesystem path or the dedupe sha — browser payloads are shaped
    field by field. The frontend AgeSample type has neither, so leaking them
    would also make the published types dishonest."""
    _upload_one(admin, 21)
    for row in _samples(admin):
        assert "stored_path" not in row
        assert "sha256" not in row


# -------------------------------------------------------------------- queue
def test_next_serves_the_oldest_open_sample(admin: TestClient, trio: list[str]):
    oldest, middle, _ = trio
    got = _next(admin)
    assert got is not None and got["sample_id"] == oldest

    _ok(_annotate(admin, oldest, 3))
    got = _next(admin)
    assert got is not None and got["sample_id"] == middle


def test_next_skips_done_and_flagged(admin: TestClient, trio: list[str]):
    """Flagged samples leave the queue exactly like done ones — a sample that
    cannot be annotated (blur, multiple bees, not a bee) must not be re-served
    to every annotator forever."""
    a, b, c = trio
    _ok(_annotate(admin, a, 14))
    _ok(_flag(admin, b, "two bees"))
    got = _next(admin)
    assert got is not None and got["sample_id"] == c


def test_next_is_204_when_dry(admin: TestClient, trio: list[str]):
    """204, not 404: an empty queue is a success (everything judged), and the
    SPA shows a different screen for it than for a broken request."""
    a, b, c = trio
    _ok(_annotate(admin, a, 0))
    _ok(_flag(admin, b))
    _ok(_annotate(admin, c, 28))
    resp = admin.get("/api/age/samples/next")
    assert resp.status_code == 204, resp.text


# --------------------------------------------------------------------- file
def test_sample_file_serves_jpeg_with_immutable_caching(admin: TestClient):
    """Cached like crop files: the stored derivative never changes for a given
    sample_id, so the browser may keep it forever — but `private`, because the
    pixels are session-gated and must not land in a shared proxy."""
    sid = _upload_one(admin, 31)
    resp = admin.get(f"/api/age/samples/{sid}/file")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content[:2] == b"\xff\xd8"      # JPEG SOI
    assert "immutable" in resp.headers.get("cache-control", ""), (
        "sample files must carry the same immutable cache policy as crop files"
    )
    etag = resp.headers["etag"]

    again = admin.get(f"/api/age/samples/{sid}/file",
                      headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_unknown_sample_is_404_not_500(admin: TestClient):
    assert admin.get("/api/age/samples/no_such_sample/file").status_code == 404
    assert _annotate(admin, "no_such_sample", 5).status_code == 404
    assert _flag(admin, "no_such_sample").status_code == 404
    assert _reopen(admin, "no_such_sample").status_code == 404
    assert admin.delete("/api/age/samples/no_such_sample").status_code == 404


# ----------------------------------------------------------------- annotate
def test_annotate_accepts_the_whole_scale_including_the_censored_end(
    admin: TestClient, trio: list[str], query
):
    """0, a mid-scale day, and 28 all store verbatim. 28 is not "day 28": it is
    the right-censored bucket meaning four weeks OR OLDER, so refusing it — or
    clamping it to 27 — would delete the only honest label for every forager
    past the polyethism window."""
    for sid, age in zip(trio, (AGE_MIN, 11, AGE_MAX)):
        _ok(_annotate(admin, sid, age))
        row = _db_row(query, sid)
        assert row["status"] == "done"
        assert row["age_days"] == age
        assert row["annotated_by"] == ADMIN_USER
        assert row["annotated_at"] is not None


def test_annotate_rejects_out_of_range_days(admin: TestClient, query):
    """29 and -1 are 400s, and a refused annotation changes nothing: the
    sample stays open with no age and no attribution."""
    sid = _upload_one(admin, 41)
    for bad in (AGE_MAX + 1, -1):
        resp = _annotate(admin, sid, bad)
        assert resp.status_code == 400, (
            f"age_days={bad} must be a 400, got {resp.status_code}: {resp.text}"
        )
        row = _db_row(query, sid)
        assert row["status"] == "open"
        assert row["age_days"] is None
        assert row["annotated_by"] is None


def test_annotate_is_for_open_samples_only(admin: TestClient, query):
    """Single-annotator model: the first answer stands. Re-annotating a done
    sample, or annotating one that was flagged as unjudgeable, is a clean 4xx
    (409 or 400) that leaves the stored answer untouched."""
    done = _upload_one(admin, 42)
    flagged = _upload_one(admin, 43)
    _ok(_annotate(admin, done, 7))
    _ok(_flag(admin, flagged, "not a bee"))

    for sid in (done, flagged):
        resp = _annotate(admin, sid, 20)
        assert resp.status_code in (400, 409), (
            f"annotating a non-open sample must be a clean 4xx, got "
            f"{resp.status_code}: {resp.text}"
        )
    assert _db_row(query, done)["age_days"] == 7
    row = _db_row(query, flagged)
    assert row["status"] == "flagged" and row["age_days"] is None


def test_poweruser_may_annotate_flag_and_reopen(
    poweruser: TestClient, admin: TestClient, query
):
    """Upload is the only age route that diverges by role. Judging bees is
    exactly as open as labeling Blech crops — powerusers are the workforce."""
    a = _upload_one(admin, 44)
    b = _upload_one(admin, 45)

    _ok(_annotate(poweruser, a, 16))
    row = _db_row(query, a)
    assert row["status"] == "done"
    assert row["annotated_by"] == POWERUSER_USER, (
        "attribution must name the annotator, not the uploader"
    )

    _ok(_flag(poweruser, b, "wing torn off, no bee visible"))
    assert _db_row(query, b)["status"] == "flagged"

    _ok(_reopen(poweruser, a))
    _ok(_reopen(poweruser, b))
    assert _db_row(query, a)["status"] == "open"
    assert _db_row(query, b)["status"] == "open"


# -------------------------------------------------------------- flag/reopen
def test_flag_stores_the_reason_and_leaves_the_queue(
    admin: TestClient, query, trio: list[str]
):
    a, b, c = trio
    _ok(_flag(admin, a, "two bees in frame"))
    row = _db_row(query, a)
    assert row["status"] == "flagged"
    assert row["flag_reason"] == "two bees in frame"
    got = _next(admin)
    assert got is not None and got["sample_id"] == b

    # AgeHome shows flagged samples with their reason, so the list row must
    # carry it too — the frontend AgeSample type says `flag_reason`.
    listed = {r["sample_id"]: r for r in _samples(admin)}[a]
    assert listed["flag_reason"] == "two bees in frame"
    assert c  # trio unpacking kept honest


def test_flag_reason_is_optional(admin: TestClient, query):
    """The reason is one optional line — a poweruser facing an obvious blur
    should be one click away from the next sample, and an empty reason column
    stays NULL rather than an empty string a UI would render as a blank pill."""
    sid = _upload_one(admin, 51)
    _ok(_flag(admin, sid))
    row = _db_row(query, sid)
    assert row["status"] == "flagged"
    assert row["flag_reason"] in (None, ""), row["flag_reason"]


def test_reopen_clears_annotation_and_flag_state_and_requeues(
    admin: TestClient, query
):
    """Reopen is the undo for both exits. It must clear age, attribution AND
    flag reason: a reopened sample re-enters the queue as if never touched, and
    stale attribution would credit the eventual answer to the wrong person."""
    was_done = _upload_one(admin, 52)
    was_flagged = _upload_one(admin, 53)
    _ok(_annotate(admin, was_done, 22))
    _ok(_flag(admin, was_flagged, "blur"))

    _ok(_reopen(admin, was_done))
    row = _db_row(query, was_done)
    assert row["status"] == "open"
    assert row["age_days"] is None
    assert row["annotated_by"] is None
    assert row["annotated_at"] is None

    _ok(_reopen(admin, was_flagged))
    row = _db_row(query, was_flagged)
    assert row["status"] == "open"
    assert row["flag_reason"] is None

    # And both are servable again, oldest first.
    got = _next(admin)
    assert got is not None and got["sample_id"] == was_done


# ------------------------------------------------------------------- delete
def test_delete_is_admin_only_and_hard(
    admin: TestClient, poweruser: TestClient, query
):
    """The one hard delete in the age schema, mirroring image delete: pixels
    are re-obtainable from the masking pipeline, so admin cleanup outweighs
    soft-delete bookkeeping — but only for admins, and the file goes with the
    row so the store never accretes orphan JPEGs."""
    sid = _upload_one(admin, 61)
    stored = Path(_db_row(query, sid)["stored_path"])
    assert stored.is_file()

    refused = poweruser.delete(f"/api/age/samples/{sid}")
    assert refused.status_code == 403, refused.text
    assert stored.is_file(), "a refused delete must not remove the file"
    assert len(_samples(admin)) == 1

    _ok(admin.delete(f"/api/age/samples/{sid}"))
    assert query("SELECT count(*) FROM age_samples WHERE sample_id = ?", [sid]) \
        == [(0,)], "delete must remove the row, not flag it"
    assert not stored.exists(), "delete must remove the stored file too"
    assert _samples(admin) == []
    assert admin.get(f"/api/age/samples/{sid}/file").status_code == 404


# ------------------------------------------------------------------- export
def test_export_is_admin_only(poweruser: TestClient, admin: TestClient):
    _upload_one(admin, 71)
    _ok(_annotate(admin, _samples(admin)[0]["sample_id"], 4))
    resp = poweruser.get("/api/age/export")
    assert resp.status_code == 403, resp.text


def test_export_refuses_a_store_with_nothing_annotated(admin: TestClient):
    """No annotations means no dataset: a 400 with a sentence, not an empty
    zip a training script would happily "succeed" on. Flagged-only stores are
    equally empty — flags are refusals, not labels."""
    assert admin.get("/api/age/export").status_code == 400

    sid = _upload_one(admin, 72)
    _ok(_flag(admin, sid, "blur"))
    assert admin.get("/api/age/export").status_code == 400


def test_export_zip_is_annotated_samples_only_with_exact_csv_columns(
    admin: TestClient, poweruser: TestClient
):
    """The export IS the dataset: exactly the done samples' JPEGs plus a
    labels.csv in the pinned column order. Flagged samples are excluded — a
    flag means "no valid judgment exists", and a row for it would either carry
    a NULL age (crashes the loader) or a fake one (poisons the model)."""
    done_a = _upload_one(admin, 81, name="alpha.png")
    done_b = _upload_one(admin, 82, name="beta.png")
    flagged = _upload_one(admin, 83, name="gamma.png")
    open_one = _upload_one(admin, 84, name="delta.png")
    _ok(_annotate(admin, done_a, 3))
    _ok(_annotate(poweruser, done_b, AGE_MAX))
    _ok(_flag(admin, flagged, "two bees"))

    resp = admin.get("/api/age/export")
    assert resp.status_code == 200, resp.text
    assert "zip" in resp.headers.get("content-type", "")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
        assert names == sorted(
            [f"images/{done_a}.jpg", f"images/{done_b}.jpg", "labels.csv"]
        ), f"zip must hold exactly the annotated samples + labels.csv: {names}"
        assert flagged not in str(names) and open_one not in str(names)

        for member in names:
            if member.endswith(".jpg"):
                assert zf.read(member)[:2] == b"\xff\xd8", (
                    f"{member} is not the stored JPEG"
                )

        rows = list(csv.reader(io.StringIO(zf.read("labels.csv").decode("utf-8"))))

    assert rows[0] == CSV_COLUMNS, (
        f"labels.csv header must be exactly {CSV_COLUMNS}, got {rows[0]}"
    )
    by_id = {r[0]: dict(zip(CSV_COLUMNS, r)) for r in rows[1:]}
    assert set(by_id) == {done_a, done_b}
    assert by_id[done_a]["filename"] == "alpha.png"
    assert by_id[done_a]["age_days"] == "3"
    assert by_id[done_a]["annotated_by"] == ADMIN_USER
    assert by_id[done_b]["age_days"] == str(AGE_MAX)
    assert by_id[done_b]["annotated_by"] == POWERUSER_USER
    for row in by_id.values():
        assert row["annotated_at"], "annotated_at must be stamped in the csv"


# -------------------------------------------------------------------- stats
def _bucket_counts(hist: Any) -> dict[int, int]:
    """Normalize the histogram the way the frontend does: the AgeStats type
    allows an array indexed by bucket or an object keyed by bucket number."""
    if isinstance(hist, dict):
        return {int(k): int(v) for k, v in hist.items()}
    if isinstance(hist, list):
        return {i: int(v) for i, v in enumerate(hist)}
    raise AssertionError(f"histogram is neither array nor object: {hist!r}")


def test_stats_counts_and_week_buckets(admin: TestClient):
    """Counts drive the AgeHome tiles; the histogram counts ANNOTATED samples
    per week bucket 0..4 (bucket = age_days // 7, so bucket 4 is exactly the
    right-censored 28). Day 10 is used as the week-1 representative because it
    lands in bucket 1 under floor division AND rounding — the test must not
    depend on which of the two a sane implementation picked."""
    ids = [_upload_one(admin, 90 + i) for i in range(5)]
    _ok(_annotate(admin, ids[0], 0))       # bucket 0
    _ok(_annotate(admin, ids[1], 10))      # bucket 1
    _ok(_annotate(admin, ids[2], AGE_MAX)) # bucket 4, the censored end
    _ok(_flag(admin, ids[3], "blur"))      # excluded from the histogram
    # ids[4] stays open — excluded too

    resp = admin.get("/api/age/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5
    assert body["open"] == 1
    assert body["done"] == 3
    assert body["flagged"] == 1

    buckets = _bucket_counts(body["histogram"])
    assert buckets.get(0, 0) == 1
    assert buckets.get(1, 0) == 1
    assert buckets.get(4, 0) == 1, "day 28 must land in the censored 28+ bucket"
    assert sum(buckets.values()) == 3, (
        "the histogram counts annotated samples only — flagged and open "
        "samples have no age to bucket"
    )


# ------------------------------------------------------------------- picker
def test_picker_examples_is_null_safe_on_an_empty_store(admin: TestClient):
    """The picker is the post-login landing page; on a fresh install both
    tools are empty and the tiles fall back to text. `null`, not 404 and not a
    missing key — the SPA reads both fields unconditionally."""
    resp = admin.get("/api/picker/examples")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"blech": None, "age": None}


def test_picker_examples_returns_real_ids(
    poweruser: TestClient, admin: TestClient, image: dict
):
    """With data present, each tile gets an id it can actually render: a crop
    id the crop-image route serves and a sample id the sample-file route
    serves. WHICH crop or sample is representative is deliberately unpinned —
    the contract only promises a real, renderable id. Read as poweruser: the
    picker must work for every signed-in role."""
    sid = _upload_one(admin, 95)

    resp = poweruser.get("/api/picker/examples")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"blech", "age"}

    assert body["age"] == sid
    assert poweruser.get(f"/api/age/samples/{body['age']}/file").status_code == 200
    assert body["blech"] is not None
    assert poweruser.get(f"/api/crops/{body['blech']}/image").status_code == 200


# ------------------------------------------------- migration (pre-age store)
# `images` and `users` exactly as the last PRE-AGE build created them, frozen
# as text like test_db.py freezes its old shapes: the migration under test
# exists because stores without `age_samples` are on disk in production, and
# the test must keep producing that shape after db.py has moved on. Only the
# two row-carrying tables are seeded — init_db creating the REST of the schema
# around them is exactly the additive behavior under test.
PRE_AGE_IMAGES_DDL = """
    CREATE TABLE images (
        image_id     TEXT PRIMARY KEY,
        filename     TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        width        INTEGER NOT NULL,
        height       INTEGER NOT NULL,
        stored_path  TEXT NOT NULL,
        "bytes"      BIGINT NOT NULL,
        crop_size    INTEGER NOT NULL,
        crop_overlap DOUBLE  NOT NULL,
        uploaded_by  TEXT,
        uploaded_at  TIMESTAMP NOT NULL,
        note         TEXT
    );
"""

PRE_AGE_USERS_DDL = """
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'poweruser',
        created_at    TIMESTAMP NOT NULL DEFAULT now()
    );
"""

# Self-describing shape, not a real credential; byte-identity through the
# migration is what is asserted.
FROZEN_HASH = "scrypt$16384$8$1$" + "ab" * 16 + "$" + "cd" * 32

AGE_COLUMNS = {
    "sample_id", "filename", "sha256", "stored_path", "width", "height",
    "bytes", "uploaded_by", "uploaded_at", "status", "age_days",
    "annotated_by", "annotated_at", "flag_reason",
}


@pytest.fixture()
def mcon(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    """A brand-new DuckDB file under tmp_path — same substrate as test_db.py,
    so a failure reproduces with the CLI against the same file."""
    handle = duckdb.connect(str(tmp_path / "store.duckdb"))
    try:
        yield handle
    finally:
        handle.close()


def _seed_pre_age_store(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(PRE_AGE_IMAGES_DDL)
    con.execute(PRE_AGE_USERS_DDL)
    con.execute(
        "INSERT INTO images VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), ?)",
        ["img1", "frame.png", "a" * 64, 1920, 1280,
         "data/images/img1.jpg", 12345, 640, 0.0, "alice", None],
    )
    con.execute("INSERT INTO users VALUES ('alice', ?, 'poweruser', now())",
                [FROZEN_HASH])


def _schema_snapshot(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT table_name, column_name, data_type, is_nullable "
        "FROM information_schema.columns ORDER BY table_name, ordinal_position"
    ).fetchall()


def _insert_age_row(con: duckdb.DuckDBPyConnection, sid: str, *,
                    sha: str, age: int | None = None,
                    status: str = "open") -> None:
    con.execute(
        "INSERT INTO age_samples (sample_id, filename, sha256, stored_path, "
        'width, height, "bytes", uploaded_at, status, age_days) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?, ?)",
        [sid, f"{sid}.png", sha, f"data/age/{sid}.jpg", BEE_W, BEE_H, 999,
         status, age],
    )


def test_init_db_adds_age_samples_to_a_pre_age_store_with_data_intact(mcon):
    """The whole age table is the migration: a store the last pre-age build
    wrote gains `age_samples` from `init_db` ALONE, and the rows already
    there — the labeling hours the SPEC calls irreplaceable — ride through
    byte-identical."""
    _seed_pre_age_store(mcon)
    assert mcon.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name = 'age_samples'"
    ).fetchone()[0] == 0  # the pre-age shape is real

    db.init_db(mcon)

    cols = {
        r[0] for r in mcon.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'age_samples'"
        ).fetchall()
    }
    assert cols == AGE_COLUMNS, (
        f"age_samples columns diverge from the contract: {sorted(cols)}"
    )
    assert mcon.execute(
        "SELECT image_id, filename, \"bytes\" FROM images"
    ).fetchall() == [("img1", "frame.png", 12345)]
    assert mcon.execute(
        "SELECT username, password_hash, role FROM users"
    ).fetchall() == [("alice", FROZEN_HASH, "poweruser")]


def test_the_age_migration_replays_as_a_no_op(mcon):
    """init_db runs on every boot, so the second run against a just-migrated
    store IS the common case: same schema, same rows."""
    _seed_pre_age_store(mcon)
    db.init_db(mcon)
    _insert_age_row(mcon, "s1", sha="e" * 64, age=9, status="done")
    first = _schema_snapshot(mcon)
    rows = mcon.execute(
        "SELECT sample_id, age_days, status FROM age_samples"
    ).fetchall()

    db.init_db(mcon)

    assert _schema_snapshot(mcon) == first, "replaying init_db changed the schema"
    assert mcon.execute(
        "SELECT sample_id, age_days, status FROM age_samples"
    ).fetchall() == rows


def test_age_schema_enforces_range_bigint_and_dedupe(mcon):
    """The three DDL-level teeth of the contract, on a fresh store:

    - `age_days` CHECK 0..28 — the DB refuses an out-of-scale day even if some
      future writer forgets the API-level guard;
    - `"bytes"` is BIGINT for the same reason as images (A2): INTEGER is one
      config bump away from a silent overflow;
    - `sha256` UNIQUE — the dedupe answer rests on the constraint, not on a
      read-then-write race.
    """
    db.init_db(mcon)

    bytes_type = mcon.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'age_samples' AND column_name = 'bytes'"
    ).fetchone()[0]
    assert str(bytes_type).upper() == "BIGINT"

    _insert_age_row(mcon, "ok_low", sha="1" * 64, age=0, status="done")
    _insert_age_row(mcon, "ok_high", sha="2" * 64, age=28, status="done")
    with pytest.raises(duckdb.Error):
        _insert_age_row(mcon, "too_old", sha="3" * 64, age=29, status="done")
    with pytest.raises(duckdb.Error):
        _insert_age_row(mcon, "negative", sha="4" * 64, age=-1, status="done")
    with pytest.raises(duckdb.Error):
        _insert_age_row(mcon, "dupe", sha="1" * 64)  # same sha as ok_low
