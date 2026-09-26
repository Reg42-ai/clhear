# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""SEC.gov publications: the press release or staff page, not the site around it.

sec.gov wraps each publication in navigation, breadcrumbs and a resources rail.
The publication is the page-title block and the main content region, read as
its own paragraphs and list items (``publication_pages``): a sweep release's
charged firms and a risk alert's observations stay individually addressable.
"""
from __future__ import annotations

import hashlib

from app.clhear.l1 import http
from app.clhear.l1.adapters import publication_pages
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult
from app.clhear.l1.adapters.sec_edgar import SEC_HEADERS

TITLE_CLASS = "page-title"
BODY_CLASS = "node-details-layout__main-region__content"
DOCUMENT_TYPE = "sec_page"
STRUCTURE = publication_pages.STRUCTURE
PROFILE = publication_pages.PROFILES[DOCUMENT_TYPE]


def is_sec_page(content: bytes) -> bool:
    return BODY_CLASS.encode() in content and f'"{TITLE_CLASS}'.encode() in content


def parse(content: bytes, source_key: str, title: str) -> list[DocNode]:
    return publication_pages.parse(content, source_key, title, PROFILE)


def original_text(content: bytes) -> str:
    return publication_pages.original_text(content)


def verify(artifacts: list[Artifact], source_key: str, tree: list[DocNode]) -> bool:
    return publication_pages.verify(artifacts, source_key, tree)


class SecPageAdapter:
    """One sec.gov publication page on an existing SEC lane (``sec_enforcement`` or ``sec_edgar``)."""

    def __init__(self, source_key: str, title: str, url: str, *, meta, adapter: str):
        self.key = adapter
        self._source_key, self._title, self._url, self._meta = source_key, title, url, meta

    def meta(self):
        return self._meta

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url, headers=SEC_HEADERS)
        if not is_sec_page(content):
            raise ValueError("Expected an SEC publication page; a listing or landing page cannot stand in")
        label = "as_published:acquired-sha256-" + hashlib.sha256(content).hexdigest()
        if since_version == label:
            return None
        return FetchResult(version_label=label,
                           artifacts=[Artifact(name="publication.html", content=content, content_type="text/html")],
                           tree=parse(content, self._source_key, self._title), version_kind="as_published")

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        return [original_text(artifact.content) for artifact in artifacts]
