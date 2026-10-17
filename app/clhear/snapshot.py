# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Reference blueprint snapshots: one release's reference blueprints as files anyone
can download, check and match against an organisation profile.

A snapshot holds, for one release of one scope:

* ``manifest.json``: the format, title, licence, notice, release, scope, engine
  version, the models the run used, each layer's revision, the profiles, the
  sources, and the SHA-256 of every other file;
* ``blueprints/<profile>.json``: each profile's reference blueprint, in the shape
  ``GET /v1/releases/{release_id}/blueprints/{profile_id}`` returns;
* ``applicability.json``: every obligation in scope, the conditions that decide
  whether it applies, and its verdict for each profile of the snapshot;
* ``questions.json``: the questions the texts in scope raise (jurisdictions,
  roles, conditions): what a profile answers;
* ``sources.json``: each source in scope with its version and the hash of the
  text that was read;
* ``risk.json``: Layer 7 of the run: the penalties the binding texts state, with
  their quotes and the obligations they refer to, and each obligation's risk score
  under the published method;
* ``practices.json``: Layer 8 of the run: the practices read from the texts in scope
  that do not bind, each quoted, with the component it concerns;
* ``recipe.json``: the sources, the scope and the profiles, to run the scope
  again (``clhear recipe apply``);
* ``README.md``: the same for people.

A profile matches a snapshot profile when every obligation in scope gets the same
verdict (applies, not applicable, undetermined) for both: then the snapshot's
reference blueprint for that profile is the one for the reader's profile, as
published. Matching reads only the files; it calls no model and needs no database.
Nothing in a snapshot comes from an organisation's own program.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

FORMAT = "clhear-blueprint-snapshot/1"
NOT_FOR_PUBLICATION = "not-for-publication"
# What a .zip may hold before it is read: a snapshot has a few files per profile.
MAX_FILES = 10_000
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class SnapshotError(ValueError):
    """A snapshot cannot be made, read or matched; the message says why."""


# ----------------------------------------------------------------- captured with each run


def last_call_id(engine: Engine) -> int:
    """The id of the latest model call, so a run can name the models it used."""
    from app.clhear.models import llm_calls

    with engine.connect() as conn:
        return int(conn.execute(sa.select(sa.func.max(llm_calls.c.id))).scalar() or 0)


def capture(engine: Engine, source_keys, *, calls_after: int = 0) -> dict:
    """What a snapshot of this run needs beyond its compositions: the conditions of every
    obligation in scope, the questions the texts raise, the sources as read, the models used,
    and Layers 7 and 8 as the run left them (a later run rewrites those tables)."""
    from app.clhear.l4.predicates import LIVE_STATUS, canonical_in, edges_by_obligation, questions
    from app.clhear.derived_models import obligations
    from app.clhear.models import llm_calls

    keys = sorted(set(source_keys or []))
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(obligations).where(
            obligations.c.source_key.in_(keys), obligations.c.status.in_(LIVE_STATUS))).mappings()]
        edges = edges_by_obligation(conn)
        applicability = []
        for ob in sorted(canonical_in(rows), key=lambda o: (o["source_key"], o["clause_ref"] or "", o["id"])):
            applicability.append({
                "obligation_id": ob["id"], "stable_id": ob.get("stable_id"), "source_key": ob["source_key"],
                "clause_ref": ob.get("clause_ref"), "title": ob.get("title") or "",
                "conditions": [{"predicate": e["predicate"], "basis": e.get("basis"), "rationale": e.get("rationale")}
                               for e in sorted(edges.get(ob["id"], []), key=lambda e: json.dumps(e["predicate"], sort_keys=True))],
            })
        asked = questions(conn, keys)
        models = [{"provider": r.provider, "model": r.model, "calls": int(r.calls), "input_tokens": int(r.tin or 0),
                   "output_tokens": int(r.tout or 0)}
                  for r in conn.execute(sa.select(llm_calls.c.provider, llm_calls.c.model,
                                                  sa.func.count().label("calls"),
                                                  sa.func.sum(llm_calls.c.input_tokens).label("tin"),
                                                  sa.func.sum(llm_calls.c.output_tokens).label("tout"))
                                        .where(llm_calls.c.id > calls_after)
                                        .group_by(llm_calls.c.provider, llm_calls.c.model)
                                        .order_by(llm_calls.c.provider, llm_calls.c.model))]
        versions = _source_versions(conn, keys)
        risk = _risk(conn, keys, {ob["obligation_id"] for ob in applicability})
        practices = _practices(conn)
    return {"applicability": applicability, "questions": _plain(asked), "source_versions": versions, "models": models,
            "risk": risk, "practices": practices}


