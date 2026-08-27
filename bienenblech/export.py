"""YOLO11-seg dataset export: `done` crops -> a zip Ultralytics can train on.

The load-bearing rule is SPEC section 1, and it is the reason this module is so
short: **a crop is exported only when it is `status='done'`**. YOLO-seg training
reads every pixel of a training image that carries no polygon as an explicit
*background* teaching signal, so a half-labeled 640x640 tile does not merely
contribute less — it actively trains the model to suppress true positives. An
`open` crop is therefore omitted entirely rather than exported with whatever
polygons it happens to have so far. An `is_empty` crop is the opposite case and
IS exported, with a zero-byte label file: a reviewed tile that genuinely contains
nothing is a hard negative, which is worth real money in a segmentation dataset.

Two more things here are easy to "clean up" into bugs:

- **The train/val split is grouped by `image_id`, never by crop.** Tiles of one
  4000x3000 frame share lighting, bees, hive furniture and often overlap at the
  seams; two tiles of the same frame on opposite sides of the split is textbook
  leakage and turns the val metric into a flattering lie. `split_for_image` is
  keyed by image_id for that reason alone — read its docstring before refactoring
  it to "just hash the crop_id", which is the obvious-looking change.
- **`data.yaml` names are keyed by `yolo_index` and include archived classes.**
  An archived class keeps its reserved index (SPEC section 4), so a checkpoint
  trained against an older export still lines up with a newer one. Renumbering to
  close a gap silently relabels every prediction of every model already trained.

Coordinates: the DB stores polygon points in SOURCE-IMAGE pixels (SPEC section
3). This module is the only place that converts them to crop-normalized YOLO
coordinates, `(px - crop.x) / crop.w`, clamped to [0,1]. An instance clipped by a
tile edge is correct and expected, not a data error.

Memory: crop pixels come from `crops.render_crop` (disk-cached) and go into the
zip via `ZipFile.write`, which streams from disk. Nothing here ever holds the
dataset — one frame's worth of derivative JPEGs is already more than a small box
wants to buffer while an HTTP response is in flight.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

import duckdb

from . import __version__, crops, db
from .config import Config

# YOLO-seg has no notion of a degenerate polygon: a 2-point "polygon" is a line
# that Ultralytics either drops or turns into a NaN loss depending on version.
# Skip it and keep the rest of the crop rather than emit a malformed line that
# poisons a whole training run three hours in.
_MIN_VERTICES = 3

# 6 decimals on a 640 px tile is ~0.0006 px of quantisation — far below the
# precision an annotator's mouse has — and it keeps the label files small.
_COORD_FMT = "%.6f"

# JPEGs are already compressed; deflating them again costs CPU per crop for ~0%.
# The text members do compress, so those stay deflated.
_IMG_COMPRESS = zipfile.ZIP_STORED
_TXT_COMPRESS = zipfile.ZIP_DEFLATED

_TRAIN_CMD = "yolo segment train data=data.yaml model=yolo11n-seg.pt"

_MISSING = object()


class EmptyExport(ValueError):
    """No crop is `done`, so there is nothing legitimate to export.

    A hard refusal rather than an empty-but-valid zip on purpose: Ultralytics
    happily accepts a dataset with zero images and burns a full training run
    producing a checkpoint that predicts nothing. Subclasses ValueError so a
    caller that maps ValueError -> HTTP 400 does the right thing without having
    to import this name."""


# ------------------------------------------------------------------- row access

def _get(row: Any, key: str, default: Any = _MISSING) -> Any:
    """Read `key` off a DB row that may be a Mapping or an attribute object.

    SPEC section 6 freezes the *TypeScript* shapes; it does not freeze what
    `db.list_crops` hands back in Python, and this module was written in parallel
    with `db.py`. Supporting both shapes costs six lines and removes a whole class
    of "works on my branch" breakage. If `db.py` settles on one, this collapses to
    plain subscripting."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        pass
    if hasattr(row, key):
        return getattr(row, key)
    if default is not _MISSING:
        return default
    raise KeyError(f"row has no field {key!r} (row type {type(row).__name__})")


