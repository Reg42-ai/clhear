# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""clhear command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from app.clhear.l1 import scopes
from app.clhear.providers import describe
from app.clhear.settings import get_settings


def _engine():
    from app.clhear.runtime import engine

    return engine()


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_init(_args) -> int:
    root = scopes.directory()
    root.mkdir(parents=True, exist_ok=True)
    print(root.resolve())
    return 0


def cmd_version(_args) -> int:
    version = get_settings().clhear_engine_version
    print(version if version.startswith("v") else f"v{version}")
    return 0


def cmd_doctor(_args) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    status = describe()
    database = "ok"
    try:
        _engine()
    except SQLAlchemyError as exc:
        database = str(exc)
    root = scopes.directory()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "database": database,
        "scopes": str(root.resolve()),
        "provider": status["provider"] or "unconfigured",
        "live_run": "ready" if status["live"] else "blocked",
    }
    _print(payload)
    if database != "ok":
        return 1
    if status["provider"] in {"anthropic", "openai_compatible", "bedrock"} and not status["configured"]:
        return 1
    if not status["configured"]:
        return 1
    return 0


def cmd_migrate(_args) -> int:
    from app.clhear.db import run_migrations
    from app.clhear.runtime import engine

    applied = run_migrations(engine())
    _print({"applied": applied})
    return 0


def cmd_build(args) -> int:
    os.environ[scopes.SCOPE_ENV] = args.scope
    from app.clhear import scope_build

    handle = _engine()
    profiles = [_load_json(path) for path in args.profile]
    report = scope_build.build(handle, scope_build._router(handle), profiles=profiles)
    _print(report)
    return 0


def cmd_run(args) -> int:
    from app.clhear import hoststore
    from app.clhear.runner import execute

    scopes.get(args.scope)
    handle = _engine()
    run = hoststore.create_run(handle, args.scope, list(args.profile_id or []))
    if args.queue_only:
        _print({"run_id": run["run_id"], "status": run["status"]})
        return 0
    result = execute(handle, run)
    _print({"run_id": run["run_id"], "status": result["run"]["status"], "release_id": result["release"]["id"]})
    return 0


def cmd_release(args) -> int:
    from app.clhear import hoststore

    handle = _engine()
    row = hoststore.get_release(handle, args.release)
    if row is None:
        print(f"unknown release {args.release}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{row['id']}.json"
    target.write_text(json.dumps(row["blueprints"], indent=2, default=str), encoding="utf-8")
    print(target.resolve())
    return 0


def cmd_validate(args) -> int:
    payload = _load_json(args.file)
    if args.contribution:
        from app.clhear.contribution import ProposalRejected, parse_proposal

        try:
            proposal = parse_proposal(payload)
        except ProposalRejected as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print(proposal.model_dump())
        return 0
    from app.clhear.l4.validate import validate

    attributes = payload.get("attributes", payload)
    _print(validate(_engine(), attributes))
    return 0


def cmd_export(args) -> int:
    from app.clhear import hoststore

    row = hoststore.get_release(_engine(), args.release)
    if row is None:
        print(f"unknown release {args.release}", file=sys.stderr)
        return 1
    body = row["blueprints"] if isinstance(row["blueprints"], dict) else {}
    composition = (body.get("profiles") or {}).get(args.profile)
    if composition is None:
        print(f"unknown profile {args.profile}", file=sys.stderr)
        return 1
    text = json.dumps(composition, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(Path(args.out).resolve())
    else:
        print(text)
    return 0


def cmd_compose(args) -> int:
    from app.clhear.l6 import composer

    result = composer.compose_for_profile(_engine(), args.profile)
    _print({
        "blueprint_id": result.get("blueprint_id"),
        "items": result.get("items"),
        "coverage": result.get("coverage"),
        "minimality": result.get("minimality"),
    })
    return 0


def cmd_quickstart(_args) -> int:
    os.environ["CLHEAR_LLM_PROVIDER"] = "fake"
    get_settings.cache_clear()
    root = scopes.directory()
    root.mkdir(parents=True, exist_ok=True)
    from app.clhear import hoststore
    from app.clhear.runner import execute

    handle = _engine()
    hoststore.upsert_source(handle, "example-source", {
        "adapter": "local_text",
        "locator": {"text": "An organisation must keep a record of each decision and the reason for it."},
        "licence": "open",
        "name": "Example source",
        "kind": "guidance",
        "jurisdiction": "",
        "issuer": "Example issuer",
    })
    scopes.put("example-scope", ["example-source"], label="Example scope")
    hoststore.put_profile(handle, "example-profile", name="Example profile", attributes={"jurisdictions": [], "channels": []})
    run = hoststore.create_run(handle, "example-scope", ["example-profile"])
    result = execute(handle, run)
    composition = result["release"]["blueprints"]["profiles"]["example-profile"]
    _print({
        "run_id": run["run_id"],
        "release_id": result["release"]["id"],
        "blueprint_id": composition.get("blueprint_id"),
        "items": len(composition.get("items") or []),
        "layers": sorted((result["release"]["blueprints"].get("layers") or {}).keys()),
    })
    return 0


def cmd_serve(args) -> int:
    from app.clhear.service_auth import auth_required, tokens

    if auth_required() and not tokens():
        print("refusing to serve: bind is not loopback and no service token is configured", file=sys.stderr)
        return 1
    import uvicorn

    uvicorn.run("app.clhear.api:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_worker(args) -> int:
    from app.clhear.runner import work_once

    handle = _engine()
    while True:
        worked = work_once(handle)
        if args.once:
            return 0
        if worked is None:
            time.sleep(args.poll)
        else:
            time.sleep(0)


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clhear")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty scopes directory").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check the database and the model provider").set_defaults(func=cmd_doctor)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("version", help="print the engine tag").set_defaults(func=cmd_version)
    sub.add_parser("quickstart", help="write sample layers with the offline provider").set_defaults(func=cmd_quickstart)

    build = sub.add_parser("build", help="derive L1 through L8 for one scope")
    build.add_argument("--scope", required=True)
    build.add_argument("--profile", action="append", default=[])
    build.set_defaults(func=cmd_build)

    run = sub.add_parser("run", help="queue a run and execute it")
    run.add_argument("--scope", required=True)
    run.add_argument("--profile-id", action="append", default=[])
    run.add_argument("--queue-only", action="store_true")
    run.set_defaults(func=cmd_run)

    release = sub.add_parser("release", help="write a stored release to a directory")
    release.add_argument("--release", required=True)
    release.add_argument("--out", default="artifacts")
    release.set_defaults(func=cmd_release)

    validate = sub.add_parser("validate", help="validate a profile or a contribution proposal")
    validate.add_argument("file")
    validate.add_argument("--contribution", action="store_true")
    validate.set_defaults(func=cmd_validate)

    export = sub.add_parser("export", help="write one blueprint as JSON")
    export.add_argument("--release", required=True)
    export.add_argument("--profile", required=True)
    export.add_argument("--out", default="")
    export.set_defaults(func=cmd_export)

    compose = sub.add_parser("compose", help="compose a stored applicability profile")
    compose.add_argument("--profile", required=True)
    compose.set_defaults(func=cmd_compose)

    serve = sub.add_parser("serve", help="serve the HTTP API")
    serve.add_argument("--host", default=os.environ.get("CLHEAR_BIND_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("CLHEAR_PORT", "8000")))
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="execute queued runs")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll", type=float, default=2.0)
    worker.set_defaults(func=cmd_worker)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
