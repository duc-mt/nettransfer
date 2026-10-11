#!/usr/bin/env python3
"""Release consistency check (used by CI and `make check-release`).

    python scripts/check_version.py            # version format + CHANGELOG entry
    python scripts/check_version.py v2.1.0     # additionally: the tag must equal app.__version__

Reads the version from app/__init__.py without importing the package.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")


def read_version(root: Path = ROOT) -> str:
    text = (root / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not match:
        raise SystemExit("error: __version__ not found in app/__init__.py")
    return match.group(1)


def check(tag: str = "", root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    version = read_version(root)
    if not SEMVER.match(version):
        problems.append(f"__version__ '{version}' is not MAJOR.MINOR.PATCH[-prerelease]")
    if tag and tag != f"v{version}":
        problems.append(f"tag '{tag}' does not match app.__version__ 'v{version}'")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        problems.append(f"CHANGELOG.md has no '## [{version}]' section")
    if "## [Unreleased]" not in changelog:
        problems.append("CHANGELOG.md has no '## [Unreleased]' section")
    return problems


def main(argv: list[str]) -> int:
    problems = check(argv[1] if len(argv) > 1 else "")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print(f"version {read_version()} OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
