# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Query graph projection (HLD v2 I7, §8 item 10).

Postgres is the record. The graph is a *projection* of the live rows — rebuilt
from the record nightly (and on demand), never written to directly, and
disposable: dropping it loses nothing. Two backends serve the same queries:

* ``Neo4jGraph`` — Neo4j Community on Fargate + EFS (``infra/neo4j.tf``).
  ``rebuild`` is idempotent: every node and edge is ``MERGE``d by its CLHEAR id
  and stamped with the projection id; whatever the record no longer produces
  is removed from the projection afterwards (the record itself keeps it, I2).
* ``LocalGraph`` — the same snapshot held as in-memory adjacency lists. It is
  the offline / SQLite backend and the reference the Neo4j Cypher is checked
  against in tests.

The two acceptance queries walk one hop each way between an obligation and its
evidence: ``evidence_for(obligation)`` → the clauses that assert it and the
sources they sit in; ``derived_from(clause)`` → the obligations asserted on
that clause and what they require downstream. Both must answer in < 300 ms
(``tests/test_graph_latency.py``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import (
    activities,
    applies_to,
    asserts,
    blocks,
    blueprint_items,
    blueprints,
    licences,
    mitigates,
    obligations,
    operates,
    profiles,
    requires,
)
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.l1.public import clauses_public_select
from app.clhear.models import graph_projections
from app.clhear.platform import events

log = logging.getLogger("clhear.graph")

LIVE = ("derived", "validated", "curated")
LABELS = {
    "source": "Source", "clause": "Clause", "obligation": "Obligation", "block": "Block",
    "activity": "Activity", "licence": "Licence", "profile": "Profile", "blueprint": "Blueprint",
    "predicate": "Predicate",
}
PRODUCER = "platform.graph"


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


# --------------------------------------------------------------------------- snapshot


@dataclass
class GraphSnapshot:
    """What the record projects to: nodes keyed by CLHEAR id, typed edges."""

    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[dict] = field(default_factory=list)
    projected_at: str = ""
    release: str = ""

    def node(self, node_id: str, kind: str, layer: str, label: str, **props) -> None:
        if node_id not in self.nodes:
            self.nodes[node_id] = {"id": node_id, "kind": kind, "layer": layer, "label": label, **props}

    def edge(self, src: str, dst: str, rel: str, layer: str, **props) -> None:
        if src in self.nodes and dst in self.nodes:
            self.edges.append({"from": src, "to": dst, "rel": rel, "layer": layer, **props})

    def checksum(self) -> str:
        h = hashlib.sha256()
        for nid in sorted(self.nodes):
            h.update(json.dumps(self.nodes[nid], sort_keys=True, default=str).encode())
        for e in sorted(self.edges, key=lambda e: (e["from"], e["rel"], e["to"])):
            h.update(json.dumps(e, sort_keys=True, default=str).encode())
        return h.hexdigest()[:16]

    def counts(self) -> dict:
        by_label: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_label[LABELS[n["kind"]]] += 1
        by_rel: dict[str, int] = defaultdict(int)
        for e in self.edges:
            by_rel[e["rel"]] += 1
        return {"nodes": len(self.nodes), "edges": len(self.edges), "by_label": dict(by_label), "by_rel": dict(by_rel)}


def project(engine: Engine, *, release: str = "") -> GraphSnapshot:
    """Read the live record rows into a snapshot. Only current / live rows are
    projected: the graph answers "what holds now"; history stays in the record."""
    snap = GraphSnapshot(projected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), release=release)
    with engine.connect() as conn:
        _project_l1(conn, snap)
        _project_l2(conn, snap)
        _project_l3(conn, snap)
        _project_l4(conn, snap)
        _project_l5(conn, snap)
        _project_l6(conn, snap)
    return snap


