# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Cross-references: where a clause cites another text or another provision.

When L1 stores a version, every clause is read for mentions of other texts and
provisions, and each mention is kept with its quote and offsets into
``clauses.text`` (table ``clause_references``). A build resolves them against the
sources in its scope: a cited text matches a source by its publisher reference
(``sources.instrument``) or its name, and a cited provision matches a clause by
its reference (``sec-3/1``). What does not resolve is reported as the text words
it, so the user can register the missing source.

The grammar is generic legal drafting, the same in every sector:

* numbered provisions: section, article, regulation, rule, part, subpart,
  chapter, schedule, annex ("section 3(1)(a)", "Part 7", "§ 4", "Article II");
* named texts: "the <Title> Act|Regulation(s)|Rule(s)|Directive|Code|Standard",
  with an optional year ("the Harbour Lighting Act 2019");
* numbered texts: "<Designator> (<ABBR>) <number>/<number>";
* both together: "section 3 of the <Title> Act", "<Title> Act, section 3".

A provision's own label at the start of a line, a title line, "this Act" and
"the Act" (defined terms) and the name a text gives itself ("may be cited as")
are not references. No list names a text or a publisher.
"""
from __future__ import annotations

import re
from collections import defaultdict

import sqlalchemy as sa
from sqlalchemy.engine import Connection

# ----------------------------------------------------------------- grammar

# The word a text uses for a numbered provision, and the clause-reference slug
# the document reader gives the same provision (l1.adapters.document._KIND_SLUG).
UNIT_SLUG = {
    "section": "sec", "sections": "sec", "s.": "sec", "ss.": "sec", "sec.": "sec", "§": "sec", "§§": "sec",
    "article": "art", "articles": "art", "art.": "art",
    "regulation": "reg", "regulations": "reg", "reg.": "reg",
    "rule": "rule", "rules": "rule",
    "part": "part", "parts": "part", "subpart": "subpart",
    "chapter": "ch", "chapters": "ch",
    "schedule": "sch", "annex": "annex",
}
# Slugs a clause reference starts with when the clause is a provision or a division.
UNIT_REF = re.compile(r"^(?:sec|art|reg|rule|part|subpart|ch|sch|annex|cl|std|req|ctl|prin|title|book|app|div)-")

_UNIT = (r"(?P<unit>(?i:sections?|ss?\.|sec\.|§§?|articles?|art\.|regulations?|reg\.|rules?|parts?|subpart|"
         r"chapters?|schedule|annex))")
# 3, 3A, 160.103, 3(1)(a), upper-case Roman numerals, a single capital letter.
_NUM = (r"(?:\d+[A-Za-z]?(?:\.\d+[A-Za-z]?)*(?:\s?\((?:\d+[A-Za-z]?|[a-z]{1,4})\))*"
        r"|[IVXLC]{1,7}(?![A-Za-z])|[A-Z](?![A-Za-z]))")
_MORE = r"\d+[A-Za-z]?(?:\.\d+[A-Za-z]?)*(?:\s?\((?:\d+[A-Za-z]?|[a-z]{1,4})\))*"
_UNIT_RE = re.compile(rf"(?<![A-Za-z]){_UNIT}\s*(?P<nums>{_NUM}(?:\s*(?:,|and|or|to|-|–)\s*{_MORE})*)")
_ONE_NUM = re.compile(_NUM)
_SINGULAR = {"sections": "section", "articles": "article", "regulations": "regulation", "rules": "rule",
             "parts": "part", "chapters": "chapter", "§§": "§", "ss.": "s."}

_DESIGNATORS = ("Act", "Regulation", "Regulations", "Rule", "Rules", "Directive", "Code", "Standard")
_TITLE_WORD = r"[A-Z][A-Za-z'’\-]*"
_JOIN = r"(?:of|and|for|on|the|in|to|&)"
_NAMED_RE = re.compile(
    rf"(?P<title>{_TITLE_WORD}(?:\s+(?:{_TITLE_WORD}|{_JOIN}))*?)\s+"
    rf"(?P<designator>{'|'.join(_DESIGNATORS)})\b(?:,?\s+(?:of\s+)?(?P<year>1[89]\d\d|20\d\d)\b)?")
_NUMBERED_RE = re.compile(
    r"\b(?P<designator>Regulation|Directive|Decision|Act|Rule|Law|Order)\s+(?:\((?P<abbr>[A-Z]{1,6})\)\s+)?"
    r"(?:No\.?\s*)?(?P<number>\d{1,4}/\d{1,4})(?:/[A-Z]{1,6})?\b")
# Words that open a sentence or a phrase, never a title: "Under the Code", "Each Standard".
_LEADING = frozenset("""a an the this that these those such its their any each every no all some under where if in by for
from with without subject despite notwithstanding pursuant according as before after see also and or of to on at
upon unless when while within made issued""".split())
_JOIN_WORDS = frozenset({"of", "and", "for", "on", "the", "in", "to", "&"})
_SELF_NAME = re.compile(r"(?:cited as|referred to as|known as|called)\s*[\"'“‘]?\s*$", re.I)
_OF_INSTRUMENT = re.compile(r"^,?\s+(?:of|in|under)\s+(?:the\s+)?$")
_YEAR = re.compile(r"^(?:1[89]\d\d|20\d\d)$")


def _slug(num: str) -> str:
    """"3(1)(a)" -> "3/1/a", "160.103" -> "160.103", "II" -> "ii"."""
    parts = [p for p in re.split(r"[()\s]+", num.strip()) if p]
    return "/".join(re.sub(r"[^a-z0-9.]+", "-", p.lower()).strip("-.") for p in parts)


def _line_start(text: str, at: int) -> bool:
    before = text[:at]
    return not before.strip() or before.rstrip(" \t").endswith("\n")


def _whole_line(text: str, start: int, end: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start: line_end if line_end >= 0 else len(text)]
    return line.strip(" \t.:;—–-") == text[start:end].strip()


def _instruments(text: str) -> list[dict]:
    """Named and numbered texts the clause cites, left to right, without overlaps."""
    found: list[dict] = []
    for m in _NUMBERED_RE.finditer(text):
        found.append({"start": m.start(), "end": m.end(), "instrument": m.group(0), "designator": m.group("designator"),
                      "number": m.group("number"), "year": ""})
    taken = [(f["start"], f["end"]) for f in found]
    for m in _NAMED_RE.finditer(text):
        # Drop the words that open a sentence or phrase; a title starts at its first capitalised content word.
        tokens = [(m.start("title") + t.start(), t.group(0)) for t in re.finditer(r"\S+", m.group("title"))]
        # "Article II of the Lantern Standard": the provision is not part of the title.
        cut = max((i + 2 for i in range(len(tokens) - 1) if tokens[i][1].lower() in UNIT_SLUG
                   and _ONE_NUM.fullmatch(tokens[i + 1][1])), default=0)
        tokens = tokens[cut:]
        while tokens and (tokens[0][1].lower() in _LEADING or tokens[0][1].lower() in _JOIN_WORDS):
            tokens.pop(0)
        if not tokens or tokens[-1][1].lower() in _JOIN_WORDS:
            continue
        start = tokens[0][0]
        # "the" right before the title belongs to the quote: "the Harbour Lighting Act".
        lead = re.search(r"\b[Tt]he\s+$", text[:start])
        quote_start = lead.start() if lead else start
        end = m.end()
        if any(s < end and start < e for s, e in taken):
            continue
        if _whole_line(text, quote_start, end) or _SELF_NAME.search(text[:quote_start]):
            continue
        title = " ".join(t for _, t in tokens)
        found.append({"start": quote_start, "end": end, "instrument": text[quote_start:end],
                      "designator": m.group("designator"), "number": "", "year": m.group("year") or "",
                      "title": title})
        taken.append((quote_start, end))
    return sorted(found, key=lambda f: f["start"])


def extract(text: str) -> list[dict]:
    """Every reference a clause makes, with offsets into ``text``.

    Each mention is ``{start, end, quote, path, path_ref, instrument, designator,
    number, year}``: ``path`` is the provision as written ("section 3(1)"),
    ``path_ref`` the clause reference it points at ("sec-3/1"), ``instrument``
    the cited text as written, when the mention names one."""
    if not text:
        return []
    instruments = _instruments(text)
    used: set[int] = set()
    out: list[dict] = []
    for m in _UNIT_RE.finditer(text):
        if any(i["start"] < m.end() and m.start() < i["end"] for i in instruments):
            continue  # "the Harbour Rules 2019" is a title, not rule 2019
        unit = m.group("unit").lower()
        slug = UNIT_SLUG.get(unit) or UNIT_SLUG.get(unit.rstrip("s"))
        if not slug:
            continue
        # "section 3 of the <Title> Act" / "<Title> Act, section 3": the provision is in that text.
        cited = None
        for i, inst in enumerate(instruments):
            if inst["start"] >= m.end() and _OF_INSTRUMENT.match(text[m.end():inst["start"]]):
                cited = i
                break
            if inst["end"] <= m.start() and re.fullmatch(r",?\s*", text[inst["end"]:m.start()]):
                cited = i
                break
        if cited is None and _line_start(text, m.start()) and re.match(r"[ \t]*(?:[.:—–-]|\n|$)", text[m.end():]):
            continue  # the provision's own label: "Section 4." at the start of a line
        start = m.start()
        end = m.end()
        if cited is not None:
            used.add(cited)
            start, end = min(start, instruments[cited]["start"]), max(end, instruments[cited]["end"])
        label = _SINGULAR.get(m.group("unit").lower(), m.group("unit"))
        for n in _ONE_NUM.finditer(m.group("nums")):
            num = n.group(0)
            if _YEAR.match(num) and cited is None and m.group("unit")[0].isupper():
                continue
            mention = {"start": start, "end": end, "quote": text[start:end], "path": f"{label} {num}",
                       "path_ref": f"{slug}-{_slug(num)}", "instrument": "", "designator": "", "number": "",
                       "year": ""}
            if cited is not None:
                inst = instruments[cited]
                mention.update(instrument=inst["instrument"], designator=inst["designator"], number=inst["number"],
                               year=inst["year"])
            out.append(mention)
    for i, inst in enumerate(instruments):
        if i in used:
            continue
        out.append({"start": inst["start"], "end": inst["end"], "quote": text[inst["start"]:inst["end"]], "path": "",
                    "path_ref": "", "instrument": inst["instrument"], "designator": inst["designator"],
                    "number": inst["number"], "year": inst["year"]})
    return sorted(out, key=lambda r: (r["start"], r["path_ref"]))


# ----------------------------------------------------------------- recording


def record_version(conn: Connection, version_id: int) -> dict:
    """Store the references every clause of one version makes.

    A parent clause repeats its children's text, so a mention is stored once,
    on the most specific clause that contains it (same canonical offsets)."""
    from app.clhear.l1.models import clause_references, clauses, source_versions, sources

    key = conn.execute(sa.select(sources.c.key).join(source_versions, source_versions.c.source_id == sources.c.id)
                       .where(source_versions.c.id == version_id)).scalar()
    if key is None:
        return {"references": 0}
    rows = conn.execute(sa.select(clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.span_start,
                                  clauses.c.span_end).where(clauses.c.source_version_id == version_id)).all()
    rows = sorted(rows, key=lambda r: ((r.span_end - r.span_start) if r.span_start is not None and r.span_end is not None
                                       else len(r.text or ""), r.id))
    conn.execute(clause_references.delete().where(clause_references.c.source_version_id == version_id))
    seen: set = set()
    values = []
    for row in rows:
        for m in extract(row.text or ""):
            canonical = ((row.span_start + m["start"], row.span_start + m["end"]) if row.span_start is not None
                         else (row.id, m["start"], m["end"]))
            if (canonical, m["path_ref"], m["instrument"]) in seen:
                continue
            seen.add((canonical, m["path_ref"], m["instrument"]))
            values.append({"source_version_id": version_id, "from_clause_id": row.id, "source_key": key,
                           "clause_ref": row.ref, "start_offset": m["start"], "end_offset": m["end"],
                           "quote": m["quote"], "cited_path": m["path"], "path_ref": m["path_ref"],
                           "cited_instrument": m["instrument"], "designator": m["designator"],
                           "number": m["number"], "year": m["year"], "derived_by": "l1.references"})
    if values:
        conn.execute(clause_references.insert(), values)
    return {"references": len(values)}


# ----------------------------------------------------------------- resolving


def _fold(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def title_key(text: str) -> tuple[tuple[str, ...], str]:
    """(words, year) of a text's title: case, a leading "the", plurals, and
    parenthetical notes do not matter."""
    text = re.sub(r"\([^)]*\)", " ", text or "")
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)]
    year = next((w for w in words if _YEAR.match(w)), "")
    kept = tuple(_fold(w) for w in words if w != "the" and not _YEAR.match(w) and w != "of")
    return kept, year


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\b\d{1,4}/\d{1,4}\b", text or ""))


def in_force_clauses(conn: Connection, keys) -> dict[str, list[dict]]:
    """source key -> its in-force clauses (id, ref, text, spans, ordering)."""
    from app.clhear.l1.models import clauses, source_versions, sources

    out: dict[str, list[dict]] = defaultdict(list)
    keys = sorted(set(keys or []))
    if not keys:
        return out
    for r in conn.execute(sa.select(sources.c.key, clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.text_hash,
                                    clauses.c.span_start,
                                    clauses.c.span_end, clauses.c.ordering, clauses.c.source_version_id)
                          .join(source_versions, source_versions.c.source_id == sources.c.id)
                          .join(clauses, clauses.c.source_version_id == source_versions.c.id)
                          .where(sources.c.key.in_(keys), source_versions.c.status == "in_force")
                          .order_by(sources.c.key, clauses.c.ordering)).mappings():
        out[r["key"]].append(dict(r))
    return out


def registered(conn: Connection) -> dict[str, dict]:
    """Every registered source (host registrations and L1 rows): key -> names and kind."""
    from app.clhear.l1.models import sources

    found: dict[str, dict] = {}
    for r in conn.execute(sa.select(sources.c.key, sources.c.name, sources.c.instrument, sources.c.short_name,
                                    sources.c.kind)):
        found[r.key] = {"key": r.key, "names": [r.name or "", r.instrument or "", r.short_name or ""],
                        "kind": r.kind or "", "name": r.name or r.key,
                        "reference": r.instrument if r.instrument not in (r.name, r.key, r.short_name) else ""}
    from app.clhear.hoststore import host_sources

    if sa.inspect(conn).has_table(host_sources.name, schema=host_sources.schema if conn.dialect.name == "postgresql"
                                  else None):
        columns = {c["name"] for c in sa.inspect(conn).get_columns(
            host_sources.name, schema=host_sources.schema if conn.dialect.name == "postgresql" else None)}
        cols = [host_sources.c.key, host_sources.c.name, host_sources.c.kind]
        if "reference" in columns:
            cols.append(host_sources.c.reference)
        for r in conn.execute(sa.select(*cols)).mappings():
            entry = found.setdefault(r["key"], {"key": r["key"], "names": [], "kind": r["kind"] or "",
                                                "name": r["name"] or r["key"], "reference": ""})
            entry["names"] += [r["name"] or "", r.get("reference") or ""]
            entry["reference"] = r.get("reference") or entry["reference"]
            entry["kind"] = r["kind"] or entry["kind"]
    return found


def _matches(mention: dict, names: list[str]) -> bool:
    if mention["number"]:
        return any(mention["number"] in _numbers(n) for n in names)
    cited, year = title_key(mention["instrument"])
    for name in names:
        words, its_year = title_key(name)
        if words and words == cited and (not year or not its_year or year == its_year):
            return True
    return False


def _find(clauses: list[dict], path_ref: str) -> dict | None:
    by_ref = {c["ref"]: c for c in clauses}
    if path_ref in by_ref:
        return by_ref[path_ref]
    base = path_ref.split("/", 1)[0]
    return by_ref.get(base)


def descendants(clauses: list[dict], target: dict) -> list[dict]:
    """The clause and every clause inside it.

    Inside means within its span. A provision written inline ("Section 3. A
    keeper shall:" followed by "(1) …") holds the clauses after it up to the
    next provision or division."""
    out = [c for c in clauses if c["id"] == target["id"]]
    if target.get("span_start") is not None and target.get("span_end") is not None:
        out += [c for c in clauses if c["id"] != target["id"] and c.get("span_start") is not None
                and target["span_start"] <= c["span_start"] and c["span_end"] <= target["span_end"]]
    else:
        out += [c for c in clauses if c["ref"].startswith(target["ref"] + "/")]
    if len(out) == 1 and UNIT_REF.match(target["ref"]):
        after = sorted((c for c in clauses if c["ordering"] > target["ordering"]), key=lambda c: c["ordering"])
        for c in after:
            if UNIT_REF.match(c["ref"]):
                break
            out.append(c)
    return out


def register_as(mention: dict, citing_kind: str) -> str:
    """The source kind to register a cited text as."""
    designator = (mention.get("designator") or "").lower()
    if designator in ("act", "code", "law"):
        return "law"
    if designator == "standard":
        return "standard"
    if designator:
        return "regulation"
    return citing_kind if citing_kind in ("law", "regulation") else "law"


def _quote(ref: dict) -> dict:
    return {"layer": "L1", "clause_id": ref["from_clause_id"], "source_key": ref["source_key"],
            "clause_ref": ref["clause_ref"], "start": ref["start_offset"], "end": ref["end_offset"],
            "quote": ref["quote"]}


def resolve(conn: Connection, source_keys) -> list[dict]:
    """Every reference the scope's texts make, resolved against the scope.

    ``status`` is ``resolved`` (the cited text, and the provision when one is
    cited, is in scope), ``not_in_scope`` (the cited text is registered but not
    in this scope) or ``unresolved`` (no registered source is the cited text).
    A cited provision that the cited text in scope does not hold still resolves
    to that text: the provision may be numbered differently."""
    from app.clhear.l1.models import clause_references, source_versions, sources

    keys = sorted(set(source_keys or []))
    if not keys:
        return []
    clauses = in_force_clauses(conn, keys)
    registry = registered(conn)
    kinds = {k: (registry.get(k) or {}).get("kind", "") for k in keys}
    titles = {k: (" ".join((rows[0]["text"] or "").split("\n")[0].split()) if rows else "") for k, rows in clauses.items()}
    rows = conn.execute(sa.select(clause_references).join(
        source_versions, source_versions.c.id == clause_references.c.source_version_id).join(
        sources, sources.c.id == source_versions.c.source_id).where(
        sources.c.key.in_(keys), source_versions.c.status == "in_force")
        .order_by(clause_references.c.source_key, clause_references.c.id)).mappings().all()
    out = []
    for ref in rows:
        mention = {"instrument": ref["cited_instrument"], "designator": ref["designator"], "number": ref["number"],
                   "path_ref": ref["path_ref"]}
        own = ref["source_key"]
        entry = {"from": _quote(ref), "cited_path": ref["cited_path"], "path_ref": ref["path_ref"],
                 "cited_instrument": ref["cited_instrument"],
                 "cited_as": ref["cited_instrument"] or ref["cited_path"],
                 "register_as": register_as(mention, kinds.get(own, "")),
                 "status": "unresolved", "target_source": None, "target_ref": None, "target_clause_id": None,
                 "registered_as": None}
        target = None
        if mention["instrument"]:
            own_names = (registry.get(own) or {}).get("names", []) + [titles.get(own, "")]
            if _matches(mention, own_names):
                target = own
            else:
                in_scope = [k for k in keys if k != own and _matches(mention, (registry.get(k) or {}).get("names", []))]
                if in_scope:
                    target = in_scope[0]
                else:
                    others = [k for k, r in sorted(registry.items()) if k not in keys and _matches(mention, r["names"])]
                    if others:
                        entry.update(status="not_in_scope", registered_as=others[0])
        else:
            if _find(clauses.get(own, []), mention["path_ref"]) is not None:
                target = own
            else:
                holders = [k for k in keys if k != own and _find(clauses.get(k, []), mention["path_ref"]) is not None]
                if len(holders) == 1:
                    target = holders[0]
            if target is None:
                entry["cited_as"] = ref["cited_path"]
        if target is not None:
            entry.update(status="resolved", target_source=target)
            if mention["path_ref"]:
                found = _find(clauses.get(target, []), mention["path_ref"])
                if found is not None:
                    entry.update(target_ref=found["ref"], target_clause_id=found["id"])
        out.append(entry)
    return out


def cited_key(entry: dict) -> str:
    """One key per cited text: the same title cited twice is one missing source.
    A bare provision that does not resolve belongs to the text citing it."""
    if entry["cited_instrument"]:
        words, _year = title_key(entry["cited_instrument"])
        numbers = sorted(_numbers(entry["cited_instrument"]))
        return "ref:" + (" ".join(numbers) if numbers else " ".join(words))
    return f"ref:{entry['from']['source_key']}:{entry['path_ref']}"


def _order(quotes: list[dict]) -> list[dict]:
    return sorted(quotes, key=lambda q: (q["source_key"], q["clause_ref"], q["start"], q["end"]))


def missing(conn: Connection, source_keys, resolved: list[dict] | None = None) -> list[dict]:
    """The texts the scope cites but does not hold, one per cited text, with every
    clause that cites it."""
    grouped: dict[str, dict] = {}
    for entry in resolve(conn, source_keys) if resolved is None else resolved:
        if entry["status"] == "resolved":
            continue
        key = cited_key(entry)
        slot = grouped.setdefault(key, {"subject": key, "cited_as": entry["cited_as"], "status": entry["status"],
                                        "register_as": entry["register_as"], "registered_as": entry["registered_as"],
                                        "cited_by": []})
        slot["cited_by"].append(entry["from"])
        if len(entry["cited_as"]) > len(slot["cited_as"]):
            slot["cited_as"] = entry["cited_as"]  # the fullest wording, with its year when one clause gives it
    for slot in grouped.values():
        slot["cited_by"] = _order(slot["cited_by"])
    return [grouped[k] for k in sorted(grouped)]


def stored_clauses(conn: Connection, source_keys) -> dict[str, int]:
    """source key -> clauses of its in-force version."""
    return {key: len(rows) for key, rows in in_force_clauses(conn, source_keys).items()}


def cited_in_scope(resolved: list[dict]) -> dict[str, list[dict]]:
    """source key -> the clauses of other sources in scope that cite it."""
    out: dict[str, list[dict]] = defaultdict(list)
    for entry in resolved:
        if entry["status"] == "resolved" and entry["target_source"] != entry["from"]["source_key"]:
            out[entry["target_source"]].append(entry["from"])
    return {key: _order(quotes) for key, quotes in out.items()}
