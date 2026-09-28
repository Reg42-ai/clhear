# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""SEC publications (HLD v2 §4.1).

SEC final rules / 17 CFR text on sec.gov: public domain (17 U.S.C. § 105).
Refs are ``§ 240.15c3-3`` or ``Rule 15c3-3``. The SEC requires a declared
``User-Agent`` with a contact address.
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

    publisher = "U.S. Securities and Exchange Commission"
    issuer = publisher
    PROVISION = _SEC

    def __init__(self, **kwargs):
        kwargs.setdefault("family_key", "us-broker-dealer")
        kwargs.setdefault("family_name", "US broker-dealer & listed company")
        super().__init__(**kwargs)

    def meta(self):
        meta = super().meta()
        if self._meta is not None:
            return meta
        from dataclasses import replace
        from app.clhear.l1.rights import rights_for

        rights = rights_for(self.key, meta.license)
        return replace(meta, rights_basis=rights.basis, rights_ref=rights.ref, publisher=self.publisher)
