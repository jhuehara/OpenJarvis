// Real-time drone light show renderer (Three.js + bloom).
//
// Each drone is a point sprite with an LED core and halo, flown by a
// critically damped spring towards its slot in the current formation. A
// mirrored, stretched copy of the swarm fakes the reflection on the water,
// and an UnrealBloom pass gives the LEDs their glow over a night skyline.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import type { ShowAction } from './actions';
import {
  buildFormation,
  clockText,
  fireworks,
  fitToCount,
  hsl,
  mulberry32,
  spatialOrder,
  type Formation,
} from './formations';

export type Mood = 'idle' | 'listening' | 'thinking' | 'speaking';

const CENTER = new THREE.Vector3(0, 100, -250);
// The camera looks a little below the formation so it sits high in the sky
// with the skyline and the water reflection underneath.
const LOOK_AT = new THREE.Vector3(0, 41, -250);
const MAX_SPEED = 70; // world units / s
const MAX_ACCEL = 90;
const STIFFNESS = 3.2;
const DAMPING = 2 * Math.sqrt(STIFFNESS);

const DRONE_VERT = /* glsl */ `
  uniform float uTime;
  uniform float uPixelScale;
  uniform float uSize;
  uniform float uMirror;
  attribute vec3 color;
  attribute float phase;
  varying vec3 vColor;
  varying float vShimmer;
  void main() {
    vec3 p = position;
    // Hover wobble: real drones never sit perfectly still.
    p += vec3(
      sin(uTime * 1.1 + phase * 6.283),
      sin(uTime * 1.7 + phase * 12.0),
      cos(uTime * 0.9 + phase * 9.0)
    ) * 0.14;
    vShimmer = 1.0;
    if (uMirror > 0.5) {
      p.y = -p.y;
      p.x += sin(uTime * 2.3 + p.y * 0.21 + phase * 3.0) * 0.9;
      vShimmer = 0.55 + 0.45 * sin(uTime * 3.1 + phase * 20.0 + p.y * 0.3);
    }
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(uSize * uPixelScale / -mv.z, 1.5, 80.0);
    vColor = color;
  }
`;

const DRONE_FRAG = /* glsl */ `
  uniform float uMirror;
  varying vec3 vColor;
  varying float vShimmer;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    if (uMirror > 0.5) c *= vec2(2.6, 0.8); // vertical streak on the water
    float d = length(c) * 2.0;
    float lum = max(vColor.r, max(vColor.g, vColor.b));
    float core = smoothstep(0.26, 0.0, d);
    float halo = exp(-d * d * 6.0) * 0.6;
    vec3 col = vColor * halo * 1.15 + mix(vColor, vec3(lum), 0.22) * core * 1.5;
    float a = (core + halo) * step(0.004, lum);
    if (a < 0.01) discard;
    float k = uMirror > 0.5 ? 0.3 * vShimmer : 1.0;
    gl_FragColor = vec4(col * k, a * k);
  }
`;

const SKY_VERT = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const SKY_FRAG = /* glsl */ `
  varying vec3 vDir;
  void main() {
    float h = vDir.y;
    vec3 horizon = vec3(0.045, 0.03, 0.055);
    vec3 mid = vec3(0.015, 0.022, 0.055);
    vec3 zenith = vec3(0.002, 0.004, 0.012);
    vec3 col = mix(horizon, mid, smoothstep(0.0, 0.12, h));
    col = mix(col, zenith, smoothstep(0.12, 0.7, h));
    if (h < 0.0) col = vec3(0.004, 0.006, 0.012);
    gl_FragColor = vec4(col, 1.0);
  }
`;

const STAR_VERT = /* glsl */ `
  uniform float uTime;
  attribute float phase;
  varying float vTwinkle;
  void main() {
    vTwinkle = 0.55 + 0.45 * sin(uTime * (0.6 + phase * 2.0) + phase * 40.0);
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = 1.0 + phase * 1.6;
  }
`;

