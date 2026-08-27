import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import type { LabelClass, Mask } from "../lib/types";
import type { Point, Transform } from "../lib/geometry";
import {
  clampPoint,
  clampTransform,
  distance,
  fitTransform,
  imageToScreen,
  nearestSegment,
  nearestVertex,
  pointInPolygon,
  polygonArea,
  polygonCentroid,
  screenToImage,
  simplify,
  zoomAt,
} from "../lib/geometry";

/**
 * The crop annotation canvas: an <img> of one 640x640 crop with an SVG polygon
 * overlay sitting in the SAME transformed space, so a mask stays welded to its
 * pixels at 0.1x and at 32x alike.
 *
 * Everything crossing this component's boundary is in CROP-LOCAL image pixels
 * (SPEC section 3). Screen coordinates never leak out through a prop; the only
 * bridge between the two spaces is `geometry.ts`.
 *
 * The component owns no data. It renders what it is given and reports gestures,
 * because the page above it is what knows about the network.
 */

const MIN_SCALE = 0.1;
const MAX_SCALE = 32;

/** Past this zoom the browser's smoothing hides the pixel grid the labeler is
 *  trying to trace, so switch to nearest-neighbour. */
const PIXELATE_AT = 4;

const FIT_PAD = 20;

/** Handle geometry in SCREEN px. Everything drawn inside the zoomed SVG divides
 *  by `scale`, so a handle is the same physical size at every zoom — a handle
 *  that grows with the image is unusable at 16x and invisible at 0.5x. */
const HANDLE_R_PX = 5;
const HANDLE_HIT_PX = 11;

/** Closing a polygon means hitting its first vertex. The radius is in screen px
 *  for the same reason: at 20x a 3-image-px tolerance is a huge target and at
 *  0.4x it is unhittable. */
const CLOSE_HIT_PX = 12;

/** Pointer travel before a left press stops being a click and becomes a
 *  freehand trace. Below this, a shaky hand still places a vertex. */
const FREEHAND_START_PX = 6;

/** Pointer travel before a press on a vertex counts as a drag. Without it the
 *  first half of a double-click-to-delete jitters by a pixel and fires a
 *  pointless PATCH just before the delete. */
const VERTEX_DRAG_MIN_PX = 2;

/** RDP tolerance in SCREEN px, converted to image px at commit time. Tying it
 *  to the zoom is the point: a trace drawn at 8x is meant to be precise, and
 *  simplifying it by a fixed 2 image px would throw that precision away. */
const FREEHAND_TOL_PX = 2.2;

const ZOOM_STEP = 1.25;

/** Only used when a mask points at a class the caller did not pass (archived,
 *  or a list still loading). Chrome tokens are fine to name; a real class colour
 *  always comes from the class row itself. */
const UNKNOWN_CLASS_COLOR = "var(--color-gray-tertiary, #9a948a)";

export type PolygonCanvasProps = {
  src: string;
  width: number;
  height: number;
  masks: Mask[];
  classes: LabelClass[];
  activeClassId: string | null;
  selectedMaskId: string | null;
  readOnly?: boolean;
  onCreate: (points: [number, number][]) => void;
  onUpdate: (maskId: string, points: [number, number][]) => void;
  onSelect: (maskId: string | null) => void;
  onDelete: (maskId: string) => void;
};

type Gesture =
  | { kind: "pan"; startX: number; startY: number; baseTx: number; baseTy: number }
  | {
      kind: "vertex";
      maskId: string;
      index: number;
      points: Point[];
      startX: number;
      startY: number;
      moved: boolean;
    }
  | { kind: "draw"; startX: number; startY: number; freehand: boolean }
  | null;

/** Keys are ignored while the user is typing a class name, a note, ... */
function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable === true;
}

function samePoints(a: readonly Point[], b: readonly Point[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i][0] !== b[i][0] || a[i][1] !== b[i][1]) return false;
  }
  return true;
}

