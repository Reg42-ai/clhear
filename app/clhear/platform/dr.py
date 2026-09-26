# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Disaster-recovery drills (HLD v2 §7.1 "Status page with SLOs; DR drills"; item 17).

A backup nobody has restored is a hope, not a control. Every night a scheduled
job takes the record's backup, restores it into a *scratch* target, and proves
three things against the live record:

* **record** — every layer table restored row-for-row (counts match), the
  migration ledger is identical, and every ``why_trail_id`` on the restored
  rows resolves (I2: no orphaned reasoning after a restore);
* **graph** — the Neo4j / local projection rebuilt *from the restored record*
  has the same checksum as the projection of the live record (the graph is
  derived state and must be reproducible from the record alone);
* **datalake** — the cross-region S3 replica carries an enabled replication
  rule, versioning, and a sample of the source keys with matching ETags.

The drill measures RPO (age of the backup it restored) and RTO (wall time of
restore + verification) against the targets in :data:`TARGETS`, records every
run in ``l0_platform.dr_drills`` (append-only) and emits ``clhear.l0.dr_drill``
so the status page and the alarm see it. Works on SQLite (backup API) for
tests and offline drills and on Postgres (``pg_dump`` / ``pg_restore``) in
production; the scratch target is never the live record.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import L0_SCHEMA, metadata, schema_migrations
from app.clhear.platform import events, record
from app.clhear.platform.shared_schema import Json

log = logging.getLogger("clhear.dr")

PRODUCER = "platform.dr"
EVENT_KIND = "clhear.l0.dr_drill"

# HLD v2 §7.1 — the objectives the drill is judged against.
TARGETS = {
    "rpo_seconds": 24 * 3600,  # nightly backup → at most one day of derivations at risk
    "rto_seconds": 4 * 3600,  # restore + verify inside four hours
    "drill_max_age_seconds": 48 * 3600,  # a drill older than two days counts as "not drilled"
}

