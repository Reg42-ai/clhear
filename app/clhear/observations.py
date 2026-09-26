# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""One observation document for every app, agent, or process.

A connector copies an MCP ``structuredContent`` or an A2A ``DataPart`` into
this shape. CLHEAR does not read the vendor envelope. Unmapped words are
stored and shown. They do not enter the compliance score and they do not
mint ontology terms.

The score is the share of required blueprint items whose latest mapped
result is ``pass``. A fail, a gap, or a missing observation lowers it. A
``not_applicable`` result leaves the denominator, as in the instance contract
(present / (items − not_applicable)); the point and its reasoning stay visible.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l3.kinds import NOT_SPECIFIED, required_fields
from app.clhear.l4.ontology import COLLECTIONS, Lookup, _TABLES
from app.clhear.l6.explain import allowed_ids, cited_ids
from app.clhear.models import Json, metadata

MAPPED_RESULTS = ("pass", "fail", "gap", "not_applicable")
PERFORMER_KINDS = ("app", "agent", "process")
PROTOCOLS = ("mcp", "a2a", "https")
_ONTOLOGY_ID = re.compile(r"^(?:OBL|BLK|ACT|ITM)[:-][A-Za-z0-9_./#():-]+$")
_CLAIMED_ID = re.compile(r"^[A-Z]{2,}:")

