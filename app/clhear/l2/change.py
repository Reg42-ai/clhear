# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 change inferencers (HLD v2 §4.2).

An L1 clause change → which obligations change how. Two entry points:

* :func:`on_l1_changed` — consumer of the ``clhear.l1.changed`` bus event.
  Re-derives the registry for the changed source (idempotent: unchanged basis
  hashes produce no events), so every added / updated / revoked obligation
  carries the L1 change event id, its cause clause ids and the effective
  date the L1 change detector extracted. Revoked + added pairs whose text
  survives a renumbering become ``supersessions``.
* :func:`nightly_change_pass` — rolling-window sweep: any L1 change event
  in the window with no L2 change events yet is processed, and obligations
  whose ``effective_to`` has passed are revoked on expiry.

:func:`infer_clause_change` is the deterministic core used by both and by
the ``l2_change_inference`` eval against ``clhear-evals/l2/change_events``.
An optional ``l2.change`` model call may only downgrade an *ambiguous*
wording change to editorial; it never invents a change.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import l2_change_events, obligations, supersessions
from app.clhear.l1.models import change_events as l1_change_events
from app.clhear.l1.models import sources
from app.clhear.l2 import registry
from app.clhear.l2.extract import detect_duty, run_extraction
from app.clhear.platform import record

log = logging.getLogger("clhear.l2.change")

FLEET = "l2.change"
SUPERSESSION_THRESHOLD = 0.6  # content-word containment old -> new

_WORD = re.compile(r"[a-z0-9]+")
_NUMBERISH = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|per cent|days?|months?|years?|hours?|business days?|working days?|eur|gbp|usd|€|£|\$)?", re.I)
_EDITORIAL = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "by", "with", "that", "which", "its", "their", "such", "any", "all", "as", "at", "be", "is", "are"})


@dataclass(frozen=True)
class ClauseChange:
    kind: str  # added | updated | revoked | none
    materiality: str  # substantive | editorial | n/a
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "materiality": self.materiality, "reasons": list(self.reasons)}


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _content(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t not in _EDITORIAL}


def infer_clause_change(old_text: str | None, new_text: str | None, ref: str = "") -> ClauseChange:
    """Deterministic: duty status flips decide added/revoked; otherwise a wording
    change is substantive when the modal, the subject/action structure, or any
    number/period/amount changes, or when > 15 % of content words differ."""
    old_duty = detect_duty(old_text or "", ref) is not None if old_text else False
    new_duty = detect_duty(new_text or "", ref) is not None if new_text else False
    if not old_text and not new_text:
        return ClauseChange("none", "n/a")
    if not old_text:
        return ClauseChange("added" if new_duty else "none", "substantive" if new_duty else "n/a", ("clause added",))
    if not new_text:
        return ClauseChange("revoked" if old_duty else "none", "substantive" if old_duty else "n/a", ("clause removed",))
    if old_duty and not new_duty:
        return ClauseChange("revoked", "substantive", ("duty language removed",))
    if new_duty and not old_duty:
        return ClauseChange("added", "substantive", ("duty language introduced",))
    if not new_duty:
        return ClauseChange("none", "n/a", ("no duty in either version",))
    if " ".join(old_text.split()) == " ".join(new_text.split()):
        return ClauseChange("none", "n/a", ("whitespace only",))
    reasons: list[str] = []
    old_s, new_s = registry.parse_structure(old_text), registry.parse_structure(new_text)
    if old_s["modal"] != new_s["modal"]:
        reasons.append(f"modal {old_s['modal']!r} -> {new_s['modal']!r}")
    if _content(_tokens(old_s["subject"])) != _content(_tokens(new_s["subject"])):
        reasons.append("subject changed")
    if _content(_tokens(old_s["condition"])) != _content(_tokens(new_s["condition"])):
        reasons.append("condition changed")
    old_nums = [m.group(0).strip().lower() for m in _NUMBERISH.finditer(old_text)]
    new_nums = [m.group(0).strip().lower() for m in _NUMBERISH.finditer(new_text)]
    if sorted(old_nums) != sorted(new_nums):
        reasons.append("number / period / amount changed")
    old_c, new_c = _content(_tokens(old_text)), _content(_tokens(new_text))
    union = old_c | new_c
    diff_share = len(old_c ^ new_c) / len(union) if union else 0.0
    if diff_share > 0.15:
        reasons.append(f"{round(diff_share * 100)}% of content words differ")
    if reasons:
        return ClauseChange("updated", "substantive", tuple(reasons))
    return ClauseChange("none", "editorial", ("wording only",))