dr_drills = sa.Table(
    "dr_drills",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("release", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("source_dialect", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("scratch_target", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("status", sa.Text, nullable=False),  # passed | failed
    sa.Column("rpo_seconds", sa.Integer, nullable=True),
    sa.Column("rto_seconds", sa.Integer, nullable=True),
    sa.Column("checks", Json, nullable=False, default=dict),  # record / graph / datalake → {passed, ...}
    sa.Column("trigger", sa.Text, nullable=False, default="schedule", server_default="schedule"),
    schema=L0_SCHEMA,
)
DR_TABLES = (dr_drills,)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(url: str) -> str:
    """Never log a database password; keep the shape so the report is useful."""
    try:
        u = sa.engine.make_url(url)
        return u.render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return url.split("@")[-1]


# --------------------------------------------------------------------------- backup / restore


def _sqlite_path(engine: Engine) -> Path:
    db = engine.url.database
    if not db or db == ":memory:":
        raise RuntimeError("DR drill needs a file-backed SQLite database")
    return Path(db)


def backup(engine: Engine, dest: Path) -> dict:
    """Take a consistent backup of the record into ``dest``.

    SQLite: the online backup API (consistent snapshot while writers continue).
    Postgres: ``pg_dump -Fc`` of the whole database (every layer schema).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    taken_at = _now()
    if engine.dialect.name == "sqlite":
        src = sqlite3.connect(str(_sqlite_path(engine)))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    elif engine.dialect.name == "postgresql":
        url = engine.url.render_as_string(hide_password=False).replace("+psycopg", "").replace("+psycopg2", "")
        subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(dest), url],
                       check=True, capture_output=True, text=True, timeout=3600)
    else:  # pragma: no cover - only sqlite and postgres are supported record stores
        raise RuntimeError(f"unsupported dialect for DR backup: {engine.dialect.name}")
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    return {"path": str(dest), "bytes": dest.stat().st_size, "sha256": digest, "taken_at": taken_at.isoformat(),
            "format": "sqlite-backup" if engine.dialect.name == "sqlite" else "pg_dump-custom"}


def restore(dump: Path, scratch_url: str) -> Engine:
    """Restore ``dump`` into the scratch target and return an engine on it.

    The scratch target must not be the live record — the caller guards that;
    this function refuses a SQLite path that already exists with content.
    """
    from app.clhear.db import make_engine

    if scratch_url.startswith("sqlite"):
        target = Path(sa.engine.make_url(scratch_url).database or "")
        if target.exists() and target.stat().st_size:
            raise RuntimeError(f"scratch target {target} is not empty — refusing to restore over it")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dump, target)
    elif scratch_url.startswith("postgresql"):
        _ensure_postgres_database(scratch_url)
        url = scratch_url.replace("+psycopg", "").replace("+psycopg2", "")
        subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "--dbname", url, str(dump)],
                       check=True, capture_output=True, text=True, timeout=3600)
    else:  # pragma: no cover
        raise RuntimeError(f"unsupported scratch target: {_redact(scratch_url)}")
    return make_engine(scratch_url)


def _ensure_postgres_database(scratch_url: str) -> None:
    """Create the scratch database on the cluster if it does not exist yet (never touches the live one)."""
    url = sa.engine.make_url(scratch_url)
    admin = sa.create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            exists = conn.execute(sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


def default_scratch_url(live_url: str) -> str:
    """Postgres: the ``<db>_drill`` database on the same cluster; SQLite: caller supplies a path."""
    url = sa.engine.make_url(live_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("default scratch target only defined for Postgres")
    return url.set(database=f"{url.database}_drill").render_as_string(hide_password=False)


# --------------------------------------------------------------------------- verification


def _platform_tables() -> list[sa.Table]:
    from app.clhear.platform.audit import audit_log

    return [record.why_trails, audit_log, schema_migrations]


def _count(conn, table: sa.Table) -> int | None:
    try:
        return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar() or 0)
    except sa.exc.DBAPIError:
        return None  # table absent on this side → reported as a mismatch below


def verify_record(source: Engine, restored: Engine) -> dict:
    """Row counts per layer table, identical migration ledger, resolvable why-trails."""
    mismatches: list[dict] = []
    counts: dict[str, dict] = {}
    tables = list(record.layer_tables()) + _platform_tables()
    with source.connect() as s, restored.connect() as r:
        for t in tables:
            a, b = _count(s, t), _count(r, t)
            counts[t.name] = {"source": a, "restored": b}
            if a != b:
                mismatches.append({"table": t.name, "source": a, "restored": b})
        src_mig = sorted((row.version, row.name) for row in s.execute(sa.select(schema_migrations.c.version, schema_migrations.c.name)))
        dst_mig = sorted((row.version, row.name) for row in r.execute(sa.select(schema_migrations.c.version, schema_migrations.c.name)))
        migrations_equal = src_mig == dst_mig
        trail_ids = {row[0] for row in r.execute(sa.select(record.why_trails.c.id))}
        dangling: list[dict] = []
        checked_trails = 0
        for t in record.layer_tables():
            if "why_trail_id" not in t.c:
                continue
            try:
                ids = [row[0] for row in r.execute(sa.select(t.c.why_trail_id).where(t.c.why_trail_id.isnot(None)).distinct())]
            except sa.exc.DBAPIError:
                continue
            checked_trails += len(ids)
            for wid in ids:
                if wid not in trail_ids:
                    dangling.append({"table": t.name, "why_trail_id": wid})
                    if len(dangling) >= 25:
                        break
    passed = not mismatches and migrations_equal and not dangling
    return {"passed": passed, "tables_checked": len(tables), "rows_source": sum(v["source"] or 0 for v in counts.values()),
            "rows_restored": sum(v["restored"] or 0 for v in counts.values()), "mismatches": mismatches,
            "migrations_equal": migrations_equal, "migrations": len(dst_mig), "why_trails_checked": checked_trails,
            "dangling_why_trails": dangling}


def verify_graph(source: Engine, restored: Engine, *, neo4j_database: str | None = None) -> dict:
    """The projection built from the restored record must equal the live one."""
    from app.clhear.platform import graph

    live = graph.project(source)
    rebuilt = graph.project(restored)
    same = live.checksum() == rebuilt.checksum()
    out = {"passed": same, "backend": "local", "checksum_live": live.checksum(), "checksum_restored": rebuilt.checksum(),
           "nodes": rebuilt.counts()["nodes"], "edges": rebuilt.counts()["edges"]}
    scratch = graph.LocalGraph()
    loaded = scratch.rebuild(rebuilt)
    out["loaded"] = {"nodes": loaded["nodes"], "edges": loaded["edges"]}
    uri = os.environ.get("CLHEAR_NEO4J_URI", "")
    if uri and neo4j_database:
        # Restore drill against a real Neo4j: load the scratch database (never the
        # serving one) and compare what came back with what we sent.
        try:
            g = graph.Neo4jGraph(uri, os.environ.get("CLHEAR_NEO4J_USER", "neo4j"), os.environ.get("CLHEAR_NEO4J_PASSWORD", ""),
                                 database=neo4j_database)
            summary = g.rebuild(rebuilt)
            out["backend"] = "neo4j"
            out["neo4j"] = {"database": neo4j_database, **{k: summary.get(k) for k in ("nodes", "edges", "checksum")}}
            if summary.get("nodes") != rebuilt.counts()["nodes"] or summary.get("edges") != rebuilt.counts()["edges"]:
                out["passed"] = False
                out["error"] = "neo4j scratch load count mismatch"
        except Exception as exc:  # noqa: BLE001 — a failed load is a failed drill, not a crash
            out["passed"] = False
            out["backend"] = "neo4j"
            out["error"] = f"{type(exc).__name__}: {exc}"[:400]
    return out


def verify_datalake(bucket: str, replica: str, *, client=None, sample: int = 25) -> dict:
    """Cross-region replica: rule enabled, versioning on, sampled keys present with equal ETags."""
    if client is None:
        try:
            import boto3

            client = boto3.client("s3")
        except Exception as exc:  # noqa: BLE001
            return {"passed": False, "skipped": True, "error": f"no S3 client: {exc}"}
    out: dict = {"passed": True, "bucket": bucket, "replica": replica, "sampled": 0, "missing": [], "etag_mismatch": []}
    try:
        cfg = client.get_bucket_replication(Bucket=bucket)["ReplicationConfiguration"]
        rules = [r for r in cfg.get("Rules", []) if r.get("Status") == "Enabled"]
        targets = {r.get("Destination", {}).get("Bucket", "").split(":::")[-1] for r in rules}
        out["replication_rule_enabled"] = bool(rules)
        out["replication_targets_replica"] = replica in targets
        if not rules or replica not in targets:
            out["passed"] = False
        out["replica_versioning"] = client.get_bucket_versioning(Bucket=replica).get("Status") == "Enabled"
        if not out["replica_versioning"]:
            out["passed"] = False
        listed = client.list_objects_v2(Bucket=bucket, MaxKeys=sample).get("Contents", [])
        for obj in listed:
            out["sampled"] += 1
            try:
                head = client.head_object(Bucket=replica, Key=obj["Key"])
            except Exception:  # noqa: BLE001 — a missing object is a finding, not a crash
                out["missing"].append(obj["Key"])
                continue
            if head.get("ETag") != obj.get("ETag"):
                out["etag_mismatch"].append(obj["Key"])
        if out["missing"] or out["etag_mismatch"]:
            out["passed"] = False
    except Exception as exc:  # noqa: BLE001
        out["passed"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"[:400]
    return out


# --------------------------------------------------------------------------- the drill


def run(engine: Engine, *, scratch_url: str | None = None, workdir: Path | None = None, release: str = "",
        datalake: tuple[str, str] | None = None, s3_client=None, neo4j_database: str | None = None,
        trigger: str = "schedule", skip_datalake: bool = False) -> dict:
    """Backup → restore into scratch → verify record, graph, datalake → record the drill."""
    started = _now()
    t0 = time.perf_counter()
    workdir = Path(workdir or tempfile.mkdtemp(prefix="clhear-dr-"))
    live_url = engine.url.render_as_string(hide_password=False)
    scratch_url = scratch_url or os.environ.get("CLHEAR_DR_SCRATCH_URL") or (
        default_scratch_url(live_url) if engine.dialect.name == "postgresql" else f"sqlite:///{workdir}/restored.db")
    if sa.engine.make_url(scratch_url) == sa.engine.make_url(live_url):
        raise RuntimeError("DR drill scratch target must not be the live record")
    checks: dict[str, dict] = {}
    bk: dict = {}
    status = "passed"
    restored: Engine | None = None
    try:
        bk = backup(engine, workdir / ("record.db" if engine.dialect.name == "sqlite" else "record.dump"))
        restored = restore(Path(bk["path"]), scratch_url)
        checks["record"] = verify_record(engine, restored)
        checks["graph"] = verify_graph(engine, restored, neo4j_database=neo4j_database)
    except Exception as exc:  # noqa: BLE001 — the drill reports; it never takes the service down
        log.exception("DR drill failed")
        checks["restore"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        if restored is not None:
            restored.dispose()
    if skip_datalake:
        checks["datalake"] = {"passed": True, "skipped": True, "reason": "skip_datalake"}
    else:
        datalake = datalake or _datalake_from_env()
        if datalake:
            checks["datalake"] = verify_datalake(datalake[0], datalake[1], client=s3_client)
        else:
            checks["datalake"] = {"passed": True, "skipped": True, "reason": "no replica configured (CLHEAR_DATALAKE_REPLICA_BUCKET)"}
    finished = _now()
    rto = int(time.perf_counter() - t0)
    rpo = int((finished - datetime.fromisoformat(bk["taken_at"])).total_seconds()) if bk else None
    objectives = {"rpo_ok": rpo is not None and rpo <= TARGETS["rpo_seconds"], "rto_ok": rto <= TARGETS["rto_seconds"]}
    if not all(c.get("passed") for c in checks.values()) or not all(objectives.values()):
        status = "failed"
    report = {"status": status, "started_at": started.isoformat(), "finished_at": finished.isoformat(), "release": release,
              "source_dialect": engine.dialect.name, "scratch_target": _redact(scratch_url), "backup": bk,
              "rpo_seconds": rpo, "rto_seconds": rto, "targets": TARGETS, "objectives": objectives, "checks": checks,
              "trigger": trigger}
    with engine.begin() as conn:
        conn.execute(dr_drills.insert().values(
            started_at=started, finished_at=finished, release=release, source_dialect=engine.dialect.name,
            scratch_target=_redact(scratch_url), status=status, rpo_seconds=rpo, rto_seconds=rto,
            checks={**checks, "objectives": objectives}, trigger=trigger))
        events.emit(conn, layer="l0", kind=EVENT_KIND, subject_ref=release or started.date().isoformat(),
                    payload={"status": status, "rpo_seconds": rpo, "rto_seconds": rto,
                             "checks": {k: bool(v.get("passed")) for k, v in checks.items()}}, producer=PRODUCER)
    _publish_metric(status == "passed")
    return report


def _datalake_from_env() -> tuple[str, str] | None:
    replica = os.environ.get("CLHEAR_DATALAKE_REPLICA_BUCKET", "")
    if not replica:
        return None
    from app.clhear.settings import get_settings

    return get_settings().clhear_datalake_bucket, replica


def _publish_metric(passed: bool) -> None:
    try:
        import boto3

        from app.clhear.settings import get_settings

        boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
            Namespace="CLHEAR", MetricData=[{"MetricName": "DrDrillPassed", "Value": 1 if passed else 0, "Unit": "Count"}])
    except Exception:  # noqa: BLE001
        log.debug("DR metric not published (no AWS)", exc_info=True)


def last_drill(engine: Engine, *, now: datetime | None = None) -> dict:
    """What the status page shows: the latest drill, its age against the target, and whether it passed."""
    now = now or _now()
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.select(dr_drills).order_by(dr_drills.c.id.desc()).limit(1)).mappings().first()
    except sa.exc.DBAPIError:
        row = None
    if row is None:
        return {"drilled": False, "passed": False, "age_seconds": None, "max_age_seconds": TARGETS["drill_max_age_seconds"],
                "rpo_seconds": None, "rto_seconds": None, "targets": TARGETS}
    fin = row["finished_at"] or row["started_at"]
    if isinstance(fin, str):
        fin = datetime.fromisoformat(fin)
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=timezone.utc)
    age = max(0, int((now - fin).total_seconds()))
    checks = row["checks"] if isinstance(row["checks"], dict) else json.loads(row["checks"] or "{}")
    return {"drilled": age <= TARGETS["drill_max_age_seconds"], "passed": row["status"] == "passed", "status": row["status"],
            "finished_at": fin.isoformat(), "age_seconds": age, "max_age_seconds": TARGETS["drill_max_age_seconds"],
            "rpo_seconds": row["rpo_seconds"], "rto_seconds": row["rto_seconds"], "release": row["release"],
            "checks": {k: bool(v.get("passed")) for k, v in checks.items() if isinstance(v, dict) and "passed" in v},
            "targets": TARGETS}


def history(engine: Engine, limit: int = 30) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(dr_drills).order_by(dr_drills.c.id.desc()).limit(limit)).mappings()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("started_at", "finished_at"):
                d[k] = str(d[k]) if d[k] else None
            out.append(d)
        return out


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    """python -m app.clhear.platform.dr run [--scratch-url URL] [--release R] [--report PATH] [--skip-datalake] | last | history"""
    import argparse

    from app.clhear.db import get_engine, run_migrations

    p = argparse.ArgumentParser(prog="clhear-dr")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--scratch-url", default=None)
    r.add_argument("--release", default=os.environ.get("CLHEAR_RELEASE", ""))
    r.add_argument("--report", default=None)
    r.add_argument("--workdir", default=None)
    r.add_argument("--neo4j-database", default=os.environ.get("CLHEAR_DR_NEO4J_DATABASE") or None)
    r.add_argument("--skip-datalake", action="store_true")
    r.add_argument("--trigger", default="schedule")
    sub.add_parser("last")
    sub.add_parser("history")
    args = p.parse_args(argv)
    engine = get_engine()
    run_migrations(engine)
    if args.cmd == "run":
        report = run(engine, scratch_url=args.scratch_url, release=args.release, workdir=Path(args.workdir) if args.workdir else None,
                     neo4j_database=args.neo4j_database, skip_datalake=args.skip_datalake, trigger=args.trigger)
        text = json.dumps(report, indent=2, default=str)
        if args.report:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(text)
        print(text)
        return 0 if report["status"] == "passed" else 1
    if args.cmd == "last":
        print(json.dumps(last_drill(engine), indent=2, default=str))
        return 0
    print(json.dumps(history(engine), indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