def _project_l1(conn: Connection, snap: GraphSnapshot) -> None:
    src_by_id: dict[int, str] = {}
    for r in conn.execute(sa.select(sources.c.id, sources.c.key, sources.c.name, sources.c.jurisdiction)).mappings():
        src_by_id[r["id"]] = r["key"]
        snap.node(r["key"], "source", "L1", r["name"], jurisdiction=r["jurisdiction"])
    current = {r["source_id"]: (r["id"], r["version_label"]) for r in conn.execute(
        sa.select(source_versions.c.id, source_versions.c.source_id, source_versions.c.version_label)
        .where(source_versions.c.status == "in_force").order_by(source_versions.c.id)).mappings()}
    version_ids = {vid for vid, _ in current.values()}
    if not version_ids:
        return
    public_clauses = clauses_public_select(conn).with_only_columns(clauses.c.id).subquery()
    publicly_readable = clauses.c.id.in_(sa.select(public_clauses.c.id))
    for r in conn.execute(sa.select(clauses.c.id, clauses.c.source_version_id, clauses.c.ref,
                                    sa.case((publicly_readable, clauses.c.path), else_=None).label("path"),
                                    publicly_readable.label("public_ok"), clauses.c.normative, source_versions.c.source_id)
                          .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                          .where(clauses.c.source_version_id.in_(version_ids))).mappings():
        cid = f"CLS-{r['id']}"
        key = src_by_id.get(r["source_id"])
        if key is None:
            continue
        snap.node(cid, "clause", "L1", f"{key} · {r['ref']}", ref=r["ref"], path=r["path"], public_ok=bool(r["public_ok"]),
                  normative=bool(r["normative"]), version=current[r["source_id"]][1])
        snap.edge(cid, key, "PART_OF", "L1", version=current[r["source_id"]][1])


def _project_l2(conn: Connection, snap: GraphSnapshot) -> None:
    for r in conn.execute(sa.select(obligations.c.id, obligations.c.stable_id, obligations.c.title, obligations.c.jurisdiction,
                                    obligations.c.source_key, obligations.c.clause_ref, obligations.c.status)
                          .where(obligations.c.status.in_(LIVE))).mappings():
        snap.node(r["id"], "obligation", "L2", r["title"], stable_id=r["stable_id"], jurisdiction=r["jurisdiction"],
                  source_key=r["source_key"], clause_ref=r["clause_ref"], status=r["status"])
    for r in conn.execute(sa.select(asserts).where(asserts.c.valid_to.is_(None))).mappings():
        snap.edge(r["obligation_id"], f"CLS-{r['clause_id']}", "ASSERTED_BY", "L2", id=r["id"], strength=r["strength"],
                  span=[r["span_start"], r["span_end"]])


def _project_l3(conn: Connection, snap: GraphSnapshot) -> None:
    for r in conn.execute(sa.select(blocks.c.id, blocks.c.name, blocks.c.kind, blocks.c.status, blocks.c.canonical_id)
                          .where(blocks.c.valid_to.is_(None))).mappings():
        snap.node(r["id"], "block", "L3", r["name"], block_kind=r["kind"], status=r["status"], canonical_id=r["canonical_id"])
    for r in conn.execute(sa.select(requires).where(requires.c.valid_to.is_(None))).mappings():
        snap.edge(r["obligation_id"], r["block_id"], "REQUIRES", "L3", id=r["id"], method=r["method"])


def _project_l4(conn: Connection, snap: GraphSnapshot) -> None:
    for r in conn.execute(sa.select(licences.c.id, licences.c.name, licences.c.jurisdiction, licences.c.regulator)
                          .where(licences.c.valid_to.is_(None))).mappings():
        snap.node(r["id"], "licence", "L4", r["name"], jurisdiction=r["jurisdiction"], regulator=r["regulator"])
    licence_by_name = {n["label"].lower(): nid for nid, n in snap.nodes.items() if n["kind"] == "licence"}
    for r in conn.execute(sa.select(applies_to).where(applies_to.c.valid_to.is_(None))).mappings():
        pred = _json(r["predicate"], {})
        snap.node(r["id"], "predicate", "L4", json.dumps(pred, sort_keys=True), basis=r["basis"], predicate=pred)
        snap.edge(r["obligation_id"], r["id"], "APPLIES_TO", "L4", basis=r["basis"], method=r["method"])
    for r in conn.execute(sa.select(profiles.c.id, profiles.c.name, profiles.c.status, profiles.c.attributes)
                          .where(profiles.c.valid_to.is_(None))).mappings():
        attrs = _json(r["attributes"], {})
        snap.node(r["id"], "profile", "L4", r["name"], status=r["status"], jurisdictions=attrs.get("jurisdictions", []))
        for name in attrs.get("licences") or []:
            lic = licence_by_name.get(str(name).lower())
            if lic:
                snap.edge(r["id"], lic, "HOLDS", "L4")


