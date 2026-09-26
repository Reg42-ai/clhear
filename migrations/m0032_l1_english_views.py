# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Version-bound original/publisher-English links and derived English views."""
def upgrade(conn):
    from app.clhear.l1.translation_models import TABLES
    for table in TABLES:
        table.create(conn, checkfirst=True)
