"""Landing an uploaded frame: dedupe, derivative, tiling - in that order.

The whole module exists to make one thing true: **an image row and its crop rows
are created together or not at all.** A frame that lands with no crops is
invisible to the labeling queue and looks, to the user, exactly like a
frame nobody has started - which is why the insert and the tiling share one
transaction and the JPEG is written before it, where a failure is recoverable
(an orphan file, cleaned up on the error path) rather than a hole in the queue.

Two more rules that cost real work if they are broken:

*   **Dedupe is on the sha256 of the ORIGINAL bytes.** Re-uploading the same
    frame - the same card copied twice, a retried browser upload - must return
    the existing image, not fork the annotation work across two image ids where
    half the crops are done in each.
*   **Masks are stored against the DERIVATIVE**, i.e. against the JPEG this
    module writes at `upload.max_edge` / `upload.store_quality`, not against the
    bytes the user sent. Changing `max_edge` after data exists silently
    invalidates every polygon already drawn, because the pixel grid they were
    drawn on no longer exists. `max_edge` is a one-time decision. So is the
    downscale filter (LANCZOS): a different filter would move edges by a pixel
    or two, which is inside the tolerance users work at.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from . import crops

if TYPE_CHECKING:  # pragma: no cover - core owns config.py; may land after this file
    from .config import Config

# Long enough for any real camera filename, short enough that it can never hit a
# filesystem path limit once it is joined onto a data directory.
MAX_FILENAME = 200

# The stored filename is display-only (the derivative on disk is named by
# image_id), but it still ends up in exports, backups and log lines, so keep it
# to a charset that is safe in a path, a CSV cell and a shell.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")

# image_id is a uuid4 hex, but it arrives from a URL path before it is trusted;
# anything that could be a glob metacharacter or a path separator is refused
# before it is interpolated into a delete pattern.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class UploadTooLarge(ValueError):
    """Payload over `upload.max_mb`. A ValueError so callers that do not care
    about the distinction still handle it; the API maps it to 413 rather than
    400 because the client can act on it (send a smaller file)."""


def sanitise_filename(name: str) -> str:
    """Reduce a client-supplied filename to a safe display name."""
    raw = str(name or "")
    # Strip BOTH separators before taking the basename: a Windows browser sends
    # "C:\\Users\\mark\\sheet.jpg" and posixpath.basename would keep all of it.
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE.sub("_", raw).strip().strip(".")
    if not cleaned:
        return "upload"
    if len(cleaned) > MAX_FILENAME:
        stem, dot, ext = cleaned.rpartition(".")
        suffix = (dot + ext)[:16] if stem else ""
        cleaned = (stem or cleaned)[: MAX_FILENAME - len(suffix)] + suffix
    return cleaned


def _store_suffix(config: Config) -> str:
    fmt = str(getattr(config.upload, "store_format", "jpeg")).lower().lstrip(".")
    return ".jpg" if fmt in ("jpg", "jpeg") else f".{fmt}"


def _decode_upload(data: bytes):
    """Decode an uploaded payload and apply EXIF transpose. Returns a PIL image.

    EXIF transpose is applied *before* anything else: a phone frame stored
    without it would be labeled sideways relative to how it is displayed, and
    the rotation is not recoverable once the EXIF tag is dropped by the re-encode.
    """
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:      # noqa: BLE001 - Pillow raises a wide family here
        raise ValueError(f"not a readable image: {exc}") from exc
    return ImageOps.exif_transpose(im) or im


def _resize_to_max_edge(config: Config, im):
    """Downscale to `upload.max_edge` with LANCZOS — the two one-time decisions
    the module docstring pins. One implementation for every derivative writer,
    so the age tool can never drift from Blech frames on what "stored size"
    means."""
    from PIL import Image

    max_edge = int(getattr(config.upload, "max_edge", 0) or 0)
    if max_edge > 0 and max(im.size) > max_edge:
        scale = max_edge / float(max(im.size))
        im = im.resize(
            (max(1, round(im.width * scale)), max(1, round(im.height * scale))),
            Image.LANCZOS,
        )
    return im


def _save_atomic(im, dest: Path, **save_kwargs) -> None:
    """Encode into a sibling tempfile and `os.replace` into place, so a crash
    mid-write never leaves a half-encoded file at a path a DB row could name."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".", suffix=".tmp")
    os.close(fd)
    try:
        im.save(tmp, **save_kwargs)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_derivative(config: Config, data: bytes, dest: Path) -> tuple[int, int, int]:
    """Decode, normalise and store the archival Blech derivative. Returns
    (w, h, bytes). Blech frames are photographs of sticky sheets — opaque by
    nature — so RGB + JPEG at `store_quality` is always right here; age
    samples go through `write_age_derivative` below instead."""
    im = _decode_upload(data)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im = _resize_to_max_edge(config, im)
    _save_atomic(im, dest, format="JPEG", quality=int(config.upload.store_quality))
    return im.width, im.height, dest.stat().st_size