def _project_l5(conn: Connection, snap: GraphSnapshot) -> None:
    by_stable = {n.get("stable_id"): nid for nid, n in snap.nodes.items() if n["kind"] == "obligation"}
    by_anchor = defaultdict(list)
    for nid, n in snap.nodes.items():
        if n["kind"] == "obligation":
            by_anchor[(n["source_key"], n["clause_ref"])].append(nid)
    for r in conn.execute(sa.select(activities.c.id, activities.c.name, activities.c.side, activities.c.action_type,
                                    activities.c.triggers).where(activities.c.valid_to.is_(None))).mappings():
        snap.node(r["id"], "activity", "L5", r["name"], side=r["side"], action_type=r["action_type"])
        for t in _json(r["triggers"], []):
            ref = t.get("obligation_ref")
            targets = [ref] if ref in snap.nodes else [by_stable[ref]] if ref in by_stable else []
            if not targets:
                anchor = t.get("anchor") or {}
                for cref in anchor.get("refs") or []:
                    targets.extend(by_anchor.get((anchor.get("source_key"), cref), []))
            for oid in dict.fromkeys(targets):
                snap.edge(r["id"], oid, "TRIGGERED_BY", "L5", method=t.get("method", ""))
    for r in conn.execute(sa.select(operates).where(operates.c.valid_to.is_(None))).mappings():
        snap.edge(r["activity_id"], r["block_id"], "OPERATES", "L5", id=r["id"], obligations=_json(r["obligation_refs"], []))
    for r in conn.execute(sa.select(mitigates).where(mitigates.c.valid_to.is_(None))).mappings():
        snap.edge(r["compliance_activity_id"], r["business_activity_id"], "MITIGATES", "L5", id=r["id"],
                  obligations=_json(r["obligation_refs"], []))


def _project_l6(conn: Connection, snap: GraphSnapshot) -> None:
    current = {}
    for r in conn.execute(sa.select(blueprints.c.stable_id, blueprints.c.profile_id, blueprints.c.release, blueprints.c.engine_version)
                          .where(blueprints.c.status == "current", blueprints.c.stable_id.is_not(None))).mappings():
        current[r["stable_id"]] = r
        snap.node(r["stable_id"], "blueprint", "L6", r["stable_id"], profile_id=r["profile_id"], release=r["release"],
                  engine=r["engine_version"])
        if r["profile_id"]:
            snap.edge(r["stable_id"], r["profile_id"], "FOR_PROFILE", "L6")
    if not current:
        return
    for r in conn.execute(sa.select(blueprint_items).where(blueprint_items.c.blueprint_id.in_(list(current)),
                                                            blueprint_items.c.valid_to.is_(None))).mappings():
        snap.edge(r["blueprint_id"], r["block_id"], "INCLUDES", "L6", item=r["id"], basis=r["basis"],
                  obligations=_json(r["obligations_satisfied"], []))
        for oid in _json(r["obligations_satisfied"], []):
            snap.edge(r["block_id"], oid, "SATISFIES", "L6", blueprint=r["blueprint_id"], item=r["id"])


