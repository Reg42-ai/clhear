# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 register adapters — regulator registers and permission taxonomies (HLD v2 §4.4).

The ontology is *built from registers*, not from model memory. Each adapter
knows one public register (FCA Financial Services Register, ESMA / EBA
registers, SEC-FINRA BrokerCheck / IAPD, FinCEN MSB, NFA BASIC), the
permission taxonomy it publishes, and how to confirm that a permission /
activity code in our snapshot still exists there.

Fetching honours ``CLHEAR_HTTP_MODE`` through :func:`app.clhear.l1.http.get`
(replay fixtures in tests, live with cache in ingestion). When a register is
unreachable the adapter reports ``freshness = "snapshot"`` and the builder
keeps the reviewed snapshot in ``curated/l4_ontology.json`` — provenance is
recorded either way, so the validity gate can tell audited rows from
snapshot rows.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.clhear.curated import load_object
from app.clhear.l1 import http

log = logging.getLogger("clhear.l4.registers")


@dataclass
class RegisterCheck:
    key: str
    name: str
    url: str
    freshness: str  # live | snapshot
    checked_at: str
    entries_seen: int = 0
    confirmed: list[str] = field(default_factory=list)  # licence ids whose register_ref was found
    missing: list[str] = field(default_factory=list)  # licence ids whose register_ref was NOT found live
    note: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def ontology_snapshot() -> dict:
    return load_object("l4_ontology")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class RegisterAdapter:
    """One public register. ``probe_urls`` are fetched (replay/live); the
    returned text is scanned for each licence's ``register_ref``."""

    key = ""
    probe_urls: tuple[str, ...] = ()

    def __init__(self, meta: dict):
        self.meta = meta
        self.key = meta["key"]

    def fetch_texts(self) -> list[str]:
        texts: list[str] = []
        for url in self.probe_urls:
            try:
                texts.append(http.get(url, timeout=30.0).decode("utf-8", "replace"))
            except Exception as exc:  # FixtureMissing in replay, network errors live
                log.info("register %s: %s unavailable (%s)", self.key, url, exc.__class__.__name__)
        return texts

    def check(self, licences: list[dict]) -> RegisterCheck:
        now = datetime.now(timezone.utc).isoformat()
        mine = [l for l in licences if l.get("register") == self.key]
        texts = self.fetch_texts()
        if not texts:
            return RegisterCheck(self.key, self.meta["name"], self.meta["url"], "snapshot", now,
                                 note="register not fetched in this mode; reviewed snapshot retained")
        blob = _norm(" ".join(texts))
        confirmed, missing = [], []
        for l in mine:
            ref = _norm(l.get("register_ref") or l["name"])
            (confirmed if ref and ref in blob else missing).append(l["id"])
        return RegisterCheck(self.key, self.meta["name"], self.meta["url"], "live", now,
                             entries_seen=len(texts), confirmed=confirmed, missing=missing)


class FcaRegister(RegisterAdapter):
    # The public register's permission taxonomy page (no API key needed); the
    # firm API (services/V0.1/Firm/{frn}/Permissions) is used per-firm in instance mode.
    probe_urls = ("https://register.fca.org.uk/s/",
                  "https://www.handbook.fca.org.uk/handbook/glossary/G2035.html")


class EsmaRegisters(RegisterAdapter):
    probe_urls = ("https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_upreg",
                  "https://registers.esma.europa.eu/publication/searchRegister?core=esma_registers_mica_casp")


class EbaRegisters(RegisterAdapter):
    probe_urls = ("https://euclid.eba.europa.eu/register/pir/disclaimer",)


class SecFinraRegisters(RegisterAdapter):
    probe_urls = ("https://www.sec.gov/about/divisions-offices/division-trading-markets/broker-dealers",
                  "https://brokercheck.finra.org/")


class FincenMsb(RegisterAdapter):
    probe_urls = ("https://www.fincen.gov/msb-registrant-search",)


class NfaBasic(RegisterAdapter):
    probe_urls = ("https://www.nfa.futures.org/basicnet/",)


ADAPTERS = {
    "fca_register": FcaRegister,
    "esma_registers": EsmaRegisters,
    "eba_registers": EbaRegisters,
    "sec_finra": SecFinraRegisters,
    "fincen_msb": FincenMsb,
    "nfa_basic": NfaBasic,
}


def check_registers(snapshot: dict | None = None) -> dict[str, RegisterCheck]:
    """Probe every register the snapshot cites. Never raises: an unreachable
    register degrades to ``snapshot`` freshness."""
    snap = snapshot or ontology_snapshot()
    out: dict[str, RegisterCheck] = {}
    for meta in snap["registers"]:
        cls = ADAPTERS.get(meta["key"], RegisterAdapter)
        try:
            out[meta["key"]] = cls(meta).check(snap["licences"])
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("register check failed for %s", meta["key"])
            out[meta["key"]] = RegisterCheck(meta["key"], meta["name"], meta["url"], "snapshot",
                                             datetime.now(timezone.utc).isoformat(), note=str(exc))
    return out


def checks_as_json(checks: dict[str, RegisterCheck]) -> str:
    return json.dumps({k: v.as_dict() for k, v in checks.items()}, sort_keys=True)
