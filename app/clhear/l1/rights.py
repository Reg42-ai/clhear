# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Rights recorder (HLD v2 §4.1, I5/I8).

Every source carries a *rights basis* that decides what CLHEAR may do with
the text:

    public_domain  government work — full text republished
    open_licence   OGL / CC-BY / publisher reuse notice — full text with attribution
    licensed       publisher permission with conditions — text served, attribution + notice
    byol_only      customer must bring their own licensed copy — hashes only
    derived_only   we may derive facts (obligations, spans) but never republish text

The recorder writes one ``rights_records`` row per determination (evidence
URL + basis reference) and keeps ``sources.rights_basis`` in sync. Public
APIs consult :func:`republishable` before returning clause text.
"""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.l1.models import RIGHTS_BASES, rights_records, sources

REPUBLISH_TEXT = frozenset({"public_domain", "open_licence", "licensed"})


@dataclass(frozen=True)
class RightsBasis:
    basis: str
    ref: str
    evidence_url: str = ""

    def __post_init__(self):
        if self.basis not in RIGHTS_BASES:
            raise ValueError(f"unknown rights basis {self.basis!r}")


# Adapter-level defaults; an adapter's SourceMeta may override per source.
RIGHTS: dict[str, RightsBasis] = {
    "uk_legislation": RightsBasis("open_licence", "Open Government Licence v3.0", "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"),
    "eur_lex": RightsBasis("open_licence", "Commission Decision 2011/833/EU (reuse of Commission documents)", "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html"),
    "govinfo_us": RightsBasis("public_domain", "17 U.S.C. § 105 (works of the United States Government)", "https://www.govinfo.gov/about/policies"),
    "govinfo_us_usc": RightsBasis("public_domain", "17 U.S.C. § 105", "https://www.govinfo.gov/about/policies"),
    "govinfo_us_ecfr": RightsBasis("public_domain", "17 U.S.C. § 105", "https://www.ecfr.gov/reader-aids/using-ecfr/legal-status"),
    "nist": RightsBasis("public_domain", "17 U.S.C. § 105 (NIST publications)", "https://www.nist.gov/oism/copyrights"),
    "nist_sp800_53": RightsBasis("public_domain", "17 U.S.C. § 105 (NIST publications)", "https://www.nist.gov/oism/copyrights"),
    "nist_csf": RightsBasis("public_domain", "17 U.S.C. § 105 (NIST publications)", "https://www.nist.gov/oism/copyrights"),
    "fca_handbook": RightsBasis("licensed", "FCA Handbook copyright notice — reproduction permitted with acknowledgement", "https://www.handbook.fca.org.uk/copyright-notice"),
    "sec_edgar": RightsBasis("public_domain", "17 U.S.C. § 105 (SEC releases and rules)", "https://www.sec.gov/privacy#dissemination"),
    "finra": RightsBasis("derived_only", "FINRA rulebook © FINRA — no republication; derived facts only", "https://www.finra.org/terms-of-use"),
    "fca_enforcement": RightsBasis("licensed", "FCA website terms — final notices reproduced with acknowledgement; CLHEAR publishes derived facts", "https://www.fca.org.uk/legal"),
    "sec_enforcement": RightsBasis("public_domain", "17 U.S.C. § 105 (SEC litigation releases and administrative proceedings)", "https://www.sec.gov/privacy#dissemination"),
    "finra_enforcement": RightsBasis("derived_only", "FINRA disciplinary actions © FINRA — derived facts only, no republication", "https://www.finra.org/terms-of-use"),
    "esma": RightsBasis("open_licence", "ESMA legal notice — reuse permitted with source acknowledgement", "https://www.esma.europa.eu/legal-notice"),
    "fatf": RightsBasis("licensed", "FATF terms — reproduction permitted for non-commercial use with acknowledgement", "https://www.fatf-gafi.org/en/pages/terms-and-conditions.html"),
    "bis_basel": RightsBasis("open_licence", "BIS copyright notice — reproduction permitted with source acknowledgement", "https://www.bis.org/terms_conditions.htm"),
    "iosco": RightsBasis("licensed", "IOSCO copyright — reproduction of public documents permitted with acknowledgement", "https://www.iosco.org/about/?subsection=copyright"),
    "mas": RightsBasis("licensed", "MAS terms of use — reproduction of notices permitted with acknowledgement", "https://www.mas.gov.sg/terms-of-use"),
    "asic": RightsBasis("open_licence", "ASIC regulatory documents — Creative Commons Attribution 4.0", "https://asic.gov.au/about-asic/dealing-with-asic/copyright-and-linking-to-our-websites/"),
    "isa": RightsBasis("derived_only", "ISA unofficial English translations — derived facts only, no republication", "https://www.isa.gov.il/sites/ISAEng/1489/1511/Pages/default.aspx"),
    "irs_gov": RightsBasis("public_domain", "17 U.S.C. § 105 (IRS revenue procedures)", "https://www.irs.gov/privacy-disclosure/irs-privacy-policy"),
    "au_legislation": RightsBasis("open_licence", "Creative Commons Attribution 4.0 (Federal Register of Legislation)", "https://www.legislation.gov.au/copyright"),
    "sg_legislation": RightsBasis("licensed", "Singapore Statutes Online terms of use", "https://sso.agc.gov.sg/Help/TermsOfUse"),
    "lists": RightsBasis("open_licence", "Publisher terms — list data reused as published", ""),
    "restricted_file": RightsBasis("byol_only", "Bring-your-own-licence — hashes only until a licensed file is present", ""),
    "wolfsberg": RightsBasis("licensed", "Wolfsberg Group publications — reproduction with acknowledgement", "https://wolfsberg-group.org/"),
    "adgm": RightsBasis("licensed", "ADGM rulebook terms of use", "https://en.adgm.thomsonreuters.com/"),
    "nydfs": RightsBasis("public_domain", "New York State government work", "https://www.dfs.ny.gov/"),
    "nasdaq": RightsBasis("derived_only", "Nasdaq rulebook © Nasdaq — derived facts only", "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules"),
    "cysec": RightsBasis("licensed", "CySEC website terms", "https://www.cysec.gov.cy/"),
    "malta": RightsBasis("open_licence", "Government of Malta — legislation.mt reuse", "https://legislation.mt/"),
    "uae": RightsBasis("licensed", "UAE Legislation portal terms", "https://uaelegislation.gov.ae/"),
    "israel": RightsBasis("derived_only", "gov.il unofficial translation — derived facts only", "https://www.gov.il/"),
    "seychelles": RightsBasis("licensed", "Seychelles FSA publication", "https://fsaseychelles.sc/"),
    "gibraltar": RightsBasis("licensed", "Gibraltar Laws online", "https://www.gibraltarlaws.gov.gi/"),
    "overlay": RightsBasis("open_licence", "ESMA legal notice (host-state overlay PDFs republished by ESMA)", "https://www.esma.europa.eu/legal-notice"),
}

BASIS_BY_ADAPTER: dict[str, str] = {key: value.basis for key, value in RIGHTS.items()}

_DEFAULT = RightsBasis("licensed", "publisher notice — verify before republishing")


def rights_for(adapter: str, license: str = "open") -> RightsBasis:
    if license == "restricted":
        return RIGHTS["restricted_file"]
    return RIGHTS.get(adapter, _DEFAULT)


def republishable(basis: str) -> bool:
    """May clause text be served on public APIs under this basis?"""
    return basis in REPUBLISH_TEXT


def record(conn: Connection, source_id: int, basis: RightsBasis, *, recorded_by: str = "l1.rights") -> bool:
    """Append a rights determination if it differs from the latest one; sync `sources`.

    Returns True when a new ledger row was written.
    """
    latest = conn.execute(
        sa.select(rights_records.c.rights_basis, rights_records.c.basis_ref)
        .where(rights_records.c.source_id == source_id)
        .order_by(rights_records.c.id.desc())
        .limit(1)
    ).first()
    current = conn.execute(sa.select(sources.c.rights_basis).where(sources.c.id == source_id)).scalar()
    if current != basis.basis:
        conn.execute(sources.update().where(sources.c.id == source_id).values(rights_basis=basis.basis))
    if latest is not None and latest.rights_basis == basis.basis and latest.basis_ref == basis.ref:
        return False
    conn.execute(
        rights_records.insert().values(
            source_id=source_id,
            rights_basis=basis.basis,
            basis_ref=basis.ref,
            evidence_url=basis.evidence_url,
            republish_text=republishable(basis.basis),
            recorded_by=recorded_by,
        )
    )
    return True


def history(conn: Connection, source_id: int) -> list[dict]:
    rows = conn.execute(
        sa.select(rights_records).where(rights_records.c.source_id == source_id).order_by(rights_records.c.id)
    ).mappings()
    return [
        {
            "rights_basis": r["rights_basis"],
            "basis_ref": r["basis_ref"],
            "evidence_url": r["evidence_url"],
            "republish_text": bool(r["republish_text"]),
            "recorded_by": r["recorded_by"],
            "recorded_at": str(r["recorded_at"]) if r["recorded_at"] else None,
        }
        for r in rows
    ]


__all__ = ["BASIS_BY_ADAPTER", "RIGHTS", "RightsBasis", "history", "record", "republishable", "rights_for"]
