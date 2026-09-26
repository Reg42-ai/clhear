# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L7 calibrated scorer (HLD v2 §4.7): dimensions → composite → band, per
obligation and per blueprint item, with the likelihood fitted and scored on a
held-out year.

Everything the score is made of is on the record: the dimensions, the published
:data:`~app.clhear.l7.models.WEIGHTS` the composite used, the calibration run the
likelihood came from and the evidence (event ids, counts, amounts, blocks,
activities, change events). Scores supersede rather than overwrite (I2); an
unchanged input set leaves the current score untouched.

Dimensions (each scaled 0..1 against the corpus maximum so composites compare
across jurisdictions):

* ``enforcement_history`` — recency-weighted count of linked outcomes,
  half-life :data:`HALF_LIFE_YEARS`.
* ``financial_impact`` — log-scaled total and largest penalty.
* ``reputational_impact`` — share of linked outcomes naming individuals or
  imposing prohibition / censure / suspension.
* ``operational_impact`` — L3 blocks the obligation requires + L5 activities
  operating for it.
* ``regulatory_attention`` — L2 change events on the obligation's source in
  the last :data:`ATTENTION_YEARS` years + the regulator's enforcement volume.
* ``likelihood`` — ``sigmoid(a + b*history + c*attention)``; ``(a, b, c)`` are
  fitted by grid search on the years before the held-out year
  (:func:`calibrate`) and the Brier score on the held-out year is published.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timezone
from itertools import product

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import blueprint_items, blueprints, l2_change_events, obligations, operates, requires
from app.clhear.l7.enforcement import LINKER_VERSION
from app.clhear.l7.models import (
    DIMENSIONS, METHOD_VERSION, WEIGHTS, band_for, enforcement_events, enforcement_links, risk_calibrations, risk_scores,
)
from app.clhear.platform import events as l0_events
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l7.score")

SCORER_VERSION = "l7-score-v1"
HALF_LIFE_YEARS = 3.0
ATTENTION_YEARS = 2
DEFAULT_PARAMETERS = {"a": -2.5, "b": 3.0, "c": 1.0}  # used until a calibration run exists
_GRID_A = [-4.0, -3.5, -3.0, -2.5, -2.0, -1.5, -1.0, -0.5, 0.0]
_GRID_B = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
_GRID_C = [0.0, 0.5, 1.0, 2.0, 3.0]
LIVE_OBLIGATION = ("derived", "validated")


def _sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _json(v, default):
    if v is None:
        return default
    return v if isinstance(v, (list, dict)) else json.loads(v)


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


# --------------------------------------------------------------------------- inputs


def _live_obligations(conn: Connection) -> dict[str, dict]:
    from app.clhear.l1.scopes import keys as scope_keys

    rows = conn.execute(sa.select(obligations.c.id, obligations.c.stable_id, obligations.c.source_key, obligations.c.clause_ref,
                                  obligations.c.title, obligations.c.jurisdiction, obligations.c.regulator)
                        .where(obligations.c.status.in_(LIVE_OBLIGATION))).mappings()
    chosen = scope_keys()
    return {r["id"]: dict(r) for r in rows if chosen is None or r["source_key"] in chosen}


