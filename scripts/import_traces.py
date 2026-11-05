#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from kessler.spec.otel import TraceImportError, import_otlp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Turn OTLP JSON traces into a kessler.yaml")
    ap.add_argument("traces", type=Path, help="OTLP JSON or JSON lines, e.g. from the collector file exporter")
    ap.add_argument("--name", default="Imported system")
    ap.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    args = ap.parse_args()
    try:
        text, notes = import_otlp(args.traces.read_text(encoding="utf-8"), args.name)
    except TraceImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