def _points(mask: Any) -> list[tuple[float, float]]:
    """Polygon vertices as (x, y) floats in SOURCE-IMAGE pixels.

    DuckDB's JSON type arrives as a string through some driver paths and as a
    parsed list through others, so both are accepted rather than guessed at."""
    raw = _get(mask, "points", None)
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    out: list[tuple[float, float]] = []
    for point in raw or []:
        try:
            x, y = point[0], point[1]
        except (TypeError, IndexError, KeyError):
            continue
        out.append((float(x), float(y)))
    return out


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


# ------------------------------------------------------------------- the split

def split_for_image(image_id: str, *, seed: int, val_fraction: float) -> str:
    """Deterministic train/val assignment for one image. Returns 'train' | 'val'.

    **Grouped by image_id on purpose.** Every crop of one frame lands in the same
    split. Splitting per crop would put two tiles of the same 4000x3000 frame —
    same hive, same light, same bees, often overlapping pixels at the seam — on
    opposite sides of the split. That is textbook leakage: the val loss drops, the
    metric stops measuring generalisation, and the model looks ready when it is
    not. Anyone refactoring this to hash the crop_id "because the crop is the unit
    of work" is reintroducing exactly that bug — the unit of work and the unit of
    splitting are deliberately different things.

    `sha256(f"{seed}:{image_id}")` rather than `random.seed` or `hash()`: it is
    stable across processes, Python versions and PYTHONHASHSEED, so re-exporting
    the same store with the same seed reproduces the same dataset, and adding new
    images never reshuffles the old ones out of their split."""
    digest = hashlib.sha256(f"{seed}:{image_id}".encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    return "val" if fraction < float(val_fraction) else "train"


# ------------------------------------------------------------------- label text

def label_lines(
    masks: Iterable[Any],
    crop: Any,
    class_index: Mapping[str, int],
) -> list[str]:
    """Render one crop's masks as YOLO-seg label lines.

    `<yolo_index> x1 y1 x2 y2 ... xn yn`, normalized to the crop rect
    (`(px - crop.x) / crop.w`) and clamped to [0,1]. The clamp is not defensive
    noise: SPEC section 3 clamps on write too, and an instance genuinely clipped
    by a tile edge must stay clipped rather than be dropped — a bee cut in half by
    a seam is still a bee, and dropping it would make the crop incomplete, which
    is the one thing the `done` invariant forbids.

    Polygons with fewer than 3 vertices are skipped rather than emitted; so are
    masks whose class is absent from `class_index` (an unknown class id cannot be
    given an index, and inventing one would mislabel every instance of it).
    Returns [] for a crop with no masks, which the caller writes as a 0-byte
    file."""
    crop_x = float(_get(crop, "x"))
    crop_y = float(_get(crop, "y"))
    crop_w = float(_get(crop, "w"))
    crop_h = float(_get(crop, "h"))
    if crop_w <= 0 or crop_h <= 0:
        return []
    lines: list[str] = []
    for mask in masks:
        class_id = _get(mask, "class_id", None)
        index = class_index.get(str(class_id)) if class_id is not None else None
        if index is None:
            continue
        points = _points(mask)
        if len(points) < _MIN_VERTICES:
            continue
        coords: list[float] = []
        for px, py in points:
            coords.append(_clamp01((px - crop_x) / crop_w))
            coords.append(_clamp01((py - crop_y) / crop_h))
        lines.append(f"{int(index)} " + " ".join(_COORD_FMT % v for v in coords))
    return lines


# ------------------------------------------------------------------- data.yaml

def _yaml_scalar(text: str) -> str:
    """Single-quoted YAML scalar. Class names are annotator-supplied free text and
    may contain ':' or '#', either of which silently changes the parse."""
    return "'" + str(text).replace("'", "''") + "'"


def _class_table(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, int], dict[int, str]]:
    """(class_id -> yolo_index, yolo_index -> name), **including archived classes**.

    An archived class keeps its reserved index forever (SPEC section 4), so its
    name has to stay in `data.yaml`: a checkpoint trained on last month's export
    predicts index 3, and index 3 must keep meaning what it meant then. A
    reserved-but-absent index is filled with a placeholder so `nc == len(names)`,
    which Ultralytics checks, and so a gap cannot shift every later class by
    one."""
    by_id: dict[str, int] = {}
    by_index: dict[int, str] = {}
    for row in db.list_classes(con, include_archived=True):
        by_id[str(_get(row, "class_id"))] = int(_get(row, "yolo_index"))
        by_index[int(_get(row, "yolo_index"))] = str(_get(row, "name"))
    if by_index:
        for index in range(max(by_index) + 1):
            by_index.setdefault(index, f"reserved_{index}")
    return by_id, by_index


