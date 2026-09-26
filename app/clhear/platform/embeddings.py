# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Clause embeddings + vector index (HLD v2 I7, §8 item 10).

``clauses.embedding`` holds one 1024-dimensional vector per public clause of an
in-force version. On Aurora the column is ``vector(1024)`` (pgvector, HNSW
cosine index) and nearest-neighbour search runs in SQL; on SQLite the vector
is stored as packed float32 and scanned in-process. Both are *indexes*:
rebuildable from the record text at any time (``rebuild_index``), never a
source of truth.

Two embedders behind one interface:

* ``InferEmbedder`` — Bedrock through the configured model provider (``/embeddings``, task class
  ``embed``; Titan Text Embeddings v2 at 1024 dims, Cohere multilingual as the
  fallback rung). Non-derivation: nothing it produces enters the record.
* ``HashEmbedder`` — deterministic feature hashing over word uni/bi-grams and
  character trigrams, L2-normalised, 1024 dims. The offline / CI embedder and
  the fallback when Infer is not configured. Honest about what it is: the model
  name stored beside every vector is ``hash-v1``, and retrieval treats it as a
  lexical-semantic leg, not a learned one.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import struct
import time
from array import array
from datetime import datetime, timezone
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.models import graph_projections

log = logging.getLogger("clhear.embeddings")

