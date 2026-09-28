# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 explainers — per-item "why" with a mechanical rubric (HLD v2 §4.6).

Every blueprint item carries an explanation built from the evidence chain
itself: the block, the obligations it satisfies, why those obligations apply
to this profile (L4 predicates / L5 activities) and whether the item is
required by an obligation or selected as the leanest cover. The router may
rewrite an explanation for readability, but the rewrite is accepted only if
it still passes the rubric and cites nothing outside the blueprint.
"""
from __future__ import annotations

import logging
import re

from app.clhear.l6.models import EXPLANATION_RUBRIC
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l6.explain")

_ID_RE = re.compile(r"\b(?:OBL|BLK|ACT|PRF|ITM|BLU|CON)[:-][A-Za-z0-9_./#():-]+")
_TRAIL = ".,;:)]}"


def _strip_trail(match: str) -> str:
    # A closing parenthesis is part of the id when it closes one the id opened,
    # as in OBL:usc/15/ftc-act-45#sec45(a); otherwise it is prose punctuation.
    while match and match[-1] in _TRAIL:
        if match[-1] == ")" and match.count("(") >= match.count(")"):
            break
        match = match[:-1]
    return match


def cited_ids(text: str) -> set[str]:
    return {_strip_trail(m) for m in _ID_RE.findall(text or "")}


def allowed_ids(blueprint: dict) -> set[str]:
    ids: set[str] = set()
    for c in blueprint.get("coverage") or []:
        ids.add(c["obligation_id"])
        if c.get("stable_id"):
            ids.add(c["stable_id"])
        ids.update(c.get("covered_by") or [])
        ids.update(a for a in c.get("triggered_by") or [] if not a.startswith("L4:"))
    for b in blueprint.get("blocks") or []:
        ids.add(b["id"])
    for it in blueprint.get("items") or []:
        ids.add(it["block_id"])
        ids.update(a["activity_id"] if isinstance(a, dict) else a for a in it.get("activities_operated") or [])
    ids.update(blueprint.get("activities_evaluated") or [])
    if blueprint.get("profile_id"):
        ids.add(blueprint["profile_id"])
    if blueprint.get("blueprint_id"):
        ids.add(blueprint["blueprint_id"])
    return ids


def _facts(conditions: list[dict], attributes: dict) -> list[str]:
    """The profile answers that made the obligations apply, in the texts' own words."""
    out: list[str] = []
    for cond in conditions:
        cond = cond or {}
        found = []
        if "jurisdictions" in cond:
            found.append(f"operates in {cond['jurisdictions']}")
        if "roles" in cond:
            found.append("is " + " or ".join(f"'{r}'" for r in cond["roles"]))
        if "condition" in cond:
            found.append(f"'{cond.get('fact')}' is {'true' if cond.get('expect', True) else 'false'}")
        out.extend(f for f in found if f not in out)
    return out


def explain_item(item: dict, by_oid: dict[str, dict], attributes: dict, blocks_by_id: dict[str, dict],
                 activities_by_id: dict[str, dict]) -> str:
    """Deterministic explanation from the evidence chain (always passes the rubric)."""
    bid = item["block_id"]
    satisfied = item["obligations_satisfied"]
    head = f"{item['name']} ({item['kind']} {bid})"
    if item["basis"] == "required":
        role = f"is required by {_join(item['required_by'])}"
    elif item["load_bearing_for"]:
        role = f"is selected as the leanest cover for {_join(item['load_bearing_for'])}"
    else:
        role = "is selected but redundant — every obligation it satisfies is also satisfied by another item"
    sentences = [f"{head} {role}."]
    if satisfied:
        sentences.append(f"It satisfies {len(satisfied)} applicable obligation(s): {_join(satisfied)}.")
    conditions = [c for oid in satisfied for c in by_oid[oid].get("conditions", [])]
    facts = _facts(conditions, attributes)
    activities = [a for a in item.get("triggered_by", []) if not a.startswith("L4:")]
    via = []
    if facts:
        via.append("the profile's answers: it " + "; ".join(facts[:4]))
    if activities:
        names = [f"{activities_by_id.get(a, {}).get('name', a)} ({a})" for a in activities[:4]]
        via.append("activities " + ", ".join(names))
    if any(a.startswith("L4:") for oid in satisfied for a in by_oid[oid]["triggered_by"]):
        via.append("L4 applicability predicates on the profile")
    if via:
        sentences.append("These obligations apply to this profile through " + " and ".join(via) + ".")
    elif satisfied:
        sentences.append("These obligations apply to this profile through the activities that anchor them.")
    quoted = [by_oid[oid] for oid in satisfied if (by_oid[oid].get("evidence") or {}).get("quote")]
    if quoted:
        first = quoted[0]
        sentences.append(f"The text ({first['source_key']} {first['clause_ref']}): \"{first['evidence']['quote'][:300]}\"")
    backed = [c for c in item.get("characteristics", []) if c["status"] == "backed" and c.get("in_profile")]
    if backed:
        shown = "; ".join(f"{c['key']} = {c['value']} (from {c['backing_obligation_id']})" for c in backed[:4])
        sentences.append(f"Characteristics resolved for this profile: {shown}.")
    operated = item.get("activities_operated") or []
    if operated:
        sentences.append("Operated by " + ", ".join(f"{o['name']} ({o['activity_id']})" for o in operated[:3]) + ".")
    return " ".join(sentences)


