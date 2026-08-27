"""bienenblech — a minimal polygon-segmentation labeling tool.

Single camera, single frame in, fixed-size crops out. The crop is the unit of
work and the unit of training, because YOLO11-seg reads an unlabeled instance as
a background teaching signal and only a 640x640 tile can realistically be labeled
exhaustively in one sitting. See docs/SPEC.md section 1.
"""
from __future__ import annotations

# Surfaced by GET /api/health and stamped into every export/backup README, so a
# zip found on disk in a year can be traced back to the build that wrote it.
__version__ = "0.1.0"
