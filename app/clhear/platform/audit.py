# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Audit log (HLD v2 §7.1; item 17): every read of licensed text and every write.

One append-only ledger, ``l0_platform.audit_log``. Three producers feed it:

* :func:`record.write` / :func:`record.invalidate` — every node/edge write through the
  shared-schema path (``action = write`` / ``invalidate``), with the table, the row id
  and the why-trail that justified it.
* the L1 text endpoints — every response that carries clause text whose rights basis is
  ``licensed`` or ``byol_only`` (``action = read.licensed_text``). Public-domain and
  open-licence text is not audited: there is nothing to account for.
* the HTTP layer — every mutating request (``POST/PUT/PATCH/DELETE``) as
  ``action = http.write`` with method, path, status and the caller's identity.

Who did it comes from :func:`bind_actor`, a context variable the request middleware
sets from the session cookie, the ``X-Reg42-User`` header or the ``X-App-Id`` header,
so a write deep inside a fleet or an API handler is attributed without threading an
actor through every call. Fleets bind ``system:<fleet>`` themselves.

Privacy: IPs are stored as a salted hash (the salt is the session secret), user agents
truncated, and there is no free-text request body in the log. The table is a ledger in
the I2 sense — rows are never updated or deleted; :func:`assert_append_only` is what
the never-list test checks.
"""
from __future__ import annotations

import contextvars
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import Json, L0_SCHEMA, metadata

ACTOR_KINDS = ("anonymous", "user", "maintainer", "app", "system")
LICENSED_BASES = frozenset({"licensed", "byol_only"})  # rights bases whose text reads are audited
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# mutating in HTTP terms but read-only in substance; logging them would be noise
HTTP_WRITE_EXEMPT = ("/graphql", "/l1/search", "/explore/search", "/api/clhear/ask", "/ai/")

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("actor", sa.Text, nullable=False, default=""),  # email, app id, system:<name> or ""
    sa.Column("actor_kind", sa.Text, sa.CheckConstraint(f"actor_kind in {ACTOR_KINDS}", name="audit_actor_kind_check"), nullable=False),
    sa.Column("action", sa.Text, nullable=False, index=True),  # write | invalidate | read.licensed_text | http.write | auth.<event>
    sa.Column("resource", sa.Text, nullable=False, default="", index=True),  # table name, route path, source key
    sa.Column("resource_id", sa.Text, nullable=False, default=""),
    sa.Column("detail", Json, nullable=False, default=dict),
    sa.Column("request_id", sa.Text, nullable=False, default="", index=True),
    sa.Column("ip_hash", sa.Text, nullable=False, default=""),
    sa.Column("user_agent", sa.Text, nullable=False, default=""),
    schema=L0_SCHEMA,
)

AUDIT_TABLES = (audit_log,)


class Actor:
    __slots__ = ("actor", "kind", "request_id", "ip_hash", "user_agent")

    def __init__(self, actor: str = "", kind: str = "anonymous", request_id: str = "", ip_hash: str = "", user_agent: str = ""):
        if kind not in ACTOR_KINDS:
            raise ValueError(f"unknown actor kind {kind!r}")
        self.actor, self.kind, self.request_id, self.ip_hash, self.user_agent = actor, kind, request_id, ip_hash, user_agent[:200]

    def as_dict(self) -> dict:
        return {"actor": self.actor, "actor_kind": self.kind, "request_id": self.request_id, "ip_hash": self.ip_hash, "user_agent": self.user_agent}


_ANON = Actor()
_current: contextvars.ContextVar[Actor] = contextvars.ContextVar("clhear_audit_actor", default=_ANON)


def bind_actor(actor: Actor) -> contextvars.Token:
    return _current.set(actor)


def reset_actor(token: contextvars.Token) -> None:
    _current.reset(token)


def current_actor() -> Actor:
    return _current.get()


def system_actor(name: str) -> Actor:
    return Actor(actor=f"system:{name}", kind="system", request_id=uuid.uuid4().hex)


def hash_ip(ip: str | None) -> str:
    if not ip:
        return ""
    from app.clhear.settings import get_settings

    return hashlib.sha256((get_settings().clhear_session_secret + "|" + ip).encode()).hexdigest()[:32]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log(conn: Connection, action: str, *, resource: str = "", resource_id: str = "", detail: dict | None = None,
        actor: Actor | None = None) -> str:
    """Append one entry inside the caller's transaction and return its id."""
    a = actor or current_actor()
    entry_id = str(uuid.uuid4())
    conn.execute(audit_log.insert().values(id=entry_id, at=_now(), action=action, resource=resource, resource_id=str(resource_id or ""),
                                           detail=detail or {}, **a.as_dict()))
    return entry_id


