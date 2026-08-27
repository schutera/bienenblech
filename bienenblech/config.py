"""Typed configuration loaded from YAML (pydantic v2). Mirrors SPEC section 9.

Two rules govern everything in this module.

**Every field has a default and the whole file is optional.** `python -m
bienenblech.cli serve` on a bare checkout with no `config/` at all must come up
against `data/bienenblech.duckdb` and work. A labeling tool that refuses to boot
without a hand-written YAML is a tool nobody sets up on a Friday afternoon.

**No secrets live here.** `config/` is committed to the repo and bind-mounted
`:ro` into the container. The session secret, the bootstrap admin credentials and
the Discord webhook are read from `BIENENBLECH_SECRET`,
`BIENENBLECH_ADMIN_USER`/`BIENENBLECH_ADMIN_PASSWORD` and
`BIENENBLECH_DISCORD_WEBHOOK` *at the point of use*, never through a Config
field — a secret that can be read out of a config model eventually gets echoed
into a log line, an error payload or a backup zip.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List

import yaml
from pydantic import BaseModel, Field

# Where `load_config(None)` looks first. `bienenblech.yaml` is gitignored (it is
# the per-deployment copy); `bienenblech.example.yaml` is committed and is the
# documented set of defaults, so it is the second stop.
DEFAULT_CONFIG_PATH = "config/bienenblech.yaml"
EXAMPLE_CONFIG_PATH = "config/bienenblech.example.yaml"

# Escape hatch for the container, where the config lands somewhere other than the
# working directory. Only consulted when the caller passes no explicit path.
CONFIG_ENV_VAR = "BIENENBLECH_CONFIG"


class PathsCfg(BaseModel):
    """Everything the app writes lives under `data/`, which is the one bind mount.

    Keep it that way: the deploy story is a single `./data` volume, and a path
    that escapes it is state that survives no redeploy and lands in no backup."""

    db_path: str = "data/bienenblech.duckdb"
    images_dir: str = "data/images"      # the stored derivatives; masks refer to these
    cache_dir: str = "data/cache"        # rendered crop JPEGs; safe to delete, regenerated
    backups_dir: str = "data/backups"    # rotated backup zips


class UploadCfg(BaseModel):
    """Ingest of a full-resolution frame and the derivative we keep forever."""

    max_mb: int = 200                    # per file; the API answers 413 above this
    allowed: List[str] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"]
    )
    # Only "jpeg" is implemented: the DDL pins images.stored_path to
    # data/images/<image_id>.jpg and the crop cache is JPEG throughout. The field
    # exists so a future format is a config change rather than a grep.
    store_format: str = "jpeg"
    # High on purpose. This derivative IS the archival copy — the original upload
    # bytes are not kept — and every polygon in the DB is stored against its pixel
    # grid. Lowering it degrades the only pixels the dataset will ever have.
    store_quality: int = 92
    # Downscale only if the longer edge exceeds this. LOAD-BEARING: masks are
    # stored in the coordinate space of the DERIVATIVE, so raising or lowering
    # this after any image has been labeled silently misaligns every polygon on
    # every image tiled under the old value. Change it only on an empty store.
    max_edge: int = 8000


class CropCfg(BaseModel):
    """Tiling parameters. Frozen per image at upload time (images.crop_size /
    crop_overlap), so editing this file never re-tiles or invalidates old work —
    it only affects the next upload."""

    size: int = 640                      # YOLO-seg native tile; do not innovate here
    overlap: float = 0.0                 # fraction of `size`; 0 = clean partition
    # An edge tile narrower than this is shifted back to a full-size tile that
    # overlaps its neighbour, rather than emitted undersized: a 40x640 sliver is
    # unlabelable in practice and trains on letterbox padding.
    min_edge: int = 160
    jpeg_quality: int = 92               # the on-demand crop render under cache_dir


class AuthCfg(BaseModel):
    """Login gate. Only cookie policy lives here — the session secret and the
    bootstrap admin credentials come from the environment (see the module
    docstring). There is no `enabled` flag: this tool is never open."""

    # True the moment the app is behind the real domain. False on plain HTTP
    # would otherwise mean a Secure cookie the browser never sends back, i.e. a
    # login loop; True on plain HTTP means the same thing in reverse.
    https_only: bool = False
    session_days: int = 14

    @property
    def session_max_age(self) -> int:
        """Cookie lifetime in seconds, for SessionMiddleware(max_age=...)."""
        return self.session_days * 24 * 60 * 60


class BackupCfg(BaseModel):
    """Periodic zip of the store (DB + flat CSVs + the stored images), rotated
    locally and optionally posted to a Discord webhook.

    Enabled by default, unlike cownting's: here the zip carries the *pixels* as
    well as the labels, so a deployment with backups off has no recoverable copy
    of the labeling hours at all. An unset `BIENENBLECH_DISCORD_WEBHOOK` is a
    fully supported state — it still zips and still rotates locally."""

    enabled: bool = True
    interval_days: int = 7
    keep: int = 8                        # zips retained under paths.backups_dir
    # Discord's per-file cap is 8 MB unboosted. Above this the zip is still
    # written and rotated locally and only a text summary naming the path is
    # posted — a silently dropped backup is the failure mode to avoid.
    max_upload_mb: int = 8


class Config(BaseModel):
    project: str = "bienenblech"
    paths: PathsCfg = Field(default_factory=PathsCfg)
    upload: UploadCfg = Field(default_factory=UploadCfg)
    crop: CropCfg = Field(default_factory=CropCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)
    backup: BackupCfg = Field(default_factory=BackupCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Parse one YAML file. Raises if it is missing — an explicit path that
        does not exist is an operator error and must be loud, which is exactly
        why the search-and-fall-back logic lives in `load_config` instead."""
        with open(path, "r", encoding="utf-8") as f:
            data: Any = yaml.safe_load(f)
        if data is None:          # an empty (or comment-only) file means "defaults"
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping, got {type(data).__name__}")
        return cls(**data)


def load_config(path: str | Path | None = None) -> Config:
    """Load the config, falling back until something works.

    Order: the explicit `path` (must exist) -> `$BIENENBLECH_CONFIG` ->
    `config/bienenblech.yaml` -> `config/bienenblech.example.yaml` -> all
    defaults. The last two steps are what make the config file optional, so a
    fresh clone serves without any setup step at all.
    """
    if path is not None:
        return Config.load(path)

    env_path = (os.environ.get(CONFIG_ENV_VAR) or "").strip()
    if env_path:
        return Config.load(env_path)

    for candidate in (DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH):
        if Path(candidate).is_file():
            return Config.load(candidate)

    return Config()
