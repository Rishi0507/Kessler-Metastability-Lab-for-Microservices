import * as THREE from "three";
import { cssVar } from "../app/theme";

export function color(name: string, fallback = "#888888"): THREE.Color {
  const v = cssVar(name);
  try {
    return new THREE.Color(v || fallback);
  } catch {
    return new THREE.Color(fallback);
  }
}

export function makeRenderer(el: HTMLElement): THREE.WebGLRenderer {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  el.appendChild(renderer.domElement);
  return renderer;
}

export function disposeAll(root: THREE.Object3D, renderer: THREE.WebGLRenderer): void {
  root.traverse((obj) => {
    const mesh = obj as THREE.Mesh;
    mesh.geometry?.dispose?.();
    const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
    else mat?.dispose?.();
  });
  renderer.dispose();
  renderer.domElement.remove();
}

export function utilColor(u: number, out = new THREE.Color()): THREE.Color {
  const stable = color("--stable", "#1a9a7c");
  const slow = color("--slow", "#c98a12");
  const meta = color("--meta", "#cc2f55");
  if (u <= 0.6) return out.copy(stable);
  if (u <= 0.85) return out.copy(stable).lerp(slow, (u - 0.6) / 0.25);
  return out.copy(slow).lerp(meta, Math.min(1, (u - 0.85) / 0.15));
}
