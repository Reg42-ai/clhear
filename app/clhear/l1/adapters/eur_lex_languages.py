# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Language-specific EUR-Lex originals, preserving URL/cache identities."""
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.adapters.base import Artifact, FetchResult
from app.clhear.l1.structured_catalogs import EU_LANGUAGES
from app.clhear.l1 import http


class EurLexLanguageAdapter(EurLexAdapter):
    def __init__(self, *, language, **kwargs):
        if language not in EU_LANGUAGES:
            raise ValueError("Unknown Cellar language authority code")
        super().__init__(**kwargs)
        self.language = language

    def fetch(self, since_version=None):
        from urllib.parse import quote
        from bs4 import BeautifulSoup
        from app.clhear.l1.adapters.dom_document import parse
        # URL identity includes language: the shared HTTP cache is URL keyed.
        url = "https://eur-lex.europa.eu/legal-content/" + EU_LANGUAGES[self.language].upper() + "/TXT/HTML/?uri=CELEX:" + quote(self.celex_version, safe="()")
        content = http.get(url, allowed_redirect_hosts={"eur-lex.europa.eu"})
        soup = BeautifulSoup(content, "html.parser")
        html = soup.find("html")
        observed = (html.get("lang") or html.get("xml:lang") or "").lower().split("-")[0] if html else ""
        if observed not in {self.language.lower(), EU_LANGUAGES[self.language]}:
            raise ValueError("Returned Cellar document does not establish the requested language")
        if not soup.select_one('[id^="art_"], [id^="anx_"], .oj-normal, .normal, .norm') or soup.select_one("#challenge-form, .cf-turnstile"):
            raise ValueError("EUR-Lex returned no identifiable legal document body")
        return FetchResult(version_label=self.version_label, artifacts=[Artifact(
            self.celex_version + "." + self.language.lower() + ".xhtml", content, "application/xhtml+xml")],
            tree=parse(content, self.meta().source_key), version_kind=self.version_kind, as_of_date=self.as_of_date)
