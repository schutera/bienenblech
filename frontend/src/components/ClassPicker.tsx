import { useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { LabelClass } from "../lib/types";

/**
 * The class list beside the canvas: pick the class a new polygon gets, see how
 * many masks of each class are already in THIS crop, and add a class without
 * leaving the labeling screen.
 *
 * The count column is not decoration. SPEC section 1 makes exhaustiveness the
 * invariant — "every instance of every known class" — so the labeler needs to
 * see, at the moment of marking a crop done, that the class they were not
 * thinking about still reads zero.
 *
 * Owns no data: `onCreate` returns a promise and this component only reflects
 * its outcome.
 */

/**
 * Default swatches offered when adding a class. These are DATA, not chrome:
 * they end up in `label_classes.color` and get drawn on the crop, which is why
 * they are literal hex here rather than `@theme` tokens (SPEC section 12 bans
 * hard-coded hex for the app's own surfaces, not for label colours). Chosen to
 * stay distinguishable from each other and from the warm-paper background at
 * 20% fill.
 */
const PALETTE = [
  "#c0563f",
  "#d98236",
  "#c9a227",
  "#5f8b6a",
  "#2f8f7f",
  "#3d7ea6",
  "#5b5ea6",
  "#8a5a9b",
  "#a45d7a",
  "#6b6257",
];

/** A loose "is this the same class again?" test — trailing spaces and casing
 *  are the two ways a duplicate actually gets typed. */
function normalizeName(name: string): string {
  return name.trim().toLowerCase().replace(/\s+/g, " ");
}

export type ClassPickerProps = {
  classes: LabelClass[];
  activeClassId: string | null;
  onPick: (classId: string) => void;
  onCreate: (name: string, color: string) => Promise<void>;
  counts?: Record<string, number>;
  disabled?: boolean;
};

export default function ClassPicker({
  classes,
  activeClassId,
  onPick,
  onCreate,
  counts,
  disabled = false,
}: ClassPickerProps) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [color, setColor] = useState(PALETTE[0]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const usedColors = useMemo(
    () => new Set(classes.map((c) => c.color.toLowerCase())),
    [classes],
  );
  const takenNames = useMemo(
    () => new Set(classes.map((c) => normalizeName(c.name))),
    [classes],
  );

  // Opening the form pre-picks the first unused swatch, so adding a class is
  // "click, type, Enter" and never a colour-picking detour. Chosen on open
  // rather than in an effect: a `classes` refresh mid-typing must not silently
  // change the colour under the labeler.
  function openAdd() {
    setColor(PALETTE.find((c) => !usedColors.has(c)) ?? PALETTE[0]);
    setError(null);
    setAdding(true);
  }

  useEffect(() => {
    if (adding) nameRef.current?.focus();
  }, [adding]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const clean = name.trim().replace(/\s+/g, " ");
    if (!clean) {
      setError("Give the class a name.");
      return;
    }
    if (takenNames.has(normalizeName(clean))) {
      setError(`"${clean}" already exists.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onCreate(clean, color);
      setName("");
      setAdding(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-gray-tertiary">
          Classes
        </span>
        {classes.length > 0 ? (
          <span className="font-mono text-[10px] text-gray-tertiary tabular-nums">
            {classes.length}
          </span>
        ) : null}
      </div>

      <div className="flex flex-col gap-1.5">
        {classes.map((c, i) => {
          const active = c.class_id === activeClassId;
          const n = counts ? (counts[c.class_id] ?? 0) : null;
          return (
            <button
              key={c.class_id}
              type="button"
              onClick={() => onPick(c.class_id)}
              disabled={disabled}
              aria-pressed={active}
              title={c.description ?? c.name}
              className={
                "group flex items-center gap-2.5 border px-2.5 py-2 text-left transition-colors " +
                "disabled:opacity-50 disabled:pointer-events-none " +
                (active
                  ? "border-accent bg-accent-soft/60"
                  : "border-border bg-surface hover:border-accent")
              }
            >
              <span
                aria-hidden="true"
                className="h-3.5 w-3.5 shrink-0 border border-border"
                style={{ background: c.color }}
              />
              <span
                className={
                  "flex-1 truncate text-[13px] " + (active ? "text-near-black" : "text-text")
                }
              >
                {c.name}
              </span>
              {n !== null ? (
                <span
                  title="masks of this class in this crop"
                  className={
                    "font-mono text-[11px] tabular-nums " +
                    (n > 0 ? "text-near-black" : "text-gray-tertiary")
                  }
                >
                  {n}
                </span>
              ) : null}
              {i < 9 ? (
                <span
                  aria-hidden="true"
                  className="font-mono text-[10px] text-gray-tertiary border border-border bg-surface-sunk px-1 leading-[1.4]"
                >
                  {i + 1}
                </span>
              ) : null}
            </button>
          );
        })}

        {classes.length === 0 ? (
          <p className="text-[12.5px] text-gray-tertiary">
            No classes yet. Add the first one to start labeling.
          </p>
        ) : null}
      </div>

      {adding ? (
        <form onSubmit={submit} className="border border-border bg-surface p-2.5 flex flex-col gap-2.5">
          <input
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={busy}
            placeholder="Class name"
            aria-label="new class name"
            className="border border-border bg-surface px-2 py-1.5 text-[13px] text-near-black outline-none focus:border-accent disabled:opacity-50"
          />
          <div className="flex flex-wrap items-center gap-1.5">
            {PALETTE.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setColor(p)}
                disabled={busy}
                aria-label={`colour ${p}`}
                aria-pressed={p === color}
                className={
                  "h-5 w-5 border-2 transition-colors disabled:opacity-50 " +
                  (p === color ? "border-near-black" : "border-border hover:border-gray-mid")
                }
                style={{ background: p }}
              />
            ))}
            <input
              type="color"
              value={color}
              onChange={(e) => setColor(e.target.value)}
              disabled={busy}
              aria-label="custom colour"
              className="h-5 w-7 border border-border bg-surface p-0 disabled:opacity-50"
            />
          </div>
          {error ? <span className="font-mono text-[11px] text-danger">{error}</span> : null}
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={busy}
              className="border border-accent bg-accent px-3 py-1.5 font-mono text-[11px] text-white transition-opacity disabled:opacity-50"
            >
              {busy ? "Adding…" : "Add class"}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setAdding(false);
                setName("");
                setError(null);
              }}
              className="px-2 py-1.5 font-mono text-[11px] text-gray-tertiary hover:text-near-black disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button
          type="button"
          onClick={openAdd}
          disabled={disabled}
          className="border border-border bg-surface px-2.5 py-2 font-mono text-[11px] text-gray-mid hover:border-accent hover:text-accent transition-colors disabled:opacity-50 disabled:pointer-events-none"
        >
          + Add class
        </button>
      )}
    </div>
  );
}
