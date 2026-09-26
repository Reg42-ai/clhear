# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Conformance program (HLD v2 §6 institutions, standard §7; item 15).

Annex E self-assessments for CL1 Mapped / CL2 Traceable, assessor-reported marks for
CL3 Assessed / CL4 Automated, the public register of marks and the accredited
assessor register. The criteria are the published ``export/clhear/conformance/
criteria.json`` — this module reads that file so the web form, the API and the
public repo can never disagree on what a level requires.

What the program stores (Annex E §E.6): the form as submitted — references to
evidence, never artefacts or client data (I5) — the automated checks, the
verifier's decision and the mark. Nothing is deleted; a declined assessment or a
withdrawn mark stays visible with its reason (I2). Marks are granted only through
this module (trademark policy: "a conformance mark outside the program" is on the
never-list).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from app.clhear.platform.ids import next_id

CONFORMANCE_SCHEMA = "conformance"
CONFORMANCE_DIR = Path(__file__).resolve().parents[2] / "export" / "clhear" / "conformance"
CRITERIA_FILE = CONFORMANCE_DIR / "criteria.json"

metadata = sa.MetaData()
Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")

LEVELS = ("CL1", "CL2", "CL3", "CL4")
SELF_ASSESSED = ("CL1", "CL2")
ASSESSOR_LEVELS = ("CL3", "CL4")
ASSESSMENT_STATUSES = ("submitted", "granted", "declined", "changes_requested", "withdrawn")
MARK_STATUSES = ("granted", "withdrawn", "expired")
ITEM_STATUSES = ("operated", "out_of_scope")
VALIDITY_MONTHS = 12
RECONCILIATION_MAX_DAYS = 30
# an out-of-scope reason must be a fact about the organisation, never a judgement about the obligation
IMPORTANCE_WORDS = re.compile(r"\b(not material|immaterial|low risk|not relevant|irrelevant|unimportant|not a priority|too costly|no need)\b", re.I)


class InvalidSubmission(ValueError):
    pass


class NotPermitted(PermissionError):
    pass


