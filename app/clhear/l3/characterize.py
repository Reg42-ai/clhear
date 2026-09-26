# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 characterizers — fill each block's fixed characteristic schema from the
text of the obligations that require it (HLD v2 §4.3).

Every required key of the block's kind gets exactly one live row:
``backed`` (value is a substring of a backing obligation's text; the backing
span is kept), ``not_specified`` (the source says nothing — recorded
explicitly, never left blank) or ``unbacked`` (LLM proposal that failed the
grounding check; never counted as filled). Deterministic extractors run first;
the ``l3.characterize`` task is consulted only for keys they leave open and
its answers must be >= 80 % grounded in the backing text.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import blocks, characteristics, obligations, requires
from app.clhear.l3.kinds import KIND_SCHEMAS, NOT_SPECIFIED, required_fields
from app.clhear.platform import record
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l3.characterize")

AGENT = "l3.characterize"
GROUNDING = 0.8

_SENTENCE = re.compile(r"[^.;\n]+[.;]?")
_WORD = re.compile(r"[a-z0-9]+")

_CADENCE = re.compile(
    r"\b(?:at least )?(?:annually|quarterly|monthly|weekly|daily|each year|every year|once a year|"
    r"at least (?:once )?(?:every|each|in every|a) [\w\- ]{1,20}?(?:years?|months?|quarters?|days?|weeks?)|"
    r"every \d+ (?:years?|months?|weeks?|days?)|on an ongoing basis|continuous(?:ly)?|periodic(?:ally)?|"
    r"without (?:undue )?delay|immediately|promptly|within \d+ (?:business |working |calendar )?(?:days?|hours?|months?|weeks?))\b",
    re.I,
)
_RETENTION = re.compile(
    r"\b(?:retain\w*|keep|kept|retained|preserv\w+|stored?)\b[^.;]{0,80}?\b(?:for (?:a period of )?(?:at least |not less than )?"
    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten) years?)",
    re.I,
)
_YEARS = re.compile(r"\b(?:for (?:a period of )?(?:at least |not less than |a minimum of )?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) years?)\b", re.I)
_APPROVER = re.compile(r"\bapprov\w+ by (?:the |its )?(senior management|board(?: of directors)?|management body|governing body|[\w\- ]{3,40}?(?:committee|officer|director|manager))\b", re.I)
_SENIORITY = re.compile(r"\b(senior management|member of (?:the )?(?:board|management body)|board[- ]level|director|senior manager\w*|management body|(?:sufficient|appropriate|adequate|necessary) (?:seniority|authority|standing)|sufficiently senior)\b", re.I)
_INDEPENDENCE = re.compile(r"\b(independen\w+[^.;]{0,60}|free from [^.;]{0,60}|not (?:be )?involved in [^.;]{0,60})", re.I)
_COMPETENCE = re.compile(r"\b((?:sufficient |adequate |appropriate |necessary )?(?:knowledge|experience|skills?|expertise|competen\w+|qualif\w+|fit and proper|good repute)[^.;]{0,60})", re.I)
_REPORTING = re.compile(r"\breport(?:s|ing)? (?:directly )?to (?:the |its )?([\w\- ]{3,50}?)(?=[.,;]| and | or | in | on | at |$)", re.I)
_TRIGGER = re.compile(r"\b((?:where|when|whenever|if|upon|on|before|after|prior to|as soon as|in the event of) [^.;,]{5,120})", re.I)
_SYSTEM = re.compile(r"\b((?:[\w\-]+ ){0,3}(?:systems?(?: and controls)?|software|tool|register|database|automated [\w\- ]{3,30}))\b", re.I)
_OUTPUT = re.compile(r"\b((?:[\w\-]+ ){0,2}(?:reports?|notifications?|notices?|returns?|statements?|assessments?|records?|decisions?|confirmations?|disclosures?|certificates?|registers?))\b", re.I)
_QUANTITY = re.compile(r"\b((?:at least|not less than|no more than|not exceed\w*|a minimum of|a maximum of|equal to)?\s*(?:\d[\d,.]*\s*(?:%|per cent|percent|days?|months?|years?|hours?|eur|gbp|usd|million|billion)|(?:eur|gbp|usd|chf|ils)\s?[\d,.]+(?:\s?(?:million|billion))?|€\s?[\d,.]+|£\s?[\d,.]+|\$\s?[\d,.]+)(?:\s(?:million|billion|per cent|percent|%|days?|months?|years?|hours?|of (?:the |its )?[\w\-]+(?: [\w\-]+)?))?)(?=[\s.,;]|$)", re.I)
_CUSTODY = re.compile(r"\b((?:segregat\w+|held (?:in|with|at|on trust)|kept separate|separate account|designated (?:client )?account|trust)[^.;]{0,60})", re.I)
_QUORUM = re.compile(r"\b((?:quorum|composed of|comprising|consist(?:s|ing)? of|members? of|at least \w+ members?)[^.;]{0,60})", re.I)
_MANDATE = re.compile(r"\b((?:responsible for|oversee\w*|approve\w*|decide\w*|review\w*|monitor\w*)[^.;]{0,80})", re.I)
_SLA = re.compile(r"\b(within \d+ (?:business |working |calendar )?(?:days?|hours?|months?|weeks?)|no later than [^.;,]{3,40}|by [\w ]{3,30}? each year|without (?:undue )?delay)\b", re.I)
_SECTIONS = re.compile(r"\b(?:cover(?:s|ing)?|set(?:s|ting)? out|includ(?:e|es|ing)|specif(?:y|ies|ying)|address(?:es|ing)?|describ(?:e|es|ing))\s+([^.;]{10,160})", re.I)


