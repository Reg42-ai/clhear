# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Write openapi/clhear-v1.yaml and fail when the committed file drifts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from app.clhear.api import app

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "openapi" / "clhear-v1.yaml"


def document() -> dict:
    app.openapi_schema = None
    schema = app.openapi()
    schema["info"]["license"] = {
        "name": "AGPL-3.0-only",
        "url": "https://www.gnu.org/licenses/agpl-3.0.html",
    }
    schema["info"]["description"] = (
        "One CLHEAR install. Register the texts you choose, describe the organisation, "
        "and POST /v1/runs. The call returns a run id. The worker writes the blueprint. "
        "Read items, explanations, and clause pointers from the release. "
        "ContributionProposal is checked locally and is sent nowhere."
    )
    return schema


def render() -> str:
    return yaml.safe_dump(document(), sort_keys=True, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.clhear.openapi_doc")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = SPEC.read_text(encoding="utf-8") if SPEC.is_file() else ""
        if current != text:
            print("openapi/clhear-v1.yaml does not match the generated schema", file=sys.stderr)
            return 1
        print("openapi matches")
        return 0
    SPEC.parent.mkdir(parents=True, exist_ok=True)
    SPEC.write_text(text, encoding="utf-8")
    print(SPEC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