const STAR_FRAG = /* glsl */ `
  varying float vTwinkle;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    float a = smoothstep(1.0, 0.0, d) * vTwinkle * 0.55;
    gl_FragColor = vec4(vec3(0.8, 0.85, 1.0) * a, a);
  }
`;

interface Step {
  formation: Formation;
  hold: number; // seconds before the next step
}

function makeSkylineTexture(): THREE.CanvasTexture {
  const w = 2048;
  const h = 256;
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d')!;
  const glow = ctx.createLinearGradient(0, h, 0, 0);
  glow.addColorStop(0, 'rgba(255,150,80,0.10)');
  glow.addColorStop(0.5, 'rgba(120,60,140,0.03)');
  glow.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = glow;
  ctx.fillRect(0, 0, w, h);
  const rnd = mulberry32(42);
  let x = 0;
  while (x < w) {
    const bw = 14 + rnd() * 46;
    const tall = rnd() < 0.08;
    const bh = (tall ? 120 : 30) + rnd() * (tall ? 110 : 80);
    ctx.fillStyle = '#04060b';
    ctx.fillRect(x, h - bh, bw, bh);
    if (tall && rnd() < 0.6) {
      ctx.fillRect(x + bw / 2 - 1, h - bh - 18, 2, 18);
      ctx.fillStyle = 'rgba(255,60,60,0.9)';
      ctx.fillRect(x + bw / 2 - 1.5, h - bh - 20, 3, 3);
    }
    for (let wy = h - bh + 5; wy < h - 4; wy += 6) {
      for (let wx = x + 3; wx < x + bw - 3; wx += 5) {
        if (rnd() < 0.22) {
          const warm = rnd();
          ctx.fillStyle = `rgba(255,${190 + warm * 50},${120 + warm * 80},${0.25 + rnd() * 0.45})`;
          ctx.fillRect(wx, wy, 2, 2);
        }
      }
    }
    x += bw + rnd() * 6;
  }
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

export class DroneSwarm {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private clock = new THREE.Clock();
  private frame = 0;
  private disposed = false;
  private container: HTMLElement;
  private resizeObserver: ResizeObserver;
  private lastInteraction = -10;
  private spherical = new THREE.Spherical();

  private count = 0;
  private geometry = new THREE.BufferGeometry();
  private droneUniforms = {
    uTime: { value: 0 },
    uPixelScale: { value: 800 },
    uSize: { value: 2.4 },
    uMirror: { value: 0 },
  };
  private mirrorUniforms = {
    uTime: this.droneUniforms.uTime,
    uPixelScale: this.droneUniforms.uPixelScale,
    uSize: { value: 3.4 },
    uMirror: { value: 1 },
  };
  private starUniforms = { uTime: this.droneUniforms.uTime };

  private pos = new Float32Array(0);
  private vel = new Float32Array(0);
  private col = new Float32Array(0);
  private delay = new Float32Array(0);
  private hue = new Float32Array(0);

  private formation: Formation | null = null;
  private formationStart = 0;
  private angle = 0;
  private steps: Step[] = [];
  private stepEndsAt = Infinity;
  private loopSteps: (() => Step[]) | null = null;
  private currentAction: ShowAction = { shape: 'jarvis' };
  private clockLabel = '';

  private mood: Mood = 'idle';
  private level = 0;
  private pulseLevel = 0;
  private fitScale = 1;

  constructor(container: HTMLElement, count = 1500) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    // Neutral tone mapping keeps saturated LED colours from washing to white.
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.domElement.style.display = 'block';
    container.appendChild(this.renderer.domElement);

    // A long lens, like broadcast footage of real shows: the formation looms
    // over the skyline and its reflection fits in the frame.
    this.camera = new THREE.PerspectiveCamera(32, 1, 1, 9000);
    this.camera.position.set(0, 12, 300);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.copy(LOOK_AT);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;
    this.controls.enablePan = false;
    this.controls.minDistance = 260;
    this.controls.maxDistance = 1100;
    this.controls.minPolarAngle = 0.6;
    this.controls.maxPolarAngle = 1.66;
    this.controls.addEventListener('start', () => {
      this.lastInteraction = this.clock.elapsedTime + 1e6; // while dragging
    });
    this.controls.addEventListener('end', () => {
      this.lastInteraction = this.clock.elapsedTime;
    });

    this.buildEnvironment();
    this.setCount(count, true);

    this.composer = new EffectComposer(this.renderer);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    this.bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.95, 0.5, 0.22);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();

    // Take off from the water, then form the JARVIS logo.
    this.apply(buildFormation({ shape: 'jarvis' }, this.count), 1.2);
    this.loop();
  }

  // ── public API ────────────────────────────────────────────────────────

  show(action: ShowAction): void {
    this.currentAction = action;
    this.steps = [];
    this.loopSteps = null;
    this.stepEndsAt = Infinity;
    if (action.shape === 'fireworks') {
      let seed = Math.floor(Math.random() * 1e6);
      this.loopSteps = () => {
        seed++;
        const [gather, burst] = fireworks(this.count, seed);
        const wrap = (c: typeof gather, rainbowOk: boolean): Formation => {
          const f = fitToCount(c, this.count);
          return { ...f, spin: 0, sway: 0, ripple: 0, rainbow: rainbowOk && action.color === 'rainbow', halfWidth: 110 };
        };
        return [
          { formation: wrap(gather, false), hold: 1.7 },
          { formation: wrap(burst, true), hold: 3.2 },
        ];
      };
      this.nextStep();
      return;
    }
    if (action.shape === 'clock') this.clockLabel = clockText();
    this.apply(buildFormation(action, this.count));
  }

  /** Play several actions in a row, holding each for `hold` seconds. */
  sequence(actions: ShowAction[], hold = 6): void {
    if (actions.length <= 1) {
      if (actions[0]) this.show(actions[0]);
      return;
    }
    this.loopSteps = null;
    this.steps = actions.map((a) => ({ formation: buildFormation(a, this.count), hold }));
    this.currentAction = actions[actions.length - 1];
    this.stepEndsAt = 0;
    this.nextStep();
  }

  get action(): ShowAction {
    return this.currentAction;
  }

  setMood(mood: Mood): void {
    this.mood = mood;
  }

  /** Microphone or speech level, 0..1. */
  setLevel(level: number): void {
    this.level = Math.max(0, Math.min(1, level));
  }

  /** A short beat, e.g. on each spoken word. */
  pulse(strength = 0.5): void {
    this.pulseLevel = Math.min(1, this.pulseLevel + strength);
  }

  setCount(count: number, initial = false): void {
    const n = Math.max(100, Math.min(5000, Math.round(count)));
    if (n === this.count) return;
    const oldPos = this.pos;
    const oldCount = this.count;
    this.count = n;
    this.pos = new Float32Array(n * 3);
    this.vel = new Float32Array(n * 3);
    this.col = new Float32Array(n * 3);
    this.delay = new Float32Array(n);
    this.hue = new Float32Array(n);
    const phase = new Float32Array(n);
    const rnd = mulberry32(99);
    const cols = Math.ceil(Math.sqrt(n * 2));
    for (let i = 0; i < n; i++) {
      phase[i] = rnd();
      if (i < oldCount && !initial) {
        this.pos.set(oldPos.subarray(i * 3, i * 3 + 3), i * 3);
      } else {
        // Launch pad: a grid floating just above the water.
        const gx = i % cols;
        const gz = Math.floor(i / cols);
        this.pos[i * 3] = (gx / cols - 0.5) * 260;
        this.pos[i * 3 + 1] = 1.2;
        this.pos[i * 3 + 2] = (gz / (n / cols) - 0.5) * 120 + CENTER.z;
        this.col.set([0.35, 0.38, 0.42], i * 3);
      }
    }
    this.geometry.dispose();
    this.geometry = new THREE.BufferGeometry();
    this.geometry.setAttribute('position', new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    this.geometry.setAttribute('color', new THREE.BufferAttribute(this.col, 3).setUsage(THREE.DynamicDrawUsage));
    this.geometry.setAttribute('phase', new THREE.BufferAttribute(phase, 1));
    this.geometry.boundingSphere = new THREE.Sphere(CENTER.clone(), 4000);
    for (const obj of [this.drones, this.reflection]) if (obj) obj.geometry = this.geometry;
    if (!initial) this.show(this.currentAction);
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    this.composer.dispose();
    this.scene.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      mesh.geometry?.dispose();
      const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
      else mat?.dispose();
    });
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }

  // ── scene ─────────────────────────────────────────────────────────────

  private drones: THREE.Points | null = null;
  private reflection: THREE.Points | null = null;

  private buildEnvironment(): void {
    const sky = new THREE.Mesh(
      new THREE.SphereGeometry(5000, 32, 16),
      new THREE.ShaderMaterial({ vertexShader: SKY_VERT, fragmentShader: SKY_FRAG, side: THREE.BackSide, depthWrite: false }),
    );
    this.scene.add(sky);

    const starCount = 2200;
    const starPos = new Float32Array(starCount * 3);
    const starPhase = new Float32Array(starCount);
    const rnd = mulberry32(5);
    for (let i = 0; i < starCount; i++) {
      const u = 0.06 + rnd() * 0.94;
      const th = rnd() * Math.PI * 2;
      const r = Math.sqrt(1 - u * u);
      starPos.set([Math.cos(th) * r * 4200, u * 4200, Math.sin(th) * r * 4200], i * 3);
      starPhase[i] = rnd();
    }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
    starGeo.setAttribute('phase', new THREE.BufferAttribute(starPhase, 1));
    this.scene.add(
      new THREE.Points(
        starGeo,
        new THREE.ShaderMaterial({
          uniforms: this.starUniforms,
          vertexShader: STAR_VERT,
          fragmentShader: STAR_FRAG,
          transparent: true,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      ),
    );

    const moon = new THREE.Mesh(
      new THREE.CircleGeometry(42, 48),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0.62, 0.61, 0.56), fog: false }),
    );
    moon.position.set(-1150, 720, -3400);
    moon.lookAt(0, 12, 300);
    this.scene.add(moon);

    const water = new THREE.Mesh(
      new THREE.PlaneGeometry(12000, 12000),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(0.006, 0.009, 0.018), depthWrite: false }),
    );
    water.rotation.x = -Math.PI / 2;
    water.renderOrder = -2;
    this.scene.add(water);

    const skyline = new THREE.Mesh(
      new THREE.PlaneGeometry(7000, 280),
      new THREE.MeshBasicMaterial({ map: makeSkylineTexture(), transparent: true, depthWrite: false }),
    );
    skyline.position.set(0, 140, -2600);
    skyline.renderOrder = -1;
    this.scene.add(skyline);

    this.drones = new THREE.Points(
      this.geometry,
      new THREE.ShaderMaterial({
        uniforms: this.droneUniforms,
        vertexShader: DRONE_VERT,
        fragmentShader: DRONE_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    );
    this.drones.frustumCulled = false;
    this.reflection = new THREE.Points(
      this.geometry,
      new THREE.ShaderMaterial({
        uniforms: this.mirrorUniforms,
        vertexShader: DRONE_VERT,
        fragmentShader: DRONE_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    );
    this.reflection.frustumCulled = false;
    this.scene.add(this.reflection, this.drones);
  }

  private resize(): void {
    const w = Math.max(1, this.container.clientWidth);
    const h = Math.max(1, this.container.clientHeight);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = '100%';
    this.renderer.domElement.style.height = '100%';
    this.composer?.setSize(w, h);
    this.bloom?.resolution.set(w, h);
    const px = h * this.renderer.getPixelRatio();
    this.droneUniforms.uPixelScale.value = px / (2 * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)));
    this.updateFitScale();
  }

  private updateFitScale(): void {
    if (!this.formation) return;
    const dist = this.camera.position.distanceTo(CENTER);
    const visibleHalfWidth = Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)) * dist * this.camera.aspect;
    this.fitScale = Math.min(1, (visibleHalfWidth * 0.86) / Math.max(this.formation.halfWidth, 1));
  }

  // ── choreography ──────────────────────────────────────────────────────

  private apply(formation: Formation, extraDelay = 0): void {
    this.formation = formation;
    this.formationStart = this.clock.elapsedTime;
    this.angle = 0;
    this.updateFitScale();
    // Staggered departure along the Hilbert order gives the wave-like
    // transitions seen in real shows.
    const n = this.count;
    const rnd = mulberry32(Math.floor(this.formationStart * 1000));
    let minX = Infinity;
    let maxX = -Infinity;
    for (let i = 0; i < n; i++) {
      minX = Math.min(minX, formation.positions[i * 3]);
      maxX = Math.max(maxX, formation.positions[i * 3]);
    }
    for (let i = 0; i < n; i++) {
      this.delay[i] = extraDelay + (i / n) * 0.9 + rnd() * 0.25;
      this.hue[i] = (formation.positions[i * 3] - minX) / Math.max(maxX - minX, 1);
    }
    this.reassign(formation);
  }

  /**
   * Match drones to slots so neighbours stay neighbours: sort current
   * positions along the same Hilbert curve as the slots, then pair in order.
   */
  private reassign(formation: Formation): void {
    const n = this.count;
    const keyPos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      keyPos[i * 3] = this.pos[i * 3] - CENTER.x;
      keyPos[i * 3 + 1] = this.pos[i * 3 + 1] - CENTER.y;
      keyPos[i * 3 + 2] = this.pos[i * 3 + 2] - CENTER.z;
    }
    // Slots are already in Hilbert order (fitToCount); sort drones likewise.
    const order = spatialOrder(keyPos, n);
    const perm = new Array<number>(n);
    order.forEach((drone, slot) => {
      perm[slot] = drone;
    });
    const positions = new Float32Array(n * 3);
    const colors = new Float32Array(n * 3);
    const hue = new Float32Array(n);
    const delay = new Float32Array(n);
    for (let slot = 0; slot < n; slot++) {
      const d = perm[slot];
      positions.set(formation.positions.subarray(slot * 3, slot * 3 + 3), d * 3);
      colors.set(formation.colors.subarray(slot * 3, slot * 3 + 3), d * 3);
      hue[d] = this.hue[slot];
      delay[d] = this.delay[slot];
    }
    this.formation = { ...formation, positions, colors };
    this.hue = hue;
    this.delay = delay;
  }

  private nextStep(): void {
    if (!this.steps.length && this.loopSteps) this.steps = this.loopSteps();
    const step = this.steps.shift();
    if (!step) {
      this.stepEndsAt = Infinity;
      return;
    }
    this.apply(step.formation);
    this.stepEndsAt = this.clock.elapsedTime + step.hold;
  }

  // ── frame loop ────────────────────────────────────────────────────────

  private loop = (): void => {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(this.loop);
    // Sub-step the flight physics so drones keep real-time pace even when
    // the GPU only manages a few frames per second.
    const raw = Math.min(this.clock.getDelta(), 0.5);
    const t = this.clock.elapsedTime;
    this.droneUniforms.uTime.value = t;

    if (t >= this.stepEndsAt) this.nextStep();
    if (this.currentAction.shape === 'clock' && clockText() !== this.clockLabel) {
      this.show(this.currentAction);
    }

    const substeps = Math.max(1, Math.ceil(raw / (1 / 30)));
    for (let k = substeps - 1; k >= 0; k--) this.step(raw / substeps, t - (k * raw) / substeps);
    this.geometry.attributes.position.needsUpdate = true;
    this.geometry.attributes.color.needsUpdate = true;
    this.driftCamera(t);
    this.controls.update();
    this.composer.render();
  };

  private step(dt: number, t: number): void {
    const f = this.formation;
    if (!f) return;
    const n = this.count;
    const since = t - this.formationStart;

    // Mood shaping on top of the formation.
    this.pulseLevel *= Math.exp(-dt * 4);
    let spinBoost = 0;
    let breathe = 0;
    if (this.mood === 'thinking') spinBoost = 1.4;
    if (this.mood === 'listening') breathe = this.level * 0.35;
    if (this.mood === 'speaking') breathe = this.pulseLevel * 0.12 + this.level * 0.1;
    this.angle += (f.spin + (f.spin !== 0 ? spinBoost : 0)) * dt;
    const swayAngle = f.sway * Math.sin(t * 0.45);
    const a = this.angle + swayAngle;
    const ca = Math.cos(a);
    const sa = Math.sin(a);
    const scale = this.fitScale * (1 + breathe);
    const blend = Math.min(1, dt * 2.5);
    const brightness = this.mood === 'thinking' ? 0.8 + 0.2 * Math.sin(t * 6) : 1;

    const pos = this.pos;
    const vel = this.vel;
    const col = this.col;
    const tp = f.positions;
    const tc = f.colors;
    for (let i = 0; i < n; i++) {
      if (since < this.delay[i]) continue;
      const i3 = i * 3;
      let bx = tp[i3] * scale;
      let by = tp[i3 + 1] * scale;
      let bz = tp[i3 + 2] * scale;
      if (f.ripple) {
        const w = Math.sin(bx * 0.06 - t * 2.4);
        by += w * f.ripple * 0.5;
        bz += Math.cos(bx * 0.045 - t * 1.9) * f.ripple;
      }
      const tx = CENTER.x + bx * ca + bz * sa;
      const ty = CENTER.y + by;
      const tz = CENTER.z - bx * sa + bz * ca;

      // Critically damped spring with acceleration and speed limits.
      let ax = (tx - pos[i3]) * STIFFNESS - vel[i3] * DAMPING;
      let ay = (ty - pos[i3 + 1]) * STIFFNESS - vel[i3 + 1] * DAMPING;
      let az = (tz - pos[i3 + 2]) * STIFFNESS - vel[i3 + 2] * DAMPING;
      const am = Math.hypot(ax, ay, az);
      if (am > MAX_ACCEL) {
        const k = MAX_ACCEL / am;
        ax *= k;
        ay *= k;
        az *= k;
      }
      let vx = vel[i3] + ax * dt;
      let vy = vel[i3 + 1] + ay * dt;
      let vz = vel[i3 + 2] + az * dt;
      const vm = Math.hypot(vx, vy, vz);
      if (vm > MAX_SPEED) {
        const k = MAX_SPEED / vm;
        vx *= k;
        vy *= k;
        vz *= k;
      }
      vel[i3] = vx;
      vel[i3 + 1] = vy;
      vel[i3 + 2] = vz;
      pos[i3] += vx * dt;
      pos[i3 + 1] = Math.max(0.8, pos[i3 + 1] + vy * dt);
      pos[i3 + 2] += vz * dt;

      let r = tc[i3];
      let g = tc[i3 + 1];
      let b = tc[i3 + 2];
      if (f.rainbow && r + g + b > 0) {
        [r, g, b] = hsl(this.hue[i] * 0.85 + t * 0.08, 1, 0.58);
      }
      col[i3] += (r * brightness - col[i3]) * blend;
      col[i3 + 1] += (g * brightness - col[i3 + 1]) * blend;
      col[i3 + 2] += (b * brightness - col[i3 + 2]) * blend;
    }
  }

  private driftCamera(t: number): void {
    // After a few idle seconds, drift slowly like a handheld broadcast camera.
    if (t - this.lastInteraction < 5) return;
    const offset = this.camera.position.clone().sub(this.controls.target);
    this.spherical.setFromVector3(offset);
    const targetTheta = Math.sin(t * 0.05) * 0.32;
    this.spherical.theta += (targetTheta - this.spherical.theta) * 0.004;
    offset.setFromSpherical(this.spherical);
    this.camera.position.copy(this.controls.target).add(offset);
  }
}