def _join(ids: list[str]) -> str:
    ids = list(ids)
    if len(ids) <= 5:
        return ", ".join(ids)
    return ", ".join(ids[:5]) + f" and {len(ids) - 5} more"


def rubric(explanation: str, item: dict, blueprint: dict, attributes: dict | None = None) -> dict:
    """Mechanical rubric: each check is a boolean; ``score`` is the share passed."""
    text = explanation or ""
    attributes = attributes if attributes is not None else blueprint.get("profile_attributes", {}) or {}
    cited = cited_ids(text)
    lowered = text.lower()
    values: list[str] = []
    for v in attributes.values():
        values.extend(str(x).lower() for x in (v if isinstance(v, list) else [v]) if x not in (None, "", False))
    trigger_ids = [a for a in item.get("triggered_by", []) if not a.startswith("L4:")]
    checks = {
        "names_block": item["block_id"] in cited,
        "cites_obligation": any(o in cited for o in item.get("obligations_satisfied", [])),
        "states_trigger": any(a in cited for a in trigger_ids) or any(v and v in lowered for v in values)
                          or "applicability predicate" in lowered or "anchor them" in lowered,
        "states_role": "required by" in lowered or "selected" in lowered,
        "no_outside_ids": cited <= allowed_ids(blueprint),
    }
    passed = sum(1 for v in checks.values() if v)
    return {"checks": checks, "score": passed / len(EXPLANATION_RUBRIC), "passed": passed == len(EXPLANATION_RUBRIC)}


def score_blueprint(blueprint: dict) -> dict:
    """Rubric over every item explanation of one composition."""
    items = blueprint.get("items") or []
    results = [rubric(it.get("explanation", ""), it, blueprint) for it in items]
    return {
        "items": len(items),
        "passed": sum(1 for r in results if r["passed"]),
        "score": (sum(r["score"] for r in results) / len(results)) if results else None,
        "failing": [{"block_id": it["block_id"], "checks": r["checks"]} for it, r in zip(items, results) if not r["passed"]],
    }


def refine_explanations(engine, llm, blueprint: dict, *, limit: int = 5) -> dict:
    """Router rewrite of item explanations for readability; kept only when the
    rewrite still passes the rubric (closed-world: no new ids, no new facts)."""
    accepted = rejected = 0
    for it in (blueprint.get("items") or [])[:limit]:
        prompt = (
            "Rewrite this compliance-program item explanation for a compliance officer in 2-4 plain sentences. "
            "Keep EVERY id verbatim (block, obligations, activities); add no id, number or fact that is not in the input. "
            'JSON: {"explanation": ""}\n\n' + it.get("explanation", "")
        )
        try:
            result = complete(llm, "l6.rationale", prompt=prompt, system="Citing narrator only. JSON only. Never invent ids.",
                              required_keys=["explanation"], max_tokens=400)
            text = str(parse_json_object(result.text).get("explanation") or "").strip()
        except Exception:
            log.exception("L6 explanation refinement failed for %s", it["block_id"])
            rejected += 1
            continue
        if text and rubric(text, it, blueprint)["passed"]:
            it["explanation"] = text
            it["explanation_model"] = getattr(result, "model", "")
            accepted += 1
        else:
            rejected += 1
    try:
        from app.clhear import ai_ops

        ai_ops.record(engine, kind="fleet_generation", layer="L6", fleet="l6.explain",
                      reasoning=f"Explainer: {accepted} item explanations refined, {rejected} rejected by the rubric",
                      detail={"accepted": accepted, "rejected": rejected, "blueprint": blueprint.get("blueprint_id")})
    except Exception:
        pass
    return {"accepted": accepted, "rejected": rejected}
