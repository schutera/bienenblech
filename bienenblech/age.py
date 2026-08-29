"""Age tool API: judge the age of one instance-masked honeybee per photo.

The second labeling tool behind the same login and the same store as Blech.
Everything here deliberately reads like the Blech routes in `api.py` — same
auth dependencies, same `_need`/`_file_etag` patterns, same store-then-insert
upload shape via `uploads`' helpers — because two tools with two dialects in
one codebase is how one of them rots. Three rules of its own:

*   **The scale is integer DAYS 0..28 and 28 is RIGHT-CENSORED** ("28+", four
    weeks or older). Appearance-based judgment is only meaningful across the
    temporal-polyethism window (cleaning 0-3d, nursing 4-12d, maintenance
    12-20d, foraging 21d+); see `db.AGE_MAX_DAYS` and the schema comment.
*   **Upload is ADMIN-ONLY**, unlike Blech's. The samples are curated,
    instance-masked bee photos produced by a pipeline, not sheets a poweruser
    photographed this morning — letting anyone feed the queue would fill it
    with frames the annotation contract (one bee, one age) does not hold for.
    Annotate/flag/reopen stay open to any signed-in user; delete and export
    are the admin's, like every other destructive or dataset-shaped action.
*   **A flagged sample never reaches the export.** A flag means annotation was
    impossible (blur, multiple bees, not a bee); exporting it with any age at
    all would poison the regression target, so the zip filters on
    `status='done'` and `db.flag_age_sample` clears the answer columns too.

Import direction: this module imports the auth deps and response helpers FROM
`api`, and `api.create_app` imports this module lazily inside the function
body. Adding `from . import age` to api.py's top-level imports would close
that loop into a genuine import cycle — do not.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import db, uploads
from .api import _file_etag, _need, current_user, require_admin
from .config import Config

# Stored sample images are already compressed — JPEG and PNG alike (same
# reasoning as export.py's constants); only labels.csv earns deflate.
_IMG_COMPRESS = zipfile.ZIP_STORED
_TXT_COMPRESS = zipfile.ZIP_DEFLATED


# --------------------------------------------------------------- request bodies
class AnnotateReq(BaseModel):
    # int, not a float the route rounds: a client sending 11.5 has a bug, and
    # silently flooring it would hide that bug inside the dataset.
    age_days: int


class FlagReq(BaseModel):
    reason: str | None = None


# ----------------------------------------------------------------------- layout
def age_dir(config: Config) -> Path:
    """`data/age` — where sample derivatives live, created at boot by
    `create_app`. Derived as a sibling of `images_dir` because `PathsCfg` is
    core's to edit and every path this app writes lives under the one `data/`
    bind mount by design; a sibling of the images keeps that true for any
    deployment that relocates the mount as a whole."""
    return Path(config.paths.images_dir).parent / "age"


def _sample_out(row: Mapping[str, Any]) -> dict:
    """A db age_samples row -> the browser-facing AgeSample shape (the binding
    TS type in frontend/src/lib/types.ts).

    Shaped field by field like `_crop_summary`: `stored_path` and `sha256` are
    server-side extras and stay server-side (A3). `bytes` is in the type — a
    file size is not reconnaissance, and AgeHome's upload card shows it."""
    return {
        "sample_id": row["sample_id"],
        "filename": row.get("filename"),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "bytes": int(row["bytes"]),
        "status": row.get("status") or "open",
        "age_days": int(row["age_days"]) if row.get("age_days") is not None else None,
        "uploaded_by": row.get("uploaded_by"),
        "uploaded_at": row.get("uploaded_at"),
        "annotated_by": row.get("annotated_by"),
        "annotated_at": row.get("annotated_at"),
        "flag_reason": row.get("flag_reason"),
    }


