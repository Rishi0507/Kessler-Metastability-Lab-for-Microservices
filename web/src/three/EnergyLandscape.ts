import * as THREE from "three";
import type { SceneHandle } from "./useScene";
import { color, disposeAll, makeRenderer } from "./common";

const W1 = { x: -0.95, depth: 0.42, width: 0.16 };
const W2 = { x: 0.95, depth: 0.95, width: 0.2 };

function potential(x: number, z: number): number {
  const g = (w: typeof W1) => -w.depth * Math.exp(-((x - w.x) ** 2) / w.width);
  return g(W1) + g(W2) + 0.06 * Math.cos(z * 2.2) * Math.exp(-(x * x) / 1.6) + 0.05 * x * x * x * x;
}

function slope(x: number): number {
  const h = 1e-3;
  return (potential(x + h, 0) - potential(x - h, 0)) / (2 * h);
}

export type HeroPhase = "resting" | "small-push" | "recovered" | "big-push" | "stuck";

export class EnergyLandscape implements SceneHandle {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(32, 1, 0.1, 50);
  private surface: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  private ball: THREE.Mesh;
  private trail: THREE.Line;
  private trailPts: THREE.Vector3[] = [];
  private x = W1.x;
  private v = 0;
  private clock = 0;
  private cycle = 0;
  phase: HeroPhase = "resting";
  onPhase?: (p: HeroPhase) => void;

  constructor(el: HTMLElement) {
    this.renderer = makeRenderer(el);
    this.camera.position.set(0.3, 2.35, 4.6);
    this.camera.lookAt(0, -0.35, 0);

    const geo = new THREE.PlaneGeometry(4.4, 2.6, 220, 130);
    geo.rotateX(-Math.PI / 2);
    const pos = geo.attributes.position as THREE.BufferAttribute;
    for (let i = 0; i < pos.count; i++) {
      pos.setY(i, potential(pos.getX(i), pos.getZ(i)));
    }
    geo.computeVertexNormals();
    const mat = new THREE.ShaderMaterial({
      transparent: true,
      uniforms: {
        uInk: { value: color("--ink-3") },
        uHealthy: { value: color("--stable") },
        uFailure: { value: color("--meta") },
        uPaper: { value: color("--surface") },
      },
      vertexShader: `
        varying float vH; varying float vX; varying vec3 vN;
        void main() {
          vH = position.y; vX = position.x; vN = normal;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: `
        uniform vec3 uInk; uniform vec3 uHealthy; uniform vec3 uFailure; uniform vec3 uPaper;
        varying float vH; varying float vX; varying vec3 vN;
        void main() {
          float f = vH / 0.06;
          float line = abs(fract(f - 0.5) - 0.5) / fwidth(f);
          float iso = 1.0 - min(line, 1.0);
          float healthy = smoothstep(-0.15, -0.42, vH) * smoothstep(0.2, -0.5, vX);
          float failure = smoothstep(-0.2, -0.9, vH) * smoothstep(-0.2, 0.5, vX);
          vec3 base = mix(uPaper, uHealthy, healthy * 0.28);
          base = mix(base, uFailure, failure * 0.30);
          float shade = 0.82 + 0.18 * clamp(dot(normalize(vN), normalize(vec3(-0.4, 1.0, 0.6))), 0.0, 1.0);
          vec3 col = mix(base * shade, uInk, iso * 0.55);
          float edge = smoothstep(2.2, 1.6, abs(vX)) ;
          gl_FragColor = vec4(col, 0.92 * edge);
        }`,
    });
    this.surface = new THREE.Mesh(geo, mat);
    this.scene.add(this.surface);

    this.ball = new THREE.Mesh(
      new THREE.SphereGeometry(0.075, 32, 16),
      new THREE.MeshStandardMaterial({ color: color("--accent"), roughness: 0.35, metalness: 0.1 }),
    );
    this.scene.add(this.ball);
    this.trail = new THREE.Line(
      new THREE.BufferGeometry(),
      new THREE.LineBasicMaterial({ color: color("--accent"), transparent: true, opacity: 0.55 }),
    );
    this.scene.add(this.trail);
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(-2, 4, 3);
    this.scene.add(sun);
    this.place();
  }

  private setPhase(p: HeroPhase) {
    if (p !== this.phase) {
      this.phase = p;
      this.onPhase?.(p);
    }
  }

  private place() {
    this.ball.position.set(this.x, potential(this.x, 0) + 0.075, 0);
  }

  retheme(): void {
    const u = this.surface.material.uniforms;
    u.uInk!.value = color("--ink-3");
    u.uHealthy!.value = color("--stable");
    u.uFailure!.value = color("--meta");
    u.uPaper!.value = color("--surface");
    (this.ball.material as THREE.MeshStandardMaterial).color = color("--accent");
    (this.trail.material as THREE.LineBasicMaterial).color = color("--accent");
  }

  resize(w: number, h: number): void {
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  private step(dt: number) {
    this.clock += dt;
    const t = this.clock;
    let push = 0;
    const big = this.cycle % 2 === 1;
    if (t > 2.2 && t < 2.6) push = big ? 3.4 : 1.9;
    if (t > 2.2 && this.phase === "resting") this.setPhase(big ? "big-push" : "small-push");
    const acc = -slope(this.x) * 9 - 2.2 * this.v + push;
    this.v += acc * dt;
    this.x += this.v * dt;
    if (t > 4.2 && this.phase === "small-push" && Math.abs(this.x - W1.x) < 0.12) this.setPhase("recovered");
    if (t > 4.2 && this.phase === "big-push" && this.x > 0.4) this.setPhase("stuck");
    if (t > 7.8) {
      this.clock = 0;
      this.cycle += 1;
      this.x = W1.x;
      this.v = 0;
      this.trailPts = [];
      this.setPhase("resting");
    }
    this.place();
    if (this.phase !== "resting") {
      this.trailPts.push(this.ball.position.clone().add(new THREE.Vector3(0, -0.06, 0)));
      if (this.trailPts.length > 160) this.trailPts.shift();
      this.trail.geometry.setFromPoints(this.trailPts);
    }
  }

  frame(dt: number, time: number): void {
    if (dt > 0) {
      this.step(dt);
      this.camera.position.x = 0.3 + Math.sin(time * 0.12) * 0.35;
      this.camera.lookAt(0, -0.35, 0);
    }
    this.renderer.render(this.scene, this.camera);
  }

  dispose(): void {
    disposeAll(this.scene, this.renderer);
  }
}