def _risk(conn, keys: list[str], obligation_ids: set[str]) -> dict:
    """Layer 7: each penalty a binding text in scope states, quoted, with the obligations it
    refers to; and each obligation's current risk score, with the method that made it."""
    from app.clhear.l7 import models as l7

    links: dict[str, list[dict]] = {}
    for r in conn.execute(sa.select(l7.penalty_links).where(l7.penalty_links.c.valid_to.is_(None))).mappings():
        if r["obligation_id"] in obligation_ids:
            links.setdefault(r["penalty_id"], []).append(
                {"obligation_id": r["obligation_id"], "method": r["method"], "via": _plain(r["via"])})
    penalties = []
    for r in conn.execute(sa.select(l7.stated_penalties).where(
            l7.stated_penalties.c.source_key.in_(keys), l7.stated_penalties.c.valid_to.is_(None))
            .order_by(l7.stated_penalties.c.source_key, l7.stated_penalties.c.clause_ref, l7.stated_penalties.c.id)).mappings():
        penalties.append({"penalty_id": r["id"], "source_key": r["source_key"], "clause_ref": r["clause_ref"],
                          "penalty_type": r["penalty_type"], "maximum": r["maximum"], "amount": r["amount"],
                          "unit": r["unit"], "evidence": _plain(r["evidence"]),
                          "obligations": sorted(links.get(r["id"], []), key=lambda x: (x["obligation_id"], x["method"]))})
    scores = []
    for r in conn.execute(sa.select(l7.risk_scores).where(
            l7.risk_scores.c.subject_kind == "obligation", l7.risk_scores.c.status == "current")
            .order_by(l7.risk_scores.c.subject_ref)).mappings():
        if r["subject_ref"] in obligation_ids:
            scores.append({"obligation_id": r["subject_ref"], "composite": r["composite"], "band": r["band"],
                           "dimensions": _plain(r["dimensions"]), "method_version": r["method_version"],
                           "calibration": r["calibration_set_ref"], "evidence": _plain(r["evidence"])})
    method = l7.method()
    return {"method": {k: method[k] for k in ("method_version", "weights", "dimensions", "composite", "bands")},
            "penalties": penalties, "scores": scores}


def _practices(conn) -> list[dict]:
    """Layer 8: the practices read from the texts in scope that do not bind, each quoted."""
    from app.clhear.l8.reference import derived_reference_rows

    return sorted(_plain(derived_reference_rows(conn)), key=lambda r: r["id"])


def _source_versions(conn, keys: list[str]) -> list[dict]:
    from app.clhear.hoststore import host_sources
    from app.clhear.l1.models import source_versions, sources

    hosts = {r["key"]: dict(r) for r in conn.execute(sa.select(host_sources).where(host_sources.c.key.in_(keys))).mappings()}
    out = []
    for key in keys:
        src = conn.execute(sa.select(sources).where(sources.c.key == key)).mappings().first()
        host = hosts.get(key) or {}
        entry = {"source_key": key, "name": host.get("name") or key, "kind": host.get("kind") or "",
                 "binding": host.get("binding"), "jurisdiction": host.get("jurisdiction") or "",
                 "publisher": host.get("issuer") or "", "reference": host.get("reference") or "",
                 "licence": host.get("licence") or "open", "adapter": host.get("adapter") or "",
                 "locator": _public_locator(host.get("locator")), "version": None}
        if src is not None:
            entry["rights_basis"] = src.get("rights_basis")
            entry["url"] = src.get("canonical_url") or ""
            version = conn.execute(sa.select(source_versions).where(
                source_versions.c.source_id == src["id"], source_versions.c.status == "in_force")
                .order_by(source_versions.c.id.desc())).mappings().first()
            if version is not None:
                entry["version"] = {"label": version["version_label"], "sha256": version["content_hash"],
                                    "retrieved_at": _iso(version["retrieved_at"]),
                                    "as_of": _iso(version["as_of_date"]), "effective": _iso(version["effective_date"])}
        out.append(entry)
    return out