def refine_with_router(router, old_text: str, new_text: str, current: ClauseChange) -> ClauseChange:
    """Optional `l2.change` escalation for wording changes just over the
    threshold with no structural reason: the model may say 'editorial' only."""
    if router is None or current.kind != "updated":
        return current
    structural = any(not r.endswith("of content words differ") for r in current.reasons)
    if structural:
        return current
    try:
        from app.clhear.platform.gateway import parse_json_object
        from app.clhear.platform.router import complete

        result = complete(
            router, "l2.change",
            prompt=(
                "Two versions of the same regulatory clause. Does the change alter WHAT anyone must do "
                "(substantive) or only HOW it is worded (editorial)? Reply JSON "
                '{"materiality": "substantive"|"editorial", "reason": "..."}.\n\nOLD:\n'
                + old_text[:2500] + "\n\nNEW:\n" + new_text[:2500]
            ),
            required_keys=["materiality"], max_tokens=200,
        )
        parsed = parse_json_object(result.text)
    except Exception:
        return current
    if str(parsed.get("materiality", "")).lower() == "editorial":
        return ClauseChange("none", "editorial", current.reasons + (f"model: {str(parsed.get('reason', ''))[:120]}",))
    return current


def _containment(a: str, b: str) -> float:
    """Share of the smaller clause's content words that survive in the other:
    a re-enacted provision usually keeps its old text and adds to it."""
    sa_, sb = _content(_tokens(a)), _content(_tokens(b))
    smaller = min(len(sa_), len(sb))
    if smaller < 4:
        return 0.0
    return len(sa_ & sb) / smaller


def _already_processed(conn, l1_change_event_id: int) -> bool:
    """Processed = produced L2 change events, or a recorded l2.change run
    (an L1 change that touched no duty clause still counts as handled)."""
    from app.clhear.models import runs

    if conn.execute(
        sa.select(l2_change_events.c.id).where(l2_change_events.c.cause_l1_change_event_id == l1_change_event_id).limit(1)
    ).first() is not None:
        return True
    return conn.execute(
        sa.select(runs.c.id)
        .where(runs.c.fleet == FLEET)
        .where(
            sa.or_(
                runs.c.inputs.cast(sa.Text).like(f'%"l1_change_event_id": {int(l1_change_event_id)},%'),
                runs.c.inputs.cast(sa.Text).like(f'%"l1_change_event_id": {int(l1_change_event_id)}}}%'),
            )
        )
        .limit(1)
    ).first() is not None


def _record_run(engine: Engine, l1_change_event_id: int | None, source_key: str, summary: dict) -> None:
    from app.clhear.models import runs

    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=FLEET, trigger="l1.changed",
                inputs={"l1_change_event_id": l1_change_event_id, "source": source_key},
                outputs=summary, duration_ms=0,
                reasoning=f"L2 change inference for {source_key}: {summary.get('obligations')}",
            )
        )


def _link_supersessions(engine: Engine, source_key: str, l1_change_event_id: int | None) -> int:
    """Revoked + added obligations from one L1 change whose text survives
    (renumbered / moved clause) -> supersession old -> new."""
    linked = 0
    with engine.begin() as conn:
        where = l2_change_events.c.source_key == source_key
        if l1_change_event_id is not None:
            where = sa.and_(where, l2_change_events.c.cause_l1_change_event_id == l1_change_event_id)
        rows = conn.execute(sa.select(l2_change_events).where(where)).mappings().all()
        revoked = [r for r in rows if r["kind"] == "revoked"]
        added = [r for r in rows if r["kind"] == "added"]
        if not revoked or not added:
            return 0
        rows_ob = conn.execute(sa.select(obligations.c.id, obligations.c.statement, obligations.c.stable_id, obligations.c.canonical_id).where(
            obligations.c.id.in_([x["obligation_id"] for x in revoked + added]))).all()
        texts = {r.id: (r.statement or "") for r in rows_ob}
        threads = {r.id: (r.canonical_id or r.stable_id) for r in rows_ob}
        used: set[str] = set()
        for old in revoked:
            best, score = None, 0.0
            for new in added:
                if new["obligation_id"] in used:
                    continue
                s = _containment(texts.get(old["obligation_id"], ""), texts.get(new["obligation_id"], ""))
                if s > score:
                    best, score = new, s
            if best is None or score < SUPERSESSION_THRESHOLD:
                continue
            exists = conn.execute(
                sa.select(supersessions.c.id)
                .where(supersessions.c.old_obligation_id == old["obligation_id"])
                .where(supersessions.c.new_obligation_id == best["obligation_id"])
            ).first()
            if exists:
                continue
            why = registry.why_for(
                best["obligation_id"], clause_id=None, text_hash=best["new_text_hash"], method="l2.change.supersession",
                confidence=round(score, 3),
                summary=f"clause renumbered/moved: {old['obligation_id']} superseded by {best['obligation_id']} (similarity {score:.2f})",
            )
            trail = why.write(conn)
            registry.record_supersession(
                conn, old_obligation_id=old["obligation_id"], new_obligation_id=best["obligation_id"],
                cause_change_event_id=best["id"], effective_date=best["effective_date"],
                note=f"text similarity {score:.2f}", why=trail,
            )
            # The successor carries the predecessor's canonical thread so the
            # obligation's history reads continuously across renumbering.
            if threads.get(old["obligation_id"]):
                conn.execute(
                    obligations.update().where(obligations.c.id == best["obligation_id"]).values(canonical_id=threads[old["obligation_id"]])
                )
            used.add(best["obligation_id"])
            linked += 1
    return linked