def _public_result(engine, result):
    """Redact cached clause text at the response boundary, in one batched query.

    Operator exceptions never authorize graph text. Replacing a snapshot or
    revoking a public grant must not leave verbatim paths in a warm graph cache.
    Unknown/missing clause identities remain references only.
    """
    ids = set()
    def collect(value):
        if isinstance(value, dict):
            if value.get("kind") == "clause":
                raw = str(value.get("id", ""))
                if raw.startswith("CLS-") and raw[4:].isdigit():
                    ids.add(int(raw[4:]))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(result)
    paths = {}
    if engine is not None and ids:
        with engine.connect() as conn:
            paths = dict(conn.execute(clauses_public_select(conn).where(clauses.c.id.in_(ids))
                         .with_only_columns(clauses.c.id, clauses.c.path)).all())
    def clean(value):
        if isinstance(value, dict):
            out = {key: clean(child) for key, child in value.items()}
            if value.get("kind") == "clause":
                raw = str(value.get("id", ""))
                clause_id = int(raw[4:]) if raw.startswith("CLS-") and raw[4:].isdigit() else None
                out["path"] = paths.get(clause_id)
                out["public_ok"] = clause_id in paths
                # Graph clauses have no body field. Do not propagate old or
                # externally projected text-bearing fields from a cached node.
                for field in ("text", "raw_text", "heading", "source_fragment", "source_locator"):
                    out.pop(field, None)
            return out
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value
    return clean(result)


# --------------------------------------------------------------------------- local backend


class LocalGraph:
    """In-memory adjacency over a snapshot — the SQLite / offline backend and the
    reference implementation the Neo4j Cypher must agree with."""

    name = "local"

    def __init__(self) -> None:
        self.reader_engine = None
        self.snapshot = GraphSnapshot()
        self._out: dict[str, list[dict]] = defaultdict(list)
        self._in: dict[str, list[dict]] = defaultdict(list)
        self.loaded_at: float | None = None

    def rebuild(self, snapshot: GraphSnapshot) -> dict:
        out: dict[str, list[dict]] = defaultdict(list)
        inc: dict[str, list[dict]] = defaultdict(list)
        for e in snapshot.edges:
            out[e["from"]].append(e)
            inc[e["to"]].append(e)
        self.snapshot, self._out, self._in = snapshot, out, inc
        self.loaded_at = time.time()
        return {"backend": self.name, **snapshot.counts(), "checksum": snapshot.checksum()}

    @property
    def ready(self) -> bool:
        return self.loaded_at is not None

    def node(self, node_id: str) -> dict | None:
        return self.snapshot.nodes.get(node_id)

    def resolve(self, node_id: str) -> str | None:
        if node_id in self.snapshot.nodes:
            return node_id
        for nid, n in self.snapshot.nodes.items():
            if n.get("stable_id") == node_id:
                return nid
        return None

    def out(self, node_id: str, rel: str | None = None) -> list[dict]:
        return [e for e in self._out.get(node_id, []) if rel is None or e["rel"] == rel]

    def inc(self, node_id: str, rel: str | None = None) -> list[dict]:
        return [e for e in self._in.get(node_id, []) if rel is None or e["rel"] == rel]

    # -- the two acceptance queries ------------------------------------------------
    def evidence_for(self, obligation_id: str) -> dict | None:
        oid = self.resolve(obligation_id)
        ob = self.node(oid) if oid else None
        if ob is None or ob["kind"] != "obligation":
            return None
        clause_rows = []
        for e in self.out(oid, "ASSERTED_BY"):
            c = self.node(e["to"])
            src = next((self.node(p["to"]) for p in self.out(e["to"], "PART_OF")), None)
            clause_rows.append({"clause": c, "strength": e.get("strength"), "span": e.get("span"), "source": src})
        return _public_result(self.reader_engine, {"obligation": ob, "clauses": clause_rows, "count": len(clause_rows)})

    def derived_from(self, clause_id: str) -> dict | None:
        cid = clause_id if clause_id.startswith("CLS-") else f"CLS-{clause_id}"
        clause = self.node(cid)
        if clause is None:
            return None
        src = next((self.node(p["to"]) for p in self.out(cid, "PART_OF")), None)
        rows = []
        for e in self.inc(cid, "ASSERTED_BY"):
            ob = self.node(e["from"])
            rows.append({
                "obligation": ob, "strength": e.get("strength"),
                "requires": [self.node(r["to"]) for r in self.out(e["from"], "REQUIRES")],
                "triggers": [self.node(t["from"]) for t in self.inc(e["from"], "TRIGGERED_BY")],
                "satisfied_in": sorted({s["blueprint"] for s in self.inc(e["from"], "SATISFIES")}),
            })
        return _public_result(self.reader_engine, {"clause": clause, "source": src, "obligations": rows, "count": len(rows)})

    def neighbourhood(self, node_id: str) -> dict | None:
        nid = self.resolve(node_id)
        if nid is None:
            return None
        edges = self.out(nid) + self.inc(nid)
        ids = {nid, *(e["from"] for e in edges), *(e["to"] for e in edges)}
        return _public_result(self.reader_engine, {"focus": nid, "nodes": [self.node(i) for i in ids], "edges": edges})