# ----------------------------------------------------------------------- export
def build_age_zip(
    config: Config, con: Any, *, out_path: Path
) -> dict[str, Any]:
    """Write the age dataset zip at `out_path`; return `{n_samples, bytes}`.

    `images/<sample_id>.<ext>` + `labels.csv` with one row per ANNOTATED
    sample (`sample_id, filename, age_days, annotated_by, annotated_at`).
    `<ext>` is the stored derivative's own suffix — .png for masked cutouts
    with alpha, .jpg for opaque sources — because the stored file goes into
    the zip verbatim and a lying extension would break every loader. Flagged
    samples are excluded by construction — the query filters on
    `status='done'` — because a flag means the age question had no answer, and
    a row in a regression dataset must have one. Open samples contribute
    nothing yet, so a store with no done sample raises ValueError (-> 400):
    an empty dataset is a request that cannot be honoured, not a server fault.

    Same crash discipline as export.build_yolo_zip: written to `.part` and
    moved into place with `os.replace`, so a full disk never leaves a
    truncated file that looks like a dataset."""
    rows = [
        r for r in db.list_age_samples(con, status="done")
        if r.get("age_days") is not None
    ]
    if not rows:
        raise ValueError(
            "nothing to export: no age sample is annotated yet. Flagged samples "
            "are excluded by design - they carry no answer."
        )
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", _TXT_COMPRESS) as zf:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                ["sample_id", "filename", "age_days", "annotated_by", "annotated_at"]
            )
            for row in sorted(rows, key=lambda r: str(r["sample_id"])):
                ext = Path(row["stored_path"]).suffix or ".jpg"
                zf.write(
                    row["stored_path"], f"images/{row['sample_id']}{ext}",
                    compress_type=_IMG_COMPRESS,
                )
                writer.writerow([
                    row["sample_id"], row.get("filename"), int(row["age_days"]),
                    row.get("annotated_by"), row.get("annotated_at"),
                ])
            zf.writestr("labels.csv", buf.getvalue(), compress_type=_TXT_COMPRESS)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)
    return {"n_samples": len(rows), "bytes": out.stat().st_size}