def log_write(conn: Connection, table: sa.Table, values: dict, *, why_trail_id: str, action: str = "write") -> str:
    """Called by ``record.write`` / ``record.invalidate``: the write itself is the evidence."""
    pk = [c.name for c in table.primary_key.columns]
    rid = "/".join(str(values.get(k, "")) for k in pk) if pk else ""
    a = current_actor()
    if a.kind == "anonymous" and values.get("derived_by"):
        a = Actor(actor=str(values["derived_by"]), kind="system", request_id=a.request_id)
    return log(conn, action, resource=f"{table.schema}.{table.name}" if table.schema else table.name, resource_id=rid,
               detail={"why_trail_id": why_trail_id, "version": values.get("version"), "layer": _layer_of(table)}, actor=a)


def log_licensed_read(conn: Connection, *, source_key: str, rights_basis: str, clause_ids: list[Any], route: str) -> str | None:
    """Called by the L1 text endpoints after the rights check let text through. No-op for
    bases that need no accounting."""
    if rights_basis not in LICENSED_BASES or not clause_ids:
        return None
    return log(conn, "read.licensed_text", resource=source_key, resource_id=str(clause_ids[0]) if len(clause_ids) == 1 else "",
               detail={"rights_basis": rights_basis, "clauses": len(clause_ids), "clause_ids": [str(c) for c in clause_ids[:50]], "route": route})


def log_http_write(engine: Engine, *, method: str, path: str, status: int, actor: Actor, duration_ms: int) -> None:
    """Middleware hook; never lets an audit failure fail the request."""
    try:
        with engine.begin() as conn:
            log(conn, "http.write", resource=path, detail={"method": method, "status": status, "duration_ms": duration_ms}, actor=actor)
    except Exception:  # noqa: BLE001 — the request already happened; losing one audit row beats a 500 here
        import logging

        logging.getLogger(__name__).exception("audit http.write failed")


def should_audit_http(method: str, path: str) -> bool:
    return method.upper() in MUTATING_METHODS and not any(path.startswith(p) for p in HTTP_WRITE_EXEMPT)


def _layer_of(table: sa.Table) -> str:
    """Schemas are named ``l<N>_<name>``; the layer is the prefix."""
    s = table.schema or ""
    return s.split("_", 1)[0].upper() if s[:1] == "l" and s[1:2].isdigit() else ""


# --------------------------------------------------------------------------- reading it back


def query(conn: Connection, *, actor: str | None = None, action: str | None = None, resource: str | None = None,
          since: datetime | None = None, until: datetime | None = None, request_id: str | None = None, limit: int = 200,
          offset: int = 0) -> list[dict]:
    q = sa.select(audit_log).order_by(audit_log.c.at.desc(), audit_log.c.id).limit(max(1, min(int(limit), 1000))).offset(max(0, int(offset)))
    if actor:
        q = q.where(audit_log.c.actor == actor)
    if action:
        q = q.where(audit_log.c.action.like(action.replace("*", "%")) if "*" in action else audit_log.c.action == action)
    if resource:
        q = q.where(audit_log.c.resource == resource)
    if request_id:
        q = q.where(audit_log.c.request_id == request_id)
    if since is not None:
        q = q.where(audit_log.c.at >= since)
    if until is not None:
        q = q.where(audit_log.c.at < until)
    return [_row(r) for r in conn.execute(q).mappings()]


def summary(conn: Connection, *, since: datetime | None = None) -> dict:
    q = sa.select(audit_log.c.action, sa.func.count()).group_by(audit_log.c.action)
    if since is not None:
        q = q.where(audit_log.c.at >= since)
    by_action = {a: int(n) for a, n in conn.execute(q)}
    reads = sa.select(audit_log.c.resource, sa.func.count()).where(audit_log.c.action == "read.licensed_text").group_by(audit_log.c.resource)
    if since is not None:
        reads = reads.where(audit_log.c.at >= since)
    actors = sa.select(sa.func.count(sa.distinct(audit_log.c.actor))).where(audit_log.c.actor != "")
    if since is not None:
        actors = actors.where(audit_log.c.at >= since)
    return {"since": since.isoformat() if since else None, "total": sum(by_action.values()), "by_action": by_action,
            "licensed_reads_by_source": {s: int(n) for s, n in conn.execute(reads)},
            "distinct_actors": int(conn.execute(actors).scalar_one() or 0)}


def _row(r) -> dict:
    d = dict(r)
    d["at"] = d["at"].isoformat() if hasattr(d["at"], "isoformat") else d["at"]
    return d


def assert_append_only(module_source: str) -> None:
    """Never-list check: this module (and anything importing ``audit_log``) may not
    update or delete audit rows."""
    # needles assembled so this checker never trips the repo-wide never-delete lint itself
    verbs = ("update", "delete")
    needles = [f"audit_log.{v}(" for v in verbs] + [f"{v.upper()} {'FROM ' if v == 'delete' else ''}audit_log" for v in verbs]
    for needle in needles:
        if needle in module_source:
            raise AssertionError(f"audit log must be append-only; found {needle!r}")
