// Formation generators for the drone swarm.
//
// Every generator returns points in a local frame centred on the origin,
// roughly within a 70-unit radius. `fitToCount` then maps them onto exactly
// `n` drones: extra points are thinned evenly, and spare drones get their
// lights switched off (colour 0,0,0) exactly like a real show parks them.

import type { ShowAction } from './actions';

export type RGB = [number, number, number];

export interface PointCloud {
  pos: number[];
  col: number[];
}

export interface Formation {
  positions: Float32Array;
  colors: Float32Array;
  /** Rotation around the vertical axis, rad/s. */
  spin: number;
  /** Gentle left/right sway amplitude (rad) for flat shapes such as text. */
  sway: number;
  /** Cloth-like ripple amplitude (world units). */
  ripple: number;
  /** Recolour with an animated rainbow every frame. */
  rainbow: boolean;
  /** Half of the formation's width, used to fit narrow screens. */
  halfWidth: number;
}

// ── colour helpers ──────────────────────────────────────────────────────

export function hexToRgb(hex: string): RGB {
  let h = hex.replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const v = parseInt(h, 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}

export function hsl(h: number, s: number, l: number): RGB {
  const hue = ((h % 1) + 1) % 1;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + hue * 12) % 12;
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
  };
  return [f(0), f(8), f(4)];
}

