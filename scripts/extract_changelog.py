#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version (the GitHub Release body).

python scripts/extract_changelog.py 2.0.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def extract(version: str, changelog: Path = ROOT / "CHANGELOG.md") -> str:
    text = changelog.read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(1).strip() if match else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    body = extract(argv[1].lstrip("v"))
    if not body:
        print(f"error: no (or empty) CHANGELOG section for {argv[1]}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