def _data_yaml(names: Mapping[int, str]) -> str:
    """Ultralytics dataset descriptor, `names` in the dict form keyed by index.

    There is deliberately NO `path:` key (amendment A14). Ultralytics resolves a
    RELATIVE `path` against its own `settings['datasets_dir']` (`~/datasets`),
    NOT against the directory the yaml lives in — so `path: .` does not mean
    "this folder", it silently points training at whatever tree happens to sit
    under `~/datasets`. With the key omitted, Ultralytics defaults `path` to the
    yaml's own parent, which is exactly the unzipped export and exactly what a
    reader of `path: .` believed it already said. A14 forbids reintroducing the
    key in any form; an absolute path would merely break on the next machine."""
    lines = [
        "# YOLO11-seg dataset exported by bienenblech.",
        "# No 'path:' key on purpose (A14): Ultralytics resolves a RELATIVE path",
        "# against its own settings' datasets_dir (~/datasets), not against this",
        "# file's directory, so 'path: .' would point training at the wrong tree.",
        "# Omitted, 'path' defaults to this yaml's parent: the unzipped export.",
        "train: images/train",
        "val: images/val",
        "",
        f"nc: {len(names)}",
        "names:",
    ]
    lines += [f"  {index}: {_yaml_scalar(names[index])}" for index in sorted(names)]
    return "\n".join(lines) + "\n"


def _readme(counts: Mapping[str, Any], *, stamp: str, seed: int, val_fraction: float) -> str:
    per_class = counts.get("per_class") or {}
    per_class_txt = "\n".join(
        f"        {name:<24} {n}" for name, n in sorted(per_class.items())
    ) or "        (none)"
    return f"""bienenblech YOLO11-seg export
=============================

Created   {stamp} (UTC)
Exporter  bienenblech {__version__}
Split     grouped by image_id, seed={seed}, val_fraction={val_fraction:g}

Counts
    images contributing     {counts.get('n_images')}
    crops (all 'done')      {counts.get('n_crops')}
    crops train / val       {counts.get('n_train')} / {counts.get('n_val')}
    polygons                {counts.get('n_masks')}
    per class:
{per_class_txt}

Layout
    data.yaml
    images/train/<crop_id>.jpg     labels/train/<crop_id>.txt
    images/val/<crop_id>.jpg       labels/val/<crop_id>.txt

Label lines are `<yolo_index> x1 y1 ... xn yn`, normalized to the crop and
clamped to [0,1]. Class indices come from label_classes.yolo_index and include
archived classes, whose indices stay reserved so an older checkpoint keeps
matching this file.

The completeness invariant
--------------------------
Only crops marked `done` are in here. A crop is `done` only when EVERY instance
of every known class inside it has a polygon. That is not bookkeeping: YOLO-seg
treats any unlabeled instance in a training image as an explicit background
example, so one missed bee actively teaches the model to suppress true positives.
Crops still `open` were omitted entirely.

A zero-byte .txt is deliberate. Its crop was reviewed and marked empty, and it is
a hard negative — keep it. Do not "clean up" the label files with no content.

The train/val split is grouped by image_id, never by crop, so two tiles of one
frame can never straddle the split. If you re-split this dataset yourself, keep
that property or the val metric stops meaning anything.

Train
-----
    {_TRAIN_CMD}

(run from inside this directory after unzipping)
"""


# ------------------------------------------------------------------- the build

