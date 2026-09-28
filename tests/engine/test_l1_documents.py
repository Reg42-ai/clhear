# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L1: a host's own text, file or URL becomes verified, citable clauses."""
from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.clhear.l1.adapters.base import Artifact
from app.clhear.l1.adapters.document import TEXT_ARTIFACT, build_tree, check_url, read_local_path, verify

from .conftest import pdf_bytes

GDPR_LIKE = """Regulation on data protection

CHAPTER II
Principles

Article 5
Principles relating to processing of personal data

1. Personal data shall be:
(a) processed lawfully, fairly and in a transparent manner in relation to the data subject;
(b) collected for specified, explicit and legitimate purposes.

2. The controller shall be responsible for, and be able to demonstrate compliance with, paragraph 1.

Article 32 Security of processing
1. Taking into account the state of the art, the controller and the processor shall implement appropriate technical and organisational measures to ensure a level of security appropriate to the risk.
"""


def _refs(tree):
    return [node.ref for root in tree for node in root.walk()]


def test_segmentation_follows_the_documents_own_units():
    tree = build_tree(GDPR_LIKE, "gdpr")
    assert _refs(tree) == ["gdpr", "p1", "ch-ii", "art-5", "art-5/1", "art-5/1/a", "art-5/1/b", "art-5/2",
                           "art-32", "art-32/1"]
    article = tree[0].children[1].children[0]
    assert article.heading == "Article 5\nPrinciples relating to processing of personal data"
    assert verify([Artifact(TEXT_ARTIFACT, GDPR_LIKE.encode())], tree)


def test_verifier_rejects_a_node_that_changes_the_text():
    tree = build_tree(GDPR_LIKE, "gdpr")
    tree[0].children[1].children[0].children[0].raw_text = "1. Personal data may be:"
    assert not verify([Artifact(TEXT_ARTIFACT, GDPR_LIKE.encode())], tree)


def _import(install, sources: dict[str, dict]):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.runtime import engine

    for key, body in sources.items():
        hoststore.upsert_source(engine(), key, {"adapter": "local_text", "name": key, "licence": "open", **body})
    scopes.put("sc", list(sources))
    import os

    os.environ[scopes.SCOPE_ENV] = "sc"
    try:
        return scope_build.import_sources(engine(), None, scopes.get("sc"))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)


def _clauses(key):
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        rows = conn.execute(sa.select(clauses.c.ref, clauses.c.text)
                            .join(source_versions, clauses.c.source_version_id == source_versions.c.id)
                            .join(sources, source_versions.c.source_id == sources.c.id)
                            .where(sources.c.key == key).order_by(source_versions.c.id, clauses.c.ordering)).all()
    return rows


def test_pasted_text_imports_as_clauses(install):
    result = _import(install, {"gdpr": {"locator": {"text": GDPR_LIKE}}})
    assert result["sources"] == {"gdpr": "added"}
    assert result["failed_sources"] == []
    refs = [ref for ref, _ in _clauses("gdpr")]
    assert "art-32/1" in refs and "art-5/1/a" in refs


def test_reimport_is_unchanged_and_an_edit_is_amended(install):
    assert _import(install, {"doc": {"locator": {"text": GDPR_LIKE}}})["sources"] == {"doc": "added"}
    assert _import(install, {"doc": {"locator": {"text": GDPR_LIKE}}})["sources"] == {"doc": "unchanged"}
    edited = GDPR_LIKE + "\nArticle 33\n1. The controller shall notify a breach within 72 hours.\n"
    assert _import(install, {"doc": {"locator": {"text": edited}}})["sources"] == {"doc": "amended"}


def test_html_file_with_doctype_imports(install):
    html = b"""<!DOCTYPE html><html><head><title>x</title><script>var a=1;</script></head>
    <body><nav>Menu</nav><main><h1>Security Rule</h1>
    <p>Section 1. Each covered entity shall designate a security official.</p>
    <p>Section 2. Each covered entity must review access logs at least monthly.</p></main>
    <footer>Contact</footer></body></html>"""
    (install / "sources" / "rule.html").write_bytes(html)
    result = _import(install, {"rule": {"locator": {"path": "rule.html"}}})
    assert result["sources"] == {"rule": "added"}, result
    texts = [text for _, text in _clauses("rule")]
    assert any("designate a security official" in text for text in texts)
    assert not any("Menu" in text or "Contact" in text for text in texts)


def test_pdf_file_without_numbered_sections_imports(install):
    pdf = pdf_bytes(["Records policy", "", "Every team must keep a record of each decision.",
                     "Staff should be trained every year."])
    (install / "sources" / "policy.pdf").write_bytes(pdf)
    result = _import(install, {"policy": {"locator": {"path": "policy.pdf"}}})
    assert result["sources"] == {"policy": "added"}, result
    assert any("keep a record of each decision" in text for _, text in _clauses("policy"))


def test_a_scope_that_yields_no_text_fails_with_the_reason(install):
    with pytest.raises(RuntimeError, match="No text could be read.*gone"):
        _import_and_build(install)


def _import_and_build(install):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.runtime import engine
    import os

    hoststore.upsert_source(engine(), "gone", {"adapter": "local_text", "locator": {"path": "missing.txt"}})
    scopes.put("sc", ["gone"])
    os.environ[scopes.SCOPE_ENV] = "sc"
    try:
        return scope_build.build(engine(), None, layers=("L1",))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)


def test_paths_are_confined_to_the_local_sources_directory(install):
    (install / "secret.txt").write_text("not a source")
    with pytest.raises(PermissionError):
        read_local_path("../secret.txt")
    with pytest.raises(PermissionError):
        read_local_path(str(install / "secret.txt"))


@pytest.mark.parametrize("url", ["http://example.com/a", "https://user:pw@example.com/", "https://127.0.0.1/x",
                                 "https://169.254.169.254/latest/meta-data", "https://localhost/"])
def test_url_sources_must_be_public_https(url):
    with pytest.raises((ValueError, PermissionError)):
        check_url(url)
