/**
 * The labeling screen — the one place labeling time is actually spent.
 *
 * Three things here are product requirements rather than taste, and all three
 * come from SPEC section 1:
 *
 * 1. The completeness rule is on screen at all times. <CropProgress> carries the
 *    sentence and it is parked in a bar that sticks directly under the app
 *    header, so it cannot be scrolled away from while drawing.
 * 2. "Mark done" and "Nothing here" are two different assertions, not two ways
 *    of finishing. Done means every instance has a polygon; empty means the crop
 *    genuinely contains none and is a valuable negative sample. Each is enabled
 *    only in the state where it can be true, so the pair can never be misread.
 * 3. There is no skip button. Leaving a crop `open` IS the skip — an
 *    unremarkable, reversible act that costs nothing. A skip control would
 *    invite "done, but I gave up", which is the exact poison the crop design
 *    exists to prevent.
 *
 * Mask edits are optimistic with rollback: at 200-plus instances a labeler
 * cannot wait for a round trip per polygon, but they must never end up believing
 * a polygon was saved when it was not — so a failure puts the canvas back the way
 * it was and says so.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import type { CropTask, LabelClass, Mask } from "../lib/types";
import {
  completeCrop,
  createClass,
  createMask,
  cropImageUrl,
  deleteMask,
  errorMessage,
  getCrop,
  imageFileUrl,
  listClasses,
  nextCrop,
  reopenCrop,
  updateMask,
} from "../lib/api";
import { useAuth } from "../lib/auth";
import PolygonCanvas from "../components/PolygonCanvas";
import ClassPicker from "../components/ClassPicker";
import CropProgress from "../components/CropProgress";
import { Button, Card, EmptyState, ErrorNote, Kbd, Pill, SectionLabel, Spinner } from "../components/ui";

/** Ids for masks the server has not acknowledged yet. Never sent anywhere. */
let tempSeq = 0;
const TEMP = "temp-";
const newTempId = () => `${TEMP}${++tempSeq}`;
const isTemp = (id: string) => id.startsWith(TEMP);

/** Digits must not fire while the user is typing a new class name. */
function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== "string") return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
}

