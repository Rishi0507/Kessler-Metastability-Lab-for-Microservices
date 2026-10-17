from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Kessler api replica")
    parser.add_argument("--host", default=os.environ.get("KESSLER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KESSLER_PORT", "9001")))
    args = parser.parse_args()
    os.environ["KESSLER_PORT"] = str(args.port)
    from ..config import get_settings

    s = get_settings()
    uvicorn.run(
        "kessler.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=s.trusted_proxies,
        log_config=None,
        access_log=False,
        timeout_keep_alive=75,
        timeout_graceful_shutdown=20,
        server_header=False,
    )


if __name__ == "__main__":
    main()
