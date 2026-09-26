# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Standard-setter and regulator publication adapters (HLD v2 §4.1).

Each class is a thin declaration over the publisher bases: the provision
numbering the publisher uses, the heading convention, and the rights basis
(recorded by l1.rights). Keys match FLEET_SCHEDULES / infra schedules.

    esma       ESMA guidelines & Q&A          numbered paragraphs "12." under "Guideline 3"
    fatf       FATF Recommendations           "1." … "40." + interpretive notes -> refs R.1 … R.40
    bis_basel  Basel Framework chapters       "CRE20.1" paragraph numbering (HTML)
    iosco      IOSCO Objectives & Principles  "Principle 1" … -> refs P.1 …
    mas        MAS Notices                    "4.1", "4.1.2" paragraph numbering
    asic       ASIC Regulatory Guides         "RG 227.1" paragraph numbering
    isa        Israel Securities Authority    "12A." section numbering (English translation, derived-only)
    irs_gov    IRS Revenue Procedures         "SECTION 3." headings + ".01" subsections -> refs sec3.01
"""
import re

from app.clhear.l1.adapters.publisher import NumberedHtmlAdapter, NumberedPdfAdapter

__all__ = [
    "AsicAdapter",
    "BisBaselAdapter",
    "EsmaAdapter",
    "FatfAdapter",
    "IoscoAdapter",
    "IrsRevProcAdapter",
    "IsaAdapter",
    "MasAdapter",
]


class EsmaAdapter(NumberedPdfAdapter):
    key = "esma"
    publisher = "European Securities and Markets Authority"
    issuer = publisher
    jurisdiction = "EU"
    kind = "guidance"
    HEADING = re.compile(r"^(?:Guideline|Section|Annex|Chapter|Part)\s+[IVX\d]+[A-Za-z]?(?:[.:]|\s|$)|^\d+(?:\.\d+)?\s+[A-Z][A-Za-z].{0,80}$")
    PROVISION = re.compile(r"^(?P<ref>\d{1,3})\.\s+(?=\S)")

    def make_ref(self, match, context) -> str:
        section = context.get("section") or ""
        g = re.match(r"^Guideline\s+(\d+)", section)
        para = match.group("ref")
        return f"G{g.group(1)}.para{para}" if g else f"para{para}"


class FatfAdapter(NumberedPdfAdapter):
    key = "fatf"
    publisher = "Financial Action Task Force"
    issuer = publisher
    jurisdiction = "INTL"
    kind = "standard"
    HEADING = re.compile(r"^(?:INTERPRETIVE NOTE TO RECOMMENDATION\s+\d+|(?:[A-Z]\.\s+)?[A-Z][A-Z /.,&\-–]{6,}$)")
    PROVISION = re.compile(r"^(?P<ref>\d{1,2})\.\s+(?=[A-Z])")

    def make_ref(self, match, context) -> str:
        section = context.get("section") or ""
        note = re.match(r"^INTERPRETIVE NOTE TO RECOMMENDATION\s+(\d+)", section, re.I)
        n = match.group("ref")
        return f"INR.{note.group(1)}.{n}" if note else f"R.{n}"


class BisBaselAdapter(NumberedHtmlAdapter):
    key = "bis_basel"
    publisher = "Bank for International Settlements (Basel Committee)"
    issuer = "Basel Committee on Banking Supervision"
    jurisdiction = "INTL"
    kind = "standard"
    PROVISION = re.compile(r"^(?P<ref>[A-Z]{3}\d{1,2}\.\d{1,3})(?=\s|$)")


class IoscoAdapter(NumberedPdfAdapter):
    key = "iosco"
    publisher = "International Organization of Securities Commissions"
    issuer = publisher
    jurisdiction = "INTL"
    kind = "standard"
    HEADING = re.compile(r"^(?:[A-Z]\.\s+)?Principles?\s+(?:for|relating to|of)\b.{0,90}$")
    PROVISION = re.compile(r"^(?:Principle\s+)?(?P<ref>\d{1,2})[.:]?\s+(?=[A-Z])")

    def make_ref(self, match, context) -> str:
        return f"P.{match.group('ref')}"


class MasAdapter(NumberedPdfAdapter):
    key = "mas"
    publisher = "Monetary Authority of Singapore"
    issuer = publisher
    jurisdiction = "SG"
    kind = "guidance"
    HEADING = re.compile(r"^\d{1,2}\s+[A-Z][A-Za-z ,&\-/()]{3,80}$")
    PROVISION = re.compile(r"^(?P<ref>\d{1,2}(?:\.\d{1,2}){1,2})\s+(?=\S)")


class AsicAdapter(NumberedPdfAdapter):
    key = "asic"
    publisher = "Australian Securities and Investments Commission"
    issuer = publisher
    jurisdiction = "AU"
    kind = "guidance"
    HEADING = re.compile(r"^(?:[A-Z]\s+[A-Z][A-Za-z ,&\-/()]{3,80}|Key points|Overview)$")
    PROVISION = re.compile(r"^(?P<ref>RG\s?\d{1,3}\.\d{1,3})\s+(?=\S)")

    def make_ref(self, match, context) -> str:
        return re.sub(r"^RG\s?", "RG ", match.group("ref"))


class IsaAdapter(NumberedPdfAdapter):
    key = "isa"
    publisher = "Israel Securities Authority"
    issuer = publisher
    jurisdiction = "IL"
    kind = "law"
    HEADING = re.compile(r"^(?:Chapter|CHAPTER|Part|PART)\s+[A-Z\d]+[A-Za-z]?(?:[:.]|\s|$)")
    PROVISION = re.compile(r"^(?P<ref>\d{1,3}[A-Z]{0,2})\.\s+(?=\S)")

    def make_ref(self, match, context) -> str:
        return f"s{match.group('ref')}"


class IrsRevProcAdapter(NumberedPdfAdapter):
    key = "irs_gov"
    publisher = "Internal Revenue Service"
    issuer = publisher
    jurisdiction = "US"
    kind = "agreement"
    HEADING = re.compile(r"^SECTION\s+\d+\.\s*\S")
    PROVISION = re.compile(r"^(?P<ref>\.\d{2})\s+(?=\S)")

    def make_ref(self, match, context) -> str:
        section = context.get("section") or ""
        s = re.match(r"^SECTION\s+(\d+)", section)
        return f"sec{s.group(1)}{match.group('ref')}" if s else f"sec{match.group('ref')}"