export default function PolygonCanvas({
  src,
  width,
  height,
  masks,
  classes,
  activeClassId,
  selectedMaskId,
  readOnly = false,
  onCreate,
  onUpdate,
  onSelect,
  onDelete,
}: PolygonCanvasProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  const [size, setSize] = useState({ w: 0, h: 0 });
  const sizeRef = useRef(size);
  sizeRef.current = size;

  const [transform, setTransform] = useState<Transform>({ scale: 1, tx: 0, ty: 0 });
  // Mirrored into a ref so the imperative wheel/pointer handlers never do their
  // maths against a transform React has already superseded.
  const transformRef = useRef(transform);
  transformRef.current = transform;

  const [draft, setDraft] = useState<Point[]>([]);
  const [trace, setTrace] = useState<Point[]>([]);
  // The freehand trace lives in a ref as well as in state: pointer-up commits
  // from the ref, which cannot be a render behind the last sampled move.
  const traceRef = useRef<Point[]>([]);
  const putTrace = useCallback((pts: Point[]) => {
    traceRef.current = pts;
    setTrace(pts);
  }, []);
  const [cursorImg, setCursorImg] = useState<Point | null>(null);
  const [spaceHeld, setSpaceHeld] = useState(false);
  const [panning, setPanning] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /** Optimistic copy of the mask being dragged. Held until the caller's `masks`
   *  prop catches up, so the polygon does not snap back to its old shape for
   *  the duration of the PATCH round trip. */
  const [pending, setPending] = useState<{ maskId: string; points: Point[] } | null>(null);

  const gesture = useRef<Gesture>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const classById = useMemo(() => {
    const m = new Map<string, LabelClass>();
    for (const c of classes) m.set(c.class_id, c);
    return m;
  }, [classes]);

  const activeColor = activeClassId
    ? (classById.get(activeClassId)?.color ?? UNKNOWN_CLASS_COLOR)
    : "var(--color-accent, #5f8b6a)";

  const flash = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 2800);
  }, []);

  useEffect(() => {
    return () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    };
  }, []);

  // ---- view transform ------------------------------------------------------

  const applyTransform = useCallback(
    (next: Transform) => {
      const { w, h } = sizeRef.current;
      setTransform(w > 0 && h > 0 ? clampTransform(next, w, h, width, height) : next);
    },
    [width, height],
  );

  const fitView = useCallback(() => {
    const { w, h } = sizeRef.current;
    if (w <= 0 || h <= 0) return;
    applyTransform(fitTransform(w, h, width, height, FIT_PAD));
  }, [applyTransform, width, height]);

  /** Zoom about the middle of the viewport — what a keyboard or a button press
   *  means when there is no cursor position to anchor to. */
  const zoomBy = useCallback(
    (factor: number) => {
      const { w, h } = sizeRef.current;
      applyTransform(zoomAt(transformRef.current, factor, w / 2, h / 2, MIN_SCALE, MAX_SCALE));
    },
    [applyTransform],
  );

  const oneToOne = useCallback(() => {
    zoomBy(1 / transformRef.current.scale);
  }, [zoomBy]);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) setSize({ w: r.width, h: r.height });
    });
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // Fit once per crop, then only re-clamp. Refitting on every resize would throw
  // away the labeler's zoom whenever the window changed by a pixel.
  const fittedFor = useRef("");
  useEffect(() => {
    const { w, h } = sizeRef.current;
    if (w <= 0 || h <= 0) return;
    const key = `${src}|${width}x${height}`;
    if (fittedFor.current === key) {
      setTransform((t) => clampTransform(t, w, h, width, height));
      return;
    }
    fittedFor.current = key;
    setTransform(clampTransform(fitTransform(w, h, width, height, FIT_PAD), w, h, width, height));
  }, [src, width, height, size.w, size.h]);

  // A new crop must never inherit the previous one's half-drawn polygon.
  useEffect(() => {
    setDraft([]);
    putTrace([]);
    setPending(null);
    gesture.current = null;
  }, [src]);

  // Drop the optimistic copy once the caller's masks agree with it (or the mask
  // is gone), so a later server correction is not permanently masked by ours.
  useEffect(() => {
    if (!pending) return;
    const m = masks.find((x) => x.mask_id === pending.maskId);
    if (!m || samePoints(m.points, pending.points)) setPending(null);
  }, [masks, pending]);

  // Wheel must be a non-passive native listener: React's synthetic onWheel is
  // passive, and without preventDefault the page scrolls while you zoom.
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      const host = rootRef.current;
      if (!host) return;
      const r = host.getBoundingClientRect();
      // deltaMode 1 = lines, 2 = pages: normalise so a mouse notch and a
      // trackpad swipe do not zoom by wildly different amounts.
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      // ctrlKey here is the browser's pinch gesture, which arrives as a wheel
      // event and is meant to feel more direct than a scroll wheel.
      const k = e.ctrlKey ? 0.012 : 0.0022;
      const factor = Math.exp(-e.deltaY * unit * k);
      applyTransform(
        zoomAt(
          transformRef.current,
          factor,
          e.clientX - r.left,
          e.clientY - r.top,
          MIN_SCALE,
          MAX_SCALE,
        ),
      );
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [applyTransform]);

  // ---- coordinate helpers --------------------------------------------------

  /** Client coords -> container-local screen px (what `Transform` is defined in). */
  const toLocal = useCallback((clientX: number, clientY: number): Point => {
    const r = rootRef.current?.getBoundingClientRect();
    if (!r) return [0, 0];
    return [clientX - r.left, clientY - r.top];
  }, []);

  const toImage = useCallback(
    (clientX: number, clientY: number): Point => {
      const [lx, ly] = toLocal(clientX, clientY);
      return screenToImage(transformRef.current, lx, ly);
    },
    [toLocal],
  );

  const pointsOf = useCallback(
    (m: Mask): Point[] => (pending && pending.maskId === m.mask_id ? pending.points : m.points),
    [pending],
  );

  const selectedMask = useMemo(
    () => masks.find((m) => m.mask_id === selectedMaskId) ?? null,
    [masks, selectedMaskId],
  );
  const selectedPoints: Point[] = selectedMask ? pointsOf(selectedMask) : [];

  /**
   * Topmost mask under an image-space point, resolving overlaps by SMALLEST
   * area: a small instance drawn on top of a large blob has to stay clickable, and it
   * never would if the big polygon won every hit test.
   */
  const hitMask = useCallback(
    (p: Point): Mask | null => {
      let best: Mask | null = null;
      let bestArea = Infinity;
      for (const m of masks) {
        const pts = pointsOf(m);
        if (pts.length < 3 || !pointInPolygon(p, pts)) continue;
        const a = Math.abs(polygonArea(pts));
        if (a <= bestArea) {
          bestArea = a;
          best = m;
        }
      }
      return best;
    },
    [masks, pointsOf],
  );

  /** Index of a selected-polygon vertex within the screen-space grab radius. */
  const hitVertex = useCallback(
    (localX: number, localY: number): number | null => {
      if (selectedPoints.length === 0) return null;
      const t = transformRef.current;
      const screen = selectedPoints.map((p) => imageToScreen(t, p[0], p[1]));
      const hit = nearestVertex(screen, localX, localY);
      return hit && hit.dist <= HANDLE_HIT_PX ? hit.index : null;
    },
    [selectedPoints],
  );

  // ---- drawing -------------------------------------------------------------

  const commitDraft = useCallback(
    (pts: Point[]) => {
      if (readOnly) return;
      if (pts.length < 3) {
        flash("A polygon needs at least 3 points.");
        return;
      }
      if (!activeClassId) {
        // Refusing here rather than inventing a class: a mask with no class
        // cannot be stored, and silently dropping the work would be worse.
        flash("Pick a class first — a polygon cannot be saved without one.");
        return;
      }
      onCreate(pts.map((p) => clampPoint(p, width, height)));
      setDraft([]);
      putTrace([]);
      setCursorImg(null);
    },
    [readOnly, activeClassId, onCreate, width, height, flash],
  );

  const addDraftPoint = useCallback(
    (p: Point) => {
      if (readOnly) return;
      if (!activeClassId) {
        flash("Pick a class first — a polygon cannot be saved without one.");
        return;
      }
      const pt = clampPoint(p, width, height);
      const t = transformRef.current;
      // Closing gesture: a click on the first vertex, within a zoom-independent
      // screen radius. Decided out here, never inside a setState updater — an
      // updater has to stay pure or React 19's double-invoke creates two masks.
      if (draft.length >= 3) {
        const a = imageToScreen(t, draft[0][0], draft[0][1]);
        const b = imageToScreen(t, pt[0], pt[1]);
        if (distance(a, b) <= CLOSE_HIT_PX) {
          commitDraft(draft);
          return;
        }
      }
      // Swallow a duplicate: a double-click that slipped through would otherwise
      // leave a zero-length edge in the polygon.
      const last = draft[draft.length - 1];
      if (last && distance(last, pt) * t.scale < 1) return;
      setDraft([...draft, pt]);
    },
    [readOnly, activeClassId, width, height, flash, commitDraft, draft],
  );

  const deleteSelected = useCallback(() => {
    if (readOnly || !selectedMaskId) return;
    onDelete(selectedMaskId);
  }, [readOnly, selectedMaskId, onDelete]);

  // ---- pointer -------------------------------------------------------------

  function beginPan(localX: number, localY: number) {
    const t = transformRef.current;
    gesture.current = {
      kind: "pan",
      startX: localX,
      startY: localY,
      baseTx: t.tx,
      baseTy: t.ty,
    };
    setPanning(true);
  }

  function onPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    e.currentTarget.setPointerCapture(e.pointerId);
    const [lx, ly] = toLocal(e.clientX, e.clientY);

    // Pan is middle button, right button, or space-drag. Never plain left-drag:
    // that is drawing, and the two must not compete for the same gesture.
    if (e.button === 1 || e.button === 2 || spaceHeld) {
      // Firefox turns an unclaimed middle press into autoscroll.
      if (e.button === 1) e.preventDefault();
      beginPan(lx, ly);
      return;
    }
    if (e.button !== 0) return;
    if (readOnly) {
      // Read-only still gets to look around and inspect a mask.
      const img = screenToImage(transformRef.current, lx, ly);
      const hit = hitMask(img);
      onSelect(hit ? hit.mask_id : null);
      return;
    }

    // 1. A grab on a handle of the selected polygon beats everything else.
    const vi = hitVertex(lx, ly);
    if (vi !== null && selectedMask) {
      gesture.current = {
        kind: "vertex",
        maskId: selectedMask.mask_id,
        index: vi,
        points: selectedPoints.map((p) => [p[0], p[1]] as Point),
        startX: lx,
        startY: ly,
        moved: false,
      };
      return;
    }

    // 2. Mid-polygon, every left press belongs to the polygon being drawn.
    if (draft.length > 0 || e.shiftKey) {
      gesture.current = { kind: "draw", startX: lx, startY: ly, freehand: false };
      return;
    }

    // 3. Otherwise a press over a mask is a selection, not a new vertex.
    //    Shift (case 2) is the escape hatch for drawing INSIDE an existing mask.
    const img = screenToImage(transformRef.current, lx, ly);
    const hit = hitMask(img);
    if (hit) {
      if (hit.mask_id !== selectedMaskId) onSelect(hit.mask_id);
      return;
    }
    if (selectedMaskId) {
      onSelect(null);
      return;
    }
    gesture.current = { kind: "draw", startX: lx, startY: ly, freehand: false };
  }

  function onPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    const [lx, ly] = toLocal(e.clientX, e.clientY);
    const img = screenToImage(transformRef.current, lx, ly);
    // Tracked only while a polygon is open: the rubber band is its one consumer,
    // and a setState on every hover move would re-render the whole overlay.
    if (draft.length > 0) setCursorImg(img);

    const g = gesture.current;
    if (!g) return;

    if (g.kind === "pan") {
      applyTransform({
        scale: transformRef.current.scale,
        tx: g.baseTx + (lx - g.startX),
        ty: g.baseTy + (ly - g.startY),
      });
      return;
    }

    if (g.kind === "vertex") {
      if (!g.moved && Math.hypot(lx - g.startX, ly - g.startY) < VERTEX_DRAG_MIN_PX) return;
      const next = g.points.map((p, i) => (i === g.index ? clampPoint(img, width, height) : p));
      g.points = next;
      g.moved = true;
      // Local only. The write happens once, on pointer-up.
      setPending({ maskId: g.maskId, points: next });
      return;
    }

    if (g.kind === "draw") {
      // Freehand only from an empty draft: mixing click-placed vertices and a
      // traced arc in one polygon is a confusing gesture with no clear commit.
      if (!g.freehand && draft.length === 0) {
        if (Math.hypot(lx - g.startX, ly - g.startY) >= FREEHAND_START_PX) {
          if (!activeClassId) {
            flash("Pick a class first — a polygon cannot be saved without one.");
            gesture.current = null;
            return;
          }
          g.freehand = true;
          const start = screenToImage(transformRef.current, g.startX, g.startY);
          putTrace([clampPoint(start, width, height), clampPoint(img, width, height)]);
          return;
        }
      }
      if (g.freehand) putTrace([...traceRef.current, clampPoint(img, width, height)]);
    }
  }

  function onPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    const g = gesture.current;
    gesture.current = null;
    setPanning(false);
    if (!g) return;

    if (g.kind === "vertex") {
      // One network write per gesture — not one per mousemove.
      if (g.moved) onUpdate(g.maskId, g.points.map((p) => clampPoint(p, width, height)));
      return;
    }

    if (g.kind === "draw") {
      if (g.freehand) {
        const t = transformRef.current;
        const tol = FREEHAND_TOL_PX / t.scale;
        const simplified = simplify(traceRef.current, tol);
        // A closed trace usually ends on top of its start; that duplicate vertex
        // is invisible but survives into the export, so drop it.
        if (
          simplified.length >= 4 &&
          distance(simplified[0], simplified[simplified.length - 1]) * t.scale < CLOSE_HIT_PX
        ) {
          simplified.pop();
        }
        putTrace([]);
        commitDraft(simplified);
        return;
      }
      addDraftPoint(toImage(e.clientX, e.clientY));
    }
  }

  function onPointerCancel() {
    gesture.current = null;
    setPanning(false);
    putTrace([]);
  }

  function onDoubleClick(e: ReactPointerEvent<HTMLDivElement>) {
    if (readOnly || !selectedMask || draft.length > 0) return;
    const [lx, ly] = toLocal(e.clientX, e.clientY);
    const t = transformRef.current;
    const pts = selectedPoints;
    const screen = pts.map((p) => imageToScreen(t, p[0], p[1]));

    const v = nearestVertex(screen, lx, ly);
    if (v && v.dist <= HANDLE_HIT_PX) {
      if (pts.length <= 3) {
        flash("A polygon needs at least 3 points.");
        return;
      }
      onUpdate(
        selectedMask.mask_id,
        pts.filter((_, i) => i !== v.index).map((p) => clampPoint(p, width, height)),
      );
      return;
    }

    // Not a vertex: an edge. Insert where the edge was hit, which for the
    // visible midpoint handle is exactly the midpoint.
    const seg = nearestSegment(screen, lx, ly, true);
    if (seg && seg.dist <= HANDLE_HIT_PX) {
      const at = screenToImage(t, seg.point[0], seg.point[1]);
      const next = [...pts];
      next.splice(seg.index + 1, 0, clampPoint(at, width, height));
      onUpdate(selectedMask.mask_id, next);
    }
  }

  // ---- keyboard ------------------------------------------------------------

  // The listener mounts once and reads the latest commands through a ref, which
  // keeps it free of stale closures without re-binding on every keystroke.
  // 1..9 are deliberately NOT bound here: the app owns them for class picking.
  const cmds = useRef({
    closeDraft: () => {},
    undoPoint: () => {},
    cancel: () => {},
    remove: () => {},
    fit: () => {},
    oneToOne: () => {},
    zoomIn: () => {},
    zoomOut: () => {},
    hasDraft: false,
    hasSelection: false,
  });
  cmds.current = {
    closeDraft: () => commitDraft(draft),
    undoPoint: () => setDraft((p) => p.slice(0, -1)),
    cancel: () => {
      if (draft.length > 0 || trace.length > 0) {
        setDraft([]);
        putTrace([]);
        gesture.current = null;
      } else if (selectedMaskId) {
        onSelect(null);
      }
    },
    remove: deleteSelected,
    fit: fitView,
    oneToOne,
    zoomIn: () => zoomBy(ZOOM_STEP),
    zoomOut: () => zoomBy(1 / ZOOM_STEP),
    hasDraft: draft.length > 0,
    hasSelection: selectedMaskId !== null,
  };

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (isTypingTarget(e.target)) return;
      // Never steal a browser or OS chord (ctrl+0 resets page zoom, and so on).
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const c = cmds.current;
      switch (e.key) {
        case "Enter":
          if (c.hasDraft) {
            e.preventDefault();
            c.closeDraft();
          }
          return;
        case "Escape":
          c.cancel();
          return;
        case "Backspace":
          // Only claim the key when it actually does something here.
          if (c.hasDraft) {
            e.preventDefault();
            c.undoPoint();
          } else if (c.hasSelection) {
            e.preventDefault();
            c.remove();
          }
          return;
        case "Delete":
          if (c.hasSelection) {
            e.preventDefault();
            c.remove();
          }
          return;
        case "f":
        case "F":
          c.fit();
          return;
        case "0":
          c.oneToOne();
          return;
        case "[":
          c.zoomOut();
          return;
        case "]":
          c.zoomIn();
          return;
        case " ":
          // Space is the pan modifier; without preventDefault the page scrolls
          // under the canvas while the labeler is holding it.
          e.preventDefault();
          setSpaceHeld(true);
          return;
        default:
      }
    }
    function onKeyUp(e: KeyboardEvent) {
      if (e.key === " ") setSpaceHeld(false);
    }
    // A window blur mid-drag would otherwise leave the canvas stuck in pan mode.
    function onBlur() {
      setSpaceHeld(false);
    }
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  // ---- render --------------------------------------------------------------

  const { scale, tx, ty } = transform;
  const inv = 1 / scale;

  // Selected last, so its heavier stroke and handles are never buried under a
  // neighbouring polygon.
  const ordered = useMemo(
    () =>
      [...masks].sort(
        (a, b) => (a.mask_id === selectedMaskId ? 1 : 0) - (b.mask_id === selectedMaskId ? 1 : 0),
      ),
    [masks, selectedMaskId],
  );

  const closeReady =
    draft.length >= 3 &&
    cursorImg !== null &&
    distance(imageToScreen(transform, draft[0][0], draft[0][1]), imageToScreen(transform, cursorImg[0], cursorImg[1])) <=
      CLOSE_HIT_PX;

  const cursor = panning ? "grabbing" : spaceHeld ? "grab" : readOnly ? "default" : "crosshair";
  const selectedClass = selectedMask ? classById.get(selectedMask.class_id) : undefined;

  return (
    <div
      ref={rootRef}
      className="relative h-full w-full min-h-[22rem] overflow-hidden border border-border bg-surface-sunk select-none"
      style={{ touchAction: "none", cursor }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onPointerLeave={() => setCursorImg(null)}
      onDoubleClick={onDoubleClick}
      onContextMenu={(e) => e.preventDefault()}
    >
      {/* One transformed space for the pixels and the vectors alike. Anything
          that must keep a constant screen size counter-scales by `inv`. */}
      <div
        className="absolute top-0 left-0"
        style={{
          width,
          height,
          transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
          transformOrigin: "0 0",
        }}
      >
        <img
          src={src}
          width={width}
          height={height}
          alt=""
          draggable={false}
          className="block"
          style={{
            width,
            height,
            imageRendering: scale >= PIXELATE_AT ? "pixelated" : "auto",
          }}
        />
        <svg
          className="absolute top-0 left-0 pointer-events-none"
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
        >
          {ordered.map((m) => {
            const pts = pointsOf(m);
            if (pts.length < 3) return null;
            const color = classById.get(m.class_id)?.color ?? UNKNOWN_CLASS_COLOR;
            const selected = m.mask_id === selectedMaskId;
            const dim = activeClassId !== null && m.class_id !== activeClassId && !selected;
            return (
              <polygon
                key={m.mask_id}
                points={pts.map((p) => `${p[0]},${p[1]}`).join(" ")}
                fill={color}
                fillOpacity={selected ? 0.3 : dim ? 0.08 : 0.2}
                stroke={color}
                strokeOpacity={dim ? 0.4 : 1}
                strokeWidth={(selected ? 3 : 1.75) * inv}
                strokeLinejoin="round"
              />
            );
          })}

          {/* Handles for the selected polygon: solid = a vertex to drag or
              double-click away, hollow = an edge to double-click a vertex into. */}
          {!readOnly && selectedMask && selectedPoints.length >= 3
            ? (() => {
                const color = classById.get(selectedMask.class_id)?.color ?? UNKNOWN_CLASS_COLOR;
                const mids = selectedPoints.map((p, i) => {
                  const q = selectedPoints[(i + 1) % selectedPoints.length];
                  return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2] as Point;
                });
                return (
                  <g>
                    {mids.map((p, i) => (
                      <circle
                        key={`mid-${i}`}
                        cx={p[0]}
                        cy={p[1]}
                        r={(HANDLE_R_PX - 1.8) * inv}
                        fill="none"
                        stroke={color}
                        strokeOpacity={0.75}
                        strokeWidth={1.5 * inv}
                      />
                    ))}
                    {selectedPoints.map((p, i) => (
                      <circle
                        key={`v-${i}`}
                        cx={p[0]}
                        cy={p[1]}
                        r={HANDLE_R_PX * inv}
                        fill="var(--color-surface, #ffffff)"
                        stroke={color}
                        strokeWidth={2 * inv}
                      />
                    ))}
                  </g>
                );
              })()
            : null}

          {/* Freehand trace, live. */}
          {trace.length >= 2 ? (
            <polyline
              points={trace.map((p) => `${p[0]},${p[1]}`).join(" ")}
              fill={activeColor}
              fillOpacity={0.12}
              stroke={activeColor}
              strokeWidth={2 * inv}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ) : null}

          {/* The polygon being drawn, plus the rubber-band segment. */}
          {draft.length >= 3 ? (
            <polygon
              points={draft.map((p) => `${p[0]},${p[1]}`).join(" ")}
              fill={activeColor}
              fillOpacity={0.14}
              stroke="none"
            />
          ) : null}
          {draft.length >= 2 ? (
            <polyline
              points={draft.map((p) => `${p[0]},${p[1]}`).join(" ")}
              fill="none"
              stroke={activeColor}
              strokeWidth={2 * inv}
              strokeLinejoin="round"
            />
          ) : null}
          {draft.length >= 1 && cursorImg ? (
            <line
              x1={draft[draft.length - 1][0]}
              y1={draft[draft.length - 1][1]}
              x2={cursorImg[0]}
              y2={cursorImg[1]}
              stroke={activeColor}
              strokeWidth={1.5 * inv}
              strokeDasharray={`${5 * inv} ${4 * inv}`}
            />
          ) : null}
          {draft.map((p, i) => (
            <circle
              key={`d-${i}`}
              cx={p[0]}
              cy={p[1]}
              r={(i === 0 ? (closeReady ? HANDLE_R_PX + 3 : HANDLE_R_PX + 1) : HANDLE_R_PX - 1.5) * inv}
              fill={i === 0 && closeReady ? activeColor : "var(--color-surface, #ffffff)"}
              stroke={activeColor}
              strokeWidth={2 * inv}
            />
          ))}

          {/* Which class the selected mask belongs to, read off the canvas
              itself rather than by cross-referencing the picker. */}
          {selectedMask && selectedClass && selectedPoints.length >= 3
            ? (() => {
                const c = polygonCentroid(selectedPoints);
                return (
                  <text
                    x={c[0]}
                    y={c[1]}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize={11 * inv}
                    style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
                    fill="var(--color-near-black, #2b2a26)"
                    stroke="var(--color-surface, #ffffff)"
                    strokeWidth={3 * inv}
                    paintOrder="stroke"
                  >
                    {selectedClass.name}
                  </text>
                );
              })()
            : null}
        </svg>
      </div>

      {/* ---- chrome. stopPropagation everywhere, or a button press would also
              start a gesture on the canvas underneath. ---- */}
      <div
        className="absolute top-2 right-2 flex items-center gap-1"
        onPointerDown={(e) => e.stopPropagation()}
        onPointerUp={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        <span className="mr-1 font-mono text-[10px] text-gray-mid tabular-nums bg-surface/85 border border-border px-1.5 py-1">
          {scale >= 1 ? `${scale.toFixed(scale < 10 ? 1 : 0)}x` : `${Math.round(scale * 100)}%`}
        </span>
        <CanvasButton label="Zoom out ( [ )" onClick={() => zoomBy(1 / ZOOM_STEP)}>
          -
        </CanvasButton>
        <CanvasButton label="Zoom in ( ] )" onClick={() => zoomBy(ZOOM_STEP)}>
          +
        </CanvasButton>
        <CanvasButton label="Fit the crop ( f )" onClick={fitView}>
          fit
        </CanvasButton>
        <CanvasButton label="Actual pixels ( 0 )" onClick={oneToOne}>
          1:1
        </CanvasButton>
      </div>

      {notice ? (
        <div className="absolute top-2 left-2 max-w-[22rem] bg-surface border border-danger px-2.5 py-1.5 font-mono text-[11px] text-danger">
          {notice}
        </div>
      ) : null}

      {draft.length > 0 ? (
        <div className="absolute bottom-2 right-2 bg-surface/90 border border-border px-2 py-1 font-mono text-[10px] text-gray-mid tabular-nums">
          {draft.length} point{draft.length === 1 ? "" : "s"}
          {draft.length >= 3 ? " · Enter or click the first point to close" : " · need 3"}
        </div>
      ) : null}

      {/* Always visible: a labeler should never have to remember this, and a
          tooltip is not a place to keep a keyboard map. */}
      <div
        className="absolute bottom-2 left-2 bg-surface/90 border border-border px-2.5 py-2 font-mono text-[10px] leading-[1.5] text-gray-mid"
        onPointerDown={(e) => e.stopPropagation()}
      >
        {readOnly ? (
          <div>
            <Key>wheel</Key> zoom · <Key>space</Key>/<Key>middle</Key>-drag pan · <Key>f</Key> fit ·{" "}
            <Key>0</Key> 1:1 — read only
          </div>
        ) : (
          <>
            <div>
              <Key>click</Key> add point · <Key>drag</Key> freehand · <Key>Enter</Key> close ·{" "}
              <Key>Backspace</Key> undo point · <Key>Esc</Key> cancel
            </div>
            <div>
              <Key>click</Key> a mask to select · <Key>drag</Key> a handle · <Key>dbl-click</Key> a
              handle removes, an edge adds · <Key>Del</Key> delete mask
            </div>
            <div>
              <Key>shift</Key>+drag draws inside a mask · <Key>space</Key>/<Key>middle</Key>/
              <Key>right</Key>-drag pan · <Key>wheel</Key> zoom · <Key>f</Key> fit · <Key>0</Key>{" "}
              1:1 · <Key>[</Key>
              <Key>]</Key> zoom
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Key({ children }: { children: ReactNode }) {
  return (
    <span className="text-near-black border border-border bg-surface-sunk px-1 py-px">
      {children}
    </span>
  );
}

function CanvasButton({
  children,
  label,
  onClick,
}: {
  children: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className="min-w-[1.9rem] bg-surface/85 border border-border px-1.5 py-1 font-mono text-[10px] text-gray-mid hover:border-accent hover:text-accent transition-colors"
    >
      {children}
    </button>
  );
}
