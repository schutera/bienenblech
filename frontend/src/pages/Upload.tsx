/**
 * Upload (admin). One frame at a time on the wire, several queued in the UI.
 *
 * Each file gets its own request so each file gets its own honest progress bar —
 * a 200 MB frame behind a single aggregate percentage is exactly the case where
 * a stalled upload looks like a slow one.
 *
 * A re-upload of a file already on the server is NOT an error and is not shown
 * as one. The server dedupes on the sha256 of the original bytes and answers
 * with what is already there; saying "already uploaded, 24 crops, 6 done" is
 * more useful than a red 409, and it keeps someone from deleting and re-adding
 * a frame that already carries hours of labeling.
 *
 * The page asks for the same sheet twice: once with debris on it, once clean.
 * The clean one is the case nobody thinks to photograph, because it looks like
 * a photograph of nothing — so the ask is made twice, once as standing
 * instruction above the drop zone and once as a prompt after an upload lands,
 * at the moment the sheet is still in the beekeeper's hands and cleaning it is
 * the next thing they will do. A nudge that arrives after the tray is back
 * under the hive is a nudge that costs another trip out to the hives.
 */

import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { ImageSummary } from "../lib/types";
import { errorMessage, uploadImages } from "../lib/api";
import { Button, Card, ErrorNote, Pill, ProgressBar, SectionLabel } from "../components/ui";

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

export default function Upload() {
  const navigate = useNavigate();
  const [items, setItems] = useState<Item[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
    setError(null);
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
    <div className="flex flex-col gap-6 max-w-3xl">
      <div>
        <SectionLabel>Upload</SectionLabel>
        <h1 className="font-display text-3xl font-light text-near-black leading-tight mt-1">
          Add frames
        </h1>
        <p className="text-sm text-gray-mid mt-2">
          Drop full-resolution frames here. The server stores each one and splits
          it into crops straight away — the crops are what you label.
        </p>
      </div>

      <Card className="p-5">
        <SectionLabel>Why the frame gets split</SectionLabel>
        <p className="text-[13px] text-gray-mid mt-2">
          Each frame is tiled into 640-pixel crops, the size YOLO11-seg trains on.
          That size is not a performance detail: segmentation training treats every
          unlabeled instance as background, so a half-labeled frame actively teaches
          the model to ignore real bees. Exhaustively labeling a 4000x3000 frame in
          one sitting is not realistic; exhaustively labeling one 640-pixel crop is.
          So the crop is the unit of work, and only crops marked done are exported.
        </p>
      </Card>

      <Card className="p-5">
        <SectionLabel>Photograph every sheet twice</SectionLabel>
        <p className="text-[13px] text-gray-mid mt-2">
          Once as it comes out from under the hive, debris and all. Then clean it
          and photograph it again, empty, before it goes back in — same sheet,
          same light, nothing on it.
        </p>
        <p className="text-[13px] text-gray-mid mt-2">
          The empty one is not a wasted upload. It is the frame where the right
          answer is nothing at all, and that is something the model has to be
          shown rather than told. Clean sheets are as welcome here as full ones.
        </p>
      </Card>

      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

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
          "rounded-2xl border-2 border-dashed px-6 py-12 text-center transition-colors " +
          (dragging ? "border-accent bg-accent-soft" : "border-border bg-surface")
        }
      >
        <p className="font-display text-2xl text-near-black leading-snug">
          Drop frames here
        </p>
        <p className="text-[13px] text-gray-mid mt-1.5">
          JPEG, PNG, TIFF or WebP, up to 200 MB each. Several at once is fine.
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
        <div className="mt-5">
          <Button variant="ghost" onClick={() => fileInput.current?.click()} disabled={busy}>
            Choose files
          </Button>
        </div>
      </div>

      {items.length > 0 ? (
        <div className="flex flex-col gap-2">
          <SectionLabel>
            {items.length} file{items.length === 1 ? "" : "s"}
          </SectionLabel>
          {items.map((it) => (
            <Card key={it.key} className="p-4">
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
              {it.status === "uploading" ? <ProgressBar value={it.progress} className="mt-2.5" height="h-1.5" /> : null}
              {it.detail ? (
                <p
                  className={
                    "text-[12px] mt-2 " + (it.status === "failed" ? "text-danger" : "text-gray-mid")
                  }
                >
                  {it.detail}
                </p>
              ) : null}
            </Card>
          ))}
        </div>
      ) : null}

      {added.length > 0 && !busy ? (
        <Card className="p-5">
          <SectionLabel>Before that sheet goes back in</SectionLabel>
          <p className="text-[13px] text-gray-mid mt-2">
            {added.length === 1
              ? "That sheet is in. Now clean it and photograph it empty, and upload that one too — "
              : `Those ${added.length} sheets are in. Now clean them and photograph them empty, and upload those too — `}
            it is the same job twice and the second half is the half everyone
            forgets.
          </p>
        </Card>
      ) : null}

      <div className="flex items-center gap-4 flex-wrap">
        <Button onClick={() => void uploadAll()} disabled={busy || pending === 0}>
          {busy ? "Uploading" : `Upload ${pending} frame${pending === 1 ? "" : "s"}`}
        </Button>
        {added.length > 0 && !busy ? (
          <>
            <Button variant="ghost" onClick={() => navigate("/label")}>
              Start labeling
            </Button>
            <span className="text-[13px] text-gray-tertiary">
              {added.length} frame{added.length === 1 ? "" : "s"} added, {newCrops} new crop
              {newCrops === 1 ? "" : "s"} in the queue.
            </span>
          </>
        ) : null}
      </div>
    </div>
  );
}
