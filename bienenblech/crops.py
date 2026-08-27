"""Crop geometry: how a full frame becomes the fixed-size tiles that are the unit
of work, plus the single pair of functions that moves polygon points between the
source frame and a crop.

Two decisions in here are load-bearing and must not be quietly relaxed.

1.  **Every emitted tile is exactly `size x size`.** An edge tile is *shifted
    back* into the image (`x = width - size`) rather than *shrunk*. A shrunk
    edge tile would make the exported YOLO-seg dataset heterogeneous: the
    trainer letterboxes odd-sized images, which changes the effective object
    scale for exactly the tiles at the frame border, and the user's canvas
    would change size between crops for no reason the user can see. The
    price is that the last row/column overlaps its neighbour; overlap is
    harmless (a duplicated instance is still a correctly labeled instance),
    missing coverage is not.

2.  **The source <-> crop-local transform lives here and nowhere else.**
    SPEC section 3: the DB stores SOURCE-image pixels, the HTTP API transmits
    CROP-LOCAL pixels. Every place that offsets a point must call
    `to_crop_local` / `to_source` - never inline `p[0] - crop["x"]`. A polygon
    that reloads shifted by one crop origin is the single most likely bug in
    this codebase, and it is only cheap to find if there is exactly one pair of
    functions to look at.

The geometry half of this module is pure and does no I/O, so tiling is testable
without a database, a config file or a JPEG. Only `render_crop` touches disk.
"""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - core owns config.py; may land after this file
    from .config import Config

# A polygon needs three vertices to have an area. The ceiling is a denial-of-
# service guard, not a UI limit: a hand-drawn mask is tens of points, and a
# 10k-vertex polygon in a JSON column would make every crop load slow forever.
MIN_POINTS = 3
MAX_POINTS = 10_000

# Sanity ceiling on one frame's grid. With the shipped config (size 640,
# overlap 0, max_edge 8000) the worst case is 13x13 = 169 tiles, so this only
# ever fires on a misconfiguration - a small `crop.size` with a large
# `crop.overlap` turns an ordinary frame into millions of crop rows, which is a
# hung upload and a DB full of work no user will ever finish.
MAX_TILES = 10_000


def crop_id(image_id: str, row: int, col: int) -> str:
    """The deterministic crop id from SPEC section 4.

    Deterministic on purpose: re-tiling an image with the same parameters
    produces the same ids, so `crops` rows are idempotent and a cached crop JPEG
    on disk still belongs to the crop the DB is talking about.
    """
    return f"{image_id}_r{int(row)}c{int(col)}"


# --------------------------------------------------------------------- tiling
def _axis_starts(extent: int, size: int, stride: int, min_edge: int) -> list[int]:
    """Tile origins along one axis, left to right, covering `0..extent` totally.

    The last origin is always `extent - size` (the shift-back rule), so the
    returned tiles all have width `size` whenever `extent >= size`. When the
    image is smaller than one tile in this axis there is nothing to shift into,
    so a single origin at 0 is returned and the caller emits a tile of the full
    axis extent.

    `min_edge` only ever *removes* a tile, and only when removing it changes no
    pixel's coverage: with a large overlap the shifted-back final tile can
    swallow its neighbour whole, and emitting both would hand the user two
    near-identical crops to label. A tile is dropped only if the tiles either
    side of it still meet (`starts[i + 1] <= starts[i - 1] + size`), so coverage
    stays total by construction.
    """
    if extent <= size:
        return [0]

    last = extent - size
    starts: list[int] = []
    pos = 0
    while pos < last:
        starts.append(pos)
        pos += stride

    # `starts` is non-empty here: extent > size implies last >= 1 > 0 = pos.
    new_pixels = extent - (starts[-1] + size)   # what the final tile adds
    starts.append(last)
    if new_pixels < min_edge and len(starts) >= 3 and last <= starts[-3] + size:
        del starts[-2]
    return starts


def tile(width: int, height: int, *, size: int, overlap: float,
         min_edge: int) -> list[dict[str, int]]:
    """Tile a `width x height` frame into crop rects.

    Returns `{"row_idx", "col_idx", "x", "y", "w", "h"}` dicts in row-major
    order (row 0 left to right, then row 1, ...). Total and deterministic: every
    pixel of the frame is inside at least one returned rect, and the same
    arguments always give the same list.
    """
    width, height, size = int(width), int(height), int(size)
    if width < 1 or height < 1:
        raise ValueError(f"image must be at least 1x1 px, got {width}x{height}")
    if size < 1:
        raise ValueError(f"crop size must be >= 1, got {size}")
    overlap = float(overlap)
    if not 0.0 <= overlap < 1.0:
        raise ValueError(f"crop overlap must be in [0, 1), got {overlap}")

    # Guarded to >= 1: an overlap close to 1.0 would otherwise round the stride
    # down to 0 and hang the loop below.
    stride = max(1, int(round(size * (1.0 - overlap))))
    min_edge = max(0, int(min_edge))

    xs = _axis_starts(width, size, stride, min_edge)
    ys = _axis_starts(height, size, stride, min_edge)
    if len(xs) * len(ys) > MAX_TILES:
        raise ValueError(
            f"tiling {width}x{height} at size={size} overlap={overlap} would emit "
            f"{len(xs) * len(ys)} crops (max {MAX_TILES}) - check crop.size / crop.overlap"
        )
    w, h = min(size, width), min(size, height)

    return [
        {"row_idx": r, "col_idx": c, "x": x, "y": y, "w": w, "h": h}
        for r, y in enumerate(ys)
        for c, x in enumerate(xs)
    ]