def _public_locator(locator) -> dict:
    """Where the text was read from, without pasted text."""
    found = locator if isinstance(locator, dict) else {}
    return {k: v for k, v in found.items() if k != "text"}


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _plain(value):
    return json.loads(json.dumps(value, default=str))


# ----------------------------------------------------------------- verdicts


def verdicts(obligations: list[dict], attributes: dict) -> dict[str, dict]:
    """Each obligation's verdict for these attributes, by the same rule composition uses."""
    from app.clhear.l4.predicates import judge

    out = {}
    for ob in obligations:
        edges = [{"predicate": c["predicate"]} for c in ob.get("conditions") or []]
        found = judge(edges, attributes or {})
        out[ob["obligation_id"]] = {"state": found["state"],
                                    "failed": [e["predicate"] for e in found["failed"]],
                                    "open": [e["predicate"] for e in found["open"]]}
    return out


# ----------------------------------------------------------------- writing


def _dump(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Fields of the manifest its content hash does not cover: the hashes themselves, and when
# the files were written (the same release with the same options gives the same hash).
_UNHASHED = ("files", "content_hash", "created_at")


def content_hash(files: dict[str, str], manifest: dict | None = None) -> str:
    """One hash for the snapshot: the sorted file names with their hashes, and the manifest's
    own fields (title, licence, notice, release, profiles, sources ...)."""
    lines = "".join(f"{name} {digest}\n" for name, digest in sorted(files.items()))
    if manifest is not None:
        lines += "manifest.json " + _sha(_dump({k: v for k, v in manifest.items() if k not in _UNHASHED})) + "\n"
    return _sha(lines.encode("utf-8"))


def _restricted(sources: list[dict]) -> list[str]:
    """Sources whose text may be read but not republished (HLD §7.3): a snapshot quotes them."""
    from app.clhear.l1.rights import republishable

    return sorted(s["source_key"] for s in sources
                  if (s.get("licence") or "open") != "open"
                  or (s.get("rights_basis") is not None and not republishable(s["rights_basis"])))


def create(engine: Engine, release_id: str, out_dir: str | Path, *, licence: str, profiles: list[str] | None = None,
           title: str = "", notice: str = "", make_zip: bool = False) -> dict:
    """Write a snapshot of a stored release to ``out_dir`` (and ``out_dir``.zip when asked)."""
    from app.clhear import hoststore
    from app.clhear.blueprint_view import public_blueprint
    from app.clhear.settings import get_settings

    if not (licence or "").strip():
        raise SnapshotError(f"Name the licence the snapshot is published under, or '{NOT_FOR_PUBLICATION}'")
    row = hoststore.get_release(engine, release_id)
    if row is None:
        raise SnapshotError(f"unknown release {release_id}")
    body = row["blueprints"] if isinstance(row["blueprints"], dict) else {}
    basis = body.get("snapshot_basis")
    if not basis:
        raise SnapshotError(f"release {release_id} was made before snapshots or by the sample provider: run the scope again")
    compositions = body.get("profiles") or {}
    chosen = list(profiles or sorted(compositions))
    unknown = [p for p in chosen if p not in compositions]
    if unknown or not chosen:
        raise SnapshotError("the release holds no blueprint for: " + (", ".join(unknown) or "any profile"))
    if any(compositions[p].get("sample") for p in chosen):
        raise SnapshotError("a sample blueprint is illustrative and is not published as a snapshot")
    sources = basis.get("source_versions") or []
    restricted = _restricted(sources)
    if restricted and licence != NOT_FOR_PUBLICATION:
        raise SnapshotError("these sources may not be republished: " + ", ".join(restricted)
                            + f". Leave them out of the scope, or make the snapshot '{NOT_FOR_PUBLICATION}'")

    obligations = basis.get("applicability") or []
    files: dict[str, bytes] = {}
    listed, warnings = [], []
    for pid in chosen:
        composition = compositions[pid]
        attributes = composition.get("profile_attributes") or {}
        found = verdicts(obligations, attributes)
        applies = {oid for oid, v in found.items() if v["state"] == "applies"}
        covered = {c.get("obligation_id") for c in composition.get("coverage") or []}
        if applies != covered:
            raise SnapshotError(f"profile {pid}: the stored conditions give {len(applies)} applicable obligations "
                                f"and the blueprint covers {len(covered)}; the release is inconsistent")
        for ob in obligations:
            ob.setdefault("verdicts", {})[pid] = found[ob["obligation_id"]]["state"]
        name = f"blueprints/{_file_name(pid)}.json"
        files[name] = _dump(public_blueprint(composition, profile_id=pid))
        counts = {state: sum(1 for v in found.values() if v["state"] == state)
                  for state in ("applies", "not_applicable", "undetermined")}
        listed.append({"profile_id": pid, "blueprint_id": composition.get("blueprint_id"), "file": name,
                       "attributes": attributes, "elements": len(composition.get("items") or []),
                       "obligations": counts, "minimal": (composition.get("minimality") or {}).get("minimal")})
        if counts["undetermined"]:
            warnings.append(f"profile {pid} leaves {counts['undetermined']} obligations undetermined: answer its open questions")
        inventory = composition.get("source_inventory") or []
        open_sources = [s.get("source_key") or s.get("cited_as") for s in inventory if s.get("status") != "derived"]
        if open_sources:
            warnings.append(f"profile {pid}: sources not derived: " + ", ".join(str(s) for s in open_sources[:10]))
    layers = {name: {k: v for k, v in (layer or {}).items() if k in ("revision", "counts", "built", "reason")}
              for name, layer in (body.get("layers") or {}).items()}
    for name, layer in layers.items():
        if layer.get("built") is False:
            warnings.append(f"{name} was not built: {layer.get('reason') or 'no source for it in scope'}")

    files["applicability.json"] = _dump({"release": release_id, "obligations": obligations})
    files["questions.json"] = _dump(basis.get("questions") or {})
    files["sources.json"] = _dump({"release": release_id, "sources": sources})
    # Releases made before Layers 7 and 8 were captured have neither file.
    if basis.get("risk") is not None:
        files["risk.json"] = _dump({"release": release_id, **basis["risk"]})
    if basis.get("practices") is not None:
        from app.clhear.l8.reference import LABEL

        files["practices.json"] = _dump({"release": release_id, "label": LABEL, "practices": basis["practices"]})
    from app.clhear import recipe

    files["recipe.json"] = _dump(recipe.build(row["scope"], sources, listed))
    manifest = {
        "format": FORMAT,
        "title": title or f"Reference blueprints of release {release_id}",
        "licence": licence,
        "notice": notice,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "engine_version": get_settings().clhear_engine_version,
        "release": {"id": release_id, "scope": row["scope"], "run_id": row["run_id"],
                    "created_at": _iso(row["created_at"])},
        "scope": {"name": row["scope"], "source_keys": [s["source_key"] for s in sources]},
        "layers": layers,
        "models": basis.get("models") or [],
        "profiles": listed,
        "sources": [{k: s.get(k) for k in ("source_key", "name", "kind", "binding", "jurisdiction", "publisher",
                                           "reference", "licence", "rights_basis", "url", "version")} for s in sources],
        "restricted_sources": restricted,
    }
    readme = _readme(manifest, warnings, basis).encode("utf-8")
    files["README.md"] = readme
    manifest["warnings"] = warnings
    manifest["files"] = {name: _sha(data) for name, data in sorted(files.items())}
    manifest["content_hash"] = content_hash(manifest["files"], manifest)

    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise SnapshotError(f"{out} is not empty")
    for name, data in files.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (out / "manifest.json").write_bytes(_dump(manifest))
    archive = None
    if make_zip:
        archive = out.with_name(out.name + ".zip")
        archive.write_bytes(_zip({**files, "manifest.json": _dump(manifest)}, root=out.name))
    return {"path": str(out), "zip": str(archive) if archive else None, "content_hash": manifest["content_hash"],
            "profiles": [p["profile_id"] for p in listed], "warnings": warnings}


def _file_name(profile_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in profile_id).strip(".-")
    return safe or "profile"


def _zip(files: dict[str, bytes], *, root: str) -> bytes:
    """The same files give the same archive: fixed order, fixed times."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(f"{root}/{name}", date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()


def _readme(manifest: dict, warnings: list[str], basis: dict | None = None) -> str:
    lines = [f"# {manifest['title']}", ""]
    lines += [f"Reference blueprints composed by the CLHEAR engine {manifest['engine_version']} in release "
              f"`{manifest['release']['id']}` of the scope `{manifest['scope']['name']}`.", ""]
    lines += [f"Licence: {manifest['licence']}."]
    if manifest.get("notice"):
        lines += ["", manifest["notice"]]
    lines += ["", "## Organisation profiles", ""]
    for p in manifest["profiles"]:
        counts = p["obligations"]
        lines.append(f"- `{p['profile_id']}`: {p['elements']} elements in `{p['file']}`; obligations that apply "
                     f"{counts['applies']}, not applicable {counts['not_applicable']}, undetermined {counts['undetermined']}.")
        lines.append(f"  Answers: `{json.dumps(p['attributes'], sort_keys=True, ensure_ascii=False)}`")
    lines += ["", "## Sources", ""]
    for s in manifest["sources"]:
        version = s.get("version") or {}
        binds = {True: "binding", False: "not binding", None: "binding by kind"}[s.get("binding")]
        lines.append(f"- {s['name']} (`{s['source_key']}`, {s.get('kind') or 'kind not set'}, {binds}"
                     + (f", {s['jurisdiction']}" if s.get("jurisdiction") else "") + ")"
                     + (f": version {version.get('label')}, SHA-256 `{version.get('sha256')}`" if version else ": not read"))
    risk = (basis or {}).get("risk")
    practices = (basis or {}).get("practices")
    if risk is not None or practices is not None:
        lines += ["", "## Layers 7 and 8", ""]
    if risk is not None:
        stated, scored = len(risk.get("penalties") or []), len(risk.get("scores") or [])
        lines.append(f"- `risk.json`: {stated} {'penalty' if stated == 1 else 'penalties'} the binding texts state, quoted, "
                     f"and {scored} obligation risk {'score' if scored == 1 else 'scores'} under method "
                     f"`{(risk.get('method') or {}).get('method_version')}`.")
    if practices is not None:
        lines.append(f"- `practices.json`: {len(practices)} {'practice' if len(practices) == 1 else 'practices'} read from "
                     "the texts in scope that do not bind, quoted.")
    if manifest.get("models"):
        lines += ["", "## Models used by the run", ""]
        lines += [f"- {m['provider']} `{m['model']}`: {m['calls']} calls" for m in manifest["models"]]
    if warnings:
        lines += ["", "## Read before use", ""] + [f"- {w}" for w in warnings]
    lines += ["", "## Check and match", "",
              "Check that the files are the ones published:", "", "```", "clhear snapshot verify <this folder or .zip>", "```",
              "", "Check whether your organisation profile matches one of the profiles above. Write your answers in the",
              "shape of the profiles above (`questions.json` lists every question the texts raise):", "",
              "```", "clhear snapshot match <this folder or .zip> --profile my-profile.json", "```", "",
              "A profile matches when every obligation in scope gets the same verdict for your answers as for the",
              "snapshot's profile. The reference blueprint of that profile is then the one for your organisation profile.",
              "Each element carries the quotes and clause references it rests on: read them against the cited text.", "",
              "## Run it again", "",
              "`recipe.json` holds the sources, the scope and the profiles of this release:", "",
              "```", "clhear recipe apply recipe.json",
              f"clhear run --scope {manifest['scope']['name']}" + "".join(f" --profile-id {p['profile_id']}" for p in manifest["profiles"]),
              "```", "", "A new run reads the texts as they are published on that day, with the model you configure.", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------- reading


def load(path: str | Path) -> dict:
    """A snapshot folder or .zip: its manifest and every file it lists."""
    location = Path(path)
    files: dict[str, bytes] = {}
    if location.is_dir():
        for item in location.rglob("*"):
            if item.is_file():
                files[item.relative_to(location).as_posix()] = item.read_bytes()
    elif location.is_file() and zipfile.is_zipfile(location):
        with zipfile.ZipFile(location) as archive:
            entries = [i for i in archive.infolist() if not i.is_dir()]
            if len(entries) > MAX_FILES or sum(i.file_size for i in entries) > MAX_UNPACKED_BYTES:
                raise SnapshotError(f"{location.name} holds more than a snapshot can: at most {MAX_FILES} files "
                                    f"and {MAX_UNPACKED_BYTES} bytes unpacked")
            names = [i.filename for i in entries]
            roots = {n.split("/", 1)[0] for n in names}
            strip = len(next(iter(roots))) + 1 if len(roots) == 1 and all("/" in n for n in names) else 0
            for name in names:
                files[name[strip:]] = archive.read(name)
    else:
        raise SnapshotError(f"{location} is not a snapshot folder or .zip")
    if "manifest.json" not in files:
        raise SnapshotError("no manifest.json: this is not a snapshot")
    try:
        manifest = json.loads(files["manifest.json"])
    except ValueError as exc:
        raise SnapshotError("manifest.json is not JSON") from exc
    found = manifest.get("format") if isinstance(manifest, dict) else None
    if found != FORMAT:
        raise SnapshotError(f"unknown snapshot format {found!r}; this engine reads {FORMAT}")
    return {"manifest": manifest, "files": files}


def verify(path: str | Path) -> dict:
    """Every file the manifest lists is present with its hash, and nothing else is there."""
    snap = load(path)
    manifest, files = snap["manifest"], snap["files"]
    problems = []
    listed = manifest.get("files") or {}
    for name, digest in sorted(listed.items()):
        if name not in files:
            problems.append(f"missing: {name}")
        elif _sha(files[name]) != digest:
            problems.append(f"changed: {name}")
    for name in sorted(set(files) - set(listed) - {"manifest.json"}):
        problems.append(f"not listed: {name}")
    if content_hash(listed, manifest) != manifest.get("content_hash"):
        problems.append("the content hash does not match the listed files and the manifest")
    return {"valid": not problems, "problems": problems, "title": manifest.get("title"),
            "release": (manifest.get("release") or {}).get("id"), "content_hash": manifest.get("content_hash"),
            "licence": manifest.get("licence")}


def match(path: str | Path, attributes: dict) -> dict:
    """Whether an organisation profile gets the same verdicts as each profile of a snapshot."""
    from app.clhear.l4.validate import validate

    checked = verify(path)
    if not checked["valid"]:
        raise SnapshotError("the snapshot does not verify: " + "; ".join(checked["problems"]))
    shape = validate(None, attributes or {})
    if shape["errors"]:
        raise SnapshotError("the profile cannot be read: " + "; ".join(e["message"] for e in shape["errors"]))
    snap = load(path)
    manifest = snap["manifest"]
    if "applicability.json" not in snap["files"]:
        raise SnapshotError("the snapshot has no applicability.json, so no profile can be matched")
    obligations = json.loads(snap["files"]["applicability.json"])["obligations"]
    yours = verdicts(obligations, shape["normalized"])
    results = []
    for profile in manifest["profiles"]:
        pid = profile["profile_id"]
        differences = []
        for ob in obligations:
            theirs = (ob.get("verdicts") or {}).get(pid)
            mine = yours[ob["obligation_id"]]
            if mine["state"] != theirs:
                differences.append({"obligation_id": ob["obligation_id"], "stable_id": ob.get("stable_id"),
                                    "source_key": ob["source_key"], "clause_ref": ob.get("clause_ref"),
                                    "title": ob.get("title"), "snapshot": theirs, "yours": mine["state"],
                                    "your_failed": mine["failed"], "your_open": mine["open"]})
        unanswered = sorted({json.dumps(p, sort_keys=True) for d in differences for p in d["your_open"]})
        results.append({"profile_id": pid, "matches": not differences, "blueprint_file": profile["file"],
                        "differences": differences, "unanswered": [json.loads(u) for u in unanswered]})
    return {"snapshot": manifest.get("title"), "release": manifest["release"]["id"],
            "content_hash": manifest.get("content_hash"), "licence": manifest.get("licence"),
            "matches": [r["profile_id"] for r in results if r["matches"]], "profiles": results}


def describe_match(result: dict) -> str:
    """The match result as text."""
    lines = [f"Snapshot: {result['snapshot']} (release {result['release']})"]
    if result["matches"]:
        for r in result["profiles"]:
            if r["matches"]:
                lines.append(f"MATCH {r['profile_id']}: the reference blueprint in {r['blueprint_file']} is the one for your profile.")
        return "\n".join(lines)
    lines.append("No profile of this snapshot matches yours.")
    for r in result["profiles"]:
        lines.append(f"- {r['profile_id']}: {len(r['differences'])} obligations get another verdict.")
        for d in r["differences"][:5]:
            lines.append(f"    {d['source_key']} {d['clause_ref']}: snapshot {d['snapshot']}, yours {d['yours']}")
        for q in r["unanswered"][:5]:
            lines.append(f"    unanswered: {json.dumps(q, sort_keys=True)}")
    lines.append("Answer the open questions, or compose a blueprint for your own profile with the engine.")
    return "\n".join(lines)
