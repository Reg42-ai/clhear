# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 member benchmarks (HLD v2 §4.8): opt-in inputs → k ≥ 5 aggregates with DP noise.

Members contribute observations for a small catalogue of metrics (e.g. how many days
a CDD refresh takes) against a cohort key (jurisdiction | sector | size). The
observation is stored under an HMAC of the member identity — the raw identity is
never in ``l8_benchmarks`` — and is *never served*. What members read is a
``benchmark_aggregates`` row: per (cohort, metric), statistics over the latest
observation of each distinct member, published only when the cohort holds at least
``K_MIN`` distinct members, with Laplace noise calibrated to ``epsilon``: values are
clamped to the metric's catalogue bounds and counted into a fixed histogram; every
bucket count takes Laplace(1/ε) noise (sensitivity 1 — each member is in one bucket);
mean and percentiles are post-processing of that noised histogram, reported on the
bucket grid, so no raw value is ever echoed.

:func:`reidentification_test` is the automated gate (``l8_reidentification``): every
current aggregate has n ≥ k, echoes no raw input, and no pair of aggregates on the
same metric differs by fewer than k members (the differencing attack). Aggregates
are written through the record path with a why-trail (I3) and superseded, never
deleted (I2).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import random
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l8.models import DEFAULT_EPSILON, K_MIN, METHOD_VERSION, benchmark_aggregates, benchmark_inputs
from app.clhear.platform import record
from app.clhear.platform.ids import next_id
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.l8.aggregate")

HISTOGRAM_BUCKETS = 10

# metric -> unit, catalogue bounds (the DP sensitivity basis), what it measures.
METRICS: dict[str, dict[str, Any]] = {
    "cdd_refresh_days": {"unit": "days", "bounds": [0, 1825], "label": "Days between periodic CDD refreshes (standard risk)"},
    "sar_decision_days": {"unit": "days", "bounds": [0, 60], "label": "Working days from alert to SAR decision"},
    "tm_alert_rate_pct": {"unit": "%", "bounds": [0, 100], "label": "Transaction-monitoring alerts per 100 transactions"},
    "tm_false_positive_pct": {"unit": "%", "bounds": [0, 100], "label": "Transaction-monitoring alerts closed as false positive"},
    "policy_review_months": {"unit": "months", "bounds": [0, 60], "label": "Months between policy reviews"},
    "training_hours_per_year": {"unit": "hours", "bounds": [0, 200], "label": "AML / conduct training hours per staff member per year"},
    "compliance_fte_per_100": {"unit": "FTE", "bounds": [0, 50], "label": "Compliance FTE per 100 staff"},
    "incident_report_hours": {"unit": "hours", "bounds": [0, 720], "label": "Hours from incident detection to regulator notification"},
    "screening_batch_hours": {"unit": "hours", "bounds": [0, 168], "label": "Hours between sanctions-screening batch runs"},
    "board_reports_per_year": {"unit": "per year", "bounds": [0, 52], "label": "Compliance reports to the board per year"},
}


