/**
 * The tool picker — the signed-in landing screen at /.
 *
 * Two tiles, one per labeling tool, each a single <Link>. A tile shows one real
 * example image when /api/picker/examples has one for that tool; a tool with no
 * data yet — or a fetch that fails — gets the quiet fallback: the tool name, one
 * line, a drawn placeholder. Never a spinner and never an error banner, because
 * this screen is a door, not a page: it must be instantly walkable even when
 * the examples call is slow, dry, or dead.
 */

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ageSampleFileUrl, cropImageUrl, pickerExamples } from "../lib/api";

/** The answer shape of GET /api/picker/examples: one representative id per
    tool, null when that tool has no data yet. Declared locally so this page
    typechecks against the contract even while api.ts is still growing its
    helper. */
type PickerExamples = { blech: string | null; age: string | null };

function Tile({
  to,
  name,
  blurb,
  imageUrl,
}: {
  to: string;
  name: string;
  blurb: string;
  imageUrl: string | null;
}) {
  // A stale example id (the crop or sample was deleted after the fetch) 404s
  // only when the <img> loads; fall back to the quiet tile rather than letting
  // the browser paint a broken-image glyph.
  const [broken, setBroken] = useState(false);
  const showImage = imageUrl !== null && !broken;
  return (
    <Link
      to={to}
      className="group block bg-surface border border-border rounded-2xl overflow-hidden shadow-[0_1px_2px_rgba(43,42,38,0.04),0_8px_24px_-12px_rgba(43,42,38,0.10)] hover:border-accent transition-colors animate-fade-slide-in"
    >
      <div className="aspect-[3/2] bg-surface-sunk grid place-items-center overflow-hidden">
        {showImage ? (
          <img
            src={imageUrl}
            alt=""
            loading="lazy"
            onError={() => setBroken(true)}
            className="w-full h-full object-cover group-hover:scale-[1.02] transition-transform duration-300"
          />
        ) : (
          <svg width="44" height="44" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M12 2.5l8.2 4.75v9.5L12 21.5 3.8 16.75v-9.5z"
              stroke="var(--color-gray-tertiary)"
              strokeWidth="1.5"
              strokeLinejoin="round"
            />
          </svg>
        )}
      </div>
      <div className="px-6 py-5">
        <div className="font-display text-2xl text-near-black leading-tight group-hover:text-accent-deep transition-colors">
          {name}
        </div>
        <p className="text-[13px] text-gray-mid mt-1">{blurb}</p>
      </div>
    </Link>
  );
}

export default function Picker() {
  // null while the examples call is in flight: the tiles render immediately
  // with the fallback and the example images pop in when (if) ids arrive.
  const [examples, setExamples] = useState<PickerExamples | null>(null);

  useEffect(() => {
    let cancelled = false;
    pickerExamples()
      .then((ex: PickerExamples) => {
        if (!cancelled) setExamples(ex);
      })
      .catch(() => {
        /* the fallback tiles ARE the error state — nothing to show */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="max-w-3xl mx-auto">
      <h1 className="font-display text-4xl font-light text-near-black leading-tight">
        Pick a tool
      </h1>
      <div className="grid sm:grid-cols-2 gap-6 mt-8">
        <Tile
          to="/blech"
          name="Blech"
          blurb="Segment debris on sticky-sheet crops."
          imageUrl={examples?.blech ? cropImageUrl(examples.blech) : null}
        />
        <Tile
          to="/age"
          name="Age"
          blurb="Judge a single bee's age in days."
          imageUrl={examples?.age ? ageSampleFileUrl(examples.age) : null}
        />
      </div>
    </div>
  );
}