self_assessments = sa.Table(
    "self_assessments",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CFA-000001
    sa.Column("organization", sa.Text, nullable=False),
    sa.Column("program", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("release", sa.Text, nullable=False),
    sa.Column("blueprint_id", sa.Text, nullable=False, index=True),
    sa.Column("blueprint_fingerprint", sa.Text, nullable=False, default=""),
    sa.Column("level_claimed", sa.Text, nullable=False),
    sa.Column("level_supported", sa.Text, nullable=True),  # from the automated checks
    sa.Column("level_granted", sa.Text, nullable=True),
    sa.Column("submitted_by", sa.Text, nullable=False, default=""),  # email
    sa.Column("contact_email", sa.Text, nullable=False, default=""),
    sa.Column("form", Json, nullable=False, default=dict),  # references only — never artefacts
    sa.Column("checks", Json, nullable=False, default=list),  # [{criterion, check, ok, detail}]
    sa.Column("status", sa.Text, sa.CheckConstraint(f"status in {ASSESSMENT_STATUSES}", name="sa_status_check"), nullable=False, default="submitted"),
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("decision_note", sa.Text, nullable=False, default=""),
    sa.Column("mark_id", sa.Text, nullable=True),
    sa.Column("criteria_version", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=CONFORMANCE_SCHEMA,
)

marks = sa.Table(
    "marks",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CFM-000001 — the register id
    sa.Column("level", sa.Text, sa.CheckConstraint(f"level in {LEVELS}", name="marks_level_check"), nullable=False),
    sa.Column("organization", sa.Text, nullable=False, index=True),
    sa.Column("program", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("release", sa.Text, nullable=False),
    sa.Column("blueprint_id", sa.Text, nullable=False),
    sa.Column("assessment_id", sa.Text, nullable=True),  # CFA- for CL1/CL2
    sa.Column("assessor_id", BigId, nullable=True),  # register entry for CL3/CL4
    sa.Column("report_ref", sa.Text, nullable=False, default=""),  # assessor report reference (not the report)
    sa.Column("period_start", sa.Date, nullable=True),
    sa.Column("period_end", sa.Date, nullable=True),
    sa.Column("granted_by", sa.Text, nullable=False),
    sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_from", sa.Date, nullable=False),
    sa.Column("valid_to", sa.Date, nullable=False),
    sa.Column("status", sa.Text, sa.CheckConstraint(f"status in {MARK_STATUSES}", name="marks_status_check"), nullable=False, default="granted"),
    sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("withdrawn_by", sa.Text, nullable=True),
    sa.Column("withdrawal_reason", sa.Text, nullable=False, default=""),
    schema=CONFORMANCE_SCHEMA,
)

assessors = sa.Table(
    "assessors",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("firm", sa.Text, nullable=False, default=""),
    sa.Column("email", sa.Text, nullable=False, index=True),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column("licence_ref", sa.Text, nullable=False, default=""),  # ISAE 3000 licensing body reference
    sa.Column("briefing_completed", sa.Date, nullable=True),
    sa.Column("conflicts_declared", sa.Boolean, nullable=False, default=False),
    sa.Column("accredited_by", sa.Text, nullable=False),
    sa.Column("accredited_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_to", sa.Date, nullable=False),
    sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("withdrawal_reason", sa.Text, nullable=False, default=""),
    schema=CONFORMANCE_SCHEMA,
)

CONFORMANCE_TABLES = (self_assessments, marks, assessors)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    for k in ("form", "checks"):
        if k in out and isinstance(out[k], str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    return out


# --------------------------------------------------------------------------- criteria (the published file)


def criteria() -> dict:
    return json.loads(CRITERIA_FILE.read_text(encoding="utf-8"))


def level_index(level: str) -> int:
    if level not in LEVELS:
        raise InvalidSubmission(f"level must be one of {LEVELS}")
    return LEVELS.index(level)


def criteria_for(level: str) -> list[dict]:
    """Every criterion required at ``level`` (its own and all lower levels)."""
    li = level_index(level)
    return [c for c in criteria()["criteria"] if level_index(c["level"]) <= li]


# --------------------------------------------------------------------------- automated checks (Annex E §E.4)


def _blueprint(conn: Connection, blueprint_id: str) -> dict | None:
    from app.clhear.derived_models import blueprint_items, blueprints

    row = conn.execute(sa.select(blueprints).where(blueprints.c.stable_id == blueprint_id)).mappings().first()
    if row is None:
        return None
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
    items = [dict(r) for r in conn.execute(sa.select(blueprint_items.c.id, blueprint_items.c.block_id, blueprint_items.c.kind,
                                                     blueprint_items.c.name, blueprint_items.c.obligations_satisfied)
                                           .where(blueprint_items.c.blueprint_id == blueprint_id)).mappings()]
    for it in items:
        if isinstance(it["obligations_satisfied"], str):
            it["obligations_satisfied"] = json.loads(it["obligations_satisfied"] or "[]")
    return {"status": row["status"], "release": row["release"], "fingerprint": row["fingerprint"],
            "coverage": result.get("coverage_summary") or {}, "items": items}


def _release_known(release: str) -> bool:
    if not release:
        return False
    if release in ("latest", "clhear-vLATEST") or re.match(r"^clhear-v\d{4}\.\d{2}\.\d{2}", release) or re.match(r"^\d{8}T\d{6}Z$", release):
        return True
    from app.clhear.releases import is_release_id

    return bool(is_release_id(release))


def run_checks(conn: Connection, form: dict) -> tuple[list[dict], str | None, dict | None]:
    """Evaluate every criterion with an automated check. Returns (checks, level_supported, blueprint)."""
    bp = _blueprint(conn, form.get("blueprint_id") or "")
    items = {i.get("item_id"): i for i in form.get("items") or [] if isinstance(i, dict)}
    operated = [i for i in items.values() if i.get("status") == "operated"]
    excluded = [i for i in items.values() if i.get("status") == "out_of_scope"]
    bp_items = {it["id"]: it for it in (bp or {}).get("items", [])}
    scope = (form.get("scope") or "").strip()
    ct = form.get("change_tracking") or {}
    review = form.get("last_review") or {}
    signatory = form.get("signatory") or {}

    def review_recent() -> bool:
        try:
            d = date.fromisoformat(str(review.get("date"))[:10])
        except (TypeError, ValueError):
            return False
        return (date.today() - d).days <= 366 and bool(review.get("reviewer_role")) and bool(review.get("outcome"))

    missing_items = sorted(set(bp_items) - set(items)) if bp else []
    unknown_status = [i["item_id"] for i in items.values() if i.get("status") not in ITEM_STATUSES]
    no_owner = [i["item_id"] for i in operated if not (i.get("owner") or "").strip()]
    bad_reason = [i["item_id"] for i in excluded if not (i.get("reason") or "").strip() or IMPORTANCE_WORDS.search(i.get("reason") or "")]
    no_evidence = [i["item_id"] for i in operated if not [e for e in (i.get("evidence") or []) if (e or {}).get("ref")]]
    untraced = []
    for i in operated:
        need = set((bp_items.get(i["item_id"]) or {}).get("obligations_satisfied") or [])
        have = {o for e in (i.get("evidence") or []) for o in ((e or {}).get("obligation_ids") or [])}
        if need and not need <= have:
            untraced.append({"item_id": i["item_id"], "missing": sorted(need - have)[:5]})
    bad_dev = [i["item_id"] for i in items.values() for d in (i.get("deviations") or []) if not (d or {}).get("rationale")]
    coverage = (bp or {}).get("coverage", {})
    complete = bool(bp) and coverage.get("total", 0) > 0 and coverage.get("covered") == coverage.get("total")

    results: dict[str, tuple[bool, str]] = {
        "release_named": (_release_known(form.get("release") or ""), f"release {form.get('release') or '—'}"),
        "blueprint_current": (bool(bp) and bp["status"] == "current", "unknown blueprint" if not bp else f"blueprint {bp['status']}"),
        "scope_stated": (len(scope) >= 40 and bool(re.search(r"\b[A-Z]{2}\b|jurisdiction", scope)), f"{len(scope)} chars"),
        "blueprint_complete": (complete, f"{coverage.get('covered', 0)}/{coverage.get('total', 0)} obligations covered" if bp else "no blueprint"),
        "items_mapped": (bool(bp) and not missing_items and not unknown_status,
                         f"{len(items)}/{len(bp_items)} items mapped" + (f"; missing {missing_items[:5]}" if missing_items else "")
                         + (f"; unknown status {unknown_status[:5]}" if unknown_status else "")),
        "owners_named": (not no_owner, f"{len(operated) - len(no_owner)}/{len(operated)} operated items have an owner"
                         + (f"; missing {no_owner[:5]}" if no_owner else "")),
        "exclusions_reasoned": (not bad_reason, f"{len(excluded)} out of scope" + (f"; unjustified {bad_reason[:5]}" if bad_reason else "")),
        "evidence_per_item": (bool(operated) and not no_evidence, f"{len(operated) - len(no_evidence)}/{len(operated)} operated items evidenced"
                              + (f"; none for {no_evidence[:5]}" if no_evidence else "")),
        "evidence_traces": (bool(operated) and not untraced, "every operated item's evidence names its obligations" if not untraced
                            else f"{len(untraced)} item(s) untraced: {untraced[:3]}"),
        "deviations_reasoned": (not bad_dev, f"{sum(len(i.get('deviations') or []) for i in items.values())} deviation(s)"
                                + (f"; no rationale on {bad_dev[:5]}" if bad_dev else "")),
        "change_tracking": (bool(ct.get("watchlist_id") or ct.get("feed") or ct.get("sdk")) and bool(ct.get("last_reconciled_release")),
                            f"last reconciled {ct.get('last_reconciled_release') or '—'}"),
        "reconciliation_window": (isinstance(form.get("reconciliation_days"), int) and 0 <= form["reconciliation_days"] <= RECONCILIATION_MAX_DAYS,
                                  f"{form.get('reconciliation_days', '—')} days (max {RECONCILIATION_MAX_DAYS})"),
        "review_recorded": (review_recent(), f"last review {review.get('date') or '—'}"),
        "signed": (all((signatory.get(k) or "").strip() for k in ("name", "role", "date")), f"{signatory.get('role') or '—'}, {signatory.get('date') or '—'}"),
    }
    checks = []
    for c in criteria()["criteria"]:
        name = c.get("automated_check")
        if not name:
            continue
        ok, detail = results[name]
        checks.append({"criterion": c["id"], "level": c["level"], "title": c["title"], "check": name, "ok": bool(ok), "detail": detail})
    supported = None
    for level in SELF_ASSESSED:
        if all(ch["ok"] for ch in checks if level_index(ch["level"]) <= level_index(level)):
            supported = level
        else:
            break
    return checks, supported, bp


# --------------------------------------------------------------------------- submissions


def _validate_form(form: dict) -> None:
    for k in ("organization", "program", "scope", "release", "blueprint_id", "level_claimed", "items", "signatory"):
        if not form.get(k):
            raise InvalidSubmission(f"{k} is required")
    if form["level_claimed"] not in SELF_ASSESSED:
        raise InvalidSubmission(f"a self-assessment claims {SELF_ASSESSED}; CL3 and CL4 come from an assessor's report")
    if form.get("profile_confirmed") is not True:
        raise InvalidSubmission("profile_confirmed must be true — the signatory confirms the profile predicates describe the organisation")
    if not re.match(r"^BLU-\d{6}$", str(form["blueprint_id"])):
        raise InvalidSubmission("blueprint_id must be a BLU- id")
    if not isinstance(form["items"], list) or not form["items"]:
        raise InvalidSubmission("items must be a non-empty list")
    for i in form["items"]:
        if not isinstance(i, dict) or not re.match(r"^ITM-\d{6}$", str(i.get("item_id") or "")):
            raise InvalidSubmission("every item needs an ITM- item_id")
        if i.get("status") not in ITEM_STATUSES:
            raise InvalidSubmission(f"item {i.get('item_id')}: status must be one of {ITEM_STATUSES}")
        for e in i.get("evidence") or []:
            ref = str((e or {}).get("ref") or "")
            if len(ref) > 500 or "\n" in ref:
                raise InvalidSubmission(f"item {i['item_id']}: evidence.ref is a reference (id, URL, hash), not content")


def submit(engine: Engine, form: dict, *, submitted_by: str) -> dict:
    """Store the form, run the automated checks, report the level the evidence supports."""
    _validate_form(form)
    with engine.begin() as conn:
        checks, supported, bp = run_checks(conn, form)
        row = {
            "id": next_id(conn, "CFA"), "organization": form["organization"][:200], "program": form["program"][:200], "scope": form["scope"][:4000],
            "release": str(form["release"]), "blueprint_id": form["blueprint_id"], "blueprint_fingerprint": (bp or {}).get("fingerprint") or "",
            "level_claimed": form["level_claimed"], "level_supported": supported, "submitted_by": (submitted_by or "").lower(),
            "contact_email": (form.get("contact_email") or submitted_by or "").lower(), "form": form, "checks": checks,
            "status": "submitted", "criteria_version": criteria()["version"],
        }
        conn.execute(self_assessments.insert().values(**row))
    return get_assessment(engine, row["id"])


def get_assessment(engine: Engine, assessment_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(self_assessments).where(self_assessments.c.id == assessment_id)).mappings().first()
    if row is None:
        return None
    out = _plain(row)
    out["checks_failed"] = [c for c in out["checks"] if not c["ok"]]
    return out


def list_assessments(engine: Engine, *, submitted_by: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    q = sa.select(self_assessments).order_by(self_assessments.c.created_at.desc(), self_assessments.c.id.desc()).limit(limit)
    if submitted_by:
        q = q.where(self_assessments.c.submitted_by == submitted_by.lower())
    if status:
        q = q.where(self_assessments.c.status == status)
    with engine.connect() as conn:
        rows = [_plain(r) for r in conn.execute(q).mappings()]
    for r in rows:
        r.pop("form", None)  # the list is a summary; the form is on the detail
    return rows


def _verifier_ok(conn: Connection, email: str) -> bool:
    from app.clhear.platform.contributions import GRANTING_ROLES, roles_for

    return bool(roles_for(conn, email) & GRANTING_ROLES)


def decide(engine: Engine, assessment_id: str, *, decided_by: str, decision: str, note: str = "",
           level: str | None = None) -> dict:
    """Program verifier grants (at the level supported, or lower), declines, or requests changes (Annex E §E.5)."""
    if decision not in ("grant", "decline", "request_changes"):
        raise InvalidSubmission("decision must be grant | decline | request_changes")
    with engine.begin() as conn:
        if not _verifier_ok(conn, decided_by):
            raise NotPermitted(f"{decided_by} is not a program verifier (maintainer or steering role)")
        row = conn.execute(sa.select(self_assessments).where(self_assessments.c.id == assessment_id)).mappings().first()
        if row is None:
            raise KeyError(assessment_id)
        if row["status"] in ("granted", "withdrawn"):
            raise InvalidSubmission(f"{assessment_id} is already {row['status']}")
        values: dict[str, Any] = {"decided_by": decided_by.lower(), "decided_at": _now(), "decision_note": note[:2000]}
        if decision == "grant":
            supported = row["level_supported"]
            if supported is None:
                raise InvalidSubmission("the automated checks support no level; fix the submission first")
            level = level or supported
            if level not in SELF_ASSESSED or level_index(level) > level_index(supported):
                raise InvalidSubmission(f"may grant at most {supported} (the level the checks support)")
            mark_id = _grant(conn, level=level, organization=row["organization"], program=row["program"], scope=row["scope"],
                             release=row["release"], blueprint_id=row["blueprint_id"], assessment_id=assessment_id, granted_by=decided_by)
            values.update(status="granted", level_granted=level, mark_id=mark_id)
        else:
            values["status"] = "declined" if decision == "decline" else "changes_requested"
        conn.execute(self_assessments.update().where(self_assessments.c.id == assessment_id).values(**values))
    return get_assessment(engine, assessment_id)


# --------------------------------------------------------------------------- marks (the register)


def _grant(conn: Connection, *, level: str, organization: str, program: str, scope: str, release: str, blueprint_id: str,
           granted_by: str, assessment_id: str | None = None, assessor_id: int | None = None, report_ref: str = "",
           period_start: date | None = None, period_end: date | None = None) -> str:
    today = date.today()
    mid = next_id(conn, "CFM")
    conn.execute(marks.insert().values(
        id=mid, level=level, organization=organization, program=program, scope=scope, release=release, blueprint_id=blueprint_id,
        assessment_id=assessment_id, assessor_id=assessor_id, report_ref=report_ref[:500], period_start=period_start, period_end=period_end,
        granted_by=granted_by.lower(), valid_from=today, valid_to=today + timedelta(days=int(VALIDITY_MONTHS * 30.44)), status="granted"))
    return mid


def record_assessed_mark(engine: Engine, *, level: str, organization: str, program: str, scope: str, release: str, blueprint_id: str,
                         assessor_id: int, report_ref: str, period_start: date, period_end: date, recorded_by: str) -> dict:
    """CL3 / CL4: the program records an accredited assessor's signed ISAE 3000 report as a mark."""
    if level not in ASSESSOR_LEVELS:
        raise InvalidSubmission(f"assessor-reported marks are {ASSESSOR_LEVELS}; CL1/CL2 come from a self-assessment")
    if not report_ref.strip():
        raise InvalidSubmission("report_ref is required (the report itself is not lodged here)")
    if (period_end - period_start).days < 180:
        raise InvalidSubmission("the assessed period must cover at least six months (E.7.2)")
    with engine.begin() as conn:
        if not _verifier_ok(conn, recorded_by):
            raise NotPermitted(f"{recorded_by} may not record marks")
        a = conn.execute(sa.select(assessors).where(assessors.c.id == assessor_id)).mappings().first()
        if a is None or a["withdrawn_at"] is not None or a["valid_to"] < period_end:
            raise InvalidSubmission("assessor is not on the accredited register for the assessed period (E.7.1)")
        if not scope.strip() or not re.match(r"^BLU-\d{6}$", blueprint_id):
            raise InvalidSubmission("scope and a BLU- blueprint id are required")
        mid = _grant(conn, level=level, organization=organization, program=program, scope=scope, release=release, blueprint_id=blueprint_id,
                     granted_by=recorded_by, assessor_id=assessor_id, report_ref=report_ref, period_start=period_start, period_end=period_end)
    return get_mark(engine, mid)


def withdraw_mark(engine: Engine, mark_id: str, *, withdrawn_by: str, reason: str) -> dict:
    if not reason.strip():
        raise InvalidSubmission("a withdrawal needs a reason (marks policy)")
    with engine.begin() as conn:
        if not _verifier_ok(conn, withdrawn_by):
            raise NotPermitted(f"{withdrawn_by} may not withdraw marks")
        row = conn.execute(sa.select(marks).where(marks.c.id == mark_id)).mappings().first()
        if row is None:
            raise KeyError(mark_id)
        if row["status"] != "granted":
            raise InvalidSubmission(f"{mark_id} is already {row['status']}")
        conn.execute(marks.update().where(marks.c.id == mark_id).values(status="withdrawn", withdrawn_at=_now(), withdrawn_by=withdrawn_by.lower(),
                                                                          withdrawal_reason=reason[:2000]))
        if row["assessment_id"]:
            conn.execute(self_assessments.update().where(self_assessments.c.id == row["assessment_id"]).values(status="withdrawn"))
    return get_mark(engine, mark_id)


def _mark_view(row) -> dict:
    out = _plain(row)
    if out["status"] == "granted" and date.fromisoformat(out["valid_to"]) < date.today():
        out["status"] = "expired"  # computed, never mutated: the grant row is what it was
    spec = next((lv for lv in criteria()["levels"] if lv["level"] == out["level"]), {})
    out["mark"] = spec.get("mark", out["level"])
    out["statement"] = (f"{out['mark']} — {out['program']}, {out['organization']}, release {out['release']}, valid to {out['valid_to']}. "
                        f"Register: https://clhear.org/conformance/register#{out['id']}")
    return out


def get_mark(engine: Engine, mark_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(marks).where(marks.c.id == mark_id)).mappings().first()
    return _mark_view(row) if row else None


def register(engine: Engine, *, organization: str | None = None, level: str | None = None, include_withdrawn: bool = True) -> list[dict]:
    """The public register — every mark ever granted, withdrawn ones included (I2)."""
    q = sa.select(marks).order_by(marks.c.granted_at.desc(), marks.c.id.desc())
    if organization:
        q = q.where(sa.func.lower(marks.c.organization).like(f"%{organization.lower()}%"))
    if level:
        q = q.where(marks.c.level == level)
    if not include_withdrawn:
        q = q.where(marks.c.status == "granted")
    with engine.connect() as conn:
        return [_mark_view(r) for r in conn.execute(q).mappings()]


# --------------------------------------------------------------------------- accredited assessors


def accredit_assessor(engine: Engine, *, name: str, email: str, firm: str = "", jurisdiction: str = "", licence_ref: str,
                      briefing_completed: date, conflicts_declared: bool, accredited_by: str, months: int = 24) -> dict:
    if not conflicts_declared:
        raise InvalidSubmission("conflicts must be declared before accreditation (CONFLICT_OF_INTEREST.md)")
    if not licence_ref.strip():
        raise InvalidSubmission("an ISAE 3000 licensing reference is required")
    with engine.begin() as conn:
        from app.clhear.platform.contributions import roles_for

        if "steering" not in roles_for(conn, accredited_by) and "maintainer" not in roles_for(conn, accredited_by):
            raise NotPermitted("accreditation is granted by the steering group")
        res = conn.execute(assessors.insert().values(name=name[:200], firm=firm[:200], email=email.lower(), jurisdiction=jurisdiction[:80],
                                                     licence_ref=licence_ref[:200], briefing_completed=briefing_completed,
                                                     conflicts_declared=True, accredited_by=accredited_by.lower(),
                                                     valid_to=date.today() + timedelta(days=int(months * 30.44))))
        aid = res.inserted_primary_key[0]
        row = conn.execute(sa.select(assessors).where(assessors.c.id == aid)).mappings().one()
    return _plain(row)


def withdraw_assessor(engine: Engine, assessor_id: int, *, withdrawn_by: str, reason: str) -> dict:
    with engine.begin() as conn:
        if not _verifier_ok(conn, withdrawn_by):
            raise NotPermitted(f"{withdrawn_by} may not withdraw accreditation")
        conn.execute(assessors.update().where(assessors.c.id == assessor_id).values(withdrawn_at=_now(), withdrawal_reason=reason[:1000]))
        row = conn.execute(sa.select(assessors).where(assessors.c.id == assessor_id)).mappings().first()
    if row is None:
        raise KeyError(assessor_id)
    return _plain(row)


def list_assessors(engine: Engine, *, active_only: bool = False) -> list[dict]:
    q = sa.select(assessors).order_by(assessors.c.accredited_at)
    with engine.connect() as conn:
        rows = [_plain(r) for r in conn.execute(q).mappings()]
    out = []
    for r in rows:
        r["active"] = r["withdrawn_at"] is None and date.fromisoformat(r["valid_to"]) >= date.today()
        r.pop("email", None)  # the public register shows name, firm, jurisdiction — not a contact address
        if active_only and not r["active"]:
            continue
        out.append(r)
    return out


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        by_level = {lv: 0 for lv in LEVELS}
        for level, n in conn.execute(sa.select(marks.c.level, sa.func.count()).where(marks.c.status == "granted").group_by(marks.c.level)):
            by_level[level] = n
        pending = conn.execute(sa.select(sa.func.count()).select_from(self_assessments).where(self_assessments.c.status == "submitted")).scalar()
        total = conn.execute(sa.select(sa.func.count()).select_from(self_assessments)).scalar()
        active_assessors = sum(1 for a in list_assessors(engine, active_only=True))
    return {"marks_granted": by_level, "self_assessments": total, "pending_verification": pending, "accredited_assessors": active_assessors,
            "criteria_version": criteria()["version"], "levels": criteria()["levels"], "validity_months": VALIDITY_MONTHS}
