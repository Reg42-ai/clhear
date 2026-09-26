# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""FTC business-guidance pages (ftc.gov), read as their own blocks of text.

The publication is the page's first ``<h1>`` and its ``field--name-body``
region; navigation, related links and the footer are site chrome.
"""
from __future__ import annotations

import hashlib

from app.clhear.l1 import http
from app.clhear.l1.adapters import publication_pages
from app.clhear.l1.adapters.base import Artifact, FetchResult

DOCUMENT_TYPE = "ftc_page"
PROFILE = publication_pages.PROFILES[DOCUMENT_TYPE]
HEADERS = {"User-Agent": "CLHEAR by Reg42 (compliance@reg42.ai)", "Accept": "text/html"}


def is_ftc_page(content: bytes) -> bool:
    profile = publication_pages.profile_for(content)
    return profile is not None and profile.document_type == DOCUMENT_TYPE


class FtcPageAdapter:
    """One ftc.gov guidance page on the US government lane."""

    def __init__(self, source_key: str, title: str, url: str, *, meta, adapter: str = "govinfo_us"):
        self.key = adapter
        self._source_key, self._title, self._url, self._meta = source_key, title, url, meta

    def meta(self):
        return self._meta

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        content = http.get(self._url, headers=HEADERS)
        if not is_ftc_page(content):
            raise ValueError("Expected an FTC guidance page; a listing or landing page cannot stand in")
        label = "as_published:acquired-sha256-" + hashlib.sha256(content).hexdigest()
        if since_version == label:
            return None
        return FetchResult(version_label=label,
                           artifacts=[Artifact(name="publication.html", content=content, content_type="text/html")],
                           tree=publication_pages.parse(content, self._source_key, self._title, PROFILE),
                           version_kind="as_published")

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        return [publication_pages.original_text(artifact.content) for artifact in artifacts]
