# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Stable identifiers (HLD v2 invariant I11).

Standard requirements: ``CLHEAR-<layer>.<n>``. Objects: ``OBL-``, ``BLK-``, ``PRF-``,
``ACT-``, ``BLU-``, ``RSK-``, ``FIL-``. An id is issued once and never reused: the
``id_sequences`` table holds the high-water mark per prefix, and an invalidated
object keeps its id forever (I2). Ids are opaque — never derive meaning from ``n``.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.models import L0_SCHEMA, metadata

OBJECT_PREFIXES: dict[str, str] = {
    "OBL": "L2",  # obligation
    "BLK": "L3",  # building block
    "PRF": "L4",  # profile
    "ACT": "L5",  # activity
    "BLU": "L6",  # blueprint
    "RSK": "L7",  # risk score
    "FIL": "L8",  # fill
    "BMA": "L8",  # benchmark aggregate
    # edge / auxiliary objects — same rules (stable, never reused)
    "AST": "L2",  # asserts edge
    "EQV": "L2",  # equivalence
    "SUP": "L2",  # supersession
    "CHG": "L2",  # L2 change event
    "REQ": "L3",  # requires edge
    "CHR": "L3",  # characteristic
    "LIC": "L4",  # licence
    "PRD": "L4",  # product / service
    "CLT": "L4",  # client type
    "CHN": "L4",  # channel
    "RUL": "L4",  # validity rule
    "APL": "L4",  # applies_to edge
    "IMP": "L5",  # implies edge
    "OPR": "L5",  # operates edge
    "MIT": "L5",  # mitigates edge
    "ITM": "L6",  # blueprint item
    "ENF": "L7",  # enforcement event
    "AGG": "L8",  # benchmark aggregate
    "WHY": "L0",  # why-trail
    "EDT": "L0",  # human-accepted edit (approval console)
    "CON": "L0",  # community contribution (HLD v2 §6, I12)
    "CFA": "L0",  # conformance self-assessment (Annex E)
    "CFM": "L0",  # conformance mark (register entry)
    "SRC": "L1",  # source (exposed id for integer-keyed L1 rows)
    "CLS": "L1",  # clause
    "VER": "L1",  # source version
    "KEY": "L0",  # API key (identity rails)
}

_ID_RE = re.compile(r"^(?P<prefix>[A-Z]{3})-(?P<n>\d{6,})$")
_REQ_RE = re.compile(r"^CLHEAR-(?P<layer>\d{1,2})\.(?P<n>\d+)$")

id_sequences = sa.Table(
    "id_sequences",
    metadata,
    sa.Column("prefix", sa.Text, primary_key=True),
    sa.Column("high_water", sa.BigInteger().with_variant(sa.Integer, "sqlite"), nullable=False, default=0),
    schema=L0_SCHEMA,
)


def is_object_id(value: str) -> bool:
    m = _ID_RE.match(value or "")
    return bool(m and m.group("prefix") in OBJECT_PREFIXES)


def is_requirement_id(value: str) -> bool:
    return bool(_REQ_RE.match(value or ""))


def layer_of(object_id: str) -> str | None:
    m = _ID_RE.match(object_id or "")
    return OBJECT_PREFIXES.get(m.group("prefix")) if m else None


def format_id(prefix: str, n: int) -> str:
    if prefix not in OBJECT_PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    return f"{prefix}-{n:06d}"


def next_id(conn: Connection, prefix: str) -> str:
    """Issue the next id for ``prefix`` inside the caller's transaction."""
    if prefix not in OBJECT_PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}")
    row = conn.execute(sa.select(id_sequences.c.high_water).where(id_sequences.c.prefix == prefix)).first()
    if row is None:
        conn.execute(id_sequences.insert().values(prefix=prefix, high_water=1))
        return format_id(prefix, 1)
    n = int(row[0]) + 1
    conn.execute(id_sequences.update().where(id_sequences.c.prefix == prefix).values(high_water=n))
    return format_id(prefix, n)


def next_ids(conn: Connection, prefix: str, count: int) -> list[str]:
    if count <= 0:
        return []
    row = conn.execute(sa.select(id_sequences.c.high_water).where(id_sequences.c.prefix == prefix)).first()
    start = int(row[0]) if row is not None else 0
    end = start + count
    if row is None:
        conn.execute(id_sequences.insert().values(prefix=prefix, high_water=end))
    else:
        conn.execute(id_sequences.update().where(id_sequences.c.prefix == prefix).values(high_water=end))
    return [format_id(prefix, n) for n in range(start + 1, end + 1)]


def exposed_id(prefix: str, integer_pk: int) -> str:
    """Stable public id for legacy integer-keyed L1 rows (SRC-/VER-/CLS-)."""
    return format_id(prefix, int(integer_pk))


def integer_pk(exposed: str) -> int:
    m = _ID_RE.match(exposed or "")
    if not m:
        raise ValueError(f"not an object id: {exposed!r}")
    return int(m.group("n"))
