from __future__ import annotations

import argparse
import asyncio
import os


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
