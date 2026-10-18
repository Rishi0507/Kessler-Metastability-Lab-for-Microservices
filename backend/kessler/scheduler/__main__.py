from __future__ import annotations

import argparse
import asyncio
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Kessler scheduler")
    parser.add_argument("--metrics-port", type=int, default=int(os.environ.get("KESSLER_METRICS_PORT", "9201")))
    args = parser.parse_args()
    from prometheus_client import start_http_server

    from ..config import get_settings
    from ..logs import setup_logging
    from ..tracing import setup_tracing
    from .service import Scheduler

    s = get_settings()
    setup_logging("scheduler", s.log_level)
    setup_tracing("scheduler", s.otlp_endpoint)
    start_http_server(args.metrics_port, addr="127.0.0.1" if s.env == "dev" else "0.0.0.0")  # noqa: S104
    asyncio.run(Scheduler(s).run())


if __name__ == "__main__":
    main()