def _linked_events(conn: Connection) -> dict[str, list[dict]]:
    """obligation id → live events linked to it (each once), any date."""
    q = (sa.select(enforcement_links.c.obligation_id, enforcement_events)
         .join(enforcement_events, sa.and_(enforcement_events.c.id == enforcement_links.c.event_id,
                                           enforcement_events.c.valid_to.is_(None)))
         .where(enforcement_links.c.valid_to.is_(None)))
    out: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    for r in conn.execute(q).mappings():
        key = (r["obligation_id"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        ev = dict(r)
        ev["decided_on"] = _as_date(ev.get("decided_on"))
        ev["amount"] = float(ev["amount"]) if ev.get("amount") is not None else None
        out.setdefault(r["obligation_id"], []).append(ev)
    return out


def _operational_counts(conn: Connection, obs: dict[str, dict]) -> dict[str, tuple[int, int]]:
    """obligation id → (blocks required, activities operating for it)."""
    blocks: dict[str, set[str]] = {}
    rq = sa.select(requires.c.obligation_id, requires.c.block_id)
    if "valid_to" in requires.c:
        rq = rq.where(requires.c.valid_to.is_(None))
    for r in conn.execute(rq):
        blocks.setdefault(r.obligation_id, set()).add(r.block_id)
    by_ref = {}
    for oid, ob in obs.items():
        by_ref[oid] = oid
        if ob.get("stable_id"):
            by_ref[ob["stable_id"]] = oid
    acts: dict[str, set[str]] = {}
    q = sa.select(operates.c.activity_id, operates.c.obligation_refs)
    if "valid_to" in operates.c:
        q = q.where(operates.c.valid_to.is_(None))
    for r in conn.execute(q):
        for ref in _json(r.obligation_refs, []):
            oid = by_ref.get(ref)
            if oid:
                acts.setdefault(oid, set()).add(r.activity_id)
    return {oid: (len(blocks.get(oid, ())), len(acts.get(oid, ()))) for oid in obs}


def _change_counts(conn: Connection, since: date, until: date) -> dict[str, int]:
    """source_key → L2 change events in the window."""
    out: dict[str, int] = {}
    for r in conn.execute(sa.select(l2_change_events.c.source_key, l2_change_events.c.effective_date,
                                    l2_change_events.c.detected_at)):
        day = _as_date(r.effective_date) or _as_date(r.detected_at)
        if day is None or day < since or day > until:
            continue
        out[r.source_key] = out.get(r.source_key, 0) + 1
    return out


def _regulator_volume(events_by_ob: dict[str, list[dict]], since: date, until: date) -> dict[str, int]:
    seen: set[str] = set()
    out: dict[str, int] = {}
    for evs in events_by_ob.values():
        for ev in evs:
            if ev["id"] in seen or not ev["decided_on"] or ev["decided_on"] < since or ev["decided_on"] > until:
                continue
            seen.add(ev["id"])
            out[ev["regulator"]] = out.get(ev["regulator"], 0) + 1
    return out


# --------------------------------------------------------------------------- raw features


def _history(events: list[dict], as_of: date) -> float:
    total = 0.0
    for ev in events:
        if not ev["decided_on"] or ev["decided_on"] > as_of:
            continue
        age = max(0.0, (as_of - ev["decided_on"]).days / 365.25)
        total += 0.5 ** (age / HALF_LIFE_YEARS)
    return total


def _financial(events: list[dict], as_of: date) -> tuple[float, float, float]:
    amounts = [ev["amount"] for ev in events if ev["amount"] and ev["decided_on"] and ev["decided_on"] <= as_of]
    total = sum(amounts)
    biggest = max(amounts) if amounts else 0.0
    raw = 0.6 * math.log10(1 + total) + 0.4 * math.log10(1 + biggest)
    return raw, total, biggest


def _reputational(events: list[dict], as_of: date) -> float:
    past = [ev for ev in events if ev["decided_on"] and ev["decided_on"] <= as_of]
    if not past:
        return 0.0
    hits = sum(1 for ev in past if ev["respondent_type"] == "individual" or ev["kind"] in ("prohibition", "censure", "suspension"))
    return hits / len(past)


def _scale(values: dict[str, float]) -> dict[str, float]:
    top = max(values.values(), default=0.0)
    if top <= 0:
        return {k: 0.0 for k in values}
    return {k: round(min(1.0, v / top), 4) for k, v in values.items()}


def _features(conn: Connection, obs: dict[str, dict], events_by_ob: dict[str, list[dict]], as_of: date,
              *, operational: dict[str, tuple[int, int]] | None = None) -> dict[str, dict]:
    """Per obligation: scaled dimensions (except likelihood) + the raw evidence, as of a date."""
    operational = operational if operational is not None else _operational_counts(conn, obs)
    window_from = date(as_of.year - ATTENTION_YEARS, as_of.month, min(as_of.day, 28))
    changes = _change_counts(conn, window_from, as_of)
    volume = _regulator_volume(events_by_ob, window_from, as_of)
    raw: dict[str, dict[str, float]] = {d: {} for d in DIMENSIONS}
    evidence: dict[str, dict] = {}
    for oid, ob in obs.items():
        evs = events_by_ob.get(oid, [])
        past = [ev for ev in evs if ev["decided_on"] and ev["decided_on"] <= as_of]
        raw["enforcement_history"][oid] = _history(evs, as_of)
        fin, total, biggest = _financial(evs, as_of)
        raw["financial_impact"][oid] = fin
        raw["reputational_impact"][oid] = _reputational(evs, as_of)
        n_blocks, n_acts = operational.get(oid, (0, 0))
        raw["operational_impact"][oid] = n_blocks + 0.5 * n_acts
        regulators = {ev["regulator"] for ev in past} or {ob.get("regulator") or ""}
        raw["regulatory_attention"][oid] = changes.get(ob["source_key"], 0) + 0.5 * sum(volume.get(r, 0) for r in regulators)
        evidence[oid] = {
            "events": sorted(ev["id"] for ev in past), "event_count": len(past),
            "total_amount": round(total, 2), "largest_amount": round(biggest, 2),
            "blocks_required": n_blocks, "activities_operating": n_acts,
            "change_events": changes.get(ob["source_key"], 0),
            "regulator_volume": sum(volume.get(r, 0) for r in regulators),
            "as_of": as_of.isoformat(),
        }
    scaled = {d: _scale(vals) for d, vals in raw.items() if d != "likelihood"}
    out: dict[str, dict] = {}
    for oid in obs:
        dims = {d: scaled[d][oid] for d in DIMENSIONS if d != "likelihood"}
        out[oid] = {"dimensions": dims, "evidence": evidence[oid]}
    return out


def _likelihood(dims: dict[str, float], params: dict) -> float:
    return round(_sigmoid(params["a"] + params["b"] * dims["enforcement_history"] + params["c"] * dims["regulatory_attention"]), 4)


def composite_of(dims: dict[str, float], weights: dict[str, float] = WEIGHTS) -> float:
    return round(sum(weights[d] * float(dims.get(d, 0.0)) for d in weights), 4)


# --------------------------------------------------------------------------- calibration


def latest_calibration(conn: Connection, *, method_version: str = METHOD_VERSION, published_only: bool = True) -> dict | None:
    q = sa.select(risk_calibrations).where(risk_calibrations.c.method_version == method_version)
    if published_only:
        q = q.where(risk_calibrations.c.published.is_(True))
    row = conn.execute(q.order_by(risk_calibrations.c.ran_at.desc(), risk_calibrations.c.id.desc())).mappings().first()
    if row is None:
        return None
    out = dict(row)
    for k in ("training_years", "parameters", "reliability"):
        out[k] = _json(out.get(k), [] if k != "parameters" else {})
    for k in ("brier", "baseline_brier"):
        if out.get(k) is not None:
            out[k] = float(out[k])
    if isinstance(out.get("ran_at"), datetime):
        out["ran_at"] = out["ran_at"].isoformat()
    return out


def _brier(rows: list[tuple[float, int]]) -> float | None:
    if not rows:
        return None
    return round(sum((p - y) ** 2 for p, y in rows) / len(rows), 5)


def _reliability(rows: list[tuple[float, int]], bins: int = 5) -> list[dict]:
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        mine = [(p, y) for p, y in rows if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if mine:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(mine),
                        "predicted": round(sum(p for p, _ in mine) / len(mine), 4),
                        "observed": round(sum(y for _, y in mine) / len(mine), 4)})
    return out


def _year_rows(conn: Connection, obs: dict, events_by_ob: dict, year: int, operational: dict) -> list[tuple[str, dict, int]]:
    """(obligation id, dims as of 31 Dec year-1, 1 if a linked outcome landed in `year`)."""
    as_of = date(year - 1, 12, 31)
    feats = _features(conn, obs, events_by_ob, as_of, operational=operational)
    out = []
    for oid in obs:
        label = int(any(ev["decided_on"] and ev["decided_on"].year == year for ev in events_by_ob.get(oid, [])))
        out.append((oid, feats[oid]["dimensions"], label))
    return out


def calibrate(engine: Engine, *, held_out_year: int | None = None, publish: bool = True) -> dict:
    """Fit (a, b, c) on the years before ``held_out_year`` and score that year (Brier).

    ``held_out_year`` defaults to the latest year with a linked outcome. With no
    earlier year to train on the default parameters are scored as-is (and the run
    is recorded as such). The run is appended to ``risk_calibrations``; the newest
    published run is the ``calibration_set_ref`` every subsequent score carries.
    """
    with engine.begin() as conn:
        obs = _live_obligations(conn)
        events_by_ob = _linked_events(conn)
        years = sorted({ev["decided_on"].year for evs in events_by_ob.values() for ev in evs if ev["decided_on"]})
        if not obs or not years:
            return {"status": "skipped", "reason": "no linked enforcement outcomes to calibrate on", "years": years}
        year = held_out_year or years[-1]
        training_years = [y for y in range(years[0] + 1, year) if y > years[0]]
        operational = _operational_counts(conn, obs)
        train: list[tuple[dict, int]] = []
        for y in training_years:
            train.extend((dims, label) for _, dims, label in _year_rows(conn, obs, events_by_ob, y, operational))
        held = _year_rows(conn, obs, events_by_ob, year, operational)
        params = dict(DEFAULT_PARAMETERS)
        fitted = False
        if train and any(label for _, label in train):
            best = None
            for a, b, c in product(_GRID_A, _GRID_B, _GRID_C):
                cand = {"a": a, "b": b, "c": c}
                score = _brier([(_likelihood(d, cand), y) for d, y in train])
                if best is None or score < best[0] or (score == best[0] and (b, c) < (best[1]["b"], best[1]["c"])):
                    best = (score, cand)
            params = best[1]
            fitted = True
        rows = [(_likelihood(dims, params), label) for _, dims, label in held]
        base_rate = (sum(y for _, y in train) / len(train)) if train else (sum(y for _, y in rows) / len(rows) if rows else 0.0)
        brier = _brier(rows)
        baseline = _brier([(base_rate, y) for _, y in rows])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        cal_id = f"CAL:{METHOD_VERSION}:{year}:{stamp}"
        conn.execute(risk_calibrations.insert().values(
            id=cal_id, method_version=METHOD_VERSION, held_out_year=year, training_years=training_years,
            n=len(rows), positives=sum(y for _, y in rows), brier=brier, baseline_brier=baseline,
            parameters={**params, "fitted": fitted, "training_rows": len(train), "base_rate": round(base_rate, 4),
                        "scorer_version": SCORER_VERSION, "linker_version": LINKER_VERSION},
            reliability=_reliability(rows), published=publish, ran_at=datetime.now(timezone.utc)))
        l0_events.emit(conn, layer="L7", kind="clhear.l7.calibrated", subject_ref=cal_id,
                       payload={"held_out_year": year, "brier": brier, "baseline_brier": baseline, "n": len(rows)},
                       producer="fleet:l7.score")
    return {"status": "calibrated", "id": cal_id, "held_out_year": year, "training_years": training_years, "n": len(rows),
            "positives": sum(y for _, y in rows), "brier": brier, "baseline_brier": baseline,
            "beats_baseline": brier is not None and baseline is not None and brier <= baseline,
            "parameters": params, "fitted": fitted, "published": publish}


# --------------------------------------------------------------------------- scoring


def _current_scores(conn: Connection, kind: str) -> dict[str, dict]:
    rows = conn.execute(sa.select(risk_scores).where(risk_scores.c.subject_kind == kind, risk_scores.c.status == "current",
                                                     risk_scores.c.valid_to.is_(None))).mappings()
    return {r["subject_ref"]: dict(r) for r in rows}


def _write_score(conn: Connection, *, kind: str, subject_ref: str, blueprint_id: str | None, dims: dict, evidence: dict,
                 cal_ref: str, previous: dict | None, summary: str, input_layers: tuple, stats: dict) -> str | None:
    composite = composite_of(dims)
    ih = record.inputs_hash(json.dumps(dims, sort_keys=True), json.dumps(WEIGHTS, sort_keys=True), METHOD_VERSION, cal_ref,
                            json.dumps(evidence.get("events") or evidence.get("obligations") or [], sort_keys=True))
    if previous is not None and previous["inputs_hash"] == ih:
        stats["unchanged"] += 1
        return None
    why = record.WhyTrail(layer="L7", subject_ref=subject_ref, reasoning_summary=summary,
                          evidence_refs=[{"layer": "L7", "events": evidence.get("events", [])[:50]},
                                         {"layer": "L7", "calibration": cal_ref}],
                          inputs=(ih,), agent_id="fleet:l7.score", skill_version=SCORER_VERSION,
                          confidence=round(0.6 + 0.35 * dims.get("likelihood", 0.0), 3) if evidence.get("event_count") or evidence.get("obligations") else 0.55,
                          input_layers=input_layers)
    trail = why.write(conn)
    if previous is not None:
        record.invalidate(conn, risk_scores, risk_scores.c.id == previous["id"], why=trail, reason="re-scored")
        conn.execute(risk_scores.update().where(risk_scores.c.id == previous["id"]).values(status="superseded"))
        stats["superseded"] += 1
    rid = next_id(conn, "RSK")
    record.write(conn, risk_scores, {
        "id": rid, "subject_kind": kind, "subject_ref": subject_ref, "blueprint_id": blueprint_id,
        "dimensions": dims, "weights": dict(WEIGHTS), "method_version": METHOD_VERSION, "composite": composite,
        "band": band_for(composite), "calibration_set_ref": cal_ref, "evidence": evidence, "status": "current",
        "inputs_hash": ih, "derived_by": "fleet:l7.score", "confidence": why.confidence,
    }, why=trail, valid_from=date.today())
    stats["written"] += 1
    return rid


def score_obligations(engine: Engine, *, as_of: date | None = None, source_keys=None) -> dict:
    """Score every live obligation, or those of ``source_keys``; supersede scores whose inputs moved."""
    stats = {"obligations": 0, "written": 0, "superseded": 0, "unchanged": 0, "with_events": 0, "calibration": ""}
    as_of = as_of or date.today()
    with engine.begin() as conn:
        obs = _live_obligations(conn)
        if source_keys is not None:
            obs = {oid: ob for oid, ob in obs.items() if ob["source_key"] in set(source_keys)}
        stats["obligations"] = len(obs)
        if not obs:
            return stats
        cal = latest_calibration(conn)
        params = {k: cal["parameters"][k] for k in ("a", "b", "c")} if cal else dict(DEFAULT_PARAMETERS)
        cal_ref = cal["id"] if cal else "uncalibrated:defaults"
        stats["calibration"] = cal_ref
        events_by_ob = _linked_events(conn)
        feats = _features(conn, obs, events_by_ob, as_of)
        previous = _current_scores(conn, "obligation")
        bands: dict[str, int] = {}
        for oid, ob in obs.items():
            dims = dict(feats[oid]["dimensions"])
            dims["likelihood"] = _likelihood(dims, params)
            evidence = feats[oid]["evidence"]
            if evidence["event_count"]:
                stats["with_events"] += 1
            composite = composite_of(dims)
            summary = (f"{ob['source_key']} {ob['clause_ref']}: {evidence['event_count']} linked outcome(s), "
                       f"total {evidence['total_amount']:,.0f}; composite {composite} ({band_for(composite)}) under {METHOD_VERSION}")
            _write_score(conn, kind="obligation", subject_ref=oid, blueprint_id=None, dims=dims, evidence=evidence, cal_ref=cal_ref,
                         previous=previous.get(oid), summary=summary, input_layers=("L1", "L2", "L3", "L5"), stats=stats)
            bands[band_for(composite)] = bands.get(band_for(composite), 0) + 1
        stats["bands"] = bands
        if stats["written"]:
            l0_events.emit(conn, layer="L7", kind="clhear.l7.scored", subject_ref="obligations",
                           payload={k: v for k, v in stats.items() if k != "bands"}, producer="fleet:l7.score")
    return stats


def score_items(engine: Engine, *, blueprint_id: str | None = None) -> dict:
    """Priority per blueprint item: the item takes the strongest dimension values of
    the obligations it satisfies (its priority is that of its riskiest obligation);
    the composite is recomputed with the published weights."""
    stats = {"blueprints": 0, "items": 0, "written": 0, "superseded": 0, "unchanged": 0, "calibration": ""}
    with engine.begin() as conn:
        ob_scores = _current_scores(conn, "obligation")
        if not ob_scores:
            return stats
        cal_ref = next(iter(ob_scores.values()))["calibration_set_ref"]
        stats["calibration"] = cal_ref
        obs = _live_obligations(conn)
        by_ref = {oid: oid for oid in obs}
        by_ref.update({ob["stable_id"]: oid for oid, ob in obs.items() if ob.get("stable_id")})
        q = sa.select(blueprints.c.stable_id).where(blueprints.c.status == "current", blueprints.c.stable_id.isnot(None))
        if blueprint_id:
            q = q.where(blueprints.c.stable_id == blueprint_id)
        current_bps = [r[0] for r in conn.execute(q)]
        stats["blueprints"] = len(current_bps)
        if not current_bps:
            return stats
        previous = _current_scores(conn, "item")
        items = conn.execute(sa.select(blueprint_items).where(blueprint_items.c.blueprint_id.in_(current_bps))).mappings().all()
        from app.clhear.l1.scopes import keys as scope_keys

        chosen = scope_keys()
        for it in items:
            refs = list(_json(it["obligations_satisfied"], []))
            # An item that names an obligation outside the scope keeps its score.
            if chosen is not None and any(r not in by_ref for r in refs):
                continue
            stats["items"] += 1
            oids = [by_ref[r] for r in refs if r in by_ref]
            scored = [ob_scores[o] for o in oids if o in ob_scores]
            dims = {d: 0.0 for d in DIMENSIONS}
            for s in scored:
                sd = _json(s["dimensions"], {})
                for d in DIMENSIONS:
                    dims[d] = max(dims[d], float(sd.get(d, 0.0)))
            dims = {d: round(v, 4) for d, v in dims.items()}
            evidence = {"obligations": sorted(s["subject_ref"] for s in scored),
                        "obligation_scores": {s["subject_ref"]: float(s["composite"]) for s in scored},
                        "events": sorted({e for s in scored for e in _json(s["evidence"], {}).get("events", [])}),
                        "event_count": len({e for s in scored for e in _json(s["evidence"], {}).get("events", [])}),
                        "block_id": it["block_id"], "basis": it["basis"]}
            composite = composite_of(dims)
            summary = (f"{it['id']} ({it['name']}) in {it['blueprint_id']}: max over {len(scored)} obligation score(s); "
                       f"composite {composite} ({band_for(composite)})")
            _write_score(conn, kind="item", subject_ref=it["id"], blueprint_id=it["blueprint_id"], dims=dims, evidence=evidence,
                         cal_ref=cal_ref, previous=previous.get(it["id"]), summary=summary, input_layers=("L2", "L6"), stats=stats)
    return stats


# --------------------------------------------------------------------------- reads


def _plain(row: dict) -> dict:
    out = dict(row)
    for k in ("dimensions", "weights", "evidence", "review", "jurisdictions", "model_manifest"):
        if k in out and isinstance(out[k], str):
            out[k] = json.loads(out[k])
    for k in ("composite", "confidence"):
        if out.get(k) is not None:
            out[k] = float(out[k])
    for k, v in list(out.items()):
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
    return out


def resolve_obligation_ref(conn: Connection, ref: str) -> str | None:
    """Accept the derivation key (OBL:src#ref), the stable id (OBL-000001) or 'src#ref'."""
    if not ref:
        return None
    row = conn.execute(sa.select(obligations.c.id).where(sa.or_(obligations.c.id == ref, obligations.c.stable_id == ref,
                                                                  obligations.c.id == f"OBL:{ref}"))).first()
    return row[0] if row else None


def list_scores(conn: Connection, *, kind: str | None = None, subject_ref: str | None = None, band: str | None = None,
                blueprint_id: str | None = None, min_composite: float | None = None, limit: int = 200,
                include_history: bool = False) -> list[dict]:
    q = sa.select(risk_scores)
    if not include_history:
        q = q.where(risk_scores.c.status == "current", risk_scores.c.valid_to.is_(None))
    if kind:
        q = q.where(risk_scores.c.subject_kind == kind)
    if subject_ref:
        q = q.where(risk_scores.c.subject_ref == subject_ref)
    if band:
        q = q.where(risk_scores.c.band == band)
    if blueprint_id:
        q = q.where(risk_scores.c.blueprint_id == blueprint_id)
    if min_composite is not None:
        q = q.where(risk_scores.c.composite >= min_composite)
    q = q.order_by(risk_scores.c.composite.desc(), risk_scores.c.id.desc()).limit(limit)
    return [_plain(r) for r in conn.execute(q).mappings()]


def get_score(conn: Connection, score_id: str) -> dict | None:
    row = conn.execute(sa.select(risk_scores).where(risk_scores.c.id == score_id)).mappings().first()
    return _plain(row) if row else None


def scorecard(conn: Connection) -> dict:
    """Counts by band and kind, the latest calibration, the method version — the /l7 headline."""
    by: dict[str, dict[str, int]] = {}
    for r in conn.execute(sa.select(risk_scores.c.subject_kind, risk_scores.c.band, sa.func.count())
                          .where(risk_scores.c.status == "current", risk_scores.c.valid_to.is_(None))
                          .group_by(risk_scores.c.subject_kind, risk_scores.c.band)):
        by.setdefault(r[0], {})[r[1]] = int(r[2])
    events = conn.execute(sa.select(sa.func.count()).select_from(enforcement_events)
                          .where(enforcement_events.c.valid_to.is_(None))).scalar_one()
    links = conn.execute(sa.select(sa.func.count()).select_from(enforcement_links)
                         .where(enforcement_links.c.valid_to.is_(None))).scalar_one()
    return {"method_version": METHOD_VERSION, "scorer_version": SCORER_VERSION, "bands": by,
            "events": int(events), "links": int(links), "calibration": latest_calibration(conn)}


__all__ = ["DEFAULT_PARAMETERS", "HALF_LIFE_YEARS", "SCORER_VERSION", "calibrate", "composite_of", "get_score",
           "latest_calibration", "list_scores", "resolve_obligation_ref", "score_items", "score_obligations", "scorecard"]
