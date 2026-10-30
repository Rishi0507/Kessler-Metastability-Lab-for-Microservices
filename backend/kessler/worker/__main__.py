from __future__ import annotations

import argparse
import asyncio
import os
import sys


def lower_priority(nice: int) -> None:
    if nice <= 0:
        return
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        below_normal, idle = 0x4000, 0x40
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.SetPriorityClass.restype = wintypes.BOOL
        if not k.SetPriorityClass(k.GetCurrentProcess(), idle if nice >= 15 else below_normal):
            raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
    else:
        os.nice(nice)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kessler simulation worker")
    parser.add_argument("--threads", type=int, default=None, help="simulation slots (default: CPU count)")
    parser.add_argument("--metrics-port", type=int, default=int(os.environ.get("KESSLER_METRICS_PORT", "9301")))
    args = parser.parse_args()
    if args.threads:
        os.environ["KESSLER_WORKER_THREADS"] = str(args.threads)
    from prometheus_client import start_http_server

    from ..config import get_settings
    from ..logs import setup_logging
    from ..tracing import setup_tracing
    from .service import Worker

    s = get_settings()
    lower_priority(s.worker_nice)
    setup_logging("worker", s.log_level)
    setup_tracing("worker", s.otlp_endpoint)
    start_http_server(args.metrics_port, addr=s.host)
    import signal

    def _graceful(*_: object) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, _graceful)
    try:
        asyncio.run(Worker(s).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
