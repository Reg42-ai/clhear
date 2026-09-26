# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""SEC publications and the official FINRA rulebook (HLD v2 §4.1).

Two channels behind one adapter key so the fleet schedule, the polite client
(SEC requires a declared ``User-Agent`` with a contact address) and the
rights recorder stay in one place:

``channel="sec"``   SEC final rules / 17 CFR text on sec.gov — public domain
                    (17 U.S.C. § 105); refs are ``§ 240.15c3-3`` or ``Rule 15c3-3``.
``channel="finra"`` FINRA rules retrieved directly from the official
                    finra.org rulebook (not an EDGAR mirror) —
                    *derived-only* rights: CLHEAR keeps hashes, spans and
                    derived obligations but never republishes the text.
                    Refs are ``3110`` / ``3110(a)`` / ``3110(a)(1)``.
"""
import re

from app.clhear.l1.adapters.publisher import NumberedHtmlAdapter

SEC_HEADERS = {"User-Agent": "CLHEAR by Reg42 (compliance@reg42.ai)"}

_SEC = re.compile(r"^(?P<ref>§\s*2\d{2}\.\d+[A-Za-z0-9\-]*|Rule\s+\d+[a-zA-Z]?(?:-\d+)?(?:\([a-z0-9]+\))*)(?=[\s.:—-]|$)")


class SecEdgarAdapter(NumberedHtmlAdapter):
    key = "sec_edgar"
    jurisdiction = "US"
    kind = "regulation"
    headers = SEC_HEADERS

    def __init__(self, *, channel: str = "sec", **kwargs):
        if channel not in {"sec", "finra"}:
            raise ValueError("channel must be 'sec' or 'finra'")
        self.channel = channel
        if channel == "sec":
            self.publisher = "U.S. Securities and Exchange Commission"
            self.issuer = self.publisher
            self.PROVISION = _SEC
            kwargs.setdefault("family_key", "us-broker-dealer")
            kwargs.setdefault("family_name", "US broker-dealer & listed company")
        else:
            self.publisher = "FINRA (official finra.org rulebook)"
            self.issuer = "FINRA"
            kwargs.setdefault("family_key", "us-broker-dealer")
            kwargs.setdefault("family_name", "US broker-dealer & listed company")
        super().__init__(**kwargs)

    def meta(self):
        meta = super().meta()
        if self._meta is not None:
            return meta
        from dataclasses import replace
        from app.clhear.l1.rights import rights_for

        rights = rights_for("finra" if self.channel == "finra" else self.key, meta.license)
        return replace(meta, rights_basis=rights.basis, rights_ref=rights.ref, publisher=self.publisher)

    def parse_many(self, contents):
        if self.channel != "finra":
            return super().parse_many(contents)
        from app.clhear.l1.adapters import finra

        return [root for content in contents for root in finra.parse(content, self._source_key, self._url)]

    def expected_text(self, artifacts):
        if self.channel != "finra":
            return super().expected_text(artifacts)
        from app.clhear.l1.adapters import finra

        return [span for artifact in artifacts for span in finra.expected_text(artifact.content)]

    def validate_tree(self, tree, artifacts):
        if self.channel != "finra":
            return []
        from app.clhear.l1.adapters import finra

        return finra.validate_tree(tree, artifacts, self._source_key, self._url)
