#!/usr/bin/env python3
"""Generate NCTE's self-authored sample/fixture EPUBs (no third-party book content).

Usage:  python scripts/make_sample_epub.py [OUTPUT_DIR]   (default: tests/fixtures)
"""

from __future__ import annotations

import sys
from pathlib import Path

from ncte.samples import write_fixture_set


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    for name, path in write_fixture_set(out).items():
        print(f"{name:14s} {path}")


if __name__ == "__main__":
    main()
