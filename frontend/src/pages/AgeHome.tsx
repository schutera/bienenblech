/**
 * The Age tool's overview: stats, week histogram, upload, sample list.
 *
 * Upload is ADMIN-ONLY, unlike Blech frames. Age samples are a curated set —
 * each photo must be exactly one instance-masked bee, produced upstream by the
 * segmentation pipeline — so the drop box is not open to every account. The
 * card is hidden from non-admins here AND refused by the server; the UI gate
 * is a courtesy, the server is the lock.
 *
 * Everything else on the page is for everyone: reopening a sample (a mistaken
 * age or flag must be cheap to undo, exactly like reopening a crop) and seeing
 * flagged samples with their reasons, so bad photos get culled by an admin
 * instead of silently rotting in a hidden state.
 */

import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { AgeSample, AgeSampleStatus, AgeStats } from "../lib/types";
import {
  ageSampleFileUrl,
  ageStats,
  deleteAgeSample,
  errorMessage,
  listAgeSamples,
  reopenAgeSample,
  uploadAgeSamples,
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

function fmtDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** 28 is right-censored: it means "four weeks or older", never exactly 28. */
function fmtAge(days: number | null): string {
  if (days === null) return "";
  return days >= 28 ? "28+ days" : `${days} day${days === 1 ? "" : "s"}`;
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

// ------------------------------------------------------------------ histogram

const BUCKET_LABELS = ["wk0 · 0-6 d", "wk1 · 7-13 d", "wk2 · 14-20 d", "wk3 · 21-27 d", "wk4 · 28+"];

/** The stats contract leaves the histogram's JSON shape loose (array indexed by
 *  bucket, or object keyed by bucket number); normalize both to five counts. */
function bucketCounts(h: AgeStats["histogram"] | undefined): number[] {
  const out = [0, 0, 0, 0, 0];
  if (!h) return out;
  if (Array.isArray(h)) {
    h.slice(0, 5).forEach((n, i) => {
      out[i] = Number(n) || 0;
    });
  } else {
    for (const [k, v] of Object.entries(h)) {
      const i = Number(k);
      if (Number.isInteger(i) && i >= 0 && i < 5) out[i] = Number(v) || 0;
    }
  }
  return out;
}

function WeekHistogram({ histogram }: { histogram: AgeStats["histogram"] }) {
  const counts = bucketCounts(histogram);
  const max = Math.max(1, ...counts);
  return (
    <div className="flex flex-col gap-1.5">
      {counts.map((n, i) => (
        <div key={i} className="grid grid-cols-[7.5rem_minmax(0,1fr)_2.5rem] items-center gap-3">
          <span className="font-mono text-[11px] text-gray-tertiary">{BUCKET_LABELS[i]}</span>
          <ProgressBar value={n / max} height="h-2" />
          <span className="font-mono text-[11px] text-gray-mid tabular-nums text-right">{n}</span>
        </div>
      ))}
    </div>
  );
}

// --------------------------------------------------------------------- upload

const ACCEPT = ".jpg,.jpeg,.png,.tif,.tiff,.webp";

type ItemStatus = "queued" | "uploading" | "added" | "duplicate" | "failed";

type Item = {
  key: number;
  file: File;
  status: ItemStatus;
  progress: number;
  detail: string | null;
  sample: AgeSample | null;
};

const mb = (bytes: number) => `${(bytes / 1_048_576).toFixed(1)} MB`;

/** A sha256 re-upload, stated as progress rather than a rejection — same rule
 *  as Blech frames: what matters is what the server already holds. */
function duplicateDetail(d: AgeSample): string {
  const state =
    d.status === "done"
      ? `already annotated ${fmtAge(d.age_days)}`
      : d.status === "flagged"
        ? "flagged"
        : "still open";
  return `Already uploaded as ${d.filename} — ${state}. Nothing was changed.`;
}

/**
 * The Blech UploadCard pattern, minus the empty-sheet flow (an age sample has
 * no negatives). One file per request for honest per-file bars. Rendered only
 * for admins; the server refuses everyone else anyway.
 */
function AgeUploadCard({ onAdded }: { onAdded: (s: AgeSample) => void }) {
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
      sample: null,
    }));
    setItems((cur) => [...cur, ...added]);
  }

  function patch(key: number, p: Partial<Item>) {
    setItems((cur) => cur.map((it) => (it.key === key ? { ...it, ...p } : it)));
  }

  async function uploadAll() {
    if (busy) return;
    setBusy(true);
    const queued = items.filter((it) => it.status === "queued" || it.status === "failed");
    for (const it of queued) {
      patch(it.key, { status: "uploading", progress: 0, detail: null });
      try {
        const res = await uploadAgeSamples([it.file], (f) => patch(it.key, { progress: f }));
        const sample = res.samples?.[0] ?? null;
        const dup = res.duplicates?.[0];
        if (sample) {
          patch(it.key, {
            status: "added",
            progress: 1,
            sample,
            detail: `${sample.width}x${sample.height} px, in the queue.`,
          });
          onAdded(sample);
        } else if (dup) {
          patch(it.key, { status: "duplicate", progress: 1, detail: duplicateDetail(dup) });
        } else {
          patch(it.key, {
            status: "added",
            progress: 1,
            detail: "Uploaded. The server reported no new sample and no duplicate.",
          });
        }
      } catch (e) {
        patch(it.key, { status: "failed", progress: 0, detail: errorMessage(e) });
      }
    }
    setBusy(false);
  }

  const pending = items.filter((it) => it.status === "queued" || it.status === "failed").length;
  const added = items.filter((it) => it.status === "added").length;

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
        <p className="text-sm text-near-black">Drop bee photos here</p>
        <p className="text-[12px] text-gray-tertiary mt-1">
          JPEG, PNG, TIFF or WebP. Admin only.
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
        One instance-masked bee per photo. A photo showing several bees, or none,
        gets flagged in the queue rather than labeled.
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
                    onClick={() => setItems((cur) => cur.filter((x) => x.key !== it.key))}
                    className="text-[13px] text-gray-tertiary hover:text-danger transition-colors"
                  >
                    Remove
                  </button>
                ) : null}
              </div>
              {it.status === "uploading" ? (
                <ProgressBar value={it.progress} className="mt-2" height="h-1.5" />
              ) : null}
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

      {items.length > 0 ? (
        <div className="mt-4 flex items-center gap-3 flex-wrap">
          <Button onClick={() => void uploadAll()} disabled={busy || pending === 0}>
            {busy ? "Uploading" : `Upload ${pending} photo${pending === 1 ? "" : "s"}`}
          </Button>
          {added > 0 && !busy ? (
            <span className="text-[12px] text-gray-tertiary">
              {added} sample{added === 1 ? "" : "s"} in the queue.
            </span>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

// ----------------------------------------------------------------------- page

type Filter = "all" | AgeSampleStatus;

export default function AgeHome() {
  const { isAdmin } = useAuth();
  const navigate = useNavigate();

  const [s, setS] = useState<AgeStats | null>(null);
  const [samples, setSamples] = useState<AgeSample[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [target, setTarget] = useState<AgeSample | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let alive = true;
    Promise.all([ageStats(), listAgeSamples()])
      .then(([st, sa]) => {
        if (!alive) return;
        setS(st);
        setSamples(sa);
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

  function refreshStats() {
    ageStats()
      .then(setS)
      .catch(() => undefined);
  }

  function onUploaded(sample: AgeSample) {
    // The upload response carries the new sample, so the list grows without a
    // re-fetch; the stat tiles count it too, so re-read them like the Blech
    // Overview does after its uploads.
    setSamples((cur) => [sample, ...cur.filter((x) => x.sample_id !== sample.sample_id)]);
    refreshStats();
  }

  async function reopen(sample: AgeSample) {
    // Optimistic: the row flips to open at once, and comes back on failure.
    const before = sample;
    setSamples((cur) =>
      cur.map((x) =>
        x.sample_id === sample.sample_id
          ? {
              ...x,
              status: "open" as const,
              age_days: null,
              annotated_by: null,
              annotated_at: null,
              flag_reason: null,
            }
          : x,
      ),
    );
    try {
      await reopenAgeSample(sample.sample_id);
      refreshStats();
    } catch (e) {
      setSamples((cur) => cur.map((x) => (x.sample_id === before.sample_id ? before : x)));
      setError(errorMessage(e));
    }
  }

  async function confirmDelete() {
    if (!target) return;
    setDeleting(true);
    try {
      await deleteAgeSample(target.sample_id);
      setSamples((cur) => cur.filter((x) => x.sample_id !== target.sample_id));
      setTarget(null);
      refreshStats();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(false);
    }
  }

  const openCount = s?.open ?? 0;
  const shown = filter === "all" ? samples : samples.filter((x) => x.status === filter);

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="max-w-xl">
          <SectionLabel>Age — overview</SectionLabel>
          <h1 className="font-display text-4xl font-light text-near-black leading-tight mt-1">
            Judge each bee&rsquo;s age from its photo
          </h1>
          <p className="text-sm text-gray-mid mt-2">
            Every sample is one instance-masked honeybee. The judgment is whole
            days, 0 to 28+ — 28+ means four weeks or older, past the window
            where appearance still separates ages.
          </p>
        </div>
        <div className="flex flex-col items-start gap-2">
          <Button size="lg" onClick={() => navigate("/age/label")}>
            Start labeling
          </Button>
          <span className="text-[12px] text-gray-tertiary">
            {loading
              ? "Loading the queue"
              : openCount > 0
                ? `${openCount.toLocaleString()} sample${openCount === 1 ? "" : "s"} still open`
                : "No open samples left"}
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
              <Stat value={s?.total ?? 0} label="samples" />
              <Stat value={s?.done ?? 0} label="annotated" />
              <Stat value={s?.flagged ?? 0} label="flagged" />
              <Stat value={s?.open ?? 0} label="open" />
            </div>
            {(s?.done ?? 0) > 0 ? (
              <div className="mt-6">
                <div className="flex items-baseline justify-between text-[13px] text-gray-mid">
                  <span>Ages by week</span>
                  <span className="text-[11px] text-gray-tertiary">annotated samples only</span>
                </div>
                <div className="mt-3">
                  <WeekHistogram histogram={s?.histogram ?? []} />
                </div>
              </div>
            ) : null}
          </Card>

          {isAdmin ? <AgeUploadCard onAdded={onUploaded} /> : null}

          <Card className="p-6">
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <SectionLabel>Samples</SectionLabel>
              <div className="flex items-center gap-1.5">
                {(["all", "open", "done", "flagged"] as const).map((f) => (
                  <button
                    key={f}
                    type="button"
                    onClick={() => setFilter(f)}
                    className={
                      "text-[12px] px-2.5 py-1 rounded-full border transition-colors " +
                      (filter === f
                        ? "border-accent text-accent-deep bg-accent-soft"
                        : "border-border text-gray-mid hover:border-accent hover:text-accent-deep")
                    }
                  >
                    {f}
                  </button>
                ))}
              </div>
            </div>

            {shown.length === 0 ? (
              <EmptyState
                className="mt-4"
                title={samples.length === 0 ? "No samples yet" : "Nothing with this status"}
                body={
                  samples.length === 0
                    ? isAdmin
                      ? "Drop the first bee photos in the uploader above."
                      : "An admin uploads the bee photos; the queue fills from there."
                    : undefined
                }
              />
            ) : (
              <ul className="mt-4 flex flex-col divide-y divide-border max-h-[34rem] overflow-y-auto pr-1">
                {shown.map((x) => (
                  <li key={x.sample_id} className="py-3 first:pt-0 last:pb-0">
                    <div className="flex items-center gap-3">
                      <img
                        src={ageSampleFileUrl(x.sample_id)}
                        alt={x.filename}
                        loading="lazy"
                        className="w-12 h-12 object-contain rounded-lg border border-border checkerboard-sm shrink-0"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-sm text-near-black truncate" title={x.filename}>
                            {x.filename}
                          </span>
                          {x.status === "done" ? (
                            <Pill tone="accent">{fmtAge(x.age_days)}</Pill>
                          ) : x.status === "flagged" ? (
                            <Pill tone="warn">flagged</Pill>
                          ) : (
                            <Pill>open</Pill>
                          )}
                        </div>
                        <p className="text-[11px] text-gray-tertiary mt-0.5 truncate">
                          {x.status === "done" && x.annotated_by
                            ? `by ${x.annotated_by}, ${fmtDate(x.annotated_at)}`
                            : `uploaded ${fmtDate(x.uploaded_at)}${x.uploaded_by ? ` by ${x.uploaded_by}` : ""}`}
                        </p>
                        {x.status === "flagged" ? (
                          <p className="text-[12px] text-warn mt-0.5 truncate">
                            {x.flag_reason ?? "no reason given"}
                          </p>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-1 shrink-0">
                        {x.status !== "open" ? (
                          <button
                            type="button"
                            onClick={() => void reopen(x)}
                            className="text-[12px] text-gray-mid hover:text-accent-deep transition-colors px-2"
                          >
                            Reopen
                          </button>
                        ) : null}
                        {isAdmin ? (
                          <button
                            type="button"
                            onClick={() => setTarget(x)}
                            className="text-[12px] text-gray-tertiary hover:text-danger transition-colors px-2"
                          >
                            Delete
                          </button>
                        ) : null}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </>
      )}

      <ConfirmDialog
        open={target !== null}
        danger
        busy={deleting}
        confirmLabel="Delete the sample"
        title="Delete this sample?"
        body={
          target ? (
            <p>
              <span className="text-near-black">{target.filename}</span> goes for good,
              photo included
              {target.status === "done" ? ", along with its age annotation" : ""}. No undo.
            </p>
          ) : null
        }
        onCancel={() => setTarget(null)}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}
