# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0024 — explicit, auditable protected-source operation permissions.

No permissions are seeded. Existing sources, rights records, and stored text
are unchanged; callers must obtain a grant before a protected operation.
"""
from sqlalchemy.engine import Connection

from app.clhear.l1.permissions import source_permissions


def upgrade(conn: Connection) -> None:
    source_permissions.create(conn, checkfirst=True)
