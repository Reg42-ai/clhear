# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Fail when a product name from outside this repository appears in the tree."""
from __future__ import annotations

import sys
from pathlib import Path

_SKIP = {".git", "__pycache__", ".venv", ".ruff_cache", ".pytest_cache", "clhear.egg-info"}


def _term(*parts: str) -> str:
    return "".join(parts)


TERMS = (
    _term("gal", "axy"),
    _term("reg42-", "os"),
    _term("reg42-", "infra"),
    _term("reg42_", "clhear"),
    _term("so", "lon"),
    _term("clhear.", "reg42.ai"),
    _term("@", "reg42/"),
    _term("work", "force"),
    _term("Reg42 ", "OS"),
    _term("Reg42 ", "Infer"),
    _term("cfr-17-", "ia-marketing"),
    _term("influ", "encer"),
    _term("eto", "ro"),
    _term("fin", "ra"),
    _term("poc_", "review"),
    _term("CLHEAR-", "MVP"),
)


def scan(root: Path | None = None) -> list[str]:
    root = root or Path.cwd()
    hits = []
    needles = [term.lower() for term in TERMS]
    for path in root.rglob("*"):
        if not path.is_file() or any(part in _SKIP for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".db", ".pyc"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for needle in needles:
            if needle not in text:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line:
                    hits.append(f"{path.relative_to(root)}:{number}:{needle}")
    return hits


def main() -> int:
    hits = scan(Path.cwd())
    if hits:
        print("\n".join(hits), file=sys.stderr)
        return 1
    print("denylist clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
