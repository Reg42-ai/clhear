# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Engine tests: a fresh SQLite install per test, no network, no AWS."""
from __future__ import annotations

import pytest


@pytest.fixture()
def install(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clhear.db'}")
    monkeypatch.setenv("CLHEAR_SCOPES_DIR", str(tmp_path / "scopes"))
    monkeypatch.setenv("CLHEAR_LOCAL_SOURCES_DIR", str(tmp_path / "sources"))
    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CLHEAR_HTTP_MODE", "replay")
    monkeypatch.setenv("CLHEAR_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("CLHEAR_SOURCE_SCOPE", raising=False)
    monkeypatch.delenv("CLHEAR_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CLHEAR_LLM_MODEL", raising=False)
    (tmp_path / "sources").mkdir()
    from app.clhear.runtime import reset

    reset()
    return tmp_path


def pdf_bytes(lines: list[str]) -> bytes:
    """A minimal one-page PDF with a text layer, one line per entry."""
    ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
