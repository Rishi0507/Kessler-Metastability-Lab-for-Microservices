import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { cellState } from "../lib/cells";
import type { CellAggregate, InferredState } from "../lib/types";
import { color, disposeAll, makeRenderer } from "./common";
import type { SceneHandle } from "./useScene";

export interface TerrainCell {
  agg?: CellAggregate;
  pending?: boolean;
  inferred?: InferredState;
}

export class Terrain implements SceneHandle {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100);
  private controls: OrbitControls;
  private solid: THREE.InstancedMesh;
  private ghost: THREE.InstancedMesh;
  private heights: Float32Array;
  private targets: Float32Array;
  private colorsSolid: THREE.Color[];
  private today: THREE.Mesh | null = null;
  private dummy = new THREE.Object3D();
  private readonly n: number;

  constructor(
    el: HTMLElement,
    private rows: number,
    private cols: number,
    todayCol: number | null,
  ) {
    this.renderer = makeRenderer(el);
    this.n = rows * cols;
    this.heights = new Float32Array(this.n).fill(0.02);
    this.targets = new Float32Array(this.n).fill(0.02);
    this.colorsSolid = Array.from({ length: this.n }, () => new THREE.Color());

    const box = new THREE.BoxGeometry(0.9, 1, 0.9);
    box.translate(0, 0.5, 0);
    this.solid = new THREE.InstancedMesh(box, new THREE.MeshStandardMaterial({ roughness: 0.6 }), this.n);
    this.ghost = new THREE.InstancedMesh(
      box.clone(),
      new THREE.MeshStandardMaterial({ roughness: 0.6, transparent: true, opacity: 0.28 }),
      this.n,
    );
    this.scene.add(this.solid, this.ghost);

    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(cols + 0.6, rows + 0.6),
      new THREE.MeshBasicMaterial({ color: color("--surface-2"), transparent: true, opacity: 0.9 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set((cols - 1) / 2, -0.001, (rows - 1) / 2);
    floor.name = "floor";
    this.scene.add(floor);
    if (todayCol !== null) {
      this.today = new THREE.Mesh(
        new THREE.PlaneGeometry(rows + 0.4, 1.4),
        new THREE.MeshBasicMaterial({ color: color("--accent"), transparent: true, opacity: 0.12, side: THREE.DoubleSide }),
      );
      this.today.rotation.y = Math.PI / 2;
      this.today.position.set(todayCol, 0.7, (rows - 1) / 2);
      this.scene.add(this.today);
    }
    this.scene.add(new THREE.AmbientLight(0xffffff, 1.0));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(-4, 9, 6);
    this.scene.add(sun);

    const cx = (cols - 1) / 2;
    const cz = (rows - 1) / 2;
    this.camera.position.set(cx - cols * 0.55, Math.max(cols, rows) * 0.75, cz + rows * 1.35);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(cx, 0.4, cz);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.minDistance = 3;
    this.controls.maxDistance = 40;
    this.layout();
  }

  private place(i: number, h: number, mesh: THREE.InstancedMesh, visible: boolean) {
    const row = Math.floor(i / this.cols);
    const col = i % this.cols;
    this.dummy.position.set(col, 0, this.rows - 1 - row);
    this.dummy.scale.set(1, visible ? Math.max(0.001, h) : 0.0001, 1);
    this.dummy.updateMatrix();
    mesh.setMatrixAt(i, this.dummy.matrix);
  }

  private layout() {
    for (let i = 0; i < this.n; i++) {
      this.place(i, this.heights[i] ?? 0, this.solid, true);
      this.place(i, 0, this.ghost, false);
    }
    this.solid.instanceMatrix.needsUpdate = true;
    this.ghost.instanceMatrix.needsUpdate = true;
  }

  private ghostMask: boolean[] = [];

  update(cells: TerrainCell[]): void {
    const stable = color("--stable");
    const slow = color("--slow");
    const meta = color("--meta");
    const over = color("--over");
    const pending = color("--accent");
    const empty = color("--rule-2");
    this.ghostMask = new Array(this.n).fill(false);
    cells.forEach((c, i) => {
      if (i >= this.n) return;
      let h = 0.02;
      let col = empty;
      if (c.agg) {
        const s = cellState(c.agg);
        if (s === "metastable") [h, col] = [2.2, meta];
        else if (s === "overloaded") [h, col] = [1.6, over];
        else if (s === "slow") [h, col] = [0.5 + Math.min(1, (c.agg.recovery_s ?? 10) / 120), slow];
        else [h, col] = [0.06 + Math.min(0.3, (c.agg.recovery_s ?? 0) / 40), stable];
      } else if (c.pending) {
        [h, col] = [0.12, pending];
      } else if (c.inferred) {
        this.ghostMask[i] = true;
        [h, col] = c.inferred === "metastable" ? [2.2, meta] : c.inferred === "overloaded" ? [1.6, over] : [0.06, stable];
      }
      this.targets[i] = h;
      this.colorsSolid[i]!.copy(col);
      this.solid.setColorAt(i, col);
      this.ghost.setColorAt(i, col);
    });
    if (this.solid.instanceColor) this.solid.instanceColor.needsUpdate = true;
    if (this.ghost.instanceColor) this.ghost.instanceColor.needsUpdate = true;
  }

  retheme(): void {
    const floor = this.scene.getObjectByName("floor") as THREE.Mesh | undefined;
    if (floor) (floor.material as THREE.MeshBasicMaterial).color = color("--surface-2");
    if (this.today) (this.today.material as THREE.MeshBasicMaterial).color = color("--accent");
  }

  resize(w: number, h: number): void {
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  frame(dt: number): void {
    const k = dt > 0 ? Math.min(1, dt * 6) : 1;
    for (let i = 0; i < this.n; i++) {
      const cur = this.heights[i] ?? 0;
      const tgt = this.targets[i] ?? 0;
      const next = cur + (tgt - cur) * k;
      this.heights[i] = next;
      const g = this.ghostMask[i] ?? false;
      this.place(i, next, this.solid, !g);
      this.place(i, next, this.ghost, g);
    }
    this.solid.instanceMatrix.needsUpdate = true;
    this.ghost.instanceMatrix.needsUpdate = true;
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  dispose(): void {
    this.controls.dispose();
    disposeAll(this.scene, this.renderer);
  }
}