def _has_alpha(im) -> bool:
    """True when the decoded image carries transparency a JPEG would flatten:
    an alpha band (RGBA/LA/PA) or a palette transparency entry."""
    if im.mode in ("RGBA", "LA", "PA"):
        return True
    return im.mode == "P" and "transparency" in im.info


def write_age_derivative(
    config: Config, data: bytes, dest_stem: Path
) -> tuple[int, int, int, Path]:
    """Store an age-sample derivative at `dest_stem` + ".png"/".jpg". Returns
    (w, h, bytes, dest) — the caller records `dest`, whose suffix names the
    actual stored format.

    WHY a second writer: Blech frames are photographs, and `_write_derivative`'s
    JPEG is right for them. Age samples are instance-masked bee cutouts whose
    alpha channel IS the masking — flattening it re-attaches the background the
    masking removed, and the annotator would judge that background along with
    the bee. So a source carrying alpha (RGBA, LA, or P with a transparency
    entry) is stored as PNG with its alpha intact (canonicalised to RGBA); an
    opaque source stores as JPEG exactly like a Blech frame. Decode/EXIF and
    the max-edge/LANCZOS downscale are shared with `_write_derivative`, never
    forked — the two tools must not drift on what a stored derivative is.
    """
    im = _decode_upload(data)
    if _has_alpha(im):
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        im = _resize_to_max_edge(config, im)
        dest = dest_stem.with_suffix(".png")
        _save_atomic(im, dest, format="PNG")
    else:
        if im.mode != "RGB":
            im = im.convert("RGB")
        im = _resize_to_max_edge(config, im)
        dest = dest_stem.with_suffix(".jpg")
        _save_atomic(im, dest, format="JPEG",
                     quality=int(config.upload.store_quality))
    return im.width, im.height, dest.stat().st_size, dest


