/**
 * The overview, and since the navigation collapsed, also the frames page.
 *
 * The former /images destination folded in here (its route redirects home), so
 * this page carries everything that page could do beyond listing: the per-frame
 * crop grid laid out on the real (row_idx, col_idx) — a map of the frame rather
 * than a list, so whoever is labeling can see which corner of the sheet is
 * still open and jump straight there — and the admin-only frame delete, the one
 * hard delete in the app, with a confirm that names the crops and polygons that
 * go with it.
 *
 * The Upload page folded in here too (its route also redirects home): a
 * compact upload card sits directly above the Frames list it feeds, so a new
 * frame appears in that list — and in the stat tiles — without a reload.
 */

import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { CropSummary, ImageSummary } from "../lib/types";
import {
  deleteImage,
  errorMessage,
  getImage,
  imageFileUrl,
  listImages,
  stats,
  uploadImages,
  type Stats,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorNote,
  Pill,
  ProgressBar,
  SectionLabel,
  Spinner,
} from "../components/ui";

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <div className="font-display text-4xl leading-none tabular-nums text-near-black">
        {value.toLocaleString()}
      </div>
      <div className="text-[13px] text-gray-mid mt-1.5">{label}</div>
    </div>
  );
}

function tileClass(c: CropSummary): string {
  if (c.status !== "done") {
    return "border-dashed border-border bg-surface text-gray-tertiary hover:border-accent hover:text-accent-deep";
  }
  if (c.is_empty) {
    return "border-border bg-surface-sunk text-gray-mid hover:border-accent";
  }
  return "border-accent/40 bg-accent-soft text-accent-deep hover:border-accent";
}

function CropGrid({ crops }: { crops: CropSummary[] }) {
  const navigate = useNavigate();
  const cols = crops.reduce((n, c) => Math.max(n, c.col_idx + 1), 1);
  const ordered = [...crops].sort((a, b) =>
    a.row_idx === b.row_idx ? a.col_idx - b.col_idx : a.row_idx - b.row_idx,
  );
  return (
    <div
      className="grid gap-1.5"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
    >
      {ordered.map((c) => (
        <button
          key={c.crop_id}
          type="button"
          onClick={() => navigate(`/label/${c.crop_id}`)}
          title={`Row ${c.row_idx + 1}, column ${c.col_idx + 1} — ${
            c.status === "done" ? (c.is_empty ? "done, empty" : "done") : "open"
          }, ${c.n_masks} polygon${c.n_masks === 1 ? "" : "s"}`}
          className={
            "aspect-square rounded-md border grid place-items-center font-mono text-[11px] tabular-nums transition-colors " +
            tileClass(c)
          }
        >
          {c.status === "done" && c.is_empty ? "-" : c.n_masks}
        </button>
      ))}
    </div>
  );
}

function GridLegend() {
  return (
    <div className="flex items-center gap-4 flex-wrap text-[11px] text-gray-tertiary">
      <span className="flex items-center gap-1.5">
        <span className="w-3 h-3 rounded-sm border border-dashed border-border bg-surface" />
        open
      </span>
      <span className="flex items-center gap-1.5">
        <span className="w-3 h-3 rounded-sm border border-accent/40 bg-accent-soft" />
        done
      </span>
      <span className="flex items-center gap-1.5">
        <span className="w-3 h-3 rounded-sm border border-border bg-surface-sunk" />
        done, empty
      </span>
    </div>
  );
}

const ACCEPT = ".jpg,.jpeg,.png,.tif,.tiff,.webp";

type ItemStatus = "queued" | "uploading" | "added" | "duplicate" | "failed";

type Item = {
  key: number;
  file: File;
  status: ItemStatus;
  progress: number;
  detail: string | null;
  image: ImageSummary | null;
};

const mb = (bytes: number) => `${(bytes / 1_048_576).toFixed(1)} MB`;

/** What is already on the server for a file uploaded before. Stated as progress,
 *  not as a rejection: the frame is there and some of it may already be done. */
function duplicateDetail(d: ImageSummary): string {
  return (
    `Already uploaded as ${d.filename} — ${d.n_crops} crops, ${d.n_done} done, ` +
    `${d.n_masks} polygons. Nothing was changed.`
  );
}