observations = sa.Table(
    "observations",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("post_id", sa.Text, nullable=False, default=""),
    sa.Column("ontology_ids", Json, nullable=False),
    sa.Column("subject", Json, nullable=False),
    sa.Column("result", sa.Text, nullable=False),
    sa.Column("mapped", sa.Boolean, nullable=False),
    sa.Column("unmapped_labels", Json, nullable=False),
    sa.Column("characteristics", Json, nullable=False),
    sa.Column("evidence", Json, nullable=False),
    sa.Column("reasoning", sa.Text, nullable=False, default=""),
    sa.Column("performer", Json, nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class ObservationRejected(ValueError):
    """The document is not the observation contract. Unmapped words are not this."""


def _store(engine: Engine) -> Engine:
    """Observations are tenant writes: on the web tier they go to the durable identity
    store, never into the read-only snapshot a swap replaces."""
    from app.clhear import identity

    return identity.engine() if identity.configured() else engine


def _lookups(conn) -> list[Lookup]:
    found = []
    for name in COLLECTIONS:
        table = _TABLES[name]
        try:
            rows = [dict(r) for r in conn.execute(
                sa.select(table).where(table.c.valid_to.is_(None))).mappings()]
        except (sa.exc.OperationalError, AttributeError):
            continue
        named = [row for row in rows if row.get("name") and row.get("id")]
        if named:
            found.append(Lookup(named))
    return found


def _parse_subject(subject) -> tuple[list[str], str]:
    if isinstance(subject, dict):
        ids = list(subject.get("ids") or [])
        post_id = str(subject.get("post_id") or "")
        extra = [v for k, v in subject.items() if k not in {"ids", "post_id"}]
        if extra:
            raise ObservationRejected("subject only carries ontology ids and a post id")
    elif isinstance(subject, list):
        ids, post_id = [], ""
        for item in subject:
            if not isinstance(item, str) or not item.strip():
                raise ObservationRejected("subject entries must be strings")
            token = item.strip()
            if _ONTOLOGY_ID.match(token):
                ids.append(token)
            elif not post_id:
                post_id = token
            else:
                raise ObservationRejected("subject has more than one post id")
    else:
        raise ObservationRejected("subject must list ontology ids and the post id")
    if not ids:
        raise ObservationRejected("subject needs at least one OBL, BLK, ACT, or ITM id")
    if not post_id:
        raise ObservationRejected("subject needs the post id")
    return ids, post_id


def _performer(value) -> dict:
    if not isinstance(value, dict):
        raise ObservationRejected("performer is {kind, id, protocol}")
    kind, ident, protocol = value.get("kind"), value.get("id"), value.get("protocol")
    if kind not in PERFORMER_KINDS or protocol not in PROTOCOLS or not isinstance(ident, str) or not ident.strip():
        raise ObservationRejected("performer.kind is app|agent|process, protocol is mcp|a2a|https, id is required")
    return {"kind": kind, "id": ident.strip(), "protocol": protocol}


def _measured(value, lookups: list[Lookup]) -> tuple[object | None, str | None]:
    """Return (measured, unmapped label). A resolved alias becomes its ontology id."""
    if isinstance(value, dict):
        if "value" in value and isinstance(value.get("unit"), str) and value["unit"].strip():
            return {"value": value["value"], "unit": value["unit"].strip()}, None
        return None, "quantity-without-unit"
    if not isinstance(value, str) or not value.strip():
        return None, "empty-characteristic"
    text = value.strip()
    for lookup in lookups:
        row = lookup.resolve(text)
        if row is not None:
            return row["id"], None
    if _CLAIMED_ID.match(text):
        return None, text
    return text, None


def _when(value) -> datetime:
    if value in (None, ""):
        return datetime.now(timezone.utc)
    if not isinstance(value, str):
        raise ObservationRejected("observed_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationRejected("observed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def accept_observation(engine: Engine, body: dict) -> dict:
    """Store one observation. A vendor result word is stored unmapped, not rejected."""
    if not isinstance(body, dict):
        raise ObservationRejected("observation must be a JSON object")
    ontology_ids, post_id = _parse_subject(body.get("subject"))
    performer = _performer(body.get("performer"))
    observed_at = _when(body.get("observed_at"))
    result = body.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ObservationRejected("result is required")
    result = result.strip()
    labels = body.get("labels") or []
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise ObservationRejected("labels must be a list of strings")
    unmapped = [item.strip() for item in labels if item.strip()]
    mapped = result in MAPPED_RESULTS
    if not mapped:
        unmapped.append(result)
    characteristics = body.get("characteristics") or {}
    if not isinstance(characteristics, dict):
        raise ObservationRejected("characteristics must be an object of L3 keys")
    evidence = body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ObservationRejected("evidence must be an object")
    reasoning = body.get("reasoning") or ""
    if not isinstance(reasoning, str):
        raise ObservationRejected("reasoning must be text")
    with engine.connect() as conn:
        lookups = _lookups(conn)
    measured: dict = {}
    for key, value in characteristics.items():
        if not isinstance(key, str) or not key.strip():
            unmapped.append("empty-characteristic-key")
            continue
        kept, rejected = _measured(value, lookups)
        if rejected:
            unmapped.append(rejected if rejected != "quantity-without-unit" else f"{key}:quantity-without-unit")
            continue
        measured[key.strip()] = kept
    stored = {
        "id": "obs-" + uuid.uuid4().hex,
        "post_id": post_id,
        "ontology_ids": ontology_ids,
        "subject": body.get("subject"),
        "result": result,
        "mapped": mapped,
        "unmapped_labels": unmapped,
        "characteristics": measured,
        "evidence": evidence,
        "reasoning": reasoning,
        "performer": performer,
        "observed_at": observed_at,
        "created_at": datetime.now(timezone.utc),
    }
    with _store(engine).begin() as conn:
        conn.execute(observations.insert().values(**stored))
    return _public(stored)


def _public(row: dict) -> dict:
    observed = row["observed_at"]
    return {
        "id": row["id"],
        "post_id": row["post_id"],
        "ontology_ids": list(row["ontology_ids"] or []),
        "result": row["result"],
        "mapped": bool(row["mapped"]),
        "unmapped_labels": list(row["unmapped_labels"] or []),
        "characteristics": dict(row["characteristics"] or {}),
        "evidence": dict(row["evidence"] or {}),
        "reasoning": row["reasoning"] or "",
        "performer": dict(row["performer"] or {}),
        "observed_at": observed.isoformat() if hasattr(observed, "isoformat") else observed,
    }


def list_observations(engine: Engine) -> list[dict]:
    with _store(engine).connect() as conn:
        rows = conn.execute(sa.select(observations).order_by(observations.c.observed_at, observations.c.created_at)).mappings()
        return [_public(dict(row)) for row in rows]


def _item_ids(item: dict) -> set[str]:
    found = {item.get("block_id"), item.get("id")}
    found.update(item.get("obligations_satisfied") or [])
    found.update(item.get("required_by") or [])
    return {value for value in found if isinstance(value, str) and value}


def _reasoning_on_blueprint(text: str, blueprint: dict) -> bool:
    return cited_ids(text) <= allowed_ids(blueprint)


def _required_values(item: dict) -> dict:
    fields = required_fields(item.get("kind") or "Process")
    backed = {}
    for characteristic in item.get("characteristics") or []:
        key = characteristic.get("key")
        if key:
            backed[key] = characteristic.get("value") or NOT_SPECIFIED
    return {key: backed.get(key, NOT_SPECIFIED) for key in fields}


def _actual_for(item: dict, measured: dict) -> tuple[dict, list[str]]:
    allowed = set(required_fields(item.get("kind") or "Process"))
    actual, skipped = {}, []
    for key, value in (measured or {}).items():
        if key in allowed:
            actual[key] = value
        else:
            skipped.append(key)
    return actual, skipped


def _clauses(item: dict, blueprint: dict) -> list[dict]:
    by_id = {row["obligation_id"]: row for row in blueprint.get("coverage") or []}
    out = []
    for oid in item.get("obligations_satisfied") or []:
        row = by_id.get(oid) or {}
        out.append({
            "obligation_id": oid,
            "source_key": row.get("source_key", ""),
            "clause_ref": row.get("clause_ref", ""),
            "title": row.get("title", ""),
        })
    return out


def attach_performance(engine: Engine, blueprint: dict) -> dict:
    """Overlay measured performance and the compliance score on one blueprint."""
    rows = list_observations(engine)
    points = []
    required_items = [item for item in blueprint.get("items") or [] if item.get("basis") == "required"]
    passed = 0
    for item in blueprint.get("items") or []:
        ids = _item_ids(item)
        hitting = [row for row in rows if ids.intersection(row["ontology_ids"])]
        mapped = [row for row in hitting if row["mapped"] and _reasoning_on_blueprint(row["reasoning"], blueprint)]
        chosen = mapped[-1] if mapped else None
        actual, skipped = _actual_for(item, (chosen or {}).get("characteristics") or {})
        unmapped = []
        for row in hitting:
            unmapped.extend(row["unmapped_labels"])
        unmapped.extend(skipped)
        performance = {
            "result": chosen["result"] if chosen else "missing",
            "reasoning": chosen["reasoning"] if chosen else "",
            "evidence": chosen["evidence"] if chosen else {},
            "performer": chosen["performer"] if chosen else {},
            "observed_at": chosen["observed_at"] if chosen else None,
            "post_id": chosen["post_id"] if chosen else None,
            "required": _required_values(item),
            "actual": actual,
            "unmapped_labels": list(dict.fromkeys(unmapped)),
            "clauses": _clauses(item, blueprint),
        }
        item["performance"] = performance
        if item.get("basis") == "required":
            if performance["result"] == "pass":
                passed += 1
            points.append({
                "block_id": item.get("block_id"),
                "name": item.get("name"),
                "kind": item.get("kind"),
                "result": performance["result"],
                "reasoning": performance["reasoning"],
                "clauses": performance["clauses"],
                "unmapped_labels": performance["unmapped_labels"],
            })
    excluded = sum(1 for point in points if point["result"] == "not_applicable")
    required = len(required_items) - excluded
    blueprint["compliance_score"] = {
        "label": "compliance",
        "value": (passed / required) if required else None,
        "passed": passed,
        "required": required,
        "not_applicable": excluded,
        "points": points,
    }
    blueprint["enforcement_risk"] = {
        "label": "enforcement exposure",
        "layer": "L7",
        "moves_with_observations": False,
    }
    return blueprint
