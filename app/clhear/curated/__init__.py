# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Reviewed catalogs that ship with the engine.

Nothing here is seeded into a scope build: every measure, activity, role and
condition comes from the texts in scope. What remains is the engine's own data
model (the measure kinds, seeded by :func:`seed_data_model`) and optional
benchmark definitions a host may add.
"""
import json
from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

CURATED_DIR = Path(__file__).parent

# Member benchmarks and sample profiles are not part of this distribution.
_OPTIONAL = frozenset({"l4_sample_profiles", "l8_benchmarks"})


@lru_cache
def load(name: str) -> list[dict]:
    path = CURATED_DIR / f"{name}.json"
    if not path.is_file():
        if name in _OPTIONAL:
            return []
        raise FileNotFoundError(path)
    return json.loads(path.read_text())


def seed_data_model(engine: Engine) -> dict:
    """The engine's data model: the L3 measure kinds and their fields."""
    from app.clhear.derived_models import l3_kinds
    from app.clhear.l3.kinds import kinds_catalog

    counts = {"kinds": 0}
    with engine.begin() as conn:
        for entry in kinds_catalog():
            exists = conn.execute(sa.select(l3_kinds.c.kind).where(l3_kinds.c.kind == entry["kind"])).first()
            values = dict(description=entry["description"], fields=entry["fields"])
            if exists:
                conn.execute(l3_kinds.update().where(l3_kinds.c.kind == entry["kind"]).values(**values))
            else:
                conn.execute(l3_kinds.insert().values(kind=entry["kind"], **values))
            counts["kinds"] += 1
    return counts
