/**
 * The one typed client over the HTTP surface in SPEC section 5.
 *
 * Three decisions worth stating, because breaking any of them causes a bug that
 * looks like something else entirely:
 *
 * 1. `credentials: "same-origin"`. FastAPI serves the built SPA from
 *    `frontend/dist` at `/`, and the dev server proxies `/api` (vite.config.ts),
 *    so the session cookie is ALWAYS same-origin. Using "include" would work
 *    too, but it would hide the day someone accidentally points this at another
 *    host — which would then need CORS credentials the backend does not set.
 * 2. Every failure becomes an `ApiError` carrying the HTTP status AND the
 *    server's `detail` string. The SPEC promises a real status and a real
 *    sentence for each refusal (400 bad polygon, 409 duplicate class name, 413
 *    oversized upload); pages show that sentence rather than inventing one.
 * 3. Polygon points cross this boundary in CROP-LOCAL pixels (SPEC section 3).
 *    The frontend never adds or subtracts `crop.x/crop.y` — the backend does,
 *    on both sides. There is deliberately no offset helper in this file.
 */

import type {
  AgeSample,
  AgeSampleStatus,
  AgeStats,
  CropSummary,
  CropTask,
  ImageSummary,
  LabelClass,
  Mask,
  Me,
  Role,
} from "./types";

const BASE = "/api";

/** A failed call. `status` is the HTTP code, `detail` the server's own sentence. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

/** The human sentence for any thrown value — pages render this and nothing else. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message;
  return String(e);
}

// A single place to learn the session went away: any call that comes back 401
// (expired cookie, server restart, signed out in another tab) fires this, and
// AuthProvider registers a handler that drops the app back to /login.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

async function detailOf(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      if (typeof d === "string" && d) return d;
      if (d) return JSON.stringify(d);
    }
  } catch {
    /* non-JSON error body (a proxy 502, an HTML error page) */
  }
  return `${res.status} ${res.statusText || "request failed"}`;
}

async function raw(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(BASE + path, { credentials: "same-origin", ...init });
  if (res.status === 401) {
    onUnauthorized?.();
    throw new ApiError(401, "Your session has expired. Sign in again.");
  }
  if (!res.ok) throw new ApiError(res.status, await detailOf(res));
  return res;
}

async function get<T>(path: string): Promise<T> {
  return (await raw(path)).json() as Promise<T>;
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const res = await raw(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return res.json() as Promise<T>;
}

async function sendVoid(path: string, method: string, body?: unknown): Promise<void> {
  await raw(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const q = encodeURIComponent;

// ---------------------------------------------------------------------- auth

export function login(username: string, password: string): Promise<Me> {
  return send<Me>("/login", "POST", { username, password });
}

export function logout(): Promise<void> {
  return sendVoid("/logout", "POST");
}

/** The "am I signed in?" probe. Throws ApiError(401) when nobody is. */
export function me(): Promise<Me> {
  return get<Me>("/me");
}

// --------------------------------------------------------------------- users

/**
 * A row from `GET /api/users`. SPEC section 6 pins no shape for it, so this
 * extends `Me` with the one extra column the `users` table carries (section 4).
 * `created_at` is optional because the HTTP section does not promise it.
 */
export type UserRow = Me & { created_at?: string | null };

export function listUsers(): Promise<UserRow[]> {
  return get<UserRow[]>("/users");
}

export function createUser(username: string, password: string, role: Role): Promise<UserRow> {
  return send<UserRow>("/users", "POST", { username, password, role });
}

export function deleteUser(username: string): Promise<void> {
  return sendVoid(`/users/${q(username)}`, "DELETE");
}

/** Admin, or the signed-in user changing their own password. */
export function setPassword(username: string, password: string): Promise<void> {
  return sendVoid(`/users/${q(username)}/password`, "POST", { password });
}

// -------------------------------------------------------------------- images

/**
 * The upload answer. `duplicates` carries the SAME shape as `images` — the
 * summary of what is already on the server for a file whose original bytes
 * hashed to something already stored. A re-upload is not an error and is not
 * reported as one: what the uploader needs to know is how far along that frame
 * already is.
 */
export type UploadResult = {
  images: ImageSummary[];
  duplicates: ImageSummary[];
};

/**
 * The multipart POST both uploaders (Blech frames, Age samples) ride on.
 *
 * XHR rather than fetch purely for `upload.onprogress`: fetch still cannot
 * report request-body progress in any browser we target, and a 200 MB frame
 * (config `upload.max_mb`) uploading with no feedback is indistinguishable from
 * a hang. `onProgress` receives 0..1 for THIS call, so the uploaders send one
 * file per call to get honest per-file bars.
 *
 * Cookies ride along by default on a same-origin XHR, which is what
 * `credentials: "same-origin"` means for the fetch paths above — so
 * `withCredentials` is deliberately left off.
 */
function xhrUpload<T>(
  path: string,
  form: FormData,
  onProgress: ((fraction: number) => void) | undefined,
  fallback: T,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", BASE + path);

    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (onProgress && e.lengthComputable && e.total > 0) {
        onProgress(Math.min(1, e.loaded / e.total));
      }
    };

    xhr.onload = () => {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(xhr.responseText) as unknown;
      } catch {
        /* non-JSON body */
      }
      if (xhr.status === 401) {
        onUnauthorized?.();
        reject(new ApiError(401, "Your session has expired. Sign in again."));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve((parsed ?? fallback) as T);
        return;
      }
      let detail = `${xhr.status} ${xhr.statusText || "upload failed"}`;
      if (parsed && typeof parsed === "object" && "detail" in parsed) {
        const d = (parsed as { detail: unknown }).detail;
        if (typeof d === "string" && d) detail = d;
      }
      reject(new ApiError(xhr.status, detail));
    };

    xhr.onerror = () =>
      reject(new ApiError(0, "The upload could not reach the server. Check the connection and try again."));
    xhr.onabort = () => reject(new ApiError(0, "Upload cancelled."));

    xhr.send(form);
  });
}

