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

/**
 * Amends SPEC sections 2 and 6: the non-admin role is now `poweruser` — same
 * rights plus upload. The SPEC keeps its original role name as the frozen
 * contract; a boot migration renames existing rows, so the old value never
 * reaches the browser. Still exactly two roles.
 */
export type Role = "admin" | "poweruser";
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

// ------------------------------------------------------------------------ age

/**
 * The AGE tool's sample row (`age_samples`) — the second labeling tool behind
 * the same login. Each sample is a photo of ONE instance-masked honeybee; the
 * annotation is a single age judgment. Single-annotator, like Blech crops:
 * one sample, one answer, done.
 *
 * `age_days` is a whole number of days, 0..28, and 28 is RIGHT-CENSORED —
 * displayed as "28+", meaning four weeks or older. Why the scale stops there:
 * summer workers average 15-38 days and winter bees live for months, but an
 * appearance-based judgment is only meaningful across the temporal-polyethism
 * window (cleaning 0-3 d, nursing 4-12 d, maintenance 12-20 d, foraging
 * 21 d+). Past four weeks, appearance stops separating ages, so a bigger
 * number would be false precision.
 */
export type AgeSampleStatus = "open" | "done" | "flagged";

export type AgeSample = {
  sample_id: string;
  filename: string;
  width: number;
  height: number;
  bytes: number;
  uploaded_by: string | null;
  uploaded_at: string;
  status: AgeSampleStatus;
  /** Set only while status is "done". */
  age_days: number | null;
  annotated_by: string | null;
  annotated_at: string | null;
  /** Set only while status is "flagged", and only when a reason was given. */
  flag_reason: string | null;
};

/**
 * `GET /api/age/stats`. The histogram counts ANNOTATED samples per week bucket
 * 0..4 — bucket 4 is the right-censored 28+. It may arrive as an array indexed
 * by bucket or as an object keyed by bucket number; pages normalize both.
 */
export type AgeStats = {
  total: number;
  open: number;
  done: number;
  flagged: number;
  histogram: number[] | Record<string, number>;
};
