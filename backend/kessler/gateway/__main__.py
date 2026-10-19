from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Kessler realtime gateway")
    parser.add_argument("--host", default=os.environ.get("KESSLER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KESSLER_PORT", "9101")))
    args = parser.parse_args()
    os.environ["KESSLER_PORT"] = str(args.port)
    uvicorn.run(
        "kessler.gateway.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
        ws_max_size=64 * 1024,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        timeout_graceful_shutdown=10,
        server_header=False,
    )


if __name__ == "__main__":
    main()
