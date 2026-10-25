import { useEffect, useRef, type RefObject } from "react";

export interface SceneHandle {
  resize(width: number, height: number): void;
  frame(dt: number, time: number): void;
  retheme?(): void;
  dispose(): void;
}

export function useScene<T extends SceneHandle>(
  containerRef: RefObject<HTMLElement | null>,
  create: (el: HTMLElement) => T,
  deps: unknown[] = [],
): RefObject<T | null> {
  const handleRef = useRef<T | null>(null);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let handle: T;
    try {
      handle = create(el);
    } catch (err) {
      console.warn("WebGL unavailable", err);
      el.dataset.webgl = "unavailable";
      return;
    }
    handleRef.current = handle;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let visible = true;
    let raf = 0;
    let last = performance.now();
    const start = performance.now();

    const loop = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      handle.frame(reduced.matches ? 0 : dt, (now - start) / 1000);
      raf = visible && !document.hidden && !reduced.matches ? requestAnimationFrame(loop) : 0;
    };
    const kick = () => {
      if (!raf && visible && !document.hidden) {
        last = performance.now();
        raf = requestAnimationFrame(loop);
      }
    };

    const ro = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        handle.resize(width, height);
        handle.frame(0, (performance.now() - start) / 1000);
      }
    });
    ro.observe(el);
    const io = new IntersectionObserver(([entry]) => {
      visible = !!entry?.isIntersecting;
      if (visible) kick();
    });
    io.observe(el);
    const onVis = () => kick();
    document.addEventListener("visibilitychange", onVis);
    const onTheme = () => {
      handle.retheme?.();
      handle.frame(0, 0);
    };
    const mo = new MutationObserver(onTheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", onTheme);
    kick();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      io.disconnect();
      mo.disconnect();
      scheme.removeEventListener("change", onTheme);
      document.removeEventListener("visibilitychange", onVis);
      handle.dispose();
      handleRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return handleRef;
}