def build_yolo_zip(
    config: Config,
    con: duckdb.DuckDBPyConnection,
    *,
    val_fraction: float = 0.2,
    seed: int = 0,
    out_path: Path,
) -> dict[str, Any]:
    """Write the YOLO11-seg dataset zip at `out_path`; return the counts.

    `{n_images, n_crops, n_train, n_val, n_masks, per_class, bytes}`. `n_images`
    counts images that actually contributed a `done` crop, not every row in the
    store — it is the dataset's frame count, and a store full of unlabeled uploads
    must not read as a big dataset.

    Written to `<out_path>.part` and moved into place with `os.replace`, so a
    crash or a full disk never leaves a truncated file that looks like a dataset.
    Raises `EmptyExport` when no crop is `done`."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    class_index, names = _class_table(con)

    n_images = n_crops = n_train = n_val = n_masks = 0
    per_class: dict[str, int] = defaultdict(int)
    counts: dict[str, Any] = {}

    try:
        with zipfile.ZipFile(part, "w", _TXT_COMPRESS) as zf:
            zf.writestr("data.yaml", _data_yaml(names))
            for image_id in sorted(str(_get(r, "image_id")) for r in db.list_images(con)):
                crop_rows = [
                    c for c in db.list_crops(con, image_id)
                    if str(_get(c, "status", "open")) == "done"
                ]
                if not crop_rows:
                    # Every crop of this frame is still `open`, so the frame
                    # contributes nothing. Omitting it is the point: exporting a
                    # partially-labeled tile injects the exact unlabeled-instance
                    # poison the crop design exists to prevent.
                    continue
                image_row = db.get_image(con, image_id)
                split = split_for_image(image_id, seed=seed, val_fraction=val_fraction)
                # One mask query per image, not per crop: the crop grid of a
                # 4000x3000 frame is ~30 tiles and this runs inside a streamed
                # HTTP response.
                by_crop: dict[str, list[Any]] = defaultdict(list)
                for mask in db.list_masks(con, image_id=image_id):
                    if bool(_get(mask, "deleted", False)):
                        continue
                    by_crop[str(_get(mask, "crop_id"))].append(mask)
                n_images += 1
                for crop_row in sorted(
                    crop_rows,
                    key=lambda c: (int(_get(c, "row_idx", 0)), int(_get(c, "col_idx", 0))),
                ):
                    crop_id = str(_get(crop_row, "crop_id"))
                    # An `is_empty` crop falls out of here as [] and is written as
                    # a 0-byte file — no special case needed. If such a crop
                    # somehow still carries masks, its polygons are exported
                    # rather than dropped: dropping labeled instances is the poison
                    # this whole design exists to avoid, and a hard negative with
                    # bees in it would be worse than either.
                    lines = label_lines(by_crop.get(crop_id, ()), crop_row, class_index)
                    jpg = crops.render_crop(config, image_row, crop_row)
                    zf.write(jpg, f"images/{split}/{crop_id}.jpg", compress_type=_IMG_COMPRESS)
                    zf.writestr(
                        f"labels/{split}/{crop_id}.txt",
                        ("\n".join(lines) + "\n") if lines else "",
                        compress_type=_TXT_COMPRESS,
                    )
                    n_crops += 1
                    n_train += split == "train"
                    n_val += split == "val"
                    n_masks += len(lines)
                    for line in lines:
                        # Counted off what was actually emitted, so the README can
                        # never over-report instances that were skipped.
                        per_class[names.get(int(line.split(" ", 1)[0]), "?")] += 1
            if n_crops == 0:
                raise EmptyExport(
                    "nothing to export: no crop is marked 'done'. Only completed "
                    "crops may be exported (every instance in them labeled), so "
                    "exporting now would produce an empty dataset and waste a "
                    "training run."
                )
            counts = {
                "n_images": n_images, "n_crops": n_crops,
                "n_train": n_train, "n_val": n_val,
                "n_masks": n_masks, "per_class": dict(per_class),
            }
            zf.writestr(
                "README.txt",
                _readme(counts, stamp=stamp, seed=seed, val_fraction=val_fraction),
            )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, out)
    counts["bytes"] = out.stat().st_size
    return counts