# ------------------------------------------------------------------ rendering
def source_path(config: Config, image_row: Mapping[str, Any]) -> Path:
    """Where the stored derivative for `image_row` lives on disk.

    Prefers the `stored_path` the DB recorded (relative to the process CWD, as
    the shipped config writes it) and falls back to `images_dir/<image_id>.jpg`
    so a data directory that moved between hosts still resolves.
    """
    stored = image_row.get("stored_path")
    if stored:
        candidate = Path(str(stored))
        if candidate.exists():
            return candidate
    return Path(config.paths.images_dir) / f"{image_row['image_id']}.jpg"


def render_crop(config: Config, image_row: Mapping[str, Any],
                crop_row: Mapping[str, Any]) -> Path:
    """Render one crop to `cache_dir/crops/<crop_id>.jpg` and return its path.

    The cache is a pure derivative of (stored image, crop rect) and both are
    immutable once written, so a cached file at least as new as its source is
    served untouched. Written temp-file-then-`os.replace` because two users
    can open the same crop in the same instant: a half-written JPEG served to one
    of them would look like a corrupt image, not like a race.
    """
    from PIL import Image

    cid = str(crop_row["crop_id"])
    src = source_path(config, image_row)
    try:
        src_mtime = src.stat().st_mtime_ns
    except OSError as exc:
        raise FileNotFoundError(f"stored image missing: {src}") from exc

    out_dir = Path(config.paths.cache_dir) / "crops"
    out = out_dir / f"{cid}.jpg"
    try:
        if out.stat().st_mtime_ns >= src_mtime:
            return out
    except OSError:
        pass    # not cached yet

    x, y = int(crop_row["x"]), int(crop_row["y"])
    w, h = int(crop_row["w"]), int(crop_row["h"])
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(src) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")
        tile_im = im.crop((x, y, x + w, y + h))
        tile_im.load()      # force the lazy crop while the source file is open

    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{cid}.", suffix=".tmp")
    os.close(fd)
    try:
        tile_im.save(tmp, format="JPEG", quality=int(config.crop.jpeg_quality))
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


# ------------------------------------------------------- coordinate transform
def _pairs(points: Any) -> list[tuple[float, float]]:
    """Coerce `[[x, y], ...]` to float pairs, raising ValueError on anything else."""
    if isinstance(points, (str, bytes)) or not isinstance(points, Sequence):
        raise ValueError("points must be a list of [x, y] pairs")
    out: list[tuple[float, float]] = []
    for i, pt in enumerate(points):
        if isinstance(pt, (str, bytes)) or not isinstance(pt, Sequence) or len(pt) != 2:
            raise ValueError(f"vertex {i} must be an [x, y] pair")
        out.append((float(pt[0]), float(pt[1])))
    return out


def to_crop_local(points: Any, crop: Mapping[str, Any]) -> list[list[float]]:
    """SOURCE-image points -> CROP-LOCAL points (SPEC section 3, read direction)."""
    x, y = float(crop["x"]), float(crop["y"])
    return [[px - x, py - y] for px, py in _pairs(points)]


def to_source(points: Any, crop: Mapping[str, Any]) -> list[list[float]]:
    """CROP-LOCAL points -> SOURCE-image points (SPEC section 3, write direction).

    Every vertex is clamped into the crop rect first. An instance clipped by a
    tile edge is correct and expected for YOLO-seg training; a vertex a few
    pixels outside the canvas (the user dragged past the edge) must not be
    stored as if it described pixels of the neighbouring tile.

    Clamping is also what makes the round-trip *idempotent* rather than merely
    reversible: saving a polygon, reloading it and saving it again is a no-op,
    because the second save clamps an already-clamped polygon. `crop.x/y` are
    integers, so the add-then-subtract is exact in float64 and a reloaded
    polygon compares equal to the one that was sent.
    """
    x, y = float(crop["x"]), float(crop["y"])
    w, h = float(crop["w"]), float(crop["h"])
    out: list[list[float]] = []
    for px, py in _pairs(points):
        out.append([x + min(max(px, 0.0), w), y + min(max(py, 0.0), h)])
    return out


def validate_points(points: Any, crop: Mapping[str, Any]) -> list[list[float]]:
    """Check an incoming CROP-LOCAL polygon and return it as float pairs.

    Raises ValueError (which the API maps to 400) on: fewer than three vertices,
    more than `MAX_POINTS`, a vertex that is not a numeric pair, or a NaN/inf
    coordinate. NaN is called out explicitly because it survives a JSON
    round-trip in several clients and then poisons everything downstream
    silently - clamping, area and the exporter's normalisation all quietly
    yield NaN rather than failing.

    Self-intersecting polygons are accepted on purpose (SPEC section 3):
    users draw them and the exporter does not care.
    """
    if int(crop["w"]) <= 0 or int(crop["h"]) <= 0:
        raise ValueError("crop has no area")
    if isinstance(points, (str, bytes)) or not isinstance(points, Sequence):
        raise ValueError("points must be a list of [x, y] pairs")
    if len(points) < MIN_POINTS:
        raise ValueError(f"a polygon needs at least {MIN_POINTS} vertices, got {len(points)}")
    if len(points) > MAX_POINTS:
        raise ValueError(f"a polygon may have at most {MAX_POINTS} vertices, got {len(points)}")

    out: list[list[float]] = []
    for i, pt in enumerate(points):
        if isinstance(pt, (str, bytes)) or not isinstance(pt, Sequence) or len(pt) != 2:
            raise ValueError(f"vertex {i} must be an [x, y] pair")
        vals: list[float] = []
        for v in pt:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"vertex {i} is not numeric")
            if not math.isfinite(v):
                raise ValueError(f"vertex {i} is not finite")
            vals.append(float(v))
        out.append(vals)
    return out