export default function Label() {
  const { cropId } = useParams<{ cropId?: string }>();
  const navigate = useNavigate();
  const { me, isAdmin } = useAuth();

  const [task, setTask] = useState<CropTask | null>(null);
  const [masks, setMasks] = useState<Mask[]>([]);
  const [classes, setClasses] = useState<LabelClass[]>([]);
  const [activeClassId, setActiveClassId] = useState<string | null>(null);
  const [selectedMaskId, setSelectedMaskId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [noneLeft, setNoneLeft] = useState(false);
  const [showFrame, setShowFrame] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The crop already in state, so advancing to the next one does not re-fetch it
  // the moment the URL catches up.
  const loadedFor = useRef<string | null>(null);
  // Set when we deliberately navigate back to the bare /label URL after the queue
  // ran dry, so the load effect does not immediately ask for a next crop again.
  const skipNextLoad = useRef(false);
  // Points edited while the create for that same polygon was still in flight.
  const pendingEdits = useRef(new Map<string, [number, number][]>());

  // ---- classes ----------------------------------------------------------
  // Archived classes are fetched too, and deliberately: a mask drawn before its
  // class was archived is still on the crop, and the canvas needs the colour to
  // draw it as anything other than an unknown. Only the ACTIVE ones are offered
  // for picking — the server refuses new masks on an archived class.
  const refreshClasses = useCallback(async () => {
    const cl = await listClasses(true);
    setClasses(cl);
    const live = cl.filter((c) => !c.archived);
    setActiveClassId((cur) =>
      cur && live.some((c) => c.class_id === cur) ? cur : (live[0]?.class_id ?? null),
    );
    return cl;
  }, []);

  useEffect(() => {
    let alive = true;
    refreshClasses().catch((e: unknown) => {
      if (alive) setError(errorMessage(e));
    });
    return () => {
      alive = false;
    };
  }, [refreshClasses]);

  /** The pickable classes: one array, used for the picker AND for 1..9. */
  const activeClasses = useMemo(() => classes.filter((c) => !c.archived), [classes]);

  // ---- the crop itself --------------------------------------------------
  // Every CropTask carries `n_done`, so frame progress arrives with the crop and
  // this is the only place it changes. That is deliberate: the bar used to be
  // filled by a second request that could fail or land out of order behind the
  // crop it described, and there is nothing left to go stale.
  const adopt = useCallback((t: CropTask) => {
    setTask(t);
    setMasks(t.masks);
    setSelectedMaskId(null);
    setNoneLeft(false);
    loadedFor.current = t.crop.crop_id;
    pendingEdits.current.clear();
  }, []);

  useEffect(() => {
    if (skipNextLoad.current) {
      skipNextLoad.current = false;
      return;
    }
    if (cropId && loadedFor.current === cropId) return;
    let alive = true;
    setLoading(true);
    setError(null);
    (cropId ? getCrop(cropId) : nextCrop())
      .then((t) => {
        if (!alive) return;
        if (!t) {
          setTask(null);
          setMasks([]);
          setNoneLeft(true);
          loadedFor.current = null;
          return;
        }
        adopt(t);
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
  }, [cropId, adopt]);

  // ---- masks: optimistic, with rollback ---------------------------------
  const readOnly = task?.crop.status === "done";

  const handleCreate = useCallback(
    async (points: [number, number][]) => {
      if (!task || readOnly) return;
      if (!activeClassId) {
        setError("Pick a class before drawing — every polygon belongs to exactly one.");
        return;
      }
      const tempId = newTempId();
      const optimistic: Mask = {
        mask_id: tempId,
        crop_id: task.crop.crop_id,
        class_id: activeClassId,
        points,
        created_by: me?.username ?? null,
        created_at: new Date().toISOString(),
        updated_at: null,
      };
      setMasks((m) => [...m, optimistic]);
      setSelectedMaskId(tempId);
      try {
        const saved = await createMask({
          crop_id: task.crop.crop_id,
          class_id: activeClassId,
          points,
        });
        const edited = pendingEdits.current.get(tempId);
        pendingEdits.current.delete(tempId);
        const settled: Mask = edited ? { ...saved, points: edited } : saved;
        setMasks((m) => m.map((x) => (x.mask_id === tempId ? settled : x)));
        setSelectedMaskId((cur) => (cur === tempId ? saved.mask_id : cur));
        // The polygon was dragged while the create was still in flight; the
        // server has the older shape, so send the newer one straight after.
        if (edited) {
          try {
            await updateMask(saved.mask_id, { points: edited });
          } catch (e) {
            setMasks((m) => m.map((x) => (x.mask_id === saved.mask_id ? saved : x)));
            setError(`That edit was not saved — ${errorMessage(e)}`);
          }
        }
      } catch (e) {
        pendingEdits.current.delete(tempId);
        setMasks((m) => m.filter((x) => x.mask_id !== tempId));
        setSelectedMaskId((cur) => (cur === tempId ? null : cur));
        setError(`The polygon was not saved — ${errorMessage(e)}`);
      }
    },
    [task, readOnly, activeClassId, me],
  );

  const handleUpdate = useCallback(
    async (maskId: string, points: [number, number][]) => {
      if (readOnly) return;
      const before = masks.find((m) => m.mask_id === maskId);
      if (!before) return;
      setMasks((m) => m.map((x) => (x.mask_id === maskId ? { ...x, points } : x)));
      if (isTemp(maskId)) {
        pendingEdits.current.set(maskId, points);
        return;
      }
      try {
        await updateMask(maskId, { points });
      } catch (e) {
        setMasks((m) => m.map((x) => (x.mask_id === maskId ? before : x)));
        setError(`That edit was not saved — ${errorMessage(e)}`);
      }
    },
    [masks, readOnly],
  );

  const handleDelete = useCallback(
    async (maskId: string) => {
      if (readOnly) return;
      const idx = masks.findIndex((m) => m.mask_id === maskId);
      if (idx < 0) return;
      // Author-or-admin, mirroring the server gate: deleting your own polygon
      // is part of drawing; deleting someone else's is curation. Pre-checked
      // here so the optimistic removal never has to roll back on a 403.
      const target = masks[idx];
      if (!isAdmin && target.created_by && me?.username && target.created_by !== me.username) {
        setError(
          `That polygon was drawn by ${target.created_by} — only its author or an admin can delete it.`,
        );
        return;
      }
      if (isTemp(maskId)) {
        // Still being created: there is no server id to delete yet, and removing
        // it locally would leave a polygon on the server nobody can see.
        setError("That polygon is still being saved. Give it a moment, then delete it.");
        return;
      }
      const before = masks[idx];
      setMasks((m) => m.filter((x) => x.mask_id !== maskId));
      setSelectedMaskId((cur) => (cur === maskId ? null : cur));
      try {
        await deleteMask(maskId);
      } catch (e) {
        setMasks((m) => [...m.slice(0, idx), before, ...m.slice(idx)]);
        setError(`That polygon was not deleted — ${errorMessage(e)}`);
      }
    },
    [masks, readOnly, isAdmin, me],
  );

  /**
   * Picking a class does two things: it arms the next polygon, and — if a
   * polygon is currently selected — it re-classes that polygon. The canvas
   * offers no other way to fix a mis-classed shape, and redrawing it would be
   * the alternative.
   */
  const pickClass = useCallback(
    async (classId: string) => {
      setActiveClassId(classId);
      const sel = selectedMaskId;
      if (!sel || readOnly) return;
      const before = masks.find((m) => m.mask_id === sel);
      if (!before || before.class_id === classId || isTemp(sel)) return;
      setMasks((m) => m.map((x) => (x.mask_id === sel ? { ...x, class_id: classId } : x)));
      try {
        await updateMask(sel, { class_id: classId });
      } catch (e) {
        setMasks((m) => m.map((x) => (x.mask_id === sel ? before : x)));
        setError(`The class change was not saved — ${errorMessage(e)}`);
      }
    },
    [selectedMaskId, masks, readOnly],
  );

  const handleCreateClass = useCallback(
    async (name: string, color: string) => {
      const created = await createClass({ name, color });
      const cl = await refreshClasses();
      if (cl.some((c) => c.class_id === created.class_id)) setActiveClassId(created.class_id);
    },
    [refreshClasses],
  );

  // ---- keyboard: 1..9 pick the first nine classes ------------------------
  // 1..9 are ours (PolygonCanvas leaves the digit row free apart from 0, which
  // is its 1:1 zoom). They are bound to `activeClasses` — the exact array, in
  // the exact order, that ClassPicker renders its positional 1..9 hints from,
  // so the hint and the key can never disagree. Do not sort either one.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      if (e.key < "1" || e.key > "9") return;
      const c = activeClasses[Number(e.key) - 1];
      if (!c) return;
      e.preventDefault();
      void pickClass(c.class_id);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeClasses, pickClass]);

  // ---- terminal actions --------------------------------------------------
  async function advance() {
    const next = await nextCrop();
    if (!next) {
      setTask(null);
      setMasks([]);
      setNoneLeft(true);
      loadedFor.current = null;
      if (cropId) {
        skipNextLoad.current = true;
        navigate("/blech/label", { replace: true });
      }
      return;
    }
    // adopt() has already set loadedFor, so the URL change below re-runs the load
    // effect and it no-ops instead of re-fetching the crop we are already showing.
    adopt(next);
    if (next.crop.crop_id !== cropId) {
      navigate(`/blech/label/${next.crop.crop_id}`, { replace: true });
    }
  }

  async function complete(isEmpty: boolean) {
    if (!task || busy) return;
    setBusy(true);
    setError(null);
    try {
      await completeCrop(task.crop.crop_id, { is_empty: isEmpty });
      await advance();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function reopen() {
    if (!task || busy) return;
    setBusy(true);
    setError(null);
    try {
      const t = await reopenCrop(task.crop.crop_id);
      adopt(t);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const m of masks) out[m.class_id] = (out[m.class_id] ?? 0) + 1;
    return out;
  }, [masks]);

  // ---- render ------------------------------------------------------------
  if (loading && !task) {
    return (
      <div className="py-20 grid place-items-center">
        <Spinner label="Loading the next crop" />
      </div>
    );
  }

  if (noneLeft || !task) {
    return (
      <div className="max-w-2xl mx-auto pt-10">
        {error ? <ErrorNote className="mb-6" onDismiss={() => setError(null)}>{error}</ErrorNote> : null}
        <EmptyState
          title="No open crops left"
          body="Every crop is marked done or empty. Upload the next frame on the Overview, or reopen a crop from its frame grid there."
          action={<Button onClick={() => navigate("/blech")}>Go to Overview</Button>}
        />
      </div>
    );
  }

  const crop = task.crop;
  const canMarkDone = !readOnly && masks.length > 0 && !busy;
  const canMarkEmpty = !readOnly && masks.length === 0 && !busy;

  return (
    // On a desktop the whole screen is exactly one viewport tall: the canvas
    // fills what is left and the side panel scrolls inside itself, so the
    // completeness rule at the top cannot be scrolled away from. Below `lg` the
    // layout stacks and the page does scroll — hence the sticky bar as well.
    <div className="flex flex-col lg:h-[calc(100vh-var(--app-header-h)-1.5rem)]">
      {/* The completeness rule lives in here and must never leave the screen. */}
      <div className="sticky top-[var(--app-header-h)] z-40 shrink-0 -mx-4 sm:-mx-6 px-4 sm:px-6 pt-3 pb-3 bg-bg">
        <CropProgress
          filename={task.image.filename}
          index={task.index}
          total={task.total}
          nDone={task.n_done}
          nMasks={masks.length}
          status={crop.status}
          isEmpty={crop.is_empty}
        />
      </div>

      {error ? (
        <ErrorNote className="mb-3 shrink-0" onDismiss={() => setError(null)}>
          {error}
        </ErrorNote>
      ) : null}

      <div className="grid gap-4 flex-1 min-h-0 lg:grid-cols-[minmax(0,1fr)_320px] xl:grid-cols-[minmax(0,1fr)_360px]">
        {/* The crop gets the room: PolygonCanvas fills whatever box it is given,
            so this cell is the box and it is the biggest thing on the screen. */}
        <div className="h-[60vh] lg:h-full min-h-0">
          <PolygonCanvas
            src={cropImageUrl(crop.crop_id)}
            width={crop.w}
            height={crop.h}
            masks={masks}
            classes={classes}
            activeClassId={activeClassId}
            selectedMaskId={selectedMaskId}
            readOnly={readOnly}
            onCreate={(points) => void handleCreate(points)}
            onUpdate={(maskId, points) => void handleUpdate(maskId, points)}
            onSelect={setSelectedMaskId}
            onDelete={(maskId) => void handleDelete(maskId)}
          />
        </div>

        <aside className="flex flex-col gap-4 lg:h-full lg:min-h-0 lg:overflow-y-auto lg:pr-1">
          <Card className="p-4">
            <div className="flex items-center justify-between gap-3 mb-3">
              <SectionLabel>Classes</SectionLabel>
              <span className="text-[11px] text-gray-tertiary flex items-center gap-1.5">
                <Kbd>1</Kbd>
                <span>to</span>
                <Kbd>9</Kbd>
                <span>pick</span>
              </span>
            </div>
            <ClassPicker
              classes={activeClasses}
              activeClassId={activeClassId}
              onPick={(id) => void pickClass(id)}
              onCreate={handleCreateClass}
              counts={counts}
              disabled={readOnly}
            />
            {activeClasses.length === 0 ? (
              <p className="text-[12px] text-gray-tertiary mt-3">
                No classes yet. Add the first in the picker above — a polygon
                cannot be drawn without one.
              </p>
            ) : (
              <p className="text-[12px] text-gray-tertiary mt-3">
                With a polygon selected, picking a class re-classes it.
              </p>
            )}
          </Card>

          <Card className="p-4">
            <div className="flex items-center justify-between gap-3">
              <SectionLabel>This crop</SectionLabel>
              {readOnly ? (
                <Pill tone={crop.is_empty ? "neutral" : "accent"}>
                  {crop.is_empty ? "done, empty" : "done"}
                </Pill>
              ) : (
                <Pill>open</Pill>
              )}
            </div>
            <p className="text-[13px] text-gray-mid mt-2">
              {masks.length === 0
                ? "No polygons yet."
                : `${masks.length} polygon${masks.length === 1 ? "" : "s"} drawn.`}
            </p>

            {readOnly ? (
              <div className="mt-4 flex flex-col gap-2 items-start">
                <p className="text-[13px] text-gray-mid">
                  Finished and included in exports. Reopen to change anything.
                </p>
                <Button variant="ghost" onClick={() => void reopen()} disabled={busy}>
                  Reopen this crop
                </Button>
              </div>
            ) : (
              <div className="mt-4 flex flex-col gap-4">
                <div>
                  <Button
                    size="lg"
                    className="w-full"
                    onClick={() => void complete(false)}
                    disabled={!canMarkDone}
                  >
                    Mark done
                  </Button>
                  <p className="text-[12px] text-gray-tertiary mt-1.5">
                    {masks.length === 0
                      ? "Needs at least one polygon. Genuinely nothing here? Use the button below."
                      : "Only once every instance in the crop has a polygon."}
                  </p>
                </div>
                <div>
                  <Button
                    variant="ghost"
                    className="w-full"
                    onClick={() => void complete(true)}
                    disabled={!canMarkEmpty}
                  >
                    Nothing here (empty)
                  </Button>
                  <p className="text-[12px] text-gray-tertiary mt-1.5">
                    {masks.length === 0
                      ? "A crop with no instances is a valid negative example, not a skip — exported with an empty label file."
                      : "This crop has polygons. Delete them first if it is genuinely empty."}
                  </p>
                </div>
              </div>
            )}
          </Card>

          <Card className="p-4">
            <SectionLabel>Frame</SectionLabel>
            <p className="text-[13px] text-near-black mt-1.5 break-all">{task.image.filename}</p>
            <p className="text-[12px] text-gray-tertiary mt-1">
              Crop {crop.row_idx + 1}·{crop.col_idx + 1} — {crop.w}x{crop.h} px at ({crop.x}, {crop.y})
              in a {task.image.width}x{task.image.height} frame.
            </p>
            <button
              type="button"
              onClick={() => setShowFrame((v) => !v)}
              className="text-[13px] text-accent hover:text-accent-deep transition-colors inline-block mt-2"
            >
              {showFrame ? "Hide the whole frame" : "See the whole frame"}
            </button>
            {showFrame ? (
              /* The frame inline, with this crop outlined — a map, not a
                 navigation. The rectangle is percentage-positioned so it
                 survives any rendered size. */
              <div className="relative mt-2 rounded-md overflow-hidden border border-border">
                <img
                  src={imageFileUrl(task.image.image_id)}
                  alt={task.image.filename}
                  className="block w-full h-auto"
                />
                <div
                  className="absolute border-2 border-accent pointer-events-none"
                  style={{
                    left: `${(crop.x / task.image.width) * 100}%`,
                    top: `${(crop.y / task.image.height) * 100}%`,
                    width: `${(crop.w / task.image.width) * 100}%`,
                    height: `${(crop.h / task.image.height) * 100}%`,
                  }}
                />
              </div>
            ) : null}
          </Card>
        </aside>
      </div>
    </div>
  );
}
