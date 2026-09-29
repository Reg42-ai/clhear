# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 licence types read from official registers (sources of kind ``register``).

A register lists the licence, permit, registration, authorisation, certificate
or accreditation each entity holds, or the categories of licence a regime
grants. Its entries are read without a model, in two forms:

* labelled fields, "<label>: <value>", where the label uses the words of
  licensing ("Licence type: …", "Permit category: …");
* a table whose header row names such a column ("Name | Licence category |
  Status"), read down that column.

A value is kept as a licence type when it names a licence
(``licenses.names_a_licence``) and is quoted, with its offsets, from the entry.
A value that does not itself use a licensing word ("Class A") is named with its
label ("Licence category Class A"), and both are quoted. Each distinct licence
type is a permitted value of the organisation profile's ``licences``.
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l4.licenses import LICENSING_WORDS, licence_key, names_a_licence

# Labels that say something about the holder or the record, not which licence it is.
_NOT_A_TYPE_LABEL = re.compile(r"\b(?:status|date|number|no\.?|id|reference|ref|expir\w*|since|until|holder|name|"
                               r"address|office|contact|e-?mail|phone|website|issued|granted|valid\w*|renew\w*|"
                               r"condition\w*|restriction\w*)\b", re.I)
# A single word that states a record's state, never a licence type.
_STATE = frozenset({"yes", "no", "none", "n/a", "active", "inactive", "current", "expired", "lapsed", "pending",
                    "authorised", "authorized", "licensed", "registered", "certified", "accredited", "valid"})
_FIELD = re.compile(r"(?P<label>[A-Za-z][A-Za-z /&-]{0,40}?)\s*:\s*(?P<value>[^.;|\n]{1,120})")
_CELL_SPLIT = {"|": re.compile(r"\|"), "\t": re.compile(r"\t"), ";": re.compile(r";"), ",": re.compile(r",")}
MAX_WORDS = 12
MAX_QUOTES = 3


def _label_names_a_type(label: str) -> bool:
    return bool(LICENSING_WORDS.search(label)) and not _NOT_A_TYPE_LABEL.search(label)


def _span(text: str, start: int, end: int) -> tuple[int, int]:
    """The span without surrounding whitespace."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _candidate(clause: dict, label: dict, value: tuple[int, int]) -> dict | None:
    """A licence type from one entry: its value, or its label and value; ``label`` is the label's quote."""
    val = clause["text"][value[0]:value[1]]
    words = val.split()
    if not words or len(words) > MAX_WORDS or not re.search(r"[A-Za-z]", val):
        return None
    if len(words) == 1 and val.lower().strip(".") in _STATE:
        return None
    own = bool(LICENSING_WORDS.search(val))
    name = " ".join(words) if own else f"{' '.join(label['quote'].split())} {' '.join(words)}"
    if not names_a_licence(name) or len(name.split()) > MAX_WORDS:
        return None
    return {"name": name, "quotes": [_quote(clause, *value)] if own else [label, _quote(clause, *value)]}


def _quote(clause: dict, start: int, end: int) -> dict:
    return {"layer": "L1", "clause_id": clause["id"], "source_key": clause["source_key"], "clause_ref": clause["ref"],
            "start": start, "end": end, "quote": clause["text"][start:end]}


def _fields(clause: dict) -> list[dict]:
    out = []
    for m in _FIELD.finditer(clause["text"]):
        if not _label_names_a_type(m.group("label")):
            continue
        label = _quote(clause, *_span(clause["text"], *m.span("label")))
        found = _candidate(clause, label, _span(clause["text"], *m.span("value")))
        if found:
            out.append(found)
    return out


def _cells(line: str, delimiter: str) -> list[tuple[int, int]]:
    """(start, end) of each cell of a delimited line, relative to the line."""
    spans, at = [], 0
    for part in _CELL_SPLIT[delimiter].split(line):
        spans.append((at, at + len(part)))
        at += len(part) + 1
    return spans


def _table(clause: dict, header: dict | None) -> tuple[list[dict], dict | None]:
    """Values down the licence column of a delimited table; the header carries over
    to the next clause while the table goes on."""
    text = clause["text"]
    out = []
    at = 0
    for line in text.split("\n"):
        start = at
        at += len(line) + 1
        if header is not None and line.count(header["delimiter"]) == header["cells"] - 1:
            cells = _cells(line, header["delimiter"])
            value = _span(text, start + cells[header["column"]][0], start + cells[header["column"]][1])
            found = _candidate(clause, header["quote"], value)
            if found:
                out.append(found)
            continue
        header = None
        for delimiter in ("|", "\t", ";", ","):
            if line.count(delimiter) < 1:
                continue
            cells = _cells(line, delimiter)
            for column, (s, e) in enumerate(cells):
                cell = _span(text, start + s, start + e)
                if _label_names_a_type(text[cell[0]:cell[1]]):
                    header = {"delimiter": delimiter, "cells": len(cells), "column": column,
                              "quote": _quote(clause, *cell)}
                    break
            if header:
                break
    return out, header


def _leaves(clauses: list[dict]) -> list[dict]:
    """A parent clause repeats its children's text: keep the most specific ones."""
    return [c for c in clauses if not any(o is not c and o["text"] and o["text"] in c["text"] for o in clauses)]


def read(engine: Engine) -> list[dict]:
    """Licence types quoted from the entries of the registers in scope, one per
    type, with up to three entries that name it."""
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l1.scopes import in_scope

    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.text_hash, sources.c.key,
                      sources.c.jurisdiction)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.kind == "register", source_versions.c.status == "in_force")
            .order_by(sources.c.key, clauses.c.ordering)).mappings().all()
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        if in_scope(r["key"]):
            by_source.setdefault(r["key"], []).append({"id": r["id"], "ref": r["ref"], "text": r["text"] or "",
                                                       "text_hash": r["text_hash"], "source_key": r["key"],
                                                       "jurisdiction": (r["jurisdiction"] or "").strip().upper()})
    found: dict[tuple[str, str], dict] = {}
    for key, entries in by_source.items():
        header = None
        for clause in _leaves(entries):
            from_table, header = _table(clause, header)
            for item in _fields(clause) + from_table:
                jurisdiction = clause["jurisdiction"] or "*"
                slot = found.setdefault((jurisdiction, licence_key(item["name"])),
                                        {"name": item["name"], "jurisdiction": jurisdiction, "entries": []})
                if len(slot["entries"]) < MAX_QUOTES:
                    slot["entries"].append({"clause": clause, "quotes": item["quotes"]})
    return [found[k] for k in sorted(found)]