# ----------------------------------------------------------------------- routes
def create_router(config: Config) -> APIRouter:
    """Every /api/age route. Mounted by `api.create_app`, inside the app whose
    app-level `require_login` dependency already gates all of /api/*."""
    router = APIRouter(prefix="/api/age")

    def get_con():
        """A connection per request, closed when the response is done — its own
        tiny copy of api.py's get_con, because that one is a closure over a
        different Config inside create_app and closures do not export."""
        con = db.connect(config)
        try:
            yield con
        finally:
            con.close()

    @router.post("/samples")
    def upload_samples(file: list[UploadFile] = File(...),
                       user: dict = Depends(require_admin),
                       con: Any = Depends(get_con)):
        """Land one or more bee-photo samples. ADMIN-ONLY (module docstring says
        why; Blech's upload stays open to any signed-in user).

        A sync def like Blech's upload, so Starlette threadpools the Pillow
        re-encode off the event loop. Cheap checks (extension, size) run over
        the whole batch before anything is written. Dedupe is on the sha256 of
        the ORIGINAL bytes and is answered as information, not error — the
        `duplicates` list names what was already here, and nothing is changed.
        """
        if not file:
            raise HTTPException(400, "no files uploaded")
        limit = int(config.upload.max_mb) * 1024 * 1024
        allowed = {str(e).lower() for e in config.upload.allowed}
        for up in file:
            name = uploads.sanitise_filename(up.filename or "")
            if Path(name).suffix.lower() not in allowed:
                raise ValueError(
                    f"unsupported file type {name!r}; allowed: {', '.join(sorted(allowed))}"
                )
            if up.size is not None and up.size > limit:
                raise uploads.UploadTooLarge(
                    f"{name} is {up.size / 1e6:.1f} MB; the limit is {config.upload.max_mb} MB"
                )

        stored, duplicates = [], []
        for up in file:
            name = uploads.sanitise_filename(up.filename or "upload.jpg")
            data = up.file.read()
            if len(data) > limit:    # up.size is client-reported; re-check reality
                raise uploads.UploadTooLarge(
                    f"{name} is {len(data) / 1e6:.1f} MB; the limit is "
                    f"{config.upload.max_mb} MB"
                )
            if not data:
                raise ValueError(f"{name} is empty")
            sha = hashlib.sha256(data).hexdigest()
            existing = db.find_age_sample_by_sha(con, sha)
            if existing:
                duplicates.append(_sample_out(existing))
                continue
            sample_id = uuid.uuid4().hex
            # Same decode/EXIF/max_edge pipeline as Blech frames — imported,
            # never copied — but through the alpha-preserving writer: a masked
            # cutout's transparency IS the masking, so a source with alpha
            # stores as PNG (alpha intact) and only opaque sources store as
            # JPEG. `dest`'s suffix records which one happened.
            width, height, nbytes, dest = uploads.write_age_derivative(
                config, data, age_dir(config) / sample_id
            )
            try:
                row = db.insert_age_sample(
                    con,
                    sample_id=sample_id,
                    filename=name,
                    sha256=sha,
                    # Forward slashes for the same Windows/container reason as
                    # Blech's stored_path.
                    stored_path=dest.as_posix(),
                    width=width,
                    height=height,
                    bytes=nbytes,
                    uploaded_by=user["username"],
                )
            except BaseException:
                dest.unlink(missing_ok=True)    # no row -> no orphan file either
                raise
            stored.append(_sample_out(row))
        return {"samples": stored, "duplicates": duplicates}

    @router.get("/samples")
    def list_samples(status: str | None = None, con: Any = Depends(get_con)):
        return [_sample_out(r) for r in db.list_age_samples(con, status=status)]

    # Declared before /samples/{sample_id} routes for the same Starlette
    # declaration-order reason as /api/crops/next.
    @router.get("/samples/next")
    def next_sample(con: Any = Depends(get_con)):
        """The queue: oldest open sample. 204 when dry — an empty queue is a
        success, and the SPA shows a different screen for it than for an error."""
        row = db.next_open_age_sample(con)
        if not row:
            return Response(status_code=204)
        return _sample_out(row)

    @router.get("/samples/{sample_id}/file")
    def sample_file(sample_id: str, request: Request, con: Any = Depends(get_con)):
        """The sample's pixels. Cached exactly like crop files: the derivative
        at a given path is written once at upload and never rewritten, so the
        immutable ETag headers are honest."""
        row = _need(db.get_age_sample(con, sample_id), "age sample", sample_id)
        path = Path(row["stored_path"])
        if not path.exists():
            raise HTTPException(404, "stored sample missing")
        etag, headers = _file_etag(path)
        if etag in (request.headers.get("if-none-match") or ""):
            return Response(status_code=304, headers=headers)
        # The stored suffix names the actual format (uploads.write_age_derivative):
        # .png for masked cutouts with alpha, .jpg for opaque sources.
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(str(path), media_type=media, headers=headers)

    @router.get("/samples/{sample_id}")
    def get_sample(sample_id: str, con: Any = Depends(get_con)):
        return _sample_out(_need(db.get_age_sample(con, sample_id),
                                 "age sample", sample_id))

    @router.post("/samples/{sample_id}/annotate")
    def annotate_sample(sample_id: str, body: AnnotateReq,
                        user: dict = Depends(current_user),
                        con: Any = Depends(get_con)):
        """Store one answer; any signed-in user. Out-of-range age_days is a 400
        (db.annotate_age_sample raises ValueError before writing); a sample
        that is not open is a 409, because the fix is different — reopen it
        first — and the single-annotator model means a second answer silently
        overwriting the first is exactly the race this guard exists to refuse."""
        row = _need(db.get_age_sample(con, sample_id), "age sample", sample_id)
        if row["status"] != "open":
            raise HTTPException(
                409, f"sample is {row['status']}, not open - reopen it first"
            )
        return _sample_out(db.annotate_age_sample(
            con, sample_id, age_days=body.age_days, actor=user["username"]
        ))

    @router.post("/samples/{sample_id}/flag")
    def flag_sample(sample_id: str, body: FlagReq | None = None,
                    user: dict = Depends(current_user),
                    con: Any = Depends(get_con)):
        """Annotation impossible (blur, multiple bees, not a bee): out of the
        queue and out of the export. Allowed from any status, not just open —
        a reviewer who spots a bad sample already marked done must not need a
        reopen round-trip to say so."""
        _need(db.get_age_sample(con, sample_id), "age sample", sample_id)
        reason = (body.reason or "").strip() if body else ""
        return _sample_out(db.flag_age_sample(con, sample_id, reason=reason or None))

    @router.post("/samples/{sample_id}/reopen")
    def reopen_sample(sample_id: str, user: dict = Depends(current_user),
                      con: Any = Depends(get_con)):
        """Back into the queue, answer and flag cleared — the provenance columns
        must describe only the answer that currently stands."""
        _need(db.get_age_sample(con, sample_id), "age sample", sample_id)
        return _sample_out(db.reopen_age_sample(con, sample_id))

    @router.delete("/samples/{sample_id}", dependencies=[Depends(require_admin)])
    def delete_sample(sample_id: str, con: Any = Depends(get_con)):
        """Hard delete, file included. Admin-only: destructive, like deleting a
        Blech image — but no ?force= gate, because a sample carries at most one
        answer, not hours of polygons. Row first, then the file (at whatever
        suffix `stored_path` names), so a half-deleted sample stays deletable
        on a second attempt."""
        row = db.delete_age_sample(con, sample_id)
        Path(row["stored_path"]).unlink(missing_ok=True)
        return {"ok": True}

    @router.get("/stats")
    def stats(con: Any = Depends(get_con)):
        return db.age_stats(con)

    @router.get("/export", dependencies=[Depends(require_admin)])
    def export_zip(con: Any = Depends(get_con)):
        """Stream the age dataset zip. Built into a temp file (it carries every
        annotated sample's image) and unlinked by a BackgroundTask once the
        response is sent — same lifecycle as /api/export/yolo."""
        tmpdir = Path(tempfile.mkdtemp(prefix="bienenblech-age-export-"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = tmpdir / f"bienenblech-age-{stamp}.zip"
        try:
            build_age_zip(config, con, out_path=out)
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        return FileResponse(
            str(out), media_type="application/zip", filename=out.name,
            background=BackgroundTask(shutil.rmtree, str(tmpdir), ignore_errors=True),
        )

    return router