/**
 * Frame upload, one or more files under the repeatable field `file`.
 *
 * `isEmpty` sends the optional form field `is_empty=true`: the uploader asserts
 * every sheet in THIS call is clean, so its crops are born done (no polygons,
 * attributed to the uploader), never enter the queue, and export as negatives.
 * The flag applies to the whole request — one more reason for one file per
 * call. On a sha256 duplicate the flag is ignored; nothing is changed.
 */
export function uploadImages(
  files: File[],
  onProgress?: (fraction: number) => void,
  isEmpty = false,
): Promise<UploadResult> {
  const form = new FormData();
  for (const f of files) form.append("file", f, f.name);
  if (isEmpty) form.append("is_empty", "true");
  return xhrUpload("/images", form, onProgress, { images: [], duplicates: [] });
}

export function listImages(): Promise<ImageSummary[]> {
  return get<ImageSummary[]>("/images");
}

export type ImageDetail = { image: ImageSummary; crops: CropSummary[] };

export function getImage(imageId: string): Promise<ImageDetail> {
  return get<ImageDetail>(`/images/${q(imageId)}`);
}

/**
 * The one hard delete in the whole app (SPEC section 4). The server refuses
 * unless `force` when the image has masks, so callers pass force only after a
 * confirm that names how many masks are about to go.
 */
export function deleteImage(imageId: string, force = false): Promise<void> {
  return sendVoid(`/images/${q(imageId)}${force ? "?force=true" : ""}`, "DELETE");
}

// --------------------------------------------------------------------- crops

/**
 * The work queue: the oldest still-open crop, optionally restricted to one
 * image. `null` means there is nothing left to label — a 204, not an error.
 */
export async function nextCrop(imageId?: string): Promise<CropTask | null> {
  const res = await raw(`/crops/next${imageId ? `?image_id=${q(imageId)}` : ""}`);
  if (res.status === 204) return null;
  return (await res.json()) as CropTask;
}

export function getCrop(cropId: string): Promise<CropTask> {
  return get<CropTask>(`/crops/${q(cropId)}`);
}

/**
 * Marks a crop finished. `is_empty: true` asserts the crop genuinely contains no
 * instance of any known class — a negative training sample, not a skip. Leaving
 * a crop `open` is the skip, and it is why no skip button exists.
 */
