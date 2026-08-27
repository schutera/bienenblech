/**
 * Pure 2-D geometry for the crop annotation canvas.
 *
 * WHY this module exists at all: the editor constantly moves points between two
 * spaces — CROP-LOCAL image pixels (what the API transmits and the DB derives
 * from, SPEC section 3) and container-local screen pixels (what a PointerEvent
 * hands us). A polygon that reloads a few pixels off from where it was drawn is
 * the single most likely bug in this codebase, and the cause is always two
 * slightly divergent copies of that mapping. So there is exactly ONE pair here,
 * `screenToImage` / `imageToScreen`, and everything else goes through it.
 *
 * No React and no DOM in this file, deliberately: every export is a pure
 * function of numbers, so it can be unit-tested without a browser and reasoned
 * about without a render.
 */

export type Point = [number, number];

/**
 * View transform, defined as `screen = image * scale + t`.
 *
 * `tx`/`ty` are CONTAINER-LOCAL screen pixels — i.e. measured from the
 * viewport element's `getBoundingClientRect()` origin, never raw
 * `clientX`/`clientY` — so the maths stays valid while the page scrolls or the
 * editor moves within the layout.
 */
export type Transform = { scale: number; tx: number; ty: number };

/** A nearest-thing query result. `dist` is in the same space as the inputs. */
export type Hit = { index: number; dist: number };

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

/** Euclidean distance between two points, in whatever space they share. */
export function distance(a: Point, b: Point): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1]);
}

/** Container-local screen px -> image px. The inverse of `imageToScreen`. */
export function screenToImage(t: Transform, sx: number, sy: number): Point {
  const s = t.scale || 1;
  return [(sx - t.tx) / s, (sy - t.ty) / s];
}

/** Image px -> container-local screen px. The inverse of `screenToImage`. */
export function imageToScreen(t: Transform, x: number, y: number): Point {
  return [x * t.scale + t.tx, y * t.scale + t.ty];
}

/**
 * The transform that centres the whole image inside the container with `pad`
 * screen px of breathing room on every side. Never upscales past what fits, so
 * "fit" always shows the entire crop.
 */
export function fitTransform(
  containerW: number,
  containerH: number,
  imageW: number,
  imageH: number,
  pad = 0,
): Transform {
  const availW = Math.max(1, containerW - pad * 2);
  const availH = Math.max(1, containerH - pad * 2);
  const scale = Math.min(availW / Math.max(1, imageW), availH / Math.max(1, imageH));
  return {
    scale,
    tx: (containerW - imageW * scale) / 2,
    ty: (containerH - imageH * scale) / 2,
  };
}

/**
 * Zoom by `factor` about a fixed screen point.
 *
 * WHY the extra algebra: the image point currently under the cursor must not
 * move, otherwise the view slides away from whatever the annotator was aiming
 * at. Solve `screen = img * scale + t` for `t` at the new scale with `img` and
 * `screen` held fixed:  `t' = screen - img * scale'`.
 */
export function zoomAt(
  t: Transform,
  factor: number,
  screenX: number,
  screenY: number,
  min: number,
  max: number,
): Transform {
  const next = clamp(t.scale * factor, min, max);
  if (next === t.scale) return t;
  const [ix, iy] = screenToImage(t, screenX, screenY);
  return { scale: next, tx: screenX - ix * next, ty: screenY - iy * next };
}

/**
 * Keep the image from being flung entirely out of the viewport.
 *
 * The rule is deliberately loose rather than "the image must cover the
 * container": annotators need to drag a crop edge towards the middle of the
 * screen to work comfortably on instances clipped by the tile boundary. So the
 * only invariant is that at least `minVisible` px of the image stay inside the
 * container on each axis (or the whole of it, if it is smaller than that).
 */
export function clampTransform(
  t: Transform,
  containerW: number,
  containerH: number,
  imageW: number,
  imageH: number,
  minVisible = 48,
): Transform {
  const w = imageW * t.scale;
  const h = imageH * t.scale;
  const kx = Math.min(minVisible, w, containerW);
  const ky = Math.min(minVisible, h, containerH);
  return {
    scale: t.scale,
    tx: clamp(t.tx, kx - w, containerW - kx),
    ty: clamp(t.ty, ky - h, containerH - ky),
  };
}

/** Clamp a point into the `0..w` / `0..h` image rect (SPEC section 3). */
export function clampPoint(p: Point, w: number, h: number): Point {
  return [clamp(p[0], 0, w), clamp(p[1], 0, h)];
}

/**
 * Even-odd ray cast. Self-intersecting polygons are accepted by the API, so
 * this is "the crossing-number answer", not "the visually obvious answer" —
 * good enough for hit-testing and cheap.
 */
