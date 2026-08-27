import type { ReactNode } from "react";

/**
 * The header strip above the canvas: where this crop sits in its frame, how far
 * the frame has got, and what state this crop is in.
 *
 * It also carries the completeness instruction, and that line is a product
 * requirement rather than copy: SPEC section 1 makes a crop exportable only when
 * EVERY instance in it has a polygon, because YOLO-seg reads an unlabeled bee as
 * an explicit "this is background" teaching signal. An annotator who forgets
 * that is not making a small mistake, they are poisoning the training set — so
 * the sentence sits in the layout, in plain words, permanently visible. Not a
 * tooltip, not a one-time modal, and not shouty either: it has to survive being
 * read four hundred times in an afternoon.
 */

export type CropProgressProps = {
  filename: string;
  index: number;
  total: number;
  nDone: number;
  nMasks: number;
  status: "open" | "done";
  isEmpty: boolean;
};

export default function CropProgress({
  filename,
  index,
  total,
  nDone,
  nMasks,
  status,
  isEmpty,
}: CropProgressProps) {
  const safeTotal = Math.max(0, total);
  const done = Math.min(Math.max(0, nDone), safeTotal || Math.max(0, nDone));
  const frac = safeTotal > 0 ? done / safeTotal : 0;

  return (
    <header className="border border-border bg-surface">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3.5 py-2.5">
        <span
          className="font-mono text-[12px] text-near-black truncate max-w-[18rem]"
          title={filename}
        >
          {filename}
        </span>

        <span className="font-mono text-[12px] text-text tabular-nums whitespace-nowrap">
          Crop {index} of {safeTotal}
        </span>

        {/* Frame progress: the bar answers "how much of this image is left",
            which is the question that decides whether to keep going. */}
        <div className="flex items-center gap-2 min-w-[10rem] flex-1">
          <div
            className="h-1.5 flex-1 bg-surface-sunk overflow-hidden"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={safeTotal}
            aria-valuenow={done}
            aria-label="crops done in this image"
          >
            <div
              className="h-full bg-accent transition-[width] duration-300"
              style={{ width: `${frac * 100}%` }}
            />
          </div>
          <span className="font-mono text-[11px] text-gray-mid tabular-nums whitespace-nowrap">
            {done}/{safeTotal} done
          </span>
        </div>

        <span className="font-mono text-[11px] text-gray-mid tabular-nums whitespace-nowrap">
          {nMasks} mask{nMasks === 1 ? "" : "s"}
        </span>

        <div className="flex items-center gap-1.5">
          {status === "done" ? <Pill tone="accent">done</Pill> : <Pill tone="muted">open</Pill>}
          {isEmpty ? <Pill tone="muted">empty</Pill> : null}
        </div>
      </div>

      <p className="border-t border-border border-l-2 border-l-accent bg-accent-soft/50 px-3.5 py-2 text-[13px] leading-[1.5] text-text">
        <span className="text-near-black font-medium">
          Label every instance in this crop before marking it done.
        </span>{" "}
        A missed instance teaches the model that it is background.
        {isEmpty || nMasks === 0 ? (
          <span className="text-gray-mid">
            {" "}
            Genuinely nothing here? Mark the crop empty — a negative sample is
            worth keeping, a skip is not.
          </span>
        ) : null}
      </p>
    </header>
  );
}

function Pill({ tone, children }: { tone: "accent" | "muted"; children: ReactNode }) {
  return (
    <span
      className={
        "font-mono text-[10px] uppercase tracking-[0.1em] border px-1.5 py-0.5 " +
        (tone === "accent"
          ? "border-accent text-accent-deep bg-accent-soft"
          : "border-border text-gray-tertiary bg-surface-sunk")
      }
    >
      {children}
    </span>
  );
}