def store_upload(config: Config, con: Any, *, filename: str, data: bytes,
                 username: str | None, is_empty: bool = False) -> tuple[dict, bool]:
    """Land one uploaded frame. Returns `(image_row, is_duplicate)`.

    On a duplicate nothing at all is written - not the file, not a row - and the
    existing image is returned, so the caller can tell the user which frame it
    already was. That holds regardless of `is_empty`: the duplicate return sits
    above the point where the flag is consulted, so an empty assertion on a
    re-upload can never retro-complete an existing frame's crops - "nothing was
    changed" stays true.

    `is_empty` is the uploader asserting the sheet holds no instances: the frame
    is stored and tiled exactly like any other, but every crop is born finished
    (done + empty, attributed to `username`), inside the same transaction as the
    tiling - so the frame is never observable half-completed and its crops never
    enter the labeling queue. A mis-marked sheet stays recoverable per crop via
    the reopen flow.

    Raises ValueError (-> 400) for an unsupported extension or an undecodable
    payload and UploadTooLarge (-> 413) for an oversized one.
    """
    # core's db.py may land after this module; imported at call time so uploads
    # (and the pure geometry it leans on) still import cleanly on their own.
    from . import db

    name = sanitise_filename(filename)
    allowed = {str(e).lower() for e in config.upload.allowed}
    suffix = Path(name).suffix.lower()
    if suffix not in allowed:
        raise ValueError(
            f"unsupported file type {suffix or name!r}; allowed: {', '.join(sorted(allowed))}"
        )
    limit = int(config.upload.max_mb) * 1024 * 1024
    if len(data) > limit:
        raise UploadTooLarge(
            f"{name} is {len(data) / 1e6:.1f} MB; the limit is {config.upload.max_mb} MB"
        )
    if not data:
        raise ValueError(f"{name} is empty")

    sha = hashlib.sha256(data).hexdigest()
    existing = db.find_image_by_sha(con, sha)
    if existing:
        return existing, True

    image_id = uuid.uuid4().hex
    stored = Path(config.paths.images_dir) / f"{image_id}{_store_suffix(config)}"
    width, height, nbytes = _write_derivative(config, data, stored)

    size = int(config.crop.size)
    overlap = float(config.crop.overlap)
    rects = crops.tile(width, height, size=size, overlap=overlap,
                       min_edge=int(config.crop.min_edge))
    crop_rows = [
        {
            "crop_id": crops.crop_id(image_id, r["row_idx"], r["col_idx"]),
            "image_id": image_id,
            **r,
        }
        for r in rects
    ]

    # One transaction: an image with no crops is invisible to the queue, so a
    # crash between the two inserts must leave neither.
    con.execute("BEGIN TRANSACTION")
    try:
        db.insert_image(
            con,
            image_id=image_id,
            filename=name,
            sha256=sha,
            width=width,
            height=height,
            # Stored with forward slashes so a DB written on Windows still
            # resolves inside the Linux container that reads the same bind mount.
            stored_path=stored.as_posix(),
            bytes=nbytes,
            # Frozen at upload time (SPEC section 4): changing the config later
            # never re-tiles work that is already underway.
            crop_size=size,
            crop_overlap=overlap,
            uploaded_by=username,
            note=None,
        )
        db.insert_crops(con, crop_rows)
        if is_empty:
            # Fresh crop ids inside an uncommitted transaction: no mask can
            # reference them yet, so the empty-with-masks guard (SPEC section 1)
            # holds by construction - db.complete_empty_crops re-checks anyway.
            db.complete_empty_crops(con, image_id, actor=username)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        stored.unlink(missing_ok=True)
        raise

    row = db.get_image(con, image_id)
    return row, False


def remove_image_files(config: Config, image_id: str) -> None:
    """Delete the stored derivative and every cached crop of one image.

    Called after the DB row is gone (SPEC section 4: deleting an image is the one
    hard delete). Missing files are not an error - a half-deleted image must be
    fully deletable on a second attempt.
    """
    if not _ID_RE.match(str(image_id)):
        raise ValueError(f"invalid image id {image_id!r}")

    targets = list(Path(config.paths.images_dir).glob(f"{image_id}.*"))
    targets += list((Path(config.paths.cache_dir) / "crops").glob(f"{image_id}_r*c*.jpg"))
    for path in targets:
        try:
            path.unlink()
        except OSError:
            pass    # a locked or already-removed file must not block the delete


def image_summary(row: Mapping[str, Any]) -> dict:
    """The SPEC section 6 `ImageSummary` shape, straight from a db row."""
    return {
        "image_id": row["image_id"],
        "filename": row.get("filename"),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "crop_size": int(row["crop_size"]),
        "crop_overlap": float(row["crop_overlap"]),
        "n_crops": int(row.get("n_crops") or 0),
        "n_done": int(row.get("n_done") or 0),
        "n_masks": int(row.get("n_masks") or 0),
        "uploaded_by": row.get("uploaded_by"),
        "uploaded_at": row.get("uploaded_at"),
        "note": row.get("note"),
    }