# --------------------------------------------------------------------------- neo4j backend


CYPHER = {
    "constraints": [
        f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE" for label in LABELS.values()
    ],
    "merge_nodes": (
        "UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) SET n += row.props, n.projected = $stamp"
    ),
    # labelled end-points so each MATCH hits the per-label unique index
    "merge_edges": (
        "UNWIND $rows AS row MATCH (a:{from_label} {{id: row.from}}) MATCH (b:{to_label} {{id: row.to}}) "
        "MERGE (a)-[r:{rel}]->(b) SET r += row.props, r.projected = $stamp"
    ),
    "sweep_edges": "MATCH ()-[r]->() WHERE r.projected IS NULL OR r.projected <> $stamp DELETE r",
    "sweep_nodes": "MATCH (n) WHERE n.projected IS NULL OR n.projected <> $stamp DETACH DELETE n",
    # one hop toward the evidence: obligation -> asserting clause -> its source
    "evidence_for": (
        "MATCH (o:Obligation) WHERE o.id = $id OR o.stable_id = $id "
        "OPTIONAL MATCH (o)-[a:ASSERTED_BY]->(c:Clause) OPTIONAL MATCH (c)-[:PART_OF]->(s:Source) "
        "RETURN o, collect({clause: c, strength: a.strength, span: a.span, source: s}) AS clauses"
    ),
    # one hop toward the derived: clause -> obligations asserted on it -> what they require / trigger / satisfy
    # (aggregates are staged through WITH — Cypher forbids nesting collect() in collect())
    "derived_from": (
        "MATCH (c:Clause {id: $id}) OPTIONAL MATCH (c)-[:PART_OF]->(s:Source) "
        "OPTIONAL MATCH (c)<-[a:ASSERTED_BY]-(o:Obligation) "
        "OPTIONAL MATCH (o)-[:REQUIRES]->(b:Block) "
        "WITH c, s, o, a, collect(DISTINCT b) AS requires "
        "OPTIONAL MATCH (act:Activity)-[:TRIGGERED_BY]->(o) "
        "WITH c, s, o, a, requires, collect(DISTINCT act) AS triggers "
        "OPTIONAL MATCH (:Block)-[sat:SATISFIES]->(o) "
        "WITH c, s, o, a, requires, triggers, collect(DISTINCT sat.blueprint) AS satisfied_in "
        "RETURN c, s, collect({obligation: o, strength: a.strength, requires: requires, "
        "triggers: triggers, satisfied_in: satisfied_in}) AS obligations"
    ),
}
BATCH = 500