def _first(pattern: re.Pattern, text: str, group: int = 0) -> str:
    m = pattern.search(text or "")
    if not m:
        return ""
    value = m.group(group) if group <= (m.lastindex or 0) else m.group(0)
    return re.sub(r"\s+", " ", (value or "").strip(" ,;:."))


def extract_value(kind: str, key: str, ob: dict) -> str:
    """Deterministic value for one characteristic from one obligation."""
    text = ob.get("determination") or ob.get("statement") or ""
    full = " ".join(t for t in (ob.get("statement"), ob.get("determination")) if t)
    subject, action, condition, obj = (ob.get("subject") or "", ob.get("action") or "", ob.get("condition") or "", ob.get("object") or "")
    if key in ("cadence", "review_cadence"):
        return _first(_CADENCE, full)
    if key in ("performing_role", "owner"):
        return subject if subject and subject.lower() in full.lower() else ""
    if key == "approver":
        return _first(_APPROVER, full, 1)
    if key == "trigger":
        return condition if condition and condition.lower() in full.lower() else _first(_TRIGGER, full, 1)
    if key == "system_or_tool":
        return _first(_SYSTEM, full, 1)
    if key == "output":
        return _first(_OUTPUT, full, 1) or (obj if obj and obj.lower() in full.lower() else "")
    if key in ("record", "retention"):
        return _first(_RETENTION, full) or _first(_YEARS, full)
    if key == "mandatory_sections":
        return _first(_SECTIONS, full, 1)
    if key == "seniority":
        return _first(_SENIORITY, full, 1)
    if key == "independence":
        return _first(_INDEPENDENCE, full, 1)
    if key == "competence":
        return _first(_COMPETENCE, full, 1)
    if key == "reporting_line":
        return _first(_REPORTING, full, 1)
    if key == "mandate":
        return _first(_MANDATE, full, 1) or (action if action and action.lower() in full.lower() else "")
    if key == "quorum":
        return _first(_QUORUM, full, 1)
    if key == "capability":
        return action if action and action.lower() in full.lower() else _first(_SYSTEM, full, 1)
    if key == "data_inputs":
        return obj if obj and obj.lower() in full.lower() else ""
    if key == "quantity_or_threshold":
        return _first(_QUANTITY, full, 1)
    if key == "custody":
        return _first(_CUSTODY, full, 1)
    if key == "parameter":
        return obj if obj and obj.lower() in full.lower() else _first(_OUTPUT, full, 1)
    if key == "allowed_range":
        return _first(_QUANTITY, full, 1)
    if key == "composed_processes":
        return action if action and action.lower() in full.lower() else ""
    if key == "sla":
        return _first(_SLA, full, 1)
    return ""


def backing_span(value: str, text: str) -> str:
    """The sentence of ``text`` that contains ``value`` (case-insensitive)."""
    if not value or not text:
        return ""
    low, needle = text.lower(), value.lower()
    if needle not in low:
        return ""
    for m in _SENTENCE.finditer(text):
        if needle in m.group(0).lower():
            return m.group(0).strip()
    return text[:200]