DIM = 1024
EMBED_TASK_CLASS = "embed"
TITAN_EMBED_V2 = "amazon.titan-embed-text-v2:0"
COHERE_EMBED_MULTI = "cohere.embed-multilingual-v3"
_TOKEN = re.compile(r"[a-z0-9§]+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --------------------------------------------------------------------------- embedders


class HashEmbedder:
    """Feature hashing (uni/bi-grams + char trigrams) → unit vector. Deterministic,
    dependency-free, the same in every process."""

    name = "hash-v1"

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        toks = _TOKEN.findall(text.lower())
        feats = list(toks)
        feats += [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for t in toks:
            if len(t) > 3:
                feats += [f"#{t[i:i + 3]}" for i in range(len(t) - 2)]
        return feats

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for f in self._features(text):
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class InferEmbedder:
    """OpenAI-compatible ``/embeddings`` on the configured model provider; the model actually used is
    what Infer reports (ladder: Titan v2 → Cohere multilingual)."""

    def __init__(self, base_url: str, token: str, *, model: str = TITAN_EMBED_V2, employee_id: str = "clhear-l0",
                 data_class: str = "public", client=None, timeout: float = 60.0):
        self._base_url, self._token, self.model = base_url.rstrip("/"), token, model
        self._employee_id, self._data_class, self._client, self._timeout = employee_id, data_class, client, timeout
        self.name = model

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._http().post(
            f"{self._base_url}/embeddings",
            headers={"authorization": f"Bearer {self._token}", "content-type": "application/json",
                     "X-Employee-Id": self._employee_id, "X-Data-Class": self._data_class, "X-Task-Class": EMBED_TASK_CLASS},
            json={"model": self.model, "input": texts, "dimensions": DIM, "encoding_format": "float",
                  "metadata": {"task_class": EMBED_TASK_CLASS, "employee_id": self._employee_id}},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"infer embeddings {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        self.name = str(data.get("model") or self.model)
        rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
        out = [list(map(float, d["embedding"])) for d in rows]
        if len(out) != len(texts) or any(len(v) != DIM for v in out):
            raise RuntimeError("infer embeddings: wrong shape")
        return out


def embedder(*, provider: str | None = None) -> Embedder:
    """Infer when configured (or forced), else the hash embedder."""
    from app.clhear.settings import get_settings

    s = get_settings()
    choice = provider or s.clhear_embedding_provider
    if choice == "hash":
        return HashEmbedder()
    if choice == "infer" or (choice == "auto" and s.infer_base_url and s.infer_token and s.infer_token != "CHANGEME"):
        return InferEmbedder(s.infer_base_url, s.infer_token, model=s.clhear_embedding_model or TITAN_EMBED_V2,
                             employee_id=s.infer_employee_id, data_class=s.infer_data_class)
    return HashEmbedder()


# --------------------------------------------------------------------------- storage


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob) -> list[float]:
    if blob is None:
        return []
    if isinstance(blob, (bytes, bytearray, memoryview)):
        a = array("f")
        a.frombytes(bytes(blob))
        return a.tolist()
    if isinstance(blob, str):  # pgvector text form "[0.1,0.2,...]" or JSON
        return [float(x) for x in blob.strip("[]").split(",") if x.strip()]
    return [float(x) for x in blob]


def _is_pg(conn: Connection) -> bool:
    return conn.engine.dialect.name == "postgresql"


def _table(conn: Connection) -> str:
    return f"{clauses.schema}.clauses" if _is_pg(conn) and clauses.schema else "clauses"


def _store(conn: Connection, clause_id: int, vec: list[float], model: str, text_hash: str) -> None:
    stamp = datetime.now(timezone.utc)
    if _is_pg(conn):
        conn.execute(sa.text(f"UPDATE {_table(conn)} SET embedding = CAST(:v AS vector), embedding_model = :m, "
                             "embedding_hash = :h, embedded_at = :t WHERE id = :id"),
                     {"v": "[" + ",".join(f"{x:.7g}" for x in vec) + "]", "m": model, "h": text_hash, "t": stamp, "id": clause_id})
    else:
        conn.execute(sa.text("UPDATE clauses SET embedding = :v, embedding_model = :m, embedding_hash = :h, embedded_at = :t WHERE id = :id"),
                     {"v": pack(vec), "m": model, "h": text_hash, "t": stamp, "id": clause_id})


def _indexable(conn: Connection):
    """Public clauses of in-force versions of open sources — the only text the index may hold (I8)."""
    from app.clhear.l1 import permissions
    # A corpus-wide rebuild may be triggered by an unrelated public import.
    # Display permission and legacy public flags do not authorize embedding.
    blocked = [source.id for source in conn.execute(sa.select(sources))
               if permissions.required_for(source) and not permissions.decision(conn, source.key, "embed")["allowed"]]
    return (sa.select(clauses.c.id, clauses.c.text, clauses.c.text_hash, clauses.c.path, clauses.c.embedding_model,
                      clauses.c.embedding_hash, sources.c.short_name)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(source_versions.c.status == "in_force", clauses.c.public_ok.is_(True), sources.c.license == "open")
            .where(sources.c.id.not_in(blocked)))


def rebuild_index(engine: Engine, emb: Embedder | None = None, *, batch: int = 64, force: bool = False,
                  release: str = "", trigger: str = "nightly", limit: int | None = None) -> dict:
    """(Re)embed every indexable clause whose vector is missing, was made by another
    model, or whose text changed since (hash mismatch). Idempotent: a second run
    over the same record embeds nothing. Logged in `graph_projections`."""
    emb = emb or embedder()
    started, started_at = time.perf_counter(), datetime.now(timezone.utc)
    todo: list[tuple[int, str, str]] = []
    total = 0
    with engine.connect() as conn:
        for r in conn.execute(_indexable(conn)).mappings():
            total += 1
            if not force and r["embedding_hash"] == r["text_hash"] and r["embedding_model"] == emb.name:
                continue
            todo.append((r["id"], f"{r['short_name']} · {r['path']}\n{r['text']}", r["text_hash"]))
    if limit is not None:
        todo = todo[:limit]
    embedded, failed = 0, ""
    try:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            vectors = emb.embed([t for _, t, _ in chunk])
            with engine.begin() as conn:
                for (cid, _, h), vec in zip(chunk, vectors):
                    _store(conn, cid, vec, emb.name, h)
                    embedded += 1
    except Exception as exc:
        failed = f"{type(exc).__name__}: {exc}"[:400]
        log.exception("embedding rebuild failed after %s vectors", embedded)
    ms = int((time.perf_counter() - started) * 1000)
    backend = "pgvector" if engine.dialect.name == "postgresql" else "sqlite-vec"
    with engine.begin() as conn:
        conn.execute(graph_projections.insert().values(
            backend=backend, trigger=trigger, release=release, status="failed" if failed else "succeeded",
            started_at=started_at, finished_at=datetime.now(timezone.utc), duration_ms=ms, nodes=total, edges=embedded,
            checksum=emb.name, detail={"model": emb.name, "indexable": total, "embedded": embedded, "skipped": total - len(todo),
                                       "error": failed}))
    _CACHE.pop(str(engine.url), None)
    return {"backend": backend, "model": emb.name, "indexable": total, "embedded": embedded, "skipped": total - len(todo),
            "duration_ms": ms, "error": failed}


# --------------------------------------------------------------------------- query


_CACHE: dict[str, tuple[tuple, list[int], list[array]]] = {}


def _sqlite_matrix(conn: Connection, key: str) -> tuple[list[int], list[array]]:
    """Decode the stored vectors once per (count, max id, last embed) and keep them in-process."""
    fp = conn.execute(sa.text("SELECT count(*), max(id), max(embedded_at) FROM clauses WHERE embedding IS NOT NULL")).one()
    cached = _CACHE.get(key)
    if cached and cached[0] == tuple(fp):
        return cached[1], cached[2]
    ids, mat = [], []
    for cid, blob in conn.execute(sa.text("SELECT id, embedding FROM clauses WHERE embedding IS NOT NULL")):
        a = array("f")
        a.frombytes(bytes(blob))
        ids.append(cid)
        mat.append(a)
    _CACHE[key] = (tuple(fp), ids, mat)
    return ids, mat


def nearest(engine: Engine, query_vec: list[float], *, limit: int = 30, model: str | None = None) -> list[tuple[int, float]]:
    """Clause ids nearest to the query vector (cosine), best first."""
    with engine.connect() as conn:
        if _is_pg(conn):
            q = "[" + ",".join(f"{x:.7g}" for x in query_vec) + "]"
            where = "embedding IS NOT NULL" + (" AND embedding_model = :m" if model else "")
            rows = conn.execute(sa.text(f"SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS score FROM {_table(conn)} "
                                        f"WHERE {where} ORDER BY embedding <=> CAST(:q AS vector) LIMIT :n"),
                                {"q": q, "n": limit, **({"m": model} if model else {})}).all()
            return [(int(r[0]), float(r[1])) for r in rows]
        ids, mat = _sqlite_matrix(conn, str(engine.url))
    if not ids:
        return []
    qv = array("f", query_vec)
    scored = []
    for cid, row in zip(ids, mat):
        if len(row) != len(qv):
            continue
        scored.append((cid, math.fsum(map(float.__mul__, row, qv))))
    scored.sort(key=lambda t: -t[1])
    return scored[:limit]


def semantic_search(engine: Engine, query: str, *, limit: int = 30, emb: Embedder | None = None) -> list[tuple[int, float]]:
    emb = emb or embedder()
    return nearest(engine, emb.embed([query])[0], limit=limit, model=emb.name)


def index_status(engine: Engine) -> dict:
    with engine.connect() as conn:
        total = conn.execute(sa.select(sa.func.count()).select_from(_indexable(conn).subquery())).scalar_one()
        embedded = conn.execute(sa.select(sa.func.count(), sa.func.max(clauses.c.embedded_at), clauses.c.embedding_model)
                                .where(clauses.c.embedding.is_not(None)).group_by(clauses.c.embedding_model)).all()
    return {"backend": "pgvector" if engine.dialect.name == "postgresql" else "sqlite-vec", "dim": DIM, "indexable": total,
            "embedded": [{"model": m, "count": c, "last": str(t) if t else None} for c, t, m in embedded]}