/**
 * The uploader, folded in from the retired Upload page. Open to every
 * signed-in account — the poweruser role exists so the people labeling can
 * also feed the queue (the server enforces this on POST /api/images).
 *
 * One file per request so each file gets its own honest progress bar — a
 * 200 MB frame behind a single aggregate percentage is exactly the case where
 * a stalled upload looks like a slow one.
 *
 * A re-upload of a file already on the server is NOT an error and is not shown
 * as one. The server dedupes on the sha256 of the original bytes and answers
 * with what is already there; saying "already uploaded, 24 crops, 6 done" is
 * more useful than a red 409, and it keeps someone from deleting and re-adding
 * a frame that already carries hours of labeling.
 *
 * The two-photos ask is made twice: as the standing hint under the drop zone,
 * and again after an upload lands — while the sheet is still in the uploader's
 * hands and cleaning it is the next thing they will do. A nudge that arrives
 * after the sheet is back in service costs another collection trip.
 */
function UploadCard({ onAdded }: { onAdded: (im: ImageSummary) => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const nextKey = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  function addFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    const added: Item[] = Array.from(list).map((file) => ({
      key: nextKey.current++,
      file,
      status: "queued" as ItemStatus,
      progress: 0,
      detail: null,
      image: null,
    }));
    setItems((cur) => [...cur, ...added]);
  }

  function patch(key: number, p: Partial<Item>) {
    setItems((cur) => cur.map((it) => (it.key === key ? { ...it, ...p } : it)));
  }

  function removeItem(key: number) {
    setItems((cur) => cur.filter((it) => it.key !== key));
  }

  async function uploadAll() {
    if (busy) return;
    setBusy(true);
    const queued = items.filter((it) => it.status === "queued" || it.status === "failed");
    for (const it of queued) {
      patch(it.key, { status: "uploading", progress: 0, detail: null });
      try {
        const res = await uploadImages([it.file], (f) => patch(it.key, { progress: f }));
        const image = res.images?.[0] ?? null;
        const dup = res.duplicates?.[0];
        if (image) {
          patch(it.key, {
            status: "added",
            progress: 1,
            image,
            detail: `${image.n_crops} crops of ${image.crop_size} px from a ${image.width}x${image.height} frame.`,
          });
          onAdded(image);
        } else if (dup) {
          patch(it.key, {
            status: "duplicate",
            progress: 1,
            detail: duplicateDetail(dup),
          });
        } else {
          patch(it.key, {
            status: "added",
            progress: 1,
            detail: "Uploaded. The server reported no new frame and no duplicate.",
          });
        }
      } catch (e) {
        patch(it.key, { status: "failed", progress: 0, detail: errorMessage(e) });
      }
    }
    setBusy(false);
  }

  const pending = items.filter((it) => it.status === "queued" || it.status === "failed").length;
  const added = items.filter((it) => it.status === "added");
  const newCrops = added.reduce((n, it) => n + (it.image?.n_crops ?? 0), 0);

  return (
    <Card className="p-6">
      <SectionLabel>Upload</SectionLabel>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (!busy) addFiles(e.dataTransfer.files);
        }}
        className={
          "mt-3 rounded-xl border-2 border-dashed px-4 py-6 text-center transition-colors " +
          (dragging ? "border-accent bg-accent-soft" : "border-border bg-surface")
        }
      >
        <p className="text-sm text-near-black">Drop frames here</p>
        <p className="text-[12px] text-gray-tertiary mt-1">
          JPEG, PNG, TIFF or WebP, up to 200 MB each. Any signed-in account.
        </p>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          className="hidden"
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <div className="mt-3">
          <Button variant="ghost" onClick={() => fileInput.current?.click()} disabled={busy}>
            Choose files
          </Button>
        </div>
      </div>

      <p className="text-[12px] text-gray-tertiary mt-3">
        Photograph each sheet twice: as collected, debris and all, then cleaned
        and empty — the empty one teaches the model that nothing is a valid
        answer.
      </p>

      {items.length > 0 ? (
        <ul className="mt-4 flex flex-col divide-y divide-border border-t border-border">
          {items.map((it) => (
            <li key={it.key} className="py-3">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="text-sm text-near-black truncate flex-1 min-w-0" title={it.file.name}>
                  {it.file.name}
                </span>
                <span className="font-mono text-[11px] text-gray-tertiary tabular-nums">
                  {mb(it.file.size)}
                </span>
                {it.status === "added" ? <Pill tone="accent">Added</Pill> : null}
                {it.status === "duplicate" ? <Pill>Already here</Pill> : null}
                {it.status === "failed" ? <Pill tone="danger">Not uploaded</Pill> : null}
                {it.status === "uploading" ? (
                  <span className="font-mono text-[11px] text-gray-mid tabular-nums">
                    {Math.round(it.progress * 100)}%
                  </span>
                ) : null}
                {it.status === "queued" && !busy ? (
                  <button
                    type="button"
                    onClick={() => removeItem(it.key)}
                    className="text-[13px] text-gray-tertiary hover:text-danger transition-colors"
                  >
                    Remove
                  </button>
                ) : null}
              </div>
              {it.status === "uploading" ? <ProgressBar value={it.progress} className="mt-2" height="h-1.5" /> : null}
              {it.detail ? (
                <p
                  className={
                    "text-[12px] mt-1.5 " + (it.status === "failed" ? "text-danger" : "text-gray-mid")
                  }
                >
                  {it.detail}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {added.length > 0 && !busy ? (
        <div className="mt-4 rounded-xl border border-accent/40 bg-accent-soft p-4">
          <SectionLabel>Before that sheet goes back in</SectionLabel>
          <p className="text-[13px] text-gray-mid mt-2">
            {added.length === 1
              ? "That sheet is in. Now clean it, photograph it empty, and upload that too — "
              : `Those ${added.length} sheets are in. Now clean them, photograph them empty, and upload those too — `}
            the half everyone forgets.
          </p>
        </div>
      ) : null}

      {items.length > 0 ? (
        <div className="mt-4 flex items-center gap-3 flex-wrap">
          <Button onClick={() => void uploadAll()} disabled={busy || pending === 0}>
            {busy ? "Uploading" : `Upload ${pending} frame${pending === 1 ? "" : "s"}`}
          </Button>
          {added.length > 0 && !busy ? (
            <span className="text-[12px] text-gray-tertiary">
              {added.length} frame{added.length === 1 ? "" : "s"} added, {newCrops} new crop
              {newCrops === 1 ? "" : "s"} in the queue.
            </span>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

export default function Home() {
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const [s, setS] = useState<Stats | null>(null);
  const [images, setImages] = useState<ImageSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [crops, setCrops] = useState<Record<string, CropSummary[]>>({});
  const [loadingCrops, setLoadingCrops] = useState(false);
  const [target, setTarget] = useState<ImageSummary | null>(null);
  const [deleting, setDeleting] = useState(false);

  // The class list comes from /api/stats: `per_class` carries whole class rows
  // with their live mask counts, so there is no second call and no join that
  // could disagree with the totals above it.
  useEffect(() => {
    let alive = true;
    Promise.all([stats(), listImages()])
      .then(([st, im]) => {
        if (!alive) return;
        setS(st);
        setImages(im);
      })
      .catch((e: unknown) => {
        if (alive) setError(errorMessage(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function toggle(im: ImageSummary) {
    if (openId === im.image_id) {
      setOpenId(null);
      return;
    }
    setOpenId(im.image_id);
    if (crops[im.image_id]) return;
    setLoadingCrops(true);
    try {
      const d = await getImage(im.image_id);
      setCrops((cur) => ({ ...cur, [im.image_id]: d.crops }));
      setImages((cur) => cur.map((x) => (x.image_id === im.image_id ? d.image : x)));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoadingCrops(false);
    }
  }

  function onUploaded(im: ImageSummary) {
    // The upload response carries the new frame's ImageSummary, so the list
    // grows without a re-fetch; the filter covers a frame re-uploaded after a
    // failure landing twice. The stat tiles count it too, so re-read them the
    // same way the delete path does.
    setImages((cur) => [im, ...cur.filter((x) => x.image_id !== im.image_id)]);
    stats()
      .then(setS)
      .catch(() => undefined);
  }

  async function confirmDelete() {
    if (!target) return;
    setDeleting(true);
    try {
      // The server refuses deleting a labeled frame without force, so force is
      // passed only after this confirm has named the masks that go with it.
      await deleteImage(target.image_id, target.n_masks > 0);
      setImages((cur) => cur.filter((x) => x.image_id !== target.image_id));
      if (openId === target.image_id) setOpenId(null);
      setTarget(null);
      // The stat tiles above the list count this frame too; re-read them so the
      // page does not disagree with itself after the delete.
      stats()
        .then(setS)
        .catch(() => undefined);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(false);
    }
  }

  const open = s ? Math.max(0, s.n_crops - s.n_done) : 0;
  const fraction = s && s.n_crops > 0 ? s.n_done / s.n_crops : 0;
  const classes = s?.per_class ?? [];
  const frames = [...images].sort((a, b) => (a.uploaded_at < b.uploaded_at ? 1 : -1));

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="max-w-xl">
          <SectionLabel>Overview</SectionLabel>
          <h1 className="font-display text-4xl font-light text-near-black leading-tight mt-1">
            Label every instance, one crop at a time
          </h1>
          <p className="text-sm text-gray-mid mt-2">
            Each frame is split into 640-pixel crops. The crop is the unit of
            work: small enough that labeling every instance is realistic, which
            keeps the training data honest.
          </p>
        </div>
        <div className="flex flex-col items-start gap-2">
          <Button size="lg" onClick={() => navigate("/label")}>
            Start labeling
          </Button>
          <span className="text-[12px] text-gray-tertiary">
            {loading
              ? "Loading the queue"
              : open > 0
                ? `${open.toLocaleString()} crop${open === 1 ? "" : "s"} still open`
                : "No open crops left"}
          </span>
        </div>
      </div>

      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      {loading ? (
        <Spinner label="Loading" />
      ) : (
        <>
          <Card className="p-6">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-6">
              <Stat value={s?.n_images ?? 0} label="frames uploaded" />
              <Stat value={s?.n_crops ?? 0} label="crops" />
              <Stat value={s?.n_done ?? 0} label="crops done" />
              <Stat value={s?.n_masks ?? 0} label="polygons drawn" />
            </div>
            <div className="mt-6">
              <div className="flex items-baseline justify-between text-[13px] text-gray-mid">
                <span>Exportable coverage</span>
                <span className="font-mono text-[12px] tabular-nums">
                  {Math.round(fraction * 100)}%
                </span>
              </div>
              <ProgressBar value={fraction} className="mt-2" />
              <p className="text-[12px] text-gray-tertiary mt-2">
                Only crops marked done are exported — never a half-labeled open
                one.
              </p>
            </div>
          </Card>

          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] items-start">
            <Card className="p-6">
              <div className="flex items-center justify-between gap-3">
                <SectionLabel>Classes</SectionLabel>
                {isAdmin ? (
                  <Link
                    to="/admin"
                    className="text-[13px] text-accent hover:text-accent-deep transition-colors"
                  >
                    Manage
                  </Link>
                ) : null}
              </div>
              {classes.length === 0 ? (
                <p className="text-[13px] text-gray-mid mt-3">
                  No classes yet — a polygon cannot be drawn without one.
                </p>
              ) : (
                <ul className="mt-4 flex flex-col gap-2.5">
                  {classes.map((c) => (
                    <li key={c.class_id} className="flex items-center gap-3">
                      {/* A class's own colour is data, not theming — the one
                          place a literal hex is legitimate. */}
                      <span
                        className="w-3.5 h-3.5 rounded-sm border border-border shrink-0"
                        style={{ backgroundColor: c.color }}
                        aria-hidden="true"
                      />
                      <span
                        className={
                          "text-sm truncate " + (c.archived ? "text-gray-tertiary" : "text-near-black")
                        }
                      >
                        {c.name}
                      </span>
                      {c.archived ? <Pill tone="warn">archived</Pill> : null}
                      <Pill mono className="ml-auto">
                        {c.n_masks.toLocaleString()}
                      </Pill>
                      <span className="font-mono text-[11px] text-gray-tertiary tabular-nums w-8 text-right">
                        #{c.yolo_index}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>

            {/* The uploader sits directly above the list it feeds. */}
            <div className="flex flex-col gap-6 min-w-0">
              <UploadCard onAdded={onUploaded} />
              <Card className="p-6">
                <SectionLabel>Frames</SectionLabel>
                {frames.length === 0 ? (
                  <EmptyState
                    className="mt-4"
                    title="No frames yet"
                    body="Drop the first frame in the uploader above; it is tiled into crops on arrival."
                  />
                ) : (
                  <ul className="mt-4 flex flex-col divide-y divide-border max-h-[34rem] overflow-y-auto pr-1">
                    {frames.map((im) => {
                      const f = im.n_crops > 0 ? im.n_done / im.n_crops : 0;
                      const isOpen = openId === im.image_id;
                      return (
                        <li key={im.image_id} className="py-3 first:pt-0 last:pb-0">
                          <div className="flex items-baseline justify-between gap-3">
                            <button
                              type="button"
                              onClick={() => void toggle(im)}
                              className="text-sm text-near-black truncate hover:text-accent-deep transition-colors text-left"
                              title={im.filename}
                            >
                              {im.filename}
                            </button>
                            <span className="font-mono text-[11px] text-gray-tertiary tabular-nums shrink-0">
                              {im.n_done}/{im.n_crops} done
                            </span>
                          </div>
                          <ProgressBar value={f} height="h-1.5" className="mt-2" />
                          <div className="flex items-center gap-3 mt-1.5 text-[11px] text-gray-tertiary flex-wrap">
                            <span>{im.n_masks.toLocaleString()} polygons</span>
                            <span>
                              {im.width}x{im.height} px, tiled at {im.crop_size} px
                              {im.crop_overlap > 0
                                ? ` with ${Math.round(im.crop_overlap * 100)}% overlap`
                                : ""}
                            </span>
                            <span>
                              {fmtDate(im.uploaded_at)}
                              {im.uploaded_by ? ` by ${im.uploaded_by}` : ""}
                            </span>
                            {im.n_done === im.n_crops && im.n_crops > 0 ? (
                              <Pill tone="accent">complete</Pill>
                            ) : null}
                            <span className="ml-auto flex items-center gap-1">
                              <button
                                type="button"
                                onClick={() => void toggle(im)}
                                className="text-[12px] text-gray-mid hover:text-accent-deep transition-colors px-2"
                              >
                                {isOpen ? "Hide crops" : "Show crops"}
                              </button>
                              {isAdmin ? (
                                <button
                                  type="button"
                                  onClick={() => setTarget(im)}
                                  className="text-[12px] text-gray-tertiary hover:text-danger transition-colors px-2"
                                >
                                  Delete
                                </button>
                              ) : null}
                            </span>
                          </div>
                          {im.note ? <p className="text-[12px] text-gray-mid mt-1">{im.note}</p> : null}
                          {isOpen ? (
                            <div className="mt-3 pt-3 border-t border-border flex flex-col gap-3">
                              <GridLegend />
                              <div className="grid gap-4 lg:grid-cols-[180px_minmax(0,1fr)] items-start">
                                <img
                                  src={imageFileUrl(im.image_id)}
                                  alt={im.filename}
                                  className="w-full rounded-xl border border-border bg-surface-sunk"
                                  loading="lazy"
                                />
                                {loadingCrops && !crops[im.image_id] ? (
                                  <Spinner label="Loading crops" />
                                ) : crops[im.image_id]?.length ? (
                                  <CropGrid crops={crops[im.image_id]} />
                                ) : (
                                  <p className="text-[13px] text-gray-mid">This frame has no crops.</p>
                                )}
                              </div>
                              <p className="text-[11px] text-gray-tertiary">
                                Tiles mirror the frame's tiling and show polygon
                                counts; a dash means marked empty. Click a tile to
                                label it.
                              </p>
                            </div>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </Card>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={target !== null}
        danger
        busy={deleting}
        confirmLabel="Delete the frame"
        title="Delete this frame?"
        body={
          target ? (
            <>
              <p>
                <span className="text-near-black">{target.filename}</span> goes for good,
                along with its {target.n_crops} crop{target.n_crops === 1 ? "" : "s"} and{" "}
                {target.n_masks} polygon{target.n_masks === 1 ? "" : "s"}
                {target.n_done > 0 ? `, including ${target.n_done} finished crop${target.n_done === 1 ? "" : "s"}` : ""}.
              </p>
              <p className="mt-2">
                The app's only hard delete — masks and classes are archived,
                frames are not. No undo.
              </p>
            </>
          ) : null
        }
        onCancel={() => setTarget(null)}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
