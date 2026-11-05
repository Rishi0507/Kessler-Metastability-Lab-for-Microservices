#!/usr/bin/env python3

from __future__ import annotations

import argparse
import http.cookiejar
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid


class Client:
    def __init__(self, base: str, insecure: bool) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=ctx)
        )

    def call(self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None) -> tuple[int, object]:
        h = {"Content-Type": "application/json", **(headers or {})}
        csrf = next((c.value for c in self.jar if c.name == "ks_csrf"), None)
        if csrf and method != "GET":
            h["X-CSRF-Token"] = csrf
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=30) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw and "json" in r.headers.get("Content-Type", "") else raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end smoke test of a running Kessler")
    ap.add_argument("base", nargs="?", default="http://localhost:8080")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    ap.add_argument("--timeout", type=float, default=300)
    args = ap.parse_args()
    c = Client(args.base, args.insecure)

    deadline = time.monotonic() + args.timeout
    while True:
        try:
            status, _ = c.call("GET", "/api/v1/templates")
            if status == 200:
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            check(False, f"{args.base} answers")
        time.sleep(3)
    check(True, f"{args.base} answers")

    status, body = c.call("GET", "/api/v1/templates")
    templates = {t["id"]: t for t in body["templates"]}
    check("checkout-retry-storm" in templates, "templates load")

    status, _ = c.call("POST", "/api/v1/auth/guest")
    check(status in (200, 201), "guest sign-in")
    if args.base.startswith("https://"):
        check(all(k.secure for k in c.jar), "cookies are Secure")

    t = templates["checkout-retry-storm"]
    status, system = c.call("POST", "/api/v1/systems", {"yaml": t["yaml"], "template_id": t["id"]})
    check(status == 201, "create a system")

    body = {"system_id": system["id"], "trigger": {"kind": "latency", "target": "db", "duration_s": 20}, "seeds": 1}
    status, res = c.call("POST", "/api/v1/analyses", body, {"Idempotency-Key": str(uuid.uuid4())})
    check(status == 202, "submit a sweep")
    aid = res["analysis"]["id"]
    while time.monotonic() < deadline:
        status, snap = c.call("GET", f"/api/v1/analyses/{aid}")
        state = snap["analysis"]["status"] if status == 200 else "error"
        if state in ("done", "failed", "cancelled", "error"):
            break
        time.sleep(3)
    check(state == "done", f"sweep finishes (status: {state})")
    safe = snap["analysis"]["result"]["safe_fraction"]
    check(0 <= safe <= 1, f"sweep has a result ({safe:.0%} safe)")


if __name__ == "__main__":
    main()
