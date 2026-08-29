/**
 * The Age labeling screen: one instance-masked honeybee, one judgment.
 *
 * Two rules here are product requirements rather than taste:
 *
 * 1. Next stays DISABLED until the slider has been touched. The thumb has to
 *    start somewhere, and whatever that value is, saving it untouched would
 *    silently record the same number for every sample where the annotator
 *    forgot to look — a spike in the label distribution that no model could
 *    tell apart from real data. Requiring a touch makes every saved value a
 *    decision. The thumb parks mid-scale so the untouched position at least
 *    reads as "not judged yet" rather than "newly emerged".
 * 2. Flagging is a first-class exit, not a failure. A blurred photo, several
 *    bees, or no bee at all cannot be judged; a flag takes the sample out of
 *    the queue with its reason instead of leaving it open forever or feeding
 *    the dataset a guess.
 *
 * Keyboard: arrows nudge the slider (which also counts as touching it),
 * Enter saves and advances once Next is enabled.
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { AgeSample } from "../lib/types";
import {
  ageSampleFileUrl,
  annotateAge,
  errorMessage,
  flagAgeSample,
  nextAgeSample,
} from "../lib/api";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  INPUT_CLS,
  Kbd,
  Pill,
  SectionLabel,
  Spinner,
} from "../components/ui";

/** Mid-scale start; never recorded untouched (see the file comment). */
const DEFAULT_DAYS = 14;

/** The slider's tick positions: wk0..wk4 at days 0, 7, 14, 21, 28. */
const WEEK_MARKS = [0, 7, 14, 21, 28];

/** "17 days (week 2)" — or "28+ days": 28 is right-censored, four weeks or older. */
function readout(days: number): string {
  return days >= 28 ? "28+ days" : `${days} day${days === 1 ? "" : "s"} (week ${Math.floor(days / 7)})`;
}

/** The polyethism band the chosen value falls in — what the number means, shown
 *  so the annotator can sanity-check the judgment, not a suggestion. */
function band(days: number): string {
  if (days <= 3) return "cleaning age";
  if (days <= 12) return "nursing age";
  if (days <= 20) return "maintenance age";
  return "foraging age";
}

/** Keys must not fire while the user is typing a flag reason. The range input
 *  is NOT a typing target: arrows there are handled natively, Enter is ours. */
function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  if (el instanceof HTMLInputElement && el.type === "range") return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
}