def grounded(value: str, texts: list[str], minimum: float = GROUNDING) -> bool:
    words = [w for w in _WORD.findall((value or "").lower()) if len(w) > 2]
    if not words:
        return False
    corpus = " ".join(texts).lower()
    hits = sum(1 for w in words if w in corpus)
    return hits / len(words) >= minimum


def _backing(conn: Connection, block_id: str) -> list[dict]:
    rows = conn.execute(
        sa.select(obligations)
        .join(requires, requires.c.obligation_id == obligations.c.id)
        .where(requires.c.block_id == block_id)
        .where(requires.c.valid_to.is_(None))
        .where(obligations.c.status.in_(("derived", "validated")))
        .order_by(obligations.c.stable_id)
    ).mappings().all()
    return [dict(r) for r in rows]


def _live_keys(conn: Connection, block_id: str) -> dict[str, dict]:
    rows = conn.execute(
        sa.select(characteristics).where(characteristics.c.block_id == block_id).where(characteristics.c.valid_to.is_(None))
    ).mappings().all()
    return {r["key"]: dict(r) for r in rows}


def _why(block_id: str, *, method: str, summary: str, confidence: float | None, manifest: dict | None = None,
         evidence: list[str] | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L3",
        reasoning_summary=summary,
        evidence_refs=evidence or [],
        inputs=(block_id, method, *(evidence or [])),
        model_manifest=manifest or {"model": "deterministic", "method": method},
        skill_version=AGENT,
        confidence=confidence,
        agent_id=AGENT,
        subject_ref=block_id,
        input_layers=("L2",),
    )


def _write(conn: Connection, block_id: str, key: str, *, value: str, status: str, backing_ob: str | None,
           span: str, method: str, why) -> None:
    record.write(
        conn,
        characteristics,
        {
            "block_id": block_id,
            "key": key,
            "value": value,
            "status": status,
            "backing_obligation_id": backing_ob,
            "backing_span": span,
            "method": method,
        },
        why=why,
        valid_from=datetime.now(timezone.utc).date(),
    )


def _llm_fill(llm, block: dict, keys: list[str], backing: list[dict]) -> dict[str, str]:
    if llm is None or not keys or not backing:
        return {}
    schema = KIND_SCHEMAS[block["kind"]]["fields"]
    descriptions = {k: d for k, d in schema}
    texts = "\n".join(f"- [{o['stable_id'] or o['id']}] {o['statement'] or o['determination']}" for o in backing[:12])
    prompt = (
        f"Building block: {block['name']} (kind {block['kind']}). Purpose: {block['purpose'] or block['description']}\n"
        "From ONLY the obligation texts below, fill these characteristics. Quote or closely paraphrase the text; "
        f"if the sources do not say, answer exactly \"{NOT_SPECIFIED}\".\n"
        + "\n".join(f"- {k}: {descriptions[k]}" for k in keys)
        + "\nJSON object with exactly these keys.\n\nObligations:\n" + texts
    )
    try:
        result = complete(
            llm, "l3.characterize", prompt=prompt,
            system="You fill fixed characteristic schemas from legal text. Never invent facts. JSON only.",
            required_keys=list(keys), max_tokens=700,
        )
        parsed = parse_json_object(result.text)
    except Exception:
        log.exception("l3.characterize failed for %s", block["id"])
        return {}
    out = {k: str(parsed.get(k, "")).strip() for k in keys}
    out["__model__"] = getattr(result, "model", "") or ""
    return out


