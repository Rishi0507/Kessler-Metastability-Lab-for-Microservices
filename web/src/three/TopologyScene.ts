import * as THREE from "three";
import { layeredLayout } from "../lib/layout";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Frame, Topology } from "../lib/types";
import { color, disposeAll, makeRenderer, utilColor } from "./common";
import type { SceneHandle } from "./useScene";

const MAX_PARTICLES = 1600;

interface NodeViz {
  id: string;
  pos: THREE.Vector3;
  body: THREE.Mesh<THREE.SphereGeometry, THREE.MeshStandardMaterial>;
  ring: THREE.Mesh<THREE.TorusGeometry, THREE.MeshBasicMaterial>;
  label: HTMLDivElement;
  util: number;
  queue: number;
  mult: number;
  target: THREE.Color;
}

interface EdgeViz {
  from: string;
  to: string;
  curve: THREE.QuadraticBezierCurve3;
  line: THREE.Line;
  rps: number;
  err: number;
  retry: number;
  acc: number;
}

export class TopologyScene implements SceneHandle {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200);
  private span = 10;
  private controls: OrbitControls;
  private nodes = new Map<string, NodeViz>();
  private edges: EdgeViz[] = [];
  private particles: THREE.InstancedMesh;
  private pEdge = new Int16Array(MAX_PARTICLES).fill(-1);
  private pT = new Float32Array(MAX_PARTICLES);
  private pSpeed = new Float32Array(MAX_PARTICLES);
  private free: number[] = [];
  private labels: HTMLDivElement;
  private dummy = new THREE.Object3D();
  private raycaster = new THREE.Raycaster();
  private pointer = new THREE.Vector2();
  private width = 1;
  private height = 1;
  private faulted = new Set<string>();
  selected: string | null = null;
  onSelect?: (id: string | null) => void;
  private colors = { ok: color("--accent"), err: color("--meta"), retry: color("--slow"), ink: color("--ink-3") };

  constructor(el: HTMLElement, topo: Topology) {
    this.renderer = makeRenderer(el);
    this.labels = document.createElement("div");
    Object.assign(this.labels.style, { position: "absolute", inset: "0", pointerEvents: "none", overflow: "hidden" });
    el.appendChild(this.labels);

    this.layout(topo);
    this.particles = new THREE.InstancedMesh(
      new THREE.SphereGeometry(0.045, 8, 6),
      new THREE.MeshBasicMaterial({ color: 0xffffff }),
      MAX_PARTICLES,
    );
    this.particles.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    for (let i = 0; i < MAX_PARTICLES; i++) {
      this.free.push(i);
      this.hide(i);
      this.particles.setColorAt(i, this.colors.ok);
    }
    this.scene.add(this.particles);
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.5);
    sun.position.set(-3, 8, 6);
    this.scene.add(sun);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.49;
    this.renderer.domElement.addEventListener("click", this.onClick);
  }

  private layout(topo: Topology) {
    const { layers } = layeredLayout(topo);
    const maxDepth = layers.length - 1;
    const widest = Math.max(1, ...layers.map((l) => l.length));
    for (const [d, ids] of layers.entries()) {
      ids.forEach((id, i) => {
        const node = topo.nodes.find((n) => n.id === id);
        const x = (d - maxDepth / 2) * 2.8;
        const z = (i - (ids.length - 1) / 2) * 2.2;
        const pos = new THREE.Vector3(x, 0, z);
        const r = id === "client" ? 0.26 : 0.22 + 0.07 * Math.sqrt(node?.instances ?? 1);
        const body = new THREE.Mesh(
          id === "client" ? new THREE.SphereGeometry(r, 6, 4) : new THREE.SphereGeometry(r, 32, 20),
          new THREE.MeshStandardMaterial({ color: this.colors.ink, roughness: 0.4, emissive: new THREE.Color(0x000000) }),
        );
        body.position.copy(pos);
        body.userData.id = id;
        const ring = new THREE.Mesh(
          new THREE.TorusGeometry(r + 0.12, 0.025, 8, 48),
          new THREE.MeshBasicMaterial({ color: this.colors.ink, transparent: true, opacity: 0.6 }),
        );
        ring.rotation.x = Math.PI / 2;
        ring.position.copy(pos);
        this.scene.add(body, ring);
        const label = document.createElement("div");
        Object.assign(label.style, {
          position: "absolute",
          transform: "translate(-50%, 0)",
          font: "500 12px var(--font-ui)",
          color: "var(--ink-2)",
          whiteSpace: "nowrap",
          textAlign: "center",
        });
        label.textContent = id === "client" ? "clients" : id;
        this.labels.appendChild(label);
        this.nodes.set(id, { id, pos, body, ring, label, util: 0, queue: 0, mult: 1, target: new THREE.Color() });
      });
    }
    for (const e of topo.edges) {
      const a = this.nodes.get(e.from);
      const b = this.nodes.get(e.to);
      if (!a || !b) continue;
      const mid = a.pos.clone().lerp(b.pos, 0.5).add(new THREE.Vector3(0, 0.9 + a.pos.distanceTo(b.pos) * 0.08, 0));
      const curve = new THREE.QuadraticBezierCurve3(a.pos.clone(), mid, b.pos.clone());
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(curve.getPoints(40)),
        new THREE.LineBasicMaterial({ color: this.colors.ink, transparent: true, opacity: 0.35 }),
      );
      this.scene.add(line);
      this.edges.push({ from: e.from, to: e.to, curve, line, rps: 0, err: 0, retry: 0, acc: 0 });
    }
    this.span = Math.max((maxDepth + 1) * 2.8, widest * 2.2);
    this.fitCamera();
  }

  private fitCamera(): void {
    if (this.width > 900) this.camera.setViewOffset(this.width, this.height, 150, 0, this.width, this.height);
    else this.camera.clearViewOffset();
    const fit = Math.max(1, 1.5 / (this.camera.aspect || 1));
    this.camera.position.set(0, this.span * 0.55 * fit, this.span * 0.95 * fit);
    this.camera.lookAt(0, 0, 0);
  }

  private onClick = (e: MouseEvent) => {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([...this.nodes.values()].map((n) => n.body));
    const id = (hits[0]?.object.userData.id as string | undefined) ?? null;
    this.selected = id === "client" ? null : id;
    this.onSelect?.(this.selected);
  };

  private hide(i: number) {
    this.dummy.position.set(0, -999, 0);
    this.dummy.scale.setScalar(0.0001);
    this.dummy.updateMatrix();
    this.particles.setMatrixAt(i, this.dummy.matrix);
  }

  update(f: Frame): void {
    this.faulted = new Set(f.faults.map((x) => x.target ?? "").filter(Boolean));
    for (const s of f.services) {
      const n = this.nodes.get(s.name);
      if (!n) continue;
      n.util = s.util;
      n.queue = s.queue;
      n.mult = s.latency_mult;
      n.label.textContent = `${s.name} · ${Math.round(s.util * 100)}%${s.instances ? ` · ×${s.instances}` : ""}`;
    }
    for (const e of this.edges) {
      const fe = f.edges.find((x) => x.from === e.from && x.to === e.to);
      if (!fe) continue;
      e.rps = fe.rps;
      e.err = fe.err;
      e.retry = fe.rps > 0 ? Math.min(1, fe.retries / fe.rps) : 0;
    }
  }

  retheme(): void {
    this.colors = { ok: color("--accent"), err: color("--meta"), retry: color("--slow"), ink: color("--ink-3") };
    for (const e of this.edges) (e.line.material as THREE.LineBasicMaterial).color = this.colors.ink;
  }

  resize(w: number, h: number): void {
    this.width = w;
    this.height = h;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.fitCamera();
  }

  private spawn(edgeIdx: number, kind: "ok" | "err" | "retry") {
    const i = this.free.pop();
    if (i === undefined) return;
    this.pEdge[i] = edgeIdx;
    this.pT[i] = 0;
    this.pSpeed[i] = 0.75 + Math.random() * 0.35;
    this.particles.setColorAt(i, kind === "err" ? this.colors.err : kind === "retry" ? this.colors.retry : this.colors.ok);
  }

  frame(dt: number, time: number): void {
    this.edges.forEach((e, idx) => {
      if (dt <= 0 || e.rps <= 0) return;
      const rate = Math.min(45, 2 + 9 * Math.log10(1 + e.rps / 20));
      e.acc += rate * dt;
      while (e.acc >= 1) {
        e.acc -= 1;
        const r = Math.random();
        this.spawn(idx, r < e.err ? "err" : r < e.err + e.retry ? "retry" : "ok");
      }
    });
    const p = new THREE.Vector3();
    for (let i = 0; i < MAX_PARTICLES; i++) {
      const ei = this.pEdge[i] ?? -1;
      if (ei < 0) continue;
      const t = (this.pT[i] ?? 0) + (this.pSpeed[i] ?? 1) * dt;
      if (t >= 1) {
        this.pEdge[i] = -1;
        this.hide(i);
        this.free.push(i);
        continue;
      }
      this.pT[i] = t;
      this.edges[ei]!.curve.getPoint(t, p);
      this.dummy.position.copy(p);
      this.dummy.scale.setScalar(1);
      this.dummy.updateMatrix();
      this.particles.setMatrixAt(i, this.dummy.matrix);
    }
    this.particles.instanceMatrix.needsUpdate = true;
    if (this.particles.instanceColor) this.particles.instanceColor.needsUpdate = true;

    const sel = color("--accent");
    for (const n of this.nodes.values()) {
      if (n.id !== "client") utilColor(n.util, n.target);
      else n.target.copy(this.colors.ink);
      n.body.material.color.lerp(n.target, dt > 0 ? Math.min(1, dt * 4) : 1);
      const pulse = this.faulted.has(n.id) ? 0.35 + 0.25 * Math.sin(time * 6) : 0;
      n.body.material.emissive.setRGB(pulse, pulse * 0.15, pulse * 0.2);
      const q = Math.min(2.2, 1 + Math.log10(1 + n.queue) * 0.6);
      n.ring.scale.set(q, q, 1);
      n.ring.material.color.copy(this.selected === n.id ? sel : n.target);
      n.ring.material.opacity = this.selected === n.id ? 1 : 0.55;
      const v = n.pos.clone().add(new THREE.Vector3(0, -0.5, 0)).project(this.camera);
      n.label.style.left = `${((v.x + 1) / 2) * this.width}px`;
      n.label.style.top = `${((1 - v.y) / 2) * this.height}px`;
      n.label.style.display = v.z < 1 ? "block" : "none";
      n.label.style.color = this.selected === n.id ? "var(--accent)" : "var(--ink-2)";
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  dispose(): void {
    this.renderer.domElement.removeEventListener("click", this.onClick);
    this.controls.dispose();
    this.labels.remove();
    disposeAll(this.scene, this.renderer);
  }
}