def on_l1_changed(engine: Engine, payload: dict, llm=None) -> dict:
    """Consume one ``clhear.l1.changed`` payload (see l1.pipeline)."""
    source_key = payload.get("source") or payload.get("source_key")
    l1_id = payload.get("change_event_id")
    if not source_key:
        return {"ignored": True, "reason": "no source in payload"}
    with engine.connect() as conn:
        if l1_id is not None and _already_processed(conn, int(l1_id)):
            return {"source": source_key, "l1_change_event_id": l1_id, "skipped": "already processed"}
    extraction = run_extraction(engine, source_key=source_key)
    superseded = _link_supersessions(engine, source_key, int(l1_id) if l1_id is not None else None)
    with engine.connect() as conn:
        where = l2_change_events.c.source_key == source_key
        if l1_id is not None:
            where = sa.and_(where, l2_change_events.c.cause_l1_change_event_id == int(l1_id))
        kinds = {
            k: n for k, n in conn.execute(
                sa.select(l2_change_events.c.kind, sa.func.count()).where(where).group_by(l2_change_events.c.kind)
            )
        }
    summary = {
        "source": source_key,
        "l1_change_event_id": l1_id,
        "effective_date": payload.get("effective_date"),
        "obligations": kinds,
        "supersessions": superseded,
        "extraction": {k: extraction.get(k) for k in ("inserted", "re_derived", "stale", "unchanged")},
    }
    _record_run(engine, int(l1_id) if l1_id is not None else None, source_key, summary)
    log.info("L2 change inference: %s", summary)
    return summary


def revoke_expired(engine: Engine, today: date | None = None) -> int:
    """Revocation on expiry: an obligation whose effective_to has passed."""
    today = today or datetime.now(timezone.utc).date()
    n = 0
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(obligations)
            .where(obligations.c.effective_to.isnot(None))
            .where(obligations.c.effective_to < today)
            .where(obligations.c.status.in_(("derived", "validated")))
        ).mappings().all()
        for ob in rows:
            why = registry.why_for(
                ob["id"], clause_id=None, text_hash=ob["text_hash"], method="l2.change.expiry", confidence=1.0,
                summary=f"effective_to {ob['effective_to']} passed: revoked on expiry",
            )
            trail = why.write(conn)
            conn.execute(
                obligations.update().where(obligations.c.id == ob["id"])
                .values(status="stale", why_trail_id=trail, version=(ob["version"] or 1) + 1)
            )
            registry.record_change(
                conn, obligation_id=ob["id"], kind="revoked", cause_clause_ids=[], source_key=ob["source_key"],
                old_text_hash=ob["text_hash"], effective_date=ob["effective_to"], effective_date_basis="expiry",
                detail={"reason": "expiry"}, why=trail,
            )
            n += 1
    return n


def nightly_change_pass(engine: Engine, llm=None, *, window_days: int = 30) -> dict:
    """Rolling window: process L1 change events nobody consumed; revoke expired."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    processed = []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(l1_change_events, sources.c.key.label("source_key"))
            .join(sources, sources.c.id == l1_change_events.c.source_id)
            .where(l1_change_events.c.detected_at >= since)
            .order_by(l1_change_events.c.id)
        ).mappings().all()
        # The registry is re-derived per source, so only the latest L1 change per
        # source can still be pending (earlier ones are subsumed by it).
        latest_per_source = {r["source_key"]: r for r in rows}
        pending = [r for r in latest_per_source.values() if not _already_processed(conn, r["id"])]
    for r in pending:
        payload = {
            "source": r["source_key"],
            "change_event_id": r["id"],
            "clause_ids": r["clause_ids"] if isinstance(r["clause_ids"], list) else json.loads(r["clause_ids"] or "[]"),
            "effective_date": r["effective_date"].isoformat() if r["effective_date"] else None,
            "effective_date_basis": r["effective_date_basis"],
        }
        processed.append(on_l1_changed(engine, payload, llm))
    expired = revoke_expired(engine)
    return {"window_days": window_days, "l1_changes_seen": len(rows), "processed": len(processed), "expired_revoked": expired}


__all__ = [
    "ClauseChange",
    "infer_clause_change",
    "nightly_change_pass",
    "on_l1_changed",
    "refine_with_router",
    "revoke_expired",
]
