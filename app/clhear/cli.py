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


def _check_model() -> dict:
    """One small real call through the configured provider."""
    from app.clhear.platform.router import build_providers

    providers = build_providers()
    if not providers or set(providers) == {"fake"}:
        return {"model_check": "skipped", "reason": "no live provider configured"}
    provider = next(iter(providers.values()))
    try:
        result = provider.complete(model="", prompt='Reply with exactly this JSON: {"ok": true}', system=None, max_tokens=64)
    except Exception as exc:  # noqa: BLE001 - the message is the diagnosis
        return {"model_check": "failed", "error": str(exc)[:300]}
    return {"model_check": "ok", "model": result.model, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens}


def cmd_doctor(args) -> int:
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
    if getattr(args, "check_model", False):
        payload.update(_check_model())
    _print(payload)
    if payload.get("model_check") == "failed":
        return 1
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
    lineage = (result["release"].get("blueprints") or {}).get("lineage") or {}
    _print({"run_id": run["run_id"], "status": result["run"]["status"], "release_id": result["release"]["id"],
            "lineage": {"rows": lineage.get("rows"), "anchored": lineage.get("anchored"),
                        "unanchored": len(lineage.get("unanchored") or [])}})
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
        "coverage": len(composition.get("coverage") or []),
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


def cmd_sources_add(args) -> int:
    from app.clhear import hoststore
    from app.clhear.first_run import SOURCE_KINDS

    if args.kind not in SOURCE_KINDS:
        print(f"kind must be one of {', '.join(SOURCE_KINDS)}", file=sys.stderr)
        return 1
    if args.url:
        adapter, locator = "url", {"url": args.url}
    elif args.path:
        adapter, locator = "local_text", {"path": args.path}
    else:
        adapter, locator = "local_text", {"text": Path(args.text_file).read_text(encoding="utf-8")}
    row = hoststore.upsert_source(_engine(), args.key, {
        "adapter": adapter, "locator": locator, "name": args.name or args.key, "kind": args.kind,
        "jurisdiction": args.jurisdiction or "", "issuer": args.issuer or "", "licence": "open"})
    print(f"registered {row['key']} ({adapter}, kind {row['kind']}"
          + (f", {row['jurisdiction']}" if row["jurisdiction"] else "") + ")")
    return 0


def cmd_sources_list(_args) -> int:
    from app.clhear import hoststore

    for row in hoststore.list_sources(_engine()):
        print(f"{row['key']:<24} {row['adapter']:<12} {row['kind']:<12} {row['jurisdiction'] or '-':<6} {row['name']}")
    return 0


def cmd_sources_test(args) -> int:
    from app.clhear.first_run import preview

    found = preview(_engine(), args.key)
    print(f"{found['clauses']} clauses read ({found['bytes']} bytes)")
    for c in found["preview"]:
        print(f"  {c['clause_ref']:<12} {c['text'][:100]}")
    return 0 if found["clauses"] else 1


def cmd_sources_advise(args) -> int:
    from app.clhear.advisor import advise
    from app.clhear.first_run import advice_text

    keys = scopes.get(args.scope)["sources"]
    with _engine().connect() as conn:
        advice = advise(conn, args.scope, keys)
    _print(advice) if args.json else print(advice_text(advice))
    return 0


def cmd_scope_create(args) -> int:
    scope = scopes.put(args.name, list(args.sources))
    print(f"scope {scope['name']}: {', '.join(scope['sources'])}")
    return 0


def cmd_profile_questions(args) -> int:
    from app.clhear.first_run import questions_text
    from app.clhear.l4.validate import profile_schema

    with _engine().connect() as conn:
        schema = profile_schema(conn, args.scope)
    _print(schema) if args.json else print(questions_text(schema))
    return 0


def _answer(pair: str) -> tuple[str, bool]:
    fact, _, value = pair.rpartition("=")
    if not fact or value.lower() not in ("true", "false", "yes", "no"):
        raise ValueError(f"--condition takes 'fact=true' or 'fact=false', not {pair!r}")
    return fact.strip(), value.lower() in ("true", "yes")


def cmd_profile_set(args) -> int:
    from app.clhear.first_run import put_profile

    attributes = _load_json(args.file) if args.file else {}
    attributes = attributes.get("attributes", attributes)
    if args.jurisdiction:
        attributes["jurisdictions"] = list(args.jurisdiction)
    if args.role:
        attributes["roles"] = list(args.role)
    if args.not_role:
        roles = attributes.get("roles") or []
        roles = {r: True for r in roles} if isinstance(roles, list) else dict(roles)
        attributes["roles"] = {**roles, **{r: False for r in args.not_role}}
    if args.condition:
        attributes["conditions"] = {**(attributes.get("conditions") or {}), **dict(_answer(c) for c in args.condition)}
    if args.licence:
        attributes["licences"] = list(args.licence)
    stored = put_profile(_engine(), args.profile_id, name=args.name or args.profile_id, attributes=attributes)
    _print({"profile_id": args.profile_id, "attributes": attributes, "validation": stored["validation"]})
    return 0 if stored["validation"]["valid"] else 1


