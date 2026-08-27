import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { ImageSummary } from "../lib/types";
import { errorMessage, listImages, stats, type Stats } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  Button,
  Card,
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

export default function Home() {
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const [s, setS] = useState<Stats | null>(null);
  const [images, setImages] = useState<ImageSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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

  const open = s ? Math.max(0, s.n_crops - s.n_done) : 0;
  const fraction = s && s.n_crops > 0 ? s.n_done / s.n_crops : 0;
  const classes = s?.per_class ?? [];
  const recent = [...images]
    .sort((a, b) => (a.uploaded_at < b.uploaded_at ? 1 : -1))
    .slice(0, 6);

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="max-w-xl">
          <SectionLabel>Overview</SectionLabel>
          <h1 className="font-display text-4xl font-light text-near-black leading-tight mt-1">
            Label bees, one crop at a time
          </h1>
          <p className="text-sm text-gray-mid mt-2">
            Every uploaded frame is split into 640-pixel crops, and the crop is the
            unit of work: small enough that labeling every instance in it is
            realistic, which is what makes the training data honest.
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
                Only crops marked done are exported. An open crop is left out
                entirely rather than shipped half-labeled.
              </p>
            </div>
          </Card>

          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] items-start">
            <Card className="p-6">
              <div className="flex items-center justify-between gap-3">
                <SectionLabel>Classes</SectionLabel>
                <Link
                  to="/classes"
                  className="text-[13px] text-accent hover:text-accent-deep transition-colors"
                >
                  Manage
                </Link>
              </div>
              {classes.length === 0 ? (
                <p className="text-[13px] text-gray-mid mt-3">
                  No classes yet. The first one has to exist before a polygon can
                  be drawn.
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

            <Card className="p-6">
              <div className="flex items-center justify-between gap-3">
                <SectionLabel>Recent frames</SectionLabel>
                <Link
                  to="/images"
                  className="text-[13px] text-accent hover:text-accent-deep transition-colors"
                >
                  All frames
                </Link>
              </div>
              {recent.length === 0 ? (
                <EmptyState
                  className="mt-4"
                  title="No frames yet"
                  body={
                    isAdmin
                      ? "Upload a frame and the server tiles it into crops straight away."
                      : "An admin uploads the frames. Once one lands, its crops appear in the queue."
                  }
                  action={isAdmin ? <Button onClick={() => navigate("/upload")}>Upload a frame</Button> : undefined}
                />
              ) : (
                <ul className="mt-4 flex flex-col divide-y divide-border">
                  {recent.map((im) => {
                    const f = im.n_crops > 0 ? im.n_done / im.n_crops : 0;
                    return (
                      <li key={im.image_id} className="py-3 first:pt-0 last:pb-0">
                        <div className="flex items-baseline justify-between gap-3">
                          <Link
                            to="/images"
                            className="text-sm text-near-black truncate hover:text-accent-deep transition-colors"
                            title={im.filename}
                          >
                            {im.filename}
                          </Link>
                          <span className="font-mono text-[11px] text-gray-tertiary tabular-nums shrink-0">
                            {im.n_done}/{im.n_crops} done
                          </span>
                        </div>
                        <ProgressBar value={f} height="h-1.5" className="mt-2" />
                        <div className="flex items-center gap-3 mt-1.5 text-[11px] text-gray-tertiary">
                          <span>{im.n_masks.toLocaleString()} polygons</span>
                          <span>{fmtDate(im.uploaded_at)}</span>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
