# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0045 — penalties the binding texts state, as an L7 risk input.

* ``l7_risk.stated_penalties`` (``PEN-``): a penalty a law or regulation in scope
  states for a breach, with its type and stated maximum, quoted;
* ``l7_risk.penalty_links``: the obligations each penalty clause refers to.

Both are built by the next run (``app.clhear.l7.penalties``). The risk method
becomes ``risk-v3``, with the dimension ``stated_penalty``.
"""
from sqlalchemy.engine import Connection

from app.clhear.l7.models import penalty_links, stated_penalties


def upgrade(conn: Connection) -> None:
    stated_penalties.create(conn, checkfirst=True)
    penalty_links.create(conn, checkfirst=True)
