# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Restricted-license sources (Class D): ISO 27001, SOC 2 TSC, PCI DSS, IFRS.

The pipeline must approve acquire/store/parse permissions before calling this
adapter. An uploaded file alone is not permission. A missing file raises an
actionable error; no placeholder artifact or invented version is created.
"""
import hashlib
from datetime import datetime, timezone

from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.settings import get_settings


def _plain_text(body: bytes) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Unsupported authorized artifact format: provide PDF or valid UTF-8 plain text") from exc
    if any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
        raise ValueError("Unsupported authorized artifact format: binary content is not plain text")
    return text


def _list_restricted_objects(source_key: str) -> list[tuple[str, bytes, str]]:
    settings = get_settings()
    bucket = settings.clhear_datalake_bucket
    prefix = f"restricted/{source_key}/"
    try:
        import boto3

        client = boto3.client("s3", region_name=settings.aws_region)
        out = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if not key.startswith(prefix):
                    continue
                name = key[len(prefix):]
                # Only direct uploads are inputs. Pipeline originals/diffs live
                # in version subdirectories and must never become new inputs.
                if not name or "/" in name:
                    continue
                out.append((name, key))
        if len(out) > 1:
            raise ValueError(f"Ambiguous artifacts for {source_key}: provide exactly one authorized file directly under {prefix}")
        if not out:
            return []
        name, key = out[0]
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        ctype = "application/pdf" if body[:5] == b"%PDF-" else "application/octet-stream"
        return [(name, body, ctype)]
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Cannot read authorized artifacts for {source_key}: object-store access failed") from exc


class RestrictedFileAdapter:
    key = "restricted_file"

    def __init__(self, source_key: str, title: str, url: str = "", meta: SourceMeta | None = None):
        self._source_key = source_key
        self._title = title
        self._url = url
        self._meta = meta
        self.artifact_check = None

    def meta(self) -> SourceMeta:
        if self._meta is not None:
            return self._meta
        return SourceMeta(
            family_key="standards",
            family_name="Standards & frameworks",
            source_key=self._source_key,
            name=self._title,
            kind="standard",
            issuer="",
            jurisdiction="INTL",
            license="restricted",
            license_ref="Explicit permission evidence required; uploaded files do not grant processing or display rights",
            canonical_url=self._url,
            adapter=self.key,
            short_name=self._title,
            version_policy="edition",
        )

    def _tree_from_bytes(self, body: bytes, ctype: str) -> list[DocNode]:
        if body[:5] == b"%PDF-":
            from app.clhear.l1.adapters.pdf_docling import pages_to_tree, extract_pdf_pages
            return pages_to_tree(extract_pdf_pages(body), self._source_key, self._title)
        if b"<html" in body[:4000].lower() or b"<!doctype" in body[:200].lower() or ctype == "text/html":
            from app.clhear.l1.adapters.html_document import parse
            try:
                return parse(body, self._source_key)
            except ValueError:
                pass
            from app.clhear.l1.originals import html_text
            text = html_text(body)
            if not text.strip():
                raise ValueError("HTML original contains no document text")
            return [DocNode(node_type="title", ref=self._source_key, heading="",
                            children=[DocNode(node_type="paragraph", raw_text=text)])]
        text = _plain_text(body)
        return [DocNode(
            node_type="title",
            ref=self._source_key,
            heading="",
            children=[DocNode(node_type="paragraph", raw_text=text)],
        )]

    def fetch(self, since_version: str | None = None) -> FetchResult | None:
        self.artifact_check = None
        files = _list_restricted_objects(self._source_key)
        from_url = False
        if not files:
            raise FileNotFoundError(
                f"Awaiting authorized source artifact for {self._source_key}; provide the actual licensed file "
                "after recording explicit acquire/store/parse permissions. No placeholder version was ingested."
            )
        if len(files) != 1:
            raise ValueError(f"Ambiguous artifacts for {self._source_key}: select exactly one authorized source file")
        name, body, ctype = files[0]
        if not body:
            raise ValueError(f"Authorized source artifact for {self._source_key} is empty")
        tree = self._tree_from_bytes(body, ctype)
        self.artifact_check = {
            "schema": "clhear.authorized-artifact-check.v1", "source_key": self._source_key,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "method": "publisher_url_read" if from_url else "authorized_artifact_store_read",
            "publisher_check_performed": from_url,
            "artifacts": [{"name": name, "sha256": hashlib.sha256(body).hexdigest(),
                           "byte_count": len(body), "content_type": ctype}],
        }
        return FetchResult(
            # Identifies acquired bytes, not an invented publisher edition/date.
            version_label=f"edition:acquired-sha256-{hashlib.sha256(body).hexdigest()}",
            artifacts=[Artifact(name=name, content=body, content_type=ctype)],
            tree=tree,
            version_kind="edition",
            as_of_date=None,
        )

    def expected_text(self, artifacts: list[Artifact]) -> list[str]:
        spans: list[str] = []
        for artifact in artifacts:
            if artifact.content[:5] == b"%PDF-":
                from app.clhear.l1.originals import pdf_original
                spans.append(pdf_original(artifact.content)[0])
            elif artifact.content_type == "text/html" or b"<html" in artifact.content[:4000].lower():
                from app.clhear.l1.originals import html_text
                text = html_text(artifact.content)
                if text.strip():
                    spans.append(text)
            else:
                text = _plain_text(artifact.content)
                spans.extend(line.strip() for line in text.splitlines() if line.strip())
        return spans