class Neo4jGraph:
    """Neo4j Community projection. `driver` is injectable so the statement plan is
    testable without a server; production builds it from CLHEAR_NEO4J_URI."""

    name = "neo4j"

    def __init__(self, uri: str, user: str = "neo4j", password: str = "", *, driver=None, database: str = "neo4j"):
        self.uri, self.database = uri, database
        if driver is None:
            from neo4j import GraphDatabase  # optional dependency, present on the fleet image

            driver = GraphDatabase.driver(uri, auth=(user, password) if password else None,
                                          notifications_min_severity="WARNING")
        self._driver = driver
        self.loaded_at: float | None = None

    def _run(self, session, statement: str, **params):
        return session.run(statement, **params)

    def rebuild(self, snapshot: GraphSnapshot) -> dict:
        stamp = snapshot.projected_at
        with self._driver.session(database=self.database) as s:
            for stmt in CYPHER["constraints"]:
                self._run(s, stmt)
            by_label: dict[str, list[dict]] = defaultdict(list)
            for n in snapshot.nodes.values():
                props = {k: _scalar(v) for k, v in n.items() if k not in ("id", "kind")}
                by_label[LABELS[n["kind"]]].append({"id": n["id"], "props": props})
            for label, rows in by_label.items():
                for i in range(0, len(rows), BATCH):
                    self._run(s, CYPHER["merge_nodes"].format(label=label), rows=rows[i:i + BATCH], stamp=stamp)
            by_rel: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
            for e in snapshot.edges:
                props = {k: _scalar(v) for k, v in e.items() if k not in ("from", "to", "rel")}
                key = (e["rel"], LABELS[snapshot.nodes[e["from"]]["kind"]], LABELS[snapshot.nodes[e["to"]]["kind"]])
                by_rel[key].append({"from": e["from"], "to": e["to"], "props": props})
            for (rel, from_label, to_label), rows in by_rel.items():
                stmt = CYPHER["merge_edges"].format(rel=rel, from_label=from_label, to_label=to_label)
                for i in range(0, len(rows), BATCH):
                    self._run(s, stmt, rows=rows[i:i + BATCH], stamp=stamp)
            self._run(s, CYPHER["sweep_edges"], stamp=stamp)
            self._run(s, CYPHER["sweep_nodes"], stamp=stamp)
        self.loaded_at = time.time()
        return {"backend": self.name, **snapshot.counts(), "checksum": snapshot.checksum()}

    @property
    def ready(self) -> bool:
        return True

    def evidence_for(self, obligation_id: str) -> dict | None:
        with self._driver.session(database=self.database) as s:
            rec = self._run(s, CYPHER["evidence_for"], id=obligation_id).single()
        if rec is None:
            return None
        ob = _props(rec["o"])
        rows = [{"clause": _props(r["clause"]), "strength": r["strength"], "span": r["span"], "source": _props(r["source"])}
                for r in rec["clauses"] if r["clause"] is not None]
        return _public_result(getattr(self, "reader_engine", None), {"obligation": ob, "clauses": rows, "count": len(rows)})

    def derived_from(self, clause_id: str) -> dict | None:
        cid = clause_id if clause_id.startswith("CLS-") else f"CLS-{clause_id}"
        with self._driver.session(database=self.database) as s:
            rec = self._run(s, CYPHER["derived_from"], id=cid).single()
        if rec is None:
            return None
        rows = [{"obligation": _props(r["obligation"]), "strength": r["strength"],
                 "requires": [_props(b) for b in r["requires"]], "triggers": [_props(a) for a in r["triggers"]],
                 "satisfied_in": sorted(x for x in r["satisfied_in"] if x)}
                for r in rec["obligations"] if r["obligation"] is not None]
        return _public_result(getattr(self, "reader_engine", None), {"clause": _props(rec["c"]), "source": _props(rec["s"]), "obligations": rows, "count": len(rows)})


