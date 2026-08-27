/**
 * Every uploaded frame, and inside each one its crop grid.
 *
 * The grid is laid out on the real (row_idx, col_idx) of each crop, so it is a
 * map of the frame rather than a list: an annotator can see which corner of the
 * sheet is still open and go straight there. Deleting a frame is the one hard
 * delete in the app, so the confirm names the crops and polygons that go with it.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { CropSummary, ImageSummary } from "../lib/types";
import {
  deleteImage,
  errorMessage,
  getImage,
  imageFileUrl,
  listImages,
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

export default function Images() {
  const { isAdmin } = useAuth();
  const [images, setImages] = useState<ImageSummary[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [crops, setCrops] = useState<Record<string, CropSummary[]>>({});
  const [loadingCrops, setLoadingCrops] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState<ImageSummary | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let alive = true;
    listImages()
      .then((im) => {
        if (alive) setImages(im);
      })
      .catch((e: unknown) => {
        if (alive) {
          setError(errorMessage(e));
          setImages([]);
        }
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
      setImages((cur) => (cur ?? []).map((x) => (x.image_id === im.image_id ? d.image : x)));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoadingCrops(false);
    }
  }

  async function confirmDelete() {
    if (!target) return;
    setDeleting(true);
    try {
      await deleteImage(target.image_id, target.n_masks > 0);
      setImages((cur) => (cur ?? []).filter((x) => x.image_id !== target.image_id));
      if (openId === target.image_id) setOpenId(null);
      setTarget(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <SectionLabel>Frames</SectionLabel>
        <h1 className="font-display text-3xl font-light text-near-black leading-tight mt-1">
          Uploaded frames
        </h1>
        <p className="text-sm text-gray-mid mt-2 max-w-2xl">
          Open a frame to see its crop grid laid out the way the frame is tiled.
          Each tile shows how many polygons it holds; a dash means the crop was
          marked empty. Click any tile to label it.
        </p>
      </div>

      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <div className="flex items-center gap-4 flex-wrap text-[12px] text-gray-tertiary">
        <span className="flex items-center gap-1.5">
          <span className="w-3.5 h-3.5 rounded-sm border border-dashed border-border bg-surface" />
          open
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3.5 h-3.5 rounded-sm border border-accent/40 bg-accent-soft" />
          done
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3.5 h-3.5 rounded-sm border border-border bg-surface-sunk" />
          done, empty
        </span>
      </div>

      {images === null ? (
        <Spinner label="Loading frames" />
      ) : images.length === 0 ? (
        <EmptyState
          title="No frames uploaded yet"
          body={
            isAdmin
              ? "Upload a frame and the server tiles it into crops immediately."
              : "An admin uploads the frames. Once one lands, its crops appear in the labeling queue."
          }
        />
      ) : (
        <div className="flex flex-col gap-3">
          {images.map((im) => {
            const f = im.n_crops > 0 ? im.n_done / im.n_crops : 0;
            const isOpen = openId === im.image_id;
            return (
              <Card key={im.image_id} className="p-4">
                <div className="flex items-start gap-4 flex-wrap">
                  <div className="flex-1 min-w-[220px]">
                    <div className="flex items-center gap-2.5 flex-wrap">
                      <span className="text-sm text-near-black break-all">{im.filename}</span>
                      {im.n_done === im.n_crops && im.n_crops > 0 ? (
                        <Pill tone="accent">complete</Pill>
                      ) : null}
                    </div>
                    <p className="text-[12px] text-gray-tertiary mt-1">
                      {im.width}x{im.height} px, tiled at {im.crop_size} px
                      {im.crop_overlap > 0 ? ` with ${Math.round(im.crop_overlap * 100)}% overlap` : ""} ·{" "}
                      uploaded {fmtDate(im.uploaded_at)}
                      {im.uploaded_by ? ` by ${im.uploaded_by}` : ""}
                    </p>
                    {im.note ? <p className="text-[12px] text-gray-mid mt-1">{im.note}</p> : null}
                  </div>
                  <div className="w-full sm:w-56">
                    <div className="flex items-baseline justify-between text-[12px] text-gray-mid">
                      <span className="font-mono tabular-nums">
                        {im.n_done}/{im.n_crops} done
                      </span>
                      <span className="font-mono tabular-nums">{im.n_masks} polygons</span>
                    </div>
                    <ProgressBar value={f} className="mt-1.5" height="h-1.5" />
                  </div>
                  <div className="flex items-center gap-2">
                    <Button variant="ghost" onClick={() => void toggle(im)}>
                      {isOpen ? "Hide crops" : "Show crops"}
                    </Button>
                    {isAdmin ? (
                      <button
                        type="button"
                        onClick={() => setTarget(im)}
                        className="text-[13px] text-gray-tertiary hover:text-danger transition-colors px-2"
                      >
                        Delete
                      </button>
                    ) : null}
                  </div>
                </div>

                {isOpen ? (
                  <div className="mt-4 pt-4 border-t border-border grid gap-4 lg:grid-cols-[220px_minmax(0,1fr)] items-start">
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
                ) : null}
              </Card>
            );
          })}
        </div>
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
                This is the only hard delete in the app: masks and classes are
                archived, an image is not. It cannot be undone.
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