function mixRgb(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

/** Small deterministic PRNG so formations are stable between renders. */
export function mulberry32(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function cloud(): PointCloud {
  return { pos: [], col: [] };
}

function push(c: PointCloud, x: number, y: number, z: number, rgb: RGB): void {
  c.pos.push(x, y, z);
  c.col.push(rgb[0], rgb[1], rgb[2]);
}

// ── geometric shapes ────────────────────────────────────────────────────

export function sphere(n: number, radius = 55): PointCloud {
  const c = cloud();
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < n; i++) {
    const y = 1 - (i / Math.max(n - 1, 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = golden * i;
    const col = mixRgb([0.1, 0.35, 1], [0.2, 0.95, 1], (y + 1) / 2);
    push(c, Math.cos(th) * r * radius, y * radius, Math.sin(th) * r * radius, col);
  }
  return c;
}

export function jarvis(n: number): PointCloud {
  // Arc-reactor logo: bright core plus three tilted orbital rings.
  const c = cloud();
  const core = Math.floor(n * 0.16);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < core; i++) {
    const y = 1 - (i / Math.max(core - 1, 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    push(c, Math.cos(golden * i) * r * 11, y * 11, Math.sin(golden * i) * r * 11, [0.85, 0.97, 1]);
  }
  const rings: Array<{ radius: number; tiltX: number; tiltZ: number; col: RGB }> = [
    { radius: 62, tiltX: 0, tiltZ: 0, col: [0.15, 0.85, 1] },
    { radius: 46, tiltX: 1.1, tiltZ: 0.5, col: [0.25, 0.6, 1] },
    { radius: 32, tiltX: -0.9, tiltZ: -0.7, col: [0.5, 0.95, 1] },
  ];
  const perRing = Math.floor((n - core) / rings.length);
  for (const ring of rings) {
    for (let i = 0; i < perRing; i++) {
      const a = (i / perRing) * Math.PI * 2;
      let x = Math.cos(a) * ring.radius;
      let y = 0;
      let z = Math.sin(a) * ring.radius;
      // tilt around X then Z
      const cy = Math.cos(ring.tiltX);
      const sy = Math.sin(ring.tiltX);
      [y, z] = [y * cy - z * sy, y * sy + z * cy];
      const cz = Math.cos(ring.tiltZ);
      const sz = Math.sin(ring.tiltZ);
      [x, y] = [x * cz - y * sz, x * sz + y * cz];
      push(c, x, y, z, ring.col);
    }
  }
  return c;
}

/**
 * Fill a 2D region on a regular grid (as real shows do) with about `n`
 * drones, bulging the middle towards the audience so it reads as 3D.
 */
export function gridFill(
  n: number,
  inside: (x: number, y: number) => boolean,
  half: number,
  bulge: number,
  color: (x: number, y: number, r: number) => RGB,
): PointCloud {
  let spacing = (2 * half) / Math.sqrt(n);
  let c = cloud();
  for (let attempt = 0; attempt < 4; attempt++) {
    c = cloud();
    const offset = half - Math.floor(half / spacing) * spacing;
    for (let y = -half + offset; y <= half; y += spacing) {
      for (let x = -half + offset; x <= half; x += spacing) {
        if (!inside(x, y)) continue;
        const r = Math.min(1, Math.hypot(x, y) / half);
        push(c, x, y, Math.sqrt(1 - r * r) * bulge, color(x, y, r));
      }
    }
    const got = c.pos.length / 3;
    if (Math.abs(got - n) / n < 0.08) break;
    spacing *= Math.sqrt(got / n);
  }
  return c;
}

export function heart(n: number): PointCloud {
  const size = 50;
  return gridFill(
    n,
    (px, py) => {
      // (x² + y² - 1)³ - x² y³ <= 0
      const x = px / size;
      const y = (py - 6) / size;
      const q = x * x + y * y - 1;
      return q * q * q - x * x * y * y * y <= 0;
    },
    70,
    16,
    (_x, y) => mixRgb([1, 0.05, 0.18], [1, 0.4, 0.6], (y + 60) / 130),
  );
}

export function star(n: number): PointCloud {
  const outer = 66;
  const inner = 27;
  const pts: Array<[number, number]> = [];
  for (let i = 0; i < 10; i++) {
    const a = Math.PI / 2 + (i * Math.PI) / 5;
    const r = i % 2 === 0 ? outer : inner;
    pts.push([Math.cos(a) * r, Math.sin(a) * r]);
  }
  const inside = (x: number, y: number) => {
    let hit = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i];
      const [xj, yj] = pts[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
    }
    return hit;
  };
  return gridFill(n, inside, outer, 14, (_x, _y, r) => mixRgb([1, 0.97, 0.75], [1, 0.62, 0.08], r));
}

export function planet(n: number): PointCloud {
  const c = cloud();
  const bodyCount = Math.floor(n * 0.62);
  const golden = Math.PI * (3 - Math.sqrt(5));
  const tilt = 0.42;
  for (let i = 0; i < bodyCount; i++) {
    const y = 1 - (i / Math.max(bodyCount - 1, 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = golden * i;
    const band = 0.5 + 0.5 * Math.sin(y * 14);
    const col = mixRgb([0.95, 0.55, 0.2], [1, 0.88, 0.65], band);
    push(c, Math.cos(th) * r * 36, y * 36, Math.sin(th) * r * 36, col);
  }
  const rnd = mulberry32(3);
  const ringCount = n - bodyCount;
  for (let i = 0; i < ringCount; i++) {
    const a = rnd() * Math.PI * 2;
    const rr = 48 + rnd() * 22;
    const x = Math.cos(a) * rr;
    const z = Math.sin(a) * rr;
    const y = x * Math.sin(tilt) * 0.5;
    const col = mixRgb([1, 0.9, 0.7], [0.8, 0.65, 0.45], (rr - 48) / 22);
    push(c, x * Math.cos(tilt), y + (rnd() - 0.5) * 1.2, z, col);
  }
  return c;
}

export function galaxy(n: number): PointCloud {
  const c = cloud();
  const rnd = mulberry32(21);
  const arms = 3;
  for (let i = 0; i < n; i++) {
    const t = Math.pow(rnd(), 0.7);
    const r = t * 70;
    const arm = i % arms;
    const a = (arm / arms) * Math.PI * 2 + t * 5.2 + (rnd() - 0.5) * 0.45 * (1 - t * 0.5);
    const spread = (1 - t) * 6 + 1.5;
    const x = Math.cos(a) * r + (rnd() - 0.5) * spread;
    const z = Math.sin(a) * r + (rnd() - 0.5) * spread;
    const y = (rnd() - 0.5) * (1 - t) * 10;
    const col = mixRgb([1, 0.92, 0.75], hsl(0.62 + t * 0.18, 0.9, 0.6), Math.min(1, t * 1.6));
    // tilt the disc towards the audience
    push(c, x, y * 0.9 + z * 0.45, z * 0.9 - y * 0.45, col);
  }
  return c;
}

export function dna(n: number): PointCloud {
  const c = cloud();
  const turns = 2.6;
  const height = 130;
  const strandCount = Math.floor(n * 0.7);
  const perStrand = Math.floor(strandCount / 2);
  for (let s = 0; s < 2; s++) {
    for (let i = 0; i < perStrand; i++) {
      const t = i / perStrand;
      const a = t * turns * Math.PI * 2 + s * Math.PI;
      push(c, Math.cos(a) * 26, t * height - height / 2, Math.sin(a) * 26, s ? [1, 0.25, 0.8] : [0.2, 0.9, 1]);
    }
  }
  const rungs = 26;
  const perRung = Math.floor((n - strandCount) / rungs);
  for (let r = 0; r < rungs; r++) {
    const t = (r + 0.5) / rungs;
    const a = t * turns * Math.PI * 2;
    for (let i = 0; i < perRung; i++) {
      const u = i / Math.max(perRung - 1, 1);
      const x = Math.cos(a) * 26 * (1 - 2 * u);
      const z = Math.sin(a) * 26 * (1 - 2 * u);
      push(c, x, t * height - height / 2, z, [0.9, 0.95, 1]);
    }
  }
  return c;
}

export function cube(n: number): PointCloud {
  // Wireframe cube: drones evenly spaced along the 12 edges.
  const c = cloud();
  const s = 44;
  const corners: Array<[number, number, number]> = [];
  for (const x of [-s, s]) for (const y of [-s, s]) for (const z of [-s, s]) corners.push([x, y, z]);
  const edges: Array<[number, number]> = [];
  for (let i = 0; i < 8; i++) {
    for (let j = i + 1; j < 8; j++) {
      const d = corners[i].reduce((acc, v, k) => acc + (v !== corners[j][k] ? 1 : 0), 0);
      if (d === 1) edges.push([i, j]);
    }
  }
  const per = Math.floor(n / edges.length);
  for (const [a, b] of edges) {
    for (let i = 0; i < per; i++) {
      const t = i / per;
      const p = corners[a].map((v, k) => v + (corners[b][k] - v) * t);
      const col = mixRgb([0.2, 0.9, 1], [0.6, 0.4, 1], (p[1] + s) / (2 * s));
      push(c, p[0], p[1], p[2], col);
    }
  }
  return c;
}

export function torus(n: number): PointCloud {
  const c = cloud();
  const rings = Math.max(8, Math.round(Math.sqrt(n / 2.5)));
  const per = Math.floor(n / rings);
  for (let i = 0; i < rings; i++) {
    const u = (i / rings) * Math.PI * 2;
    for (let j = 0; j < per; j++) {
      const v = (j / per) * Math.PI * 2;
      const r = 50 + 18 * Math.cos(v);
      const x = r * Math.cos(u);
      const z = r * Math.sin(u);
      const y = 18 * Math.sin(v);
      push(c, x, y * 0.9 + z * 0.4, z * 0.9 - y * 0.4, hsl(i / rings, 0.9, 0.6));
    }
  }
  return c;
}

export function wave(n: number): PointCloud {
  // A flat grid; the swarm animates it as a waving flag via `ripple`.
  const c = cloud();
  const cols = Math.round(Math.sqrt(n * 1.8));
  const rows = Math.floor(n / cols);
  for (let r = 0; r < rows; r++) {
    for (let q = 0; q < cols; q++) {
      const x = (q / (cols - 1) - 0.5) * 150;
      const y = (r / (rows - 1) - 0.5) * 84;
      push(c, x, y, 0, hsl(0.55 + (q / cols) * 0.25, 0.9, 0.55));
    }
  }
  return c;
}

// ── canvas-sampled shapes (text, emoji) ─────────────────────────────────

type Draw = (ctx: CanvasRenderingContext2D, w: number, h: number) => void;

/** Rasterise with `draw`, then place drones on a regular grid of lit pixels. */
export function sampleCanvas(
  draw: Draw,
  w: number,
  h: number,
  n: number,
  worldWidth: number,
  keepColors: boolean,
): PointCloud {
  const c = cloud();
  if (typeof document === 'undefined') return c;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return c;
  draw(ctx, w, h);
  const data = ctx.getImageData(0, 0, w, h).data;

  let lit = 0;
  let minX = w;
  let maxX = 0;
  let minY = h;
  let maxY = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      if (data[(y * w + x) * 4 + 3] > 120) {
        lit++;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (!lit) return c;
  const step = Math.max(1, Math.sqrt(lit / n));
  const scale = worldWidth / Math.max(maxX - minX, 1);
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  for (let y = minY; y <= maxY; y += step) {
    for (let x = minX; x <= maxX; x += step) {
      const idx = (Math.floor(y) * w + Math.floor(x)) * 4;
      if (data[idx + 3] <= 120) continue;
      let rgb: RGB;
      if (keepColors) {
        rgb = [data[idx] / 255, data[idx + 1] / 255, data[idx + 2] / 255];
        const peak = Math.max(...rgb);
        // Drones emit light: lift dark pixels so outlines stay visible.
        if (peak < 0.2) rgb = [0.12, 0.12, 0.14];
        else rgb = rgb.map((v) => Math.min(1, v / Math.max(peak, 0.55))) as RGB;
      } else {
        rgb = [1, 1, 1];
      }
      push(c, (x - cx) * scale, -(y - cy) * scale, 0, rgb);
    }
  }
  return c;
}

export function textShape(n: number, text: string): PointCloud {
  const clean = text.trim().toUpperCase() || 'JARVIS';
  const words = clean.split(/\s+/);
  const lines =
    clean.length > 8 && words.length > 1
      ? [words.slice(0, Math.ceil(words.length / 2)).join(' '), words.slice(Math.ceil(words.length / 2)).join(' ')]
      : [clean];
  const longest = Math.max(...lines.map((l) => l.length));
  const worldWidth = Math.min(210, 30 + longest * 24);
  const cloudPts = sampleCanvas(
    (ctx, w, h) => {
      const font = (size: number) => `900 ${size}px "Geist Variable", "Arial Black", system-ui, sans-serif`;
      let size = Math.floor(h / lines.length) * 0.8;
      ctx.font = font(size);
      const widest = Math.max(...lines.map((l) => ctx.measureText(l).width));
      if (widest > w * 0.94) size *= (w * 0.94) / widest;
      ctx.font = font(size);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      lines.forEach((line, i) => {
        ctx.fillText(line, w / 2, (h / (lines.length + 1)) * (i + 1));
      });
    },
    1024,
    lines.length > 1 ? 400 : 240,
    n,
    worldWidth,
    false,
  );
  // Default palette: icy white to cyan, top to bottom.
  const ys = cloudPts.pos.filter((_, i) => i % 3 === 1);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  for (let i = 0; i < ys.length; i++) {
    const col = mixRgb([0.2, 0.85, 1], [0.95, 0.98, 1], (ys[i] - minY) / Math.max(maxY - minY, 1));
    cloudPts.col[i * 3] = col[0];
    cloudPts.col[i * 3 + 1] = col[1];
    cloudPts.col[i * 3 + 2] = col[2];
  }
  return cloudPts;
}

export function emojiShape(n: number, emoji: string): PointCloud {
  return sampleCanvas(
    (ctx, w, h) => {
      ctx.font = `${Math.floor(h * 0.8)}px "Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(emoji, w / 2, h / 2 + h * 0.04);
    },
    220,
    220,
    n,
    120,
    true,
  );
}

export function clockText(date = new Date()): string {
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

// ── fireworks ───────────────────────────────────────────────────────────

/** Two formations per salvo: drones gather at launch points, then burst. */
export function fireworks(n: number, seed: number): [PointCloud, PointCloud] {
  const rnd = mulberry32(seed);
  const bursts = 4;
  const centers = Array.from({ length: bursts }, () => [
    (rnd() - 0.5) * 150,
    (rnd() - 0.5) * 60 + 10,
    (rnd() - 0.5) * 60,
  ]);
  const hues = centers.map(() => rnd());
  const gather = cloud();
  const burst = cloud();
  for (let i = 0; i < n; i++) {
    const b = i % bursts;
    const [cx, cy, cz] = centers[b];
    const u = rnd() * 2 - 1;
    const th = rnd() * Math.PI * 2;
    const r = Math.sqrt(1 - u * u);
    push(gather, cx + (rnd() - 0.5) * 3, cy - 55 + (rnd() - 0.5) * 3, cz, [1, 0.85, 0.6]);
    const radius = 26 + rnd() * 8;
    const tip = rnd() < 0.15;
    const col = tip ? ([1, 1, 1] as RGB) : hsl(hues[b] + (rnd() - 0.5) * 0.06, 1, 0.58);
    push(burst, cx + Math.cos(th) * r * radius, cy + u * radius - 4, cz + Math.sin(th) * r * radius, col);
  }
  return [gather, burst];
}

// ── assignment / fitting ────────────────────────────────────────────────

/** Index along a 2D Hilbert curve (n must be a power of two). */
export function hilbertIndex(xIn: number, yIn: number, n = 256): number {
  let x = xIn;
  let y = yIn;
  let d = 0;
  for (let s = n >> 1; s > 0; s >>= 1) {
    const rx = (x & s) > 0 ? 1 : 0;
    const ry = (y & s) > 0 ? 1 : 0;
    d += s * s * ((3 * rx) ^ ry);
    if (ry === 0) {
      if (rx === 1) {
        x = n - 1 - x;
        y = n - 1 - y;
      }
      [x, y] = [y, x];
    }
  }
  return d;
}

/** Order indices so spatially close points are close in the list. */
export function spatialOrder(pos: ArrayLike<number>, count: number): number[] {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (let i = 0; i < count; i++) {
    const x = pos[i * 3] + pos[i * 3 + 2] * 0.3;
    const y = pos[i * 3 + 1];
    minX = Math.min(minX, x);
    maxX = Math.max(maxX, x);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y);
  }
  const sx = 255 / Math.max(maxX - minX, 1e-6);
  const sy = 255 / Math.max(maxY - minY, 1e-6);
  const keys = new Float64Array(count);
  for (let i = 0; i < count; i++) {
    const x = Math.round((pos[i * 3] + pos[i * 3 + 2] * 0.3 - minX) * sx);
    const y = Math.round((pos[i * 3 + 1] - minY) * sy);
    keys[i] = hilbertIndex(x, y) + i / (count + 1);
  }
  return Array.from({ length: count }, (_, i) => i).sort((a, b) => keys[a] - keys[b]);
}

/** Resample a point cloud to exactly `n` drones, sorted along the Hilbert curve. */
export function fitToCount(c: PointCloud, n: number): { positions: Float32Array; colors: Float32Array } {
  const m = c.pos.length / 3;
  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  if (m === 0) return { positions, colors };
  const order = spatialOrder(c.pos, m);
  for (let i = 0; i < n; i++) {
    const lit = i < Math.min(m, n) || m >= n;
    // Spread the lit drones evenly along the curve when thinning.
    const src = order[m >= n ? Math.floor((i * m) / n) : i % m];
    positions[i * 3] = c.pos[src * 3];
    positions[i * 3 + 1] = c.pos[src * 3 + 1];
    positions[i * 3 + 2] = c.pos[src * 3 + 2] - (lit ? 0 : 6);
    if (lit) {
      colors[i * 3] = c.col[src * 3];
      colors[i * 3 + 1] = c.col[src * 3 + 1];
      colors[i * 3 + 2] = c.col[src * 3 + 2];
    }
  }
  return { positions, colors };
}

function halfWidth(positions: Float32Array): number {
  let w = 0;
  for (let i = 0; i < positions.length; i += 3) w = Math.max(w, Math.abs(positions[i]));
  return w;
}

/** Build the target formation for `action` on `n` drones. */
export function buildFormation(action: ShowAction, n: number, now = new Date()): Formation {
  let pts: PointCloud;
  let spin = 0.25;
  let sway = 0;
  let ripple = 0;
  switch (action.shape) {
    case 'jarvis':
      pts = jarvis(n);
      spin = 0.35;
      break;
    case 'sphere':
      pts = sphere(n);
      break;
    case 'heart':
      pts = heart(n);
      spin = 0;
      sway = 0.5;
      break;
    case 'star':
      pts = star(n);
      spin = 0;
      sway = 0.45;
      break;
    case 'planet':
      pts = planet(n);
      spin = 0.18;
      break;
    case 'galaxy':
      pts = galaxy(n);
      spin = 0.2;
      break;
    case 'dna':
      pts = dna(n);
      spin = 0.5;
      break;
    case 'cube':
      pts = cube(n);
      spin = 0.4;
      break;
    case 'torus':
      pts = torus(n);
      spin = 0.3;
      break;
    case 'wave':
      pts = wave(n);
      spin = 0;
      ripple = 7;
      break;
    case 'text':
      pts = textShape(n, action.text ?? 'JARVIS');
      spin = 0;
      sway = 0.18;
      break;
    case 'emoji':
      pts = emojiShape(n, action.emoji ?? '✨');
      spin = 0;
      sway = 0.3;
      break;
    case 'clock':
      pts = textShape(n, clockText(now));
      spin = 0;
      sway = 0.12;
      break;
    case 'fireworks':
      pts = fireworks(n, 1)[1];
      spin = 0;
      break;
  }
  if (typeof action.spin === 'number') spin = action.spin;

  const rainbow = action.color === 'rainbow';
  if (action.color && !rainbow) {
    const base = hexToRgb(action.color);
    const white: RGB = [1, 1, 1];
    for (let i = 0; i < pts.col.length; i += 3) {
      // Keep a little of the shape's own shading so it doesn't look flat.
      const shade = Math.max(pts.col[i], pts.col[i + 1], pts.col[i + 2]);
      const col = mixRgb(base, white, Math.max(0, shade - 0.85) * 0.6);
      pts.col[i] = col[0];
      pts.col[i + 1] = col[1];
      pts.col[i + 2] = col[2];
    }
  }
  const { positions, colors } = fitToCount(pts, n);
  return { positions, colors, spin, sway, ripple, rainbow, halfWidth: halfWidth(positions) };
}
