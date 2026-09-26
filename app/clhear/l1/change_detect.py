# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Change detectors with effective-date extraction (HLD v2 §4.1).

Deterministic first: commencement / application phrases in the changed
clauses ("comes into force on 1 January 2027", "shall apply from 3 July
2026", "effective March 15, 2026", "with effect from ..."). When several
dates are found the earliest future-looking commencement wins; if nothing is
found the publisher's ``effective_date``/``as_of_date`` is used and the basis
is recorded so consumers know how sure we are.

An optional router hook (task class ``l1.change``) may refine a date when the
deterministic pass finds none; it is *never* allowed to invent a date the
text does not contain — the answer must quote a substring of the clause.
"""
import re
from dataclasses import dataclass
from datetime import date

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"],
        start=1,
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})

_TRIGGER = (
    r"(?:comes? into (?:force|operation|effect)|enter(?:s|ed)? into force|"
    r"shall apply|applies|apply|take(?:s)? effect|effective|with effect|"
    r"commence(?:s|ment)?|in force|applicable)"
)
_DMY = r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m>[A-Za-z]{3,9})\.?,?\s+(?P<y>\d{4})"
_MDY = r"(?P<m2>[A-Za-z]{3,9})\.?\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{4})"
_ISO = r"(?P<y3>\d{4})-(?P<m3>\d{2})-(?P<d3>\d{2})"
_DATE = rf"(?:{_DMY}|{_MDY}|{_ISO})"

_PATTERN = re.compile(
    rf"{_TRIGGER}\W{{0,3}}(?:on|from|as of|as from|after|of|:)?\W{{0,3}}(?:the\s+)?{_DATE}",
    re.I,
)
_ISO_ONLY = re.compile(_ISO)
_ANY_DATE = re.compile(_DATE, re.I)
# Cheap pre-check before spending an `l1.change` call: the model may only quote
# a date the text contains, so text with no year-like token cannot yield one.
_DATE_HINT = re.compile(rf"\b(?:19|20)\d{{2}}\b|\b(?:{'|'.join(_MONTHS)})\b", re.I)


@dataclass(frozen=True)
class EffectiveDate:
    value: date | None
    basis: str  # text | publisher | none
    evidence: str = ""


def _to_date(match: re.Match) -> date | None:
    g = match.groupdict()
    try:
        if g.get("y3"):
            return date(int(g["y3"]), int(g["m3"]), int(g["d3"]))
        if g.get("y"):
            month = _MONTHS.get(g["m"].lower())
            return date(int(g["y"]), month, int(g["d"])) if month else None
        if g.get("y2"):
            month = _MONTHS.get(g["m2"].lower())
            return date(int(g["y2"]), month, int(g["d2"])) if month else None
    except ValueError:
        return None
    return None


def extract_effective_dates(text: str) -> list[tuple[date, str]]:
    """All (date, evidence) pairs found in commencement phrases, in text order."""
    out: list[tuple[date, str]] = []
    for match in _PATTERN.finditer(text or ""):
        parsed = _to_date(match)
        if parsed is not None:
            out.append((parsed, match.group(0)))
    return out


def effective_date_for(
    changed_texts: list[str],
    *,
    publisher_effective: date | None = None,
    publisher_as_of: date | None = None,
    detected_on: date | None = None,
) -> EffectiveDate:
    """Pick the effective date for a change event.

    Preference: the earliest commencement date in the changed text that is not
    before the publisher's own date (a clause often recites older dates for
    history), else any text date, else publisher effective/as-of date.
    """
    found: list[tuple[date, str]] = []
    for text in changed_texts:
        found.extend(extract_effective_dates(text))
    floor = publisher_as_of or publisher_effective
    if found:
        forward = [f for f in found if floor is None or f[0] >= floor]
        pick = min(forward or found, key=lambda f: f[0])
        return EffectiveDate(pick[0], "text", pick[1])
    if publisher_effective is not None:
        return EffectiveDate(publisher_effective, "publisher", "source_versions.effective_date")
    if publisher_as_of is not None:
        return EffectiveDate(publisher_as_of, "publisher", "source_versions.as_of_date")
    return EffectiveDate(detected_on, "none", "")


def refine_with_router(router, source_key: str, changed_texts: list[str], current: EffectiveDate) -> EffectiveDate:
    """Optional `l1.change` escalation: only when the deterministic pass found
    nothing; the model must quote a date string present in the text."""
    if current.basis == "text" or router is None or not changed_texts:
        return current
    if not any(_DATE_HINT.search(t) for t in changed_texts):
        return current  # nothing quotable: do not spend a model call
    excerpt = "\n---\n".join(t[:1500] for t in changed_texts[:6])
    prompt = (
        "You are reading amended legal text. Quote, verbatim, the single phrase that states when the "
        "amendment comes into force or applies (for example 'comes into force on 1 January 2027'). "
        "If the text contains no such phrase reply exactly NONE.\n\n" + excerpt
    )
    try:
        from app.clhear.platform import router as _router

        result = _router.complete(router, "l1.change", prompt=prompt, max_tokens=120)
    except Exception:
        return current
    answer = (getattr(result, "text", None) or getattr(result, "content", None) or "").strip()
    if not answer or answer.upper().startswith("NONE"):
        return current
    if not any(answer in t for t in changed_texts):
        return current  # hallucinated quote: ignore
    # The quote is verified verbatim against the clause, so any date inside it
    # is a date the text states; the model's job was only to find the phrase
    # our trigger grammar missed.
    dates = extract_effective_dates(answer) or [
        (d, m.group(0)) for m in _ANY_DATE.finditer(answer) if (d := _to_date(m)) is not None
    ]
    if not dates:
        return current
    return EffectiveDate(dates[0][0], "text", dates[0][1])


__all__ = ["EffectiveDate", "effective_date_for", "extract_effective_dates", "refine_with_router"]