export function pointInPolygon(p: Point, poly: readonly Point[]): boolean {
  if (poly.length < 3) return false;
  const [x, y] = p;
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    const straddles = yi > y !== yj > y;
    if (straddles && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/**
 * Shoelace area. SIGNED: the sign is just the winding direction, and callers
 * that want a magnitude take `Math.abs`.
 *
 * It stays signed because `polygonCentroid` divides by it, and it uses the
 * cross-product form rather than the trapezoid form for the same reason: the
 * trapezoid form yields the NEGATED area, which silently mirrors every centroid
 * through the origin. That mismatch is exactly the bug this comment exists to
 * stop someone reintroducing.
 */
export function polygonArea(poly: readonly Point[]): number {
  if (poly.length < 3) return 0;
  let acc = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    acc += poly[j][0] * poly[i][1] - poly[i][0] * poly[j][1];
  }
  return acc / 2;
}

/**
 * Area centroid, falling back to the vertex mean for degenerate (zero-area)
 * rings — an annotator can and will produce a collapsed polygon, and a NaN
 * label position would take the whole overlay down with it.
 */
export function polygonCentroid(poly: readonly Point[]): Point {
  if (poly.length === 0) return [0, 0];
  if (poly.length < 3) {
    const n = poly.length;
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n];
  }
  const a = polygonArea(poly);
  if (Math.abs(a) < 1e-9) {
    const n = poly.length;
    return [poly.reduce((s, p) => s + p[0], 0) / n, poly.reduce((s, p) => s + p[1], 0) / n];
  }
  let cx = 0;
  let cy = 0;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const cross = poly[j][0] * poly[i][1] - poly[i][0] * poly[j][1];
    cx += (poly[j][0] + poly[i][0]) * cross;
    cy += (poly[j][1] + poly[i][1]) * cross;
  }
  return [cx / (6 * a), cy / (6 * a)];
}

/** Nearest vertex to `(x, y)`, or null for an empty ring. */
export function nearestVertex(points: readonly Point[], x: number, y: number): Hit | null {
  let best: Hit | null = null;
  for (let i = 0; i < points.length; i++) {
    const d = Math.hypot(points[i][0] - x, points[i][1] - y);
    if (!best || d < best.dist) best = { index: i, dist: d };
  }
  return best;
}

/** Distance from `p` to the segment `a`-`b`, plus the closest point on it. */
export function pointSegmentDistance(
  p: Point,
  a: Point,
  b: Point,
): { dist: number; point: Point; t: number } {
  const vx = b[0] - a[0];
  const vy = b[1] - a[1];
  const len2 = vx * vx + vy * vy;
  const t = len2 === 0 ? 0 : clamp(((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / len2, 0, 1);
  const point: Point = [a[0] + t * vx, a[1] + t * vy];
  return { dist: distance(p, point), point, t };
}

/**
 * Nearest edge to `(x, y)`. `index` is the edge's FIRST vertex, so the edge runs
 * `points[index] -> points[(index + 1) % n]` when `closed`. `point` is the
 * projection onto that edge, which is where a newly inserted vertex belongs.
 */
export function nearestSegment(
  points: readonly Point[],
  x: number,
  y: number,
  closed = true,
): { index: number; dist: number; point: Point } | null {
  const n = points.length;
  if (n < 2) return null;
  const last = closed ? n : n - 1;
  const p: Point = [x, y];
  let best: { index: number; dist: number; point: Point } | null = null;
  for (let i = 0; i < last; i++) {
    const a = points[i];
    const b = points[(i + 1) % n];
    const { dist, point } = pointSegmentDistance(p, a, b);
    if (!best || dist < best.dist) best = { index: i, dist, point };
  }
  return best;
}

/**
 * Ramer-Douglas-Peucker simplification, used to turn a freehand trace into a
 * polygon a human can still edit vertex by vertex.
 *
 * Iterative rather than recursive on purpose: a freehand drag easily produces a
 * few thousand samples, and the naive recursion blows the stack on the
 * degenerate near-collinear case.
 */
export function simplify(points: readonly Point[], tolerance: number): Point[] {
  const n = points.length;
  if (n <= 2 || tolerance <= 0) return points.slice();
  const keep = new Array<boolean>(n).fill(false);
  keep[0] = true;
  keep[n - 1] = true;
  const stack: Array<[number, number]> = [[0, n - 1]];
  while (stack.length > 0) {
    const span = stack.pop();
    if (!span) break;
    const [i, j] = span;
    let worst = -1;
    let worstAt = -1;
    for (let k = i + 1; k < j; k++) {
      const d = pointSegmentDistance(points[k], points[i], points[j]).dist;
      if (d > worst) {
        worst = d;
        worstAt = k;
      }
    }
    if (worstAt > 0 && worst > tolerance) {
      keep[worstAt] = true;
      stack.push([i, worstAt], [worstAt, j]);
    }
  }
  const out: Point[] = [];
  for (let i = 0; i < n; i++) if (keep[i]) out.push(points[i]);
  return out;
}