export default function AgeLabel() {
  const navigate = useNavigate();

  const [sample, setSample] = useState<AgeSample | null>(null);
  const [ageDays, setAgeDays] = useState(DEFAULT_DAYS);
  const [touched, setTouched] = useState(false);
  const [flagOpen, setFlagOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [noneLeft, setNoneLeft] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const adopt = useCallback((s: AgeSample | null) => {
    setSample(s);
    setNoneLeft(s === null);
    setAgeDays(DEFAULT_DAYS);
    setTouched(false);
    setFlagOpen(false);
    setReason("");
  }, []);

  useEffect(() => {
    let alive = true;
    nextAgeSample()
      .then((s) => {
        if (alive) adopt(s);
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
  }, [adopt]);

  const canNext = sample !== null && touched && !busy;

  const nudge = useCallback((d: number) => {
    setAgeDays((v) => Math.min(28, Math.max(0, v + d)));
    setTouched(true);
  }, []);

  const saveAndNext = useCallback(async () => {
    if (!sample || !touched || busy) return;
    setBusy(true);
    setError(null);
    try {
      await annotateAge(sample.sample_id, ageDays);
      adopt(await nextAgeSample());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [sample, touched, busy, ageDays, adopt]);

  const flagAndNext = useCallback(async () => {
    if (!sample || busy) return;
    setBusy(true);
    setError(null);
    try {
      await flagAgeSample(sample.sample_id, reason);
      adopt(await nextAgeSample());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [sample, busy, reason, adopt]);

  // Arrows nudge from anywhere (native handling covers the focused slider
  // itself); Enter is Next once the slider has been touched.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      if (e.key === "Enter") {
        if (canNext) {
          e.preventDefault();
          void saveAndNext();
        }
        return;
      }
      const el = e.target as HTMLElement | null;
      if (el instanceof HTMLInputElement && el.type === "range") return;
      if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
        e.preventDefault();
        nudge(-1);
      } else if (e.key === "ArrowRight" || e.key === "ArrowUp") {
        e.preventDefault();
        nudge(1);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canNext, saveAndNext, nudge]);

  if (loading && !sample) {
    return (
      <div className="py-20 grid place-items-center">
        <Spinner label="Loading the next bee" />
      </div>
    );
  }

  if (noneLeft || !sample) {
    return (
      <div className="max-w-2xl mx-auto pt-10">
        {error ? <ErrorNote className="mb-6" onDismiss={() => setError(null)}>{error}</ErrorNote> : null}
        <EmptyState
          title="No open samples left"
          body="Every bee in the queue has an age or a flag. New samples arrive on the Age overview."
          action={
            <div className="flex items-center gap-3">
              <Button onClick={() => navigate("/age")}>Age overview</Button>
              <Button variant="ghost" onClick={() => navigate("/")}>All tools</Button>
            </div>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-3">
          <SectionLabel>Age</SectionLabel>
          <span className="text-sm text-near-black break-all">{sample.filename}</span>
          <Pill>open</Pill>
        </div>
        <span className="text-[12px] text-gray-tertiary">
          Judge from appearance: hair loss, cuticle shine, wing wear.
        </span>
      </div>

      {error ? <ErrorNote onDismiss={() => setError(null)}>{error}</ErrorNote> : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px] items-start">
        {/* The bee gets the room — the judgment is made from pixels alone. */}
        <div className="rounded-2xl border border-border bg-surface-sunk grid place-items-center p-3 min-h-[50vh]">
          <img
            src={ageSampleFileUrl(sample.sample_id)}
            alt={sample.filename}
            className="max-w-full max-h-[72vh] rounded-lg"
          />
        </div>

        <aside className="flex flex-col gap-4">
          <Card className="p-4">
            <SectionLabel>How old is this bee?</SectionLabel>

            <div
              className={
                "font-display text-4xl leading-none tabular-nums mt-3 transition-colors " +
                (touched ? "text-near-black" : "text-gray-tertiary")
              }
            >
              {readout(ageDays)}
            </div>
            <p className="text-[12px] text-gray-tertiary mt-1.5">
              {touched ? band(ageDays) : "Move the slider to judge."}
            </p>

            {/*
             * 0..28 whole days, 28 right-censored as "28+" (four weeks or
             * older). Why the scale stops there: summer workers average 15-38
             * days and winter bees live for months, but appearance-based
             * judgment is only meaningful across the temporal-polyethism
             * window — cleaning 0-3 d, nursing 4-12 d, maintenance 12-20 d,
             * foraging 21 d+. Past four weeks, looks stop separating ages.
             */}
            <div className="mt-4">
              <input
                type="range"
                min={0}
                max={28}
                step={1}
                value={ageDays}
                aria-label="Age in days, 28 means 28 or more"
                onChange={(e) => {
                  setAgeDays(Number(e.target.value));
                  setTouched(true);
                }}
                className="w-full accent-accent cursor-pointer"
              />
              <div className="relative h-4 mt-0.5 font-mono text-[10px] text-gray-tertiary select-none">
                {WEEK_MARKS.map((d, i) => (
                  <span
                    key={d}
                    className="absolute"
                    style={
                      d === 0
                        ? { left: 0 }
                        : d === 28
                          ? { right: 0 }
                          : { left: `${(d / 28) * 100}%`, transform: "translateX(-50%)" }
                    }
                  >
                    wk{i}
                  </span>
                ))}
              </div>
            </div>

            <div className="mt-4">
              {/* Disabled until touched: an untouched default would silently
                  bias the labels toward one number (see the file comment). */}
              <Button size="lg" className="w-full" onClick={() => void saveAndNext()} disabled={!canNext}>
                {busy ? "Saving" : "Next"}
              </Button>
              <p className="text-[12px] text-gray-tertiary mt-1.5">
                {touched
                  ? "Saves this age and loads the next bee."
                  : "Enabled once the slider has been moved — an untouched default is not a judgment."}
              </p>
            </div>

            <p className="text-[11px] text-gray-tertiary mt-3 flex items-center gap-1.5 flex-wrap">
              <Kbd>&larr;</Kbd>
              <Kbd>&rarr;</Kbd>
              <span>nudge</span>
              <Kbd className="ml-2">&#9166;</Kbd>
              <span>next</span>
            </p>
          </Card>

          <Card className="p-4">
            <SectionLabel>Cannot be judged?</SectionLabel>
            <p className="text-[13px] text-gray-mid mt-2">
              Blurred, more than one bee, or not a bee: flag it. Flagged samples
              leave the queue and stay out of the export.
            </p>
            {flagOpen ? (
              <div className="mt-3 flex flex-col gap-2">
                <input
                  className={INPUT_CLS + " w-full"}
                  value={reason}
                  placeholder="Reason (optional)"
                  autoFocus
                  onChange={(e) => setReason(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      void flagAndNext();
                    }
                    if (e.key === "Escape") setFlagOpen(false);
                  }}
                />
                <div className="flex items-center gap-2">
                  <Button variant="ghost" onClick={() => void flagAndNext()} disabled={busy}>
                    {busy ? "Flagging" : "Flag and next"}
                  </Button>
                  <Button variant="ghost" onClick={() => setFlagOpen(false)} disabled={busy}>
                    Cancel
                  </Button>
                </div>
              </div>
            ) : (
              <div className="mt-3">
                <Button variant="ghost" onClick={() => setFlagOpen(true)} disabled={busy}>
                  Flag this sample
                </Button>
              </div>
            )}
          </Card>

          <Card className="p-4">
            <SectionLabel>Sample</SectionLabel>
            <p className="text-[13px] text-near-black mt-1.5 break-all">{sample.filename}</p>
            <p className="text-[12px] text-gray-tertiary mt-1">
              {sample.width}x{sample.height} px
              {sample.uploaded_by ? `, uploaded by ${sample.uploaded_by}` : ""}.
            </p>
          </Card>
        </aside>
      </div>
    </div>
  );
}