export function completeCrop(cropId: string, body: { is_empty: boolean }): Promise<CropTask> {
  return send<CropTask>(`/crops/${q(cropId)}/complete`, "POST", body);
}

export function reopenCrop(cropId: string): Promise<CropTask> {
  return send<CropTask>(`/crops/${q(cropId)}/reopen`, "POST");
}

// ------------------------------------------------------------------- classes

export function listClasses(includeArchived = false): Promise<LabelClass[]> {
  return get<LabelClass[]>(`/classes${includeArchived ? "?include_archived=true" : ""}`);
}

export function createClass(body: {
  name: string;
  color?: string;
  description?: string;
}): Promise<LabelClass> {
  return send<LabelClass>("/classes", "POST", body);
}

export function updateClass(
  classId: string,
  patch: { name?: string; color?: string; description?: string },
): Promise<LabelClass> {
  return send<LabelClass>(`/classes/${q(classId)}`, "PATCH", patch);
}

/** Soft delete: the class comes back with `archived: true`, keeping its yolo_index. */
export function archiveClass(classId: string): Promise<LabelClass> {
  return send<LabelClass>(`/classes/${q(classId)}`, "DELETE");
}

/**
 * Un-archive a class (admin). The counterpart to archiveClass: the archive is a
 * soft delete, so a mis-click has to be undoable — the class's masks are still
 * there either way, and without this they would just be invisible work.
 */
export function restoreClass(classId: string): Promise<LabelClass> {
  return send<LabelClass>(`/classes/${q(classId)}/restore`, "POST");
}

// --------------------------------------------------------------------- masks

export function createMask(body: {
  crop_id: string;
  class_id: string;
  points: [number, number][];
}): Promise<Mask> {
  return send<Mask>("/masks", "POST", body);
}

export function updateMask(
  maskId: string,
  patch: { class_id?: string; points?: [number, number][] },
): Promise<Mask> {
  return send<Mask>(`/masks/${q(maskId)}`, "PATCH", patch);
}

export function deleteMask(maskId: string): Promise<void> {
  return sendVoid(`/masks/${q(maskId)}`, "DELETE");
}

// --------------------------------------------------------------------- stats

/**
 * `per_class` carries whole LabelClass rows, not bare counts, so a page can
 * render the list without a second call. It includes an archived class while it
 * still holds masks — those masks are still in the exports, so leaving the class
 * out would make the totals disagree with the sum of the rows.
 */
export type Stats = {
  n_images: number;
  n_crops: number;
  n_done: number;
  n_masks: number;
  per_class: LabelClass[];
};

export function stats(): Promise<Stats> {
  return get<Stats>("/stats");
}

// -------------------------------------------------------------------- backup

/** One row of `backup_runs` (SPEC section 4). Everything nullable: a `skipped`
 *  contention run writes no row at all, and a `failed` one has no counts. */
export type BackupRun = {
  run_id: string;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  trigger: string;
  n_masks: number | null;
  n_images: number | null;
  bytes: number | null;
  zip_path: string | null;
  delivered: boolean | null;
  /** Why it was or was not posted — "no webhook configured", "over the size cap,
   *  summary only", "the webhook refused it" are three different operator
   *  situations that `delivered` alone collapses into one false. */
  delivery: string | null;
  error: string | null;
  host: string | null;
};

export type BackupStatus = {
  last_run: BackupRun | null;
  next_due: string | null;
  enabled: boolean;
  runs: BackupRun[];
  /** Set when the probe itself could not read the store (contention, a missing
   *  module). The endpoint answers with data rather than a 500, on purpose. */
  error?: string | null;
  webhook_configured?: boolean;
  due_reason?: string | null;
  interval_days?: number | null;
};

export function backupStatus(): Promise<BackupStatus> {
  return get<BackupStatus>("/backup/status");
}

export function runBackup(): Promise<BackupRun> {
  return send<BackupRun>("/backup/run", "POST");
}

// ------------------------------------------------------------------ raw URLs
// Three URL builders rather than fetches: these are consumed by <img src> and by
// a download link, which carry the session cookie themselves.