def characterize_block(engine: Engine, block: dict, llm=None) -> dict:
    """Fill every missing required key of one block. Deterministic values are
    written first; the model is consulted (outside any write transaction) only
    for keys the regexes could not fill, and its answers are accepted solely
    when grounded in the backing text. Returns per-status counts."""
    counts = {"backed": 0, "not_specified": 0, "unbacked": 0}
    with engine.begin() as conn:
        backing = _backing(conn, block["id"])
        texts = [" ".join(t for t in (o.get("statement"), o.get("determination")) if t) for o in backing]
        live = _live_keys(conn, block["id"])
        missing = [k for k in required_fields(block["kind"]) if k not in live]
        if not missing:
            return counts
        evidence = [o["id"] for o in backing]
        why = _why(block["id"], method="deterministic", confidence=0.85 if backing else None,
                   summary=f"characteristics of {block['kind']} block from {len(backing)} backing obligation(s)", evidence=evidence)
        trail = why.write(conn)
        still_open: list[str] = []
        for key in missing:
            found = False
            for ob in backing:
                value = extract_value(block["kind"], key, ob)
                text = " ".join(t for t in (ob.get("statement"), ob.get("determination")) if t)
                span = backing_span(value, text)
                if value and span:
                    _write(conn, block["id"], key, value=value[:300], status="backed", backing_ob=ob["id"], span=span[:500],
                           method="deterministic", why=trail)
                    counts["backed"] += 1
                    found = True
                    break
            if not found:
                still_open.append(key)
    answers: dict[str, str] = {}
    if still_open and llm is not None and backing:
        answers = _llm_fill(llm, block, still_open, backing)
    with engine.begin() as conn:
        if answers:
            model = answers.pop("__model__", "")
            llm_trail = _why(block["id"], method="l3.characterize", confidence=0.75,
                             summary=f"l3.characterize proposals for {len(still_open)} open key(s), grounding >= {GROUNDING:.0%}",
                             manifest={"model": model, "task": "l3.characterize"}, evidence=evidence).write(conn)
            for key in list(still_open):
                value = answers.get(key, "")
                if not value:
                    continue
                if value.lower().startswith(NOT_SPECIFIED):
                    _write(conn, block["id"], key, value=NOT_SPECIFIED, status="not_specified", backing_ob=None, span="",
                           method="l3.characterize", why=llm_trail)
                    counts["not_specified"] += 1
                    still_open.remove(key)
                elif grounded(value, texts):
                    ob = next((o for o, t in zip(backing, texts) if backing_span(value, t)), backing[0])
                    span = backing_span(value, " ".join(t for t in (ob.get("statement"), ob.get("determination")) if t))
                    _write(conn, block["id"], key, value=value[:300], status="backed", backing_ob=ob["id"], span=span[:500],
                           method="l3.characterize", why=llm_trail)
                    counts["backed"] += 1
                    still_open.remove(key)
                else:
                    _write(conn, block["id"], key, value=value[:300], status="unbacked", backing_ob=None, span="",
                           method="l3.characterize", why=llm_trail)
                    counts["unbacked"] += 1
                    still_open.remove(key)
        for key in still_open:
            _write(conn, block["id"], key, value=NOT_SPECIFIED, status="not_specified", backing_ob=None, span="",
                   method="deterministic", why=trail)
            counts["not_specified"] += 1
    return counts


def characterize(engine: Engine, llm=None, *, limit: int | None = None) -> dict:
    from app.clhear.l3.harmonize import blocks_in_scope

    totals = {"blocks": 0, "backed": 0, "not_specified": 0, "unbacked": 0}
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(blocks).where(blocks.c.canonical_id.is_(None)).order_by(blocks.c.id)).mappings()]
        allowed = blocks_in_scope(conn)
        if allowed is not None:
            rows = [r for r in rows if r["id"] in allowed]
    if limit:
        rows = rows[:limit]
    for b in rows:
        counts = characterize_block(engine, b, llm)
        if any(counts.values()):
            totals["blocks"] += 1
        for k in ("backed", "not_specified", "unbacked"):
            totals[k] += counts[k]
    log.info("L3 characterize: %s", totals)
    return totals


def completeness(engine: Engine) -> dict:
    """Share of required characteristics (over canonical blocks) that are
    filled: backed by a span or explicitly not specified. Unbacked and
    missing rows count against the block."""
    with engine.connect() as conn:
        rows = conn.execute(sa.select(blocks.c.id, blocks.c.kind).where(blocks.c.canonical_id.is_(None))).all()
        live = conn.execute(
            sa.select(characteristics.c.block_id, characteristics.c.key, characteristics.c.status)
            .where(characteristics.c.valid_to.is_(None))
        ).all()
    have = {(r.block_id, r.key): r.status for r in live}
    required = filled = backed = not_spec = 0
    gaps: list[str] = []
    for b in rows:
        for key in required_fields(b.kind):
            required += 1
            status = have.get((b.id, key))
            if status == "backed":
                filled += 1
                backed += 1
            elif status == "not_specified":
                filled += 1
                not_spec += 1
            else:
                gaps.append(f"{b.id}:{key}")
    return {
        "blocks": len(rows),
        "required": required,
        "filled": filled,
        "backed": backed,
        "not_specified": not_spec,
        "gaps": gaps[:40],
        "gap_count": len(gaps),
        "rate": round(filled / required, 4) if required else 0.0,
    }
