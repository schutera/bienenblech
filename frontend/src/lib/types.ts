/**
 * The wire shapes, copied verbatim from SPEC section 6.
 *
 * This file is the contract between the frontend and `bienenblech/api.py`, and
 * nothing else in the app may redefine these shapes — a second, slightly
 * different `Mask` somewhere in a page is how the crop-local / source-image
 * coordinate distinction (SPEC section 3) quietly gets lost. Types that the SPEC
 * leaves unpinned (the `/api/stats` payload, the upload response, backup rows)
 * live in `api.ts` next to the call that returns them, so it stays obvious which
 * shapes are frozen and which are inferred from an underspecified endpoint.
 */

export type Role = "admin" | "annotator";
export type Me = { username: string; role: Role };

export type LabelClass = {
  class_id: string; name: string; color: string; yolo_index: number;
  description: string | null; archived: boolean; n_masks: number;
};

export type ImageSummary = {
  image_id: string; filename: string; width: number; height: number;
  crop_size: number; crop_overlap: number;
  n_crops: number; n_done: number; n_masks: number;
  uploaded_by: string | null; uploaded_at: string; note: string | null;
};

export type CropSummary = {
  crop_id: string; row_idx: number; col_idx: number;
  x: number; y: number; w: number; h: number;
  status: "open" | "done"; is_empty: boolean;
  n_masks: number; completed_by: string | null; completed_at: string | null;
};

/** Points are CROP-LOCAL pixels: [[x,y], ...] within 0..crop.w / 0..crop.h. */
export type Mask = {
  mask_id: string; crop_id: string; class_id: string;
  points: [number, number][];
  created_by: string | null; created_at: string; updated_at: string | null;
};

export type CropTask = {
  crop: CropSummary;
  image: { image_id: string; filename: string; width: number; height: number };
  masks: Mask[];
  /** position in this image's crop grid, for the "3 of 24" progress line */
  index: number; total: number;
  /**
   * Crops of this image already marked done — the progress bar's numerator,
   * against `total`. Amends SPEC section 6, which does not list it: without it
   * the page had to re-read `/api/images/{id}` after every completion just to
   * move one number, and every one of the four CropTask endpoints already knows
   * the figure.
   */
  n_done: number;
};