export function exportYoloUrl(valFraction: number, seed: number): string {
  return `${BASE}/export/yolo?val_fraction=${encodeURIComponent(valFraction)}&seed=${encodeURIComponent(seed)}`;
}

export function cropImageUrl(cropId: string): string {
  return `${BASE}/crops/${q(cropId)}/image`;
}

export function imageFileUrl(imageId: string): string {
  return `${BASE}/images/${q(imageId)}/file`;
}

// ----------------------------------------------------------------------- age
// The Age tool's client, under /api/age. Same discipline as everything above.
// The admin-only surface (upload, delete, export) is server-enforced; the UI
// gates on `isAdmin` too, but that is a courtesy, not the lock.

/** Mirrors UploadResult: `duplicates` carries what is already on the server for
 *  a photo whose bytes hashed to something stored — information, not an error. */
export type UploadAgeResult = {
  samples: AgeSample[];
  duplicates: AgeSample[];
};

/**
 * Multipart POST under the repeatable field `file`. ADMIN-ONLY on the server —
 * the sample set is curated (one instance-masked bee per photo), not an open
 * drop box like Blech frames. One file per call from the UI, for the same
 * honest per-file bars as the frame uploader.
 */
export function uploadAgeSamples(
  files: File[],
  onProgress?: (fraction: number) => void,
): Promise<UploadAgeResult> {
  const form = new FormData();
  for (const f of files) form.append("file", f, f.name);
  return xhrUpload("/age/samples", form, onProgress, { samples: [], duplicates: [] });
}

/** Newest first; `status` narrows to one of open/done/flagged. */
export function listAgeSamples(status?: AgeSampleStatus): Promise<AgeSample[]> {
  return get<AgeSample[]>(`/age/samples${status ? `?status=${q(status)}` : ""}`);
}

/** The work queue: the oldest still-open sample. `null` is the 204 — queue dry. */
export async function nextAgeSample(): Promise<AgeSample | null> {
  const res = await raw("/age/samples/next");
  if (res.status === 204) return null;
  return (await res.json()) as AgeSample;
}

/** Whole days 0..28; 28 is right-censored ("28+"). The server refuses the rest,
 *  and refuses a sample that is not open — reopen first. */
export function annotateAge(sampleId: string, ageDays: number): Promise<AgeSample> {
  return send<AgeSample>(`/age/samples/${q(sampleId)}/annotate`, "POST", { age_days: ageDays });
}

/** Annotation impossible — blur, several bees, not a bee. The sample leaves the
 *  queue as `flagged` instead of sitting open forever or getting a guessed age. */
export function flagAgeSample(sampleId: string, reason?: string): Promise<AgeSample> {
  const r = reason?.trim();
  return send<AgeSample>(`/age/samples/${q(sampleId)}/flag`, "POST", r ? { reason: r } : {});
}

/** Back to open; the server clears age, flag and attribution. */
export function reopenAgeSample(sampleId: string): Promise<AgeSample> {
  return send<AgeSample>(`/age/samples/${q(sampleId)}/reopen`, "POST");
}

/** Hard delete, stored file included. ADMIN-ONLY on the server. */
export function deleteAgeSample(sampleId: string): Promise<void> {
  return sendVoid(`/age/samples/${q(sampleId)}`, "DELETE");
}

export function ageStats(): Promise<AgeStats> {
  return get<AgeStats>("/age/stats");
}

/**
 * One representative id per tool, so the picker tiles can show real data
 * instead of stock art. Either side is null while its tool is empty — the
 * picker falls back to a quiet named tile.
 */
export type PickerExamples = { blech: string | null; age: string | null };

export function pickerExamples(): Promise<PickerExamples> {
  return get<PickerExamples>("/picker/examples");
}

// URL builders like the crop/image ones above: consumed by <img src> and a
// download link, which carry the session cookie themselves.

export function ageSampleFileUrl(sampleId: string): string {
  return `${BASE}/age/samples/${q(sampleId)}/file`;
}

/** The export zip: images/<sample_id>.jpg + labels.csv, flagged excluded.
 *  ADMIN-ONLY; the server answers 400 while nothing is annotated. */
export function ageExportUrl(): string {
  return `${BASE}/age/export`;
}