class InvalidInput(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


# --------------------------------------------------------------------------- identity → HMAC


def member_hash(member_id: str) -> str:
    """HMAC-SHA256 of the member identity under the deployment's benchmark key.

    Only this digest reaches ``benchmark_inputs``; without the key it cannot be
    joined back to ``members``. A missing key falls back to a per-process salt so a
    dev box still works — production sets ``CLHEAR_BENCHMARK_HMAC_KEY``."""
    key = (get_settings().clhear_benchmark_hmac_key or "").encode("utf-8") or _DEV_SALT
    return hmac.new(key, (member_id or "").strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()


_DEV_SALT = hashlib.sha256(f"clhear-dev-{random.SystemRandom().random()}".encode()).digest()


# --------------------------------------------------------------------------- inputs (never served)


def submit_input(engine: Engine, *, member_id: str, cohort_key: str, metric: str, value: float,
                 block_id: str | None = None) -> dict:
    """Store one observation. Returns only what the member may see back (no hash)."""
    if metric not in METRICS:
        raise InvalidInput(f"metric must be one of {sorted(METRICS)}")
    cohort_key = "|".join(p.strip() for p in (cohort_key or "").split("|") if p.strip())
    if not cohort_key:
        raise InvalidInput("cohort_key is required (e.g. 'UK|payments|retail')")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise InvalidInput("value must be numeric") from None
    if not math.isfinite(value):
        raise InvalidInput("value must be finite")
    lo, hi = METRICS[metric]["bounds"]
    if not lo <= value <= hi:
        raise InvalidInput(f"{metric} must lie within {lo}..{hi} {METRICS[metric]['unit']}")
    with engine.begin() as conn:
        conn.execute(benchmark_inputs.insert().values(member_hash=member_hash(member_id), cohort_key=cohort_key, metric=metric,
                                                      block_id=block_id, value=value, unit=METRICS[metric]["unit"]))
    return {"accepted": True, "cohort_key": cohort_key, "metric": metric, "unit": METRICS[metric]["unit"], "k_min": K_MIN,
            "note": "Your observation is stored under an HMAC of your identity and only ever leaves L8 inside a k ≥ 5 aggregate."}


def _latest_per_member(conn: Connection) -> dict[tuple[str, str, str | None], dict[str, float]]:
    """(cohort, metric, block) -> {member_hash: latest value}."""
    groups: dict[tuple[str, str, str | None], dict[str, float]] = {}
    rows = conn.execute(sa.select(benchmark_inputs).order_by(benchmark_inputs.c.submitted_at, benchmark_inputs.c.id)).mappings()
    for r in rows:
        groups.setdefault((r["cohort_key"], r["metric"], r["block_id"]), {})[r["member_hash"]] = float(r["value"])
    return groups


# --------------------------------------------------------------------------- differential privacy


def laplace(scale: float, rng: random.Random) -> float:
    u = rng.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


def _noised_statistics(values: list[float], bounds: list[float], epsilon: float, rng: random.Random) -> tuple[dict, dict]:
    """One ε-DP release: a Laplace-noised histogram over the catalogue bounds.

    Each member sits in exactly one bucket, so the count vector has L1 sensitivity 1
    and every bucket takes Laplace(1/ε) (parallel composition). Mean and percentiles
    are post-processing of the noised histogram — free under DP — and are reported
    on the bucket grid, so no raw value is ever echoed."""
    lo, hi = float(bounds[0]), float(bounds[1])
    clamped = [min(max(v, lo), hi) for v in values]
    width = (hi - lo) / HISTOGRAM_BUCKETS if hi > lo else 1.0
    counts = [0.0] * HISTOGRAM_BUCKETS
    for v in clamped:
        idx = min(int((v - lo) / width), HISTOGRAM_BUCKETS - 1) if hi > lo else 0
        counts[idx] += 1
    scale = 1.0 / epsilon
    noised = [max(0.0, c + laplace(scale, rng)) for c in counts]
    total = sum(noised) or 1.0
    mids = [lo + (i + 0.5) * width for i in range(HISTOGRAM_BUCKETS)]

    def percentile(p: float) -> float:
        acc = 0.0
        for i, c in enumerate(noised):
            acc += c
            if acc / total >= p:
                return round(mids[i], 4)
        return round(mids[-1], 4)

    occupied = [i for i, c in enumerate(noised) if c >= 0.5]
    mean = sum(m * c for m, c in zip(mids, noised)) / total
    stats = {"mean": round(min(max(mean, lo), hi), 4), "p50": percentile(0.5), "p90": percentile(0.9),
             "min_bucket": round(lo + (occupied[0] if occupied else 0) * width, 4),
             "max_bucket": round(lo + ((occupied[-1] if occupied else HISTOGRAM_BUCKETS - 1) + 1) * width, 4),
             "bucket_width": round(width, 4), "histogram": [round(c, 2) for c in noised]}
    noise = {"mechanism": "laplace_histogram", "epsilon": epsilon, "sensitivity": 1, "scale": round(scale, 6),
             "buckets": HISTOGRAM_BUCKETS, "bounds": [lo, hi], "post_processing": ["mean", "p50", "p90", "min_bucket", "max_bucket"]}
    return stats, noise


# --------------------------------------------------------------------------- aggregation (the only export from L8)


def aggregate(engine: Engine, *, k: int = K_MIN, epsilon: float = DEFAULT_EPSILON, release: str = "",
              seed: int | None = None) -> dict:
    """Publish one aggregate per (cohort, metric, block) that holds ≥ k distinct members.

    Cohorts below k are suppressed entirely (not even n is published), and so is any
    cohort whose member set differs from an already-published cohort on the same
    metric by fewer than k members (subtracting the two would isolate them — the
    differencing attack). Already-published cohorts keep priority; among new ones the
    larger cohort wins. A group already published with the same member set and
    values is left alone; otherwise the previous row is superseded and a new version
    written (I2)."""
    if k < K_MIN:
        raise InvalidInput(f"k may not be lowered below {K_MIN}")
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    published, superseded, suppressed = [], 0, []
    with engine.begin() as conn:
        groups = _latest_per_member(conn)
        current = {(r["cohort_key"], r["metric"], r["block_id"]): dict(r) for r in conn.execute(
            sa.select(benchmark_aggregates).where(benchmark_aggregates.c.valid_to.is_(None),
                                                   benchmark_aggregates.c.status == "current")).mappings()}
        # member sets already on the record per metric — new cohorts must not differ from them by 0 < d < k
        exposed: dict[str, list[set[str]]] = {}
        for key in current:
            if key in groups:
                exposed.setdefault(key[1], []).append(set(groups[key]))
        order = sorted(groups.items(), key=lambda kv: (kv[0] not in current, -len(kv[1]), kv[0][0], kv[0][1], kv[0][2] or ""))
        for key, per_member in order:
            cohort_key, metric, block_id = key
            n = len(per_member)
            if n < k:
                suppressed.append({"cohort_key": cohort_key, "metric": metric, "reason": f"n < {k}"})
                continue
            members_set = set(per_member)
            clash = next((s for s in exposed.get(metric, []) if 0 < len(s ^ members_set) < k), None) if key not in current else None
            if clash is not None:
                suppressed.append({"cohort_key": cohort_key, "metric": metric,
                                   "reason": f"differencing risk: differs from a published cohort by {len(clash ^ members_set)} < {k} members"})
                continue
            exposed.setdefault(metric, []).append(members_set)
            spec = METRICS.get(metric, {"unit": "", "bounds": [min(per_member.values()), max(per_member.values())]})
            membership = hashlib.sha256("\n".join(sorted(f"{h}:{v}" for h, v in per_member.items())).encode()).hexdigest()
            prev = current.get(key)
            if prev and (prev.get("noise") or {}).get("membership") == membership:
                continue
            stats, noise = _noised_statistics(list(per_member.values()), spec["bounds"], epsilon, rng)
            noise["membership"] = membership  # digest over hashed members + values: change detection, not identity
            row = {"id": prev["id"] if prev else next_id(conn, "BMA"), "cohort_key": cohort_key, "metric": metric, "block_id": block_id,
                   "n": n, "k_threshold": k, "epsilon": epsilon, "statistics": stats, "noise": noise, "unit": spec["unit"],
                   "release": release, "status": "current"}
            why = record.WhyTrail(
                layer="L8", subject_ref=f"{cohort_key}/{metric}",
                reasoning_summary=f"benchmark aggregate over {n} distinct members (k={k}); Laplace(1/ε), ε={epsilon}, on a "
                                  f"{HISTOGRAM_BUCKETS}-bucket histogram over {spec['bounds']}; mean and percentiles are post-processing",
                evidence_refs=[{"cohort_key": cohort_key, "metric": metric, "n": n, "membership_digest": membership[:16]}],
                inputs=(f"benchmark_inputs:{cohort_key}:{metric}",), agent_id="l8.aggregate", skill_version=METHOD_VERSION,
                confidence=1.0, input_layers=(),  # member observations are opt-in inputs, not a derived layer
            )
            if prev:
                from app.clhear.l8.fills import reversion

                reversion(conn, benchmark_aggregates, prev, row, why, reason="cohort membership or values changed; re-aggregated")
                superseded += 1
            else:
                record.write(conn, benchmark_aggregates, row, why=why)
            published.append(row["id"])
    return {"published": len(published), "superseded": superseded, "suppressed": len(suppressed), "suppressed_detail": suppressed[:20],
            "ids": published[:50], "k": k, "epsilon": epsilon, "method": METHOD_VERSION}


# --------------------------------------------------------------------------- re-identification test (gate)


def reidentification_test(engine: Engine, *, k: int = K_MIN) -> dict:
    """Automated attack surface check over the *current* aggregates:

    1. every aggregate has n ≥ k distinct members (k-anonymity);
    2. no published statistic equals a raw input value of that group (no echo);
    3. differencing: for any two aggregates on the same metric, the symmetric
       difference of their member sets is either empty or ≥ k, so subtracting one
       from the other cannot isolate fewer than k members;
    4. the store holds no identity: every ``member_hash`` is a 64-hex digest and no
       ``members`` email or user_id appears in ``benchmark_inputs``.
    """
    from app.clhear.l8.models import members

    findings: list[dict] = []
    with engine.connect() as conn:
        groups = _latest_per_member(conn)
        aggs = [dict(r) for r in conn.execute(sa.select(benchmark_aggregates).where(
            benchmark_aggregates.c.valid_to.is_(None), benchmark_aggregates.c.status == "current")).mappings()]
        hashes = {r[0] for r in conn.execute(sa.select(benchmark_inputs.c.member_hash).distinct())}
        identities = {r[0].lower() for r in conn.execute(sa.select(members.c.email))} | {str(r[0]) for r in conn.execute(sa.select(members.c.user_id))}
    for a in aggs:
        key = (a["cohort_key"], a["metric"], a["block_id"])
        per_member = groups.get(key, {})
        if a["n"] < k or len(per_member) < k:
            findings.append({"id": a["id"], "check": "k_anonymity", "detail": f"n={a['n']}, members={len(per_member)} < k={k}"})
        stats = a["statistics"] if isinstance(a["statistics"], dict) else {}
        raw = {round(v, 4) for v in per_member.values()}
        for name in ("mean", "p50", "p90"):
            if name in stats and round(float(stats[name]), 4) in raw and len(raw) > 1:
                findings.append({"id": a["id"], "check": "raw_echo", "detail": f"{name}={stats[name]} equals a raw input"})
    by_metric: dict[str, list[tuple[dict, set[str]]]] = {}
    for a in aggs:
        by_metric.setdefault(a["metric"], []).append((a, set(groups.get((a["cohort_key"], a["metric"], a["block_id"]), {}))))
    for metric, items in by_metric.items():
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (a, sa_), (b, sb) = items[i], items[j]
                diff = len(sa_ ^ sb)
                if 0 < diff < k:
                    findings.append({"id": a["id"], "check": "differencing", "detail": f"{a['cohort_key']} vs {b['cohort_key']} on {metric} "
                                                                                        f"differ by {diff} member(s) < k={k}"})
    for h in hashes:
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h) or h in identities:
            findings.append({"id": None, "check": "identity_in_store", "detail": "member_hash is not an HMAC digest"})
            break
    return {"aggregates": len(aggs), "k": k, "findings": findings[:50], "passed": not findings,
            "checks": ["k_anonymity", "raw_echo", "differencing", "identity_in_store"]}


# --------------------------------------------------------------------------- reads (members only — the router enforces)


def list_aggregates(engine: Engine, *, cohort_key: str | None = None, metric: str | None = None, block_id: str | None = None,
                    limit: int = 200) -> list[dict]:
    q = sa.select(benchmark_aggregates).where(benchmark_aggregates.c.valid_to.is_(None), benchmark_aggregates.c.status == "current")
    if cohort_key:
        q = q.where(benchmark_aggregates.c.cohort_key == cohort_key)
    if metric:
        q = q.where(benchmark_aggregates.c.metric == metric)
    if block_id:
        q = q.where(benchmark_aggregates.c.block_id == block_id)
    with engine.connect() as conn:
        out = []
        for r in conn.execute(q.order_by(benchmark_aggregates.c.metric, benchmark_aggregates.c.cohort_key).limit(limit)).mappings():
            row = _plain(r)
            noise = row.get("noise") or {}
            row["noise"] = {k: v for k, v in noise.items() if k != "membership"}
            row["label"] = METRICS.get(row["metric"], {}).get("label", row["metric"])
            out.append(row)
    return out


def get_aggregate(engine: Engine, agg_id: str) -> dict | None:
    with engine.connect() as conn:
        r = conn.execute(sa.select(benchmark_aggregates).where(benchmark_aggregates.c.id == agg_id,
                                                                benchmark_aggregates.c.valid_to.is_(None))).mappings().first()
        if r is None:
            return None
        row = _plain(r)
        row["noise"] = {k: v for k, v in (row.get("noise") or {}).items() if k != "membership"}
        why = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == r["why_trail_id"])).mappings().first()
        row["why"] = _plain(why) if why else None
        row["versions"] = conn.execute(sa.select(sa.func.count()).select_from(benchmark_aggregates)
                                       .where(benchmark_aggregates.c.id == agg_id)).scalar()
    return row


def summary(engine: Engine) -> dict:
    """Public counts (I9): how many cohorts publish, for which metrics — never a statistic."""
    with engine.connect() as conn:
        aggs = conn.execute(sa.select(benchmark_aggregates.c.metric, benchmark_aggregates.c.cohort_key).where(
            benchmark_aggregates.c.valid_to.is_(None), benchmark_aggregates.c.status == "current")).all()
        groups = _latest_per_member(conn)
    per_metric: dict[str, int] = {}
    for metric, _ in aggs:
        per_metric[metric] = per_metric.get(metric, 0) + 1
    return {"aggregates": len(aggs), "cohorts": len({c for _, c in aggs}), "metrics": [
        {"metric": m, "label": spec["label"], "unit": spec["unit"], "published_cohorts": per_metric.get(m, 0),
         "cohorts_collecting": sum(1 for (c, mm, _b) in groups if mm == m)} for m, spec in METRICS.items()],
        "k_min": K_MIN, "epsilon": DEFAULT_EPSILON, "method": METHOD_VERSION,
        "note": "Benchmark statistics are member content (HLD v2 I9); which metrics and how many cohorts publish is public."}