def cmd_blueprint_show(args) -> int:
    from app.clhear import hoststore
    from app.clhear.api import _public_blueprint
    from app.clhear.first_run import blueprint_text

    row = hoststore.get_release(_engine(), args.release)
    if row is None:
        print(f"unknown release {args.release}", file=sys.stderr)
        return 1
    body = row["blueprints"] if isinstance(row["blueprints"], dict) else {}
    composition = (body.get("profiles") or {}).get(args.profile)
    if composition is None:
        print(f"no blueprint for {args.profile} in {args.release}", file=sys.stderr)
        return 1
    print(blueprint_text(_public_blueprint(composition, profile_id=args.profile)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clhear",
        description="Turn chosen texts and an organisation description into a compliance blueprint.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty directory for scope files").set_defaults(func=cmd_init)
    doctor = sub.add_parser("doctor", help="check the database and whether a live model is configured")
    doctor.add_argument("--check-model", action="store_true", help="also make one small real call to the model")
    doctor.set_defaults(func=cmd_doctor)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("version", help="print the engine tag").set_defaults(func=cmd_version)
    sub.add_parser("quickstart", help="write a sample blueprint on this machine").set_defaults(func=cmd_quickstart)

    build = sub.add_parser("build", help="build a blueprint for one scope")
    build.add_argument("--scope", required=True)
    build.add_argument("--profile", action="append", default=[])
    build.set_defaults(func=cmd_build)

    run = sub.add_parser("run", help="build a blueprint for a scope and store the release")
    run.add_argument("--scope", required=True)
    run.add_argument("--profile-id", action="append", default=[])
    run.add_argument("--queue-only", action="store_true")
    run.set_defaults(func=cmd_run)

    release = sub.add_parser("release", help="write a stored release to a directory")
    release.add_argument("--release", required=True)
    release.add_argument("--out", default="artifacts")
    release.set_defaults(func=cmd_release)

    validate = sub.add_parser("validate", help="check an organisation profile or a contribution proposal")
    validate.add_argument("file")
    validate.add_argument("--contribution", action="store_true")
    validate.set_defaults(func=cmd_validate)

    export = sub.add_parser("export", help="write one blueprint as JSON")
    export.add_argument("--release", required=True)
    export.add_argument("--profile", required=True)
    export.add_argument("--out", default="")
    export.set_defaults(func=cmd_export)

    compose = sub.add_parser("compose", help="compose a blueprint for a stored organisation profile")
    compose.add_argument("--profile", required=True)
    compose.set_defaults(func=cmd_compose)

    sources = sub.add_parser("sources", help="register the official texts you want read").add_subparsers(
        dest="sources_command", required=True)
    add = sources.add_parser("add", help="register a text: a file under CLHEAR_LOCAL_SOURCES_DIR, a URL, or a text file")
    add.add_argument("key")
    where = add.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", help="a text, HTML or PDF file, relative to CLHEAR_LOCAL_SOURCES_DIR")
    where.add_argument("--url", help="a public https page or PDF")
    where.add_argument("--text-file", help="a local text file whose contents are stored with the source")
    add.add_argument("--kind", default="regulation",
                     help="law, regulation, standard, guidance, form, agreement or enforcement (default regulation)")
    add.add_argument("--jurisdiction", default="", help="the jurisdiction the text is law in, e.g. US or EU")
    add.add_argument("--issuer", default="", help="who publishes it, e.g. the regulator")
    add.add_argument("--name", default="")
    add.set_defaults(func=cmd_sources_add)
    sources.add_parser("list", help="list registered sources").set_defaults(func=cmd_sources_list)
    test = sources.add_parser("test", help="read a source and preview its clauses; stores nothing")
    test.add_argument("key")
    test.set_defaults(func=cmd_sources_test)
    advise = sources.add_parser("advise", help="which official sources to add so every layer can produce records")
    advise.add_argument("--scope", required=True)
    advise.add_argument("--json", action="store_true")
    advise.set_defaults(func=cmd_sources_advise)

    scope = sub.add_parser("scope", help="name a set of sources that belong in one program").add_subparsers(
        dest="scope_command", required=True)
    create = scope.add_parser("create", help="create or replace a scope")
    create.add_argument("name")
    create.add_argument("sources", nargs="+")
    create.set_defaults(func=cmd_scope_create)

    profile = sub.add_parser("profile", help="describe an organisation by answering the texts' questions").add_subparsers(
        dest="profile_command", required=True)
    questions = profile.add_parser("questions", help="the questions a built scope's texts raise, and profiles to start from")
    questions.add_argument("--scope", required=True)
    questions.add_argument("--json", action="store_true")
    questions.set_defaults(func=cmd_profile_questions)
    pset = profile.add_parser("set", help="store a profile from a JSON file and/or flags")
    pset.add_argument("profile_id")
    pset.add_argument("--file", default="")
    pset.add_argument("--name", default="")
    pset.add_argument("--jurisdiction", action="append", default=[])
    pset.add_argument("--role", action="append", default=[], help="a role you are (repeatable)")
    pset.add_argument("--not-role", action="append", default=[], help="a role you are not (repeatable)")
    pset.add_argument("--condition", action="append", default=[], help="'fact=true' or 'fact=false' (repeatable)")
    pset.add_argument("--licence", action="append", default=[])
    pset.set_defaults(func=cmd_profile_set)

    blueprint = sub.add_parser("blueprint", help="read a blueprint").add_subparsers(dest="blueprint_command", required=True)
    show = blueprint.add_parser("show", help="a blueprint as text: measures, duties, open questions, sources to add")
    show.add_argument("--release", required=True)
    show.add_argument("--profile", required=True)
    show.set_defaults(func=cmd_blueprint_show)

    serve = sub.add_parser("serve", help="serve the HTTP API on this machine")
    serve.add_argument("--host", default=os.environ.get("CLHEAR_BIND_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("CLHEAR_PORT", "8000")))
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="build blueprints for queued runs")
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