def _scalar(v):
    """Neo4j properties are primitives or homogeneous null-free lists; nested
    maps (and mixed lists) become JSON strings, and an all-null list is null."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        items = [x for x in v if x is not None]
        if not items:
            return None
        if len({type(x) for x in items}) == 1 and isinstance(items[0], (str, int, float, bool)):
            return items if len(items) == len(v) else json.dumps(v)
    return json.dumps(v, sort_keys=True, default=str)


def _props(node) -> dict | None:
    if node is None:
        return None
    return dict(node.items()) if hasattr(node, "items") else dict(node)


# --------------------------------------------------------------------------- facade


_LOCAL: dict[str, LocalGraph] = {}


def configured_backend() -> str:
    from app.clhear.settings import get_settings

    return "neo4j" if get_settings().clhear_neo4j_uri else "local"


def get_graph(engine: Engine, *, ensure: bool = True):
    """The graph for this record: Neo4j when configured, else the process-local
    projection (built on first use)."""
    from app.clhear.settings import get_settings

    settings = get_settings()
    if settings.clhear_neo4j_uri:
        graph = Neo4jGraph(settings.clhear_neo4j_uri, settings.clhear_neo4j_user, settings.clhear_neo4j_password)
        graph.reader_engine = engine
        return graph
    key = str(engine.url)
    graph = _LOCAL.get(key)
    if graph is None:
        graph = _LOCAL[key] = LocalGraph()
    graph.reader_engine = engine
    if ensure and not graph.ready:
        rebuild(engine, graph=graph, trigger="lazy")
    return graph


def invalidate(engine: Engine | None = None) -> None:
    """Forget the local projection so the next query rebuilds it from the record."""
    if engine is None:
        _LOCAL.clear()
    else:
        _LOCAL.pop(str(engine.url), None)


def rebuild(engine: Engine, graph=None, *, release: str = "", trigger: str = "nightly", publish: bool = True) -> dict:
    """Project the record and load it into the graph. Idempotent: the same record
    yields the same checksum and a no-op-shaped result; every run is logged in
    `graph_projections` so the status page can show when the graph was last
    rebuilt and from what."""
    graph = graph or get_graph(engine, ensure=False)
    graph.reader_engine = engine
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)
    status, error, summary = "succeeded", "", {}
    try:
        snapshot = project(engine, release=release)
        summary = graph.rebuild(snapshot)
    except Exception as exc:  # the record is untouched; the projection is simply stale
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:400]
        log.exception("graph rebuild failed")
    ms = int((time.perf_counter() - started) * 1000)
    with engine.begin() as conn:
        conn.execute(graph_projections.insert().values(
            backend=getattr(graph, "name", "unknown"), trigger=trigger, release=release, status=status,
            started_at=started_at, finished_at=datetime.now(timezone.utc), duration_ms=ms,
            nodes=summary.get("nodes", 0), edges=summary.get("edges", 0), checksum=summary.get("checksum", ""),
            detail={"by_label": summary.get("by_label", {}), "by_rel": summary.get("by_rel", {}), "error": error}))
        if publish and status == "succeeded":
            events.emit(conn, layer="l0", kind="clhear.l0.graph_rebuilt", subject_ref=summary.get("checksum", ""),
                        payload={"backend": summary.get("backend"), "nodes": summary["nodes"], "edges": summary["edges"],
                                 "release": release, "trigger": trigger}, producer=PRODUCER)
    return {**summary, "status": status, "error": error, "duration_ms": ms, "trigger": trigger}


def status(engine: Engine) -> dict:
    """Last projection runs + what backend serves queries now."""
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(graph_projections).order_by(graph_projections.c.id.desc()).limit(10)).mappings()]
    for r in rows:
        for k in ("started_at", "finished_at"):
            r[k] = str(r[k]) if r[k] else None
        r["detail"] = _json(r.get("detail"), {})
    graph_backends = ("neo4j", "local")
    last_ok = next((r for r in rows if r["status"] == "succeeded" and r["backend"] in graph_backends), None)
    last_index = next((r for r in rows if r["status"] == "succeeded" and r["backend"] not in graph_backends), None)
    return {"backend": configured_backend(), "last_rebuild": last_ok, "last_index_rebuild": last_index, "runs": rows,
            "queries": {"evidence_for": "/graph/obligations/{id}/evidence", "derived_from": "/graph/clauses/{id}/derived"},
            "budget_ms": 300}
