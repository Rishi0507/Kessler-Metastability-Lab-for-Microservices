from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

__all__ = ["Engine", "EngineError", "LiveSim", "SpecInvalid", "get_engine"]


class EngineError(RuntimeError):
    pass


class SpecInvalid(ValueError):
    pass


def _candidate_paths() -> list[Path]:
    names = {
        "win32": ["libkessler_engine.dll", "kessler_engine.dll"],
        "darwin": ["libkessler_engine.dylib"],
    }.get(sys.platform, ["libkessler_engine.so"])
    env = os.environ.get("KESSLER_ENGINE_PATH")
    out: list[Path] = []
    if env:
        out.append(Path(env))
    repo = Path(__file__).resolve().parents[3]
    for base in (
        repo / "engine" / "build",
        repo / "engine" / "build-release",
        Path("/usr/local/lib"),
        Path("/usr/lib"),
    ):
        out.extend(base / n for n in names)
    return out


def _load_library() -> ctypes.CDLL:
    errors = []
    for path in _candidate_paths():
        if path.is_file():
            try:
                return ctypes.CDLL(str(path))
            except OSError as exc:  # pragma: no cover
                errors.append(f"{path}: {exc}")
    tried = ", ".join(str(p) for p in _candidate_paths())
    raise EngineError(f"kessler engine library not found (tried: {tried}) {'; '.join(errors)}")


class Engine:
    def __init__(self, lib: ctypes.CDLL | None = None) -> None:
        self._lib = lib or _load_library()
        L = self._lib
        vp, sz, cp = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p
        errp = ctypes.POINTER(ctypes.c_void_p)

        L.ks_version.restype = cp
        L.ks_version.argtypes = []
        L.ks_sim_create.restype = vp
        L.ks_sim_create.argtypes = [cp, sz, ctypes.c_int, errp]
        L.ks_sim_destroy.restype = None
        L.ks_sim_destroy.argtypes = [vp]
        L.ks_sim_advance.restype = ctypes.c_int
        L.ks_sim_advance.argtypes = [vp, ctypes.c_double]
        L.ks_sim_run.restype = None
        L.ks_sim_run.argtypes = [vp]
        L.ks_sim_now.restype = ctypes.c_double
        L.ks_sim_now.argtypes = [vp]
        L.ks_sim_events.restype = ctypes.c_int64
        L.ks_sim_events.argtypes = [vp]
        L.ks_sim_finished.restype = ctypes.c_int
        L.ks_sim_finished.argtypes = [vp]
        L.ks_sim_take_frames.restype = vp
        L.ks_sim_take_frames.argtypes = [vp, ctypes.POINTER(sz)]
        L.ks_sim_summary.restype = vp
        L.ks_sim_summary.argtypes = [vp, ctypes.c_int, ctypes.POINTER(sz)]
        L.ks_sim_command.restype = ctypes.c_int
        L.ks_sim_command.argtypes = [vp, cp, sz, errp]
        L.ks_analyze.restype = vp
        L.ks_analyze.argtypes = [cp, sz, errp]
        L.ks_run.restype = vp
        L.ks_run.argtypes = [cp, sz, ctypes.c_int, errp]
        L.ks_free.restype = None
        L.ks_free.argtypes = [vp]
        self.version: str = L.ks_version().decode()

    def _take(self, ptr: int | None, length: int | None = None) -> bytes:
        if not ptr:
            raise EngineError("engine returned no data")
        try:
            return ctypes.string_at(ptr, length) if length is not None else ctypes.string_at(ptr)
        finally:
            self._lib.ks_free(ptr)

    def _err(self, err: ctypes.c_void_p) -> str:
        if not err.value:
            return "unknown engine error"
        return self._take(err.value).decode(errors="replace")

    @staticmethod
    def _encode(spec: dict[str, Any] | str | bytes) -> bytes:
        if isinstance(spec, bytes):
            return spec
        if isinstance(spec, str):
            return spec.encode()
        return json.dumps(spec, separators=(",", ":")).encode()

    def analyze(self, spec: dict[str, Any] | str | bytes) -> dict[str, Any]:
        data = self._encode(spec)
        err = ctypes.c_void_p()
        ptr = self._lib.ks_analyze(data, len(data), ctypes.byref(err))
        if not ptr:
            raise SpecInvalid(self._err(err))
        return json.loads(self._take(ptr))

    def run(self, spec: dict[str, Any] | str | bytes, include_series: bool = False) -> dict[str, Any]:
        data = self._encode(spec)
        err = ctypes.c_void_p()
        ptr = self._lib.ks_run(data, len(data), 1 if include_series else 0, ctypes.byref(err))
        if not ptr:
            raise SpecInvalid(self._err(err))
        return json.loads(self._take(ptr))

    def live(self, spec: dict[str, Any] | str | bytes, emit_frames: bool = True) -> LiveSim:
        return LiveSim(self, self._encode(spec), emit_frames)


class LiveSim:
    def __init__(self, engine: Engine, spec: bytes, emit_frames: bool) -> None:
        self._engine = engine
        self._lib = engine._lib
        self._lock = threading.Lock()
        err = ctypes.c_void_p()
        handle = self._lib.ks_sim_create(spec, len(spec), 1 if emit_frames else 0, ctypes.byref(err))
        if not handle:
            raise SpecInvalid(engine._err(err))
        self._h: int | None = handle

    def _handle(self) -> int:
        if self._h is None:
            raise EngineError("simulation is closed")
        return self._h

    def advance(self, until_s: float) -> bool:
        with self._lock:
            return bool(self._lib.ks_sim_advance(self._handle(), float(until_s)))

    def take_frames_raw(self) -> bytes:
        with self._lock:
            n = ctypes.c_size_t()
            ptr = self._lib.ks_sim_take_frames(self._handle(), ctypes.byref(n))
            return self._engine._take(ptr, n.value)

    def take_frames(self) -> list[dict[str, Any]]:
        return json.loads(self.take_frames_raw())

    def command(self, cmd: dict[str, Any]) -> None:
        data = json.dumps(cmd, separators=(",", ":")).encode()
        with self._lock:
            err = ctypes.c_void_p()
            if self._lib.ks_sim_command(self._handle(), data, len(data), ctypes.byref(err)) != 0:
                raise SpecInvalid(self._engine._err(err))

    def summary(self, include_series: bool = False) -> dict[str, Any]:
        with self._lock:
            n = ctypes.c_size_t()
            ptr = self._lib.ks_sim_summary(self._handle(), 1 if include_series else 0, ctypes.byref(n))
            return json.loads(self._engine._take(ptr, n.value))

    @property
    def now(self) -> float:
        with self._lock:
            return float(self._lib.ks_sim_now(self._handle()))

    @property
    def events(self) -> int:
        with self._lock:
            return int(self._lib.ks_sim_events(self._handle()))

    @property
    def finished(self) -> bool:
        with self._lock:
            return bool(self._lib.ks_sim_finished(self._handle()))

    def close(self) -> None:
        with self._lock:
            if self._h is not None:
                self._lib.ks_sim_destroy(self._h)
                self._h = None

    def __enter__(self) -> LiveSim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover
        with contextlib.suppress(Exception):
            self.close()


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = Engine()
    return _engine
