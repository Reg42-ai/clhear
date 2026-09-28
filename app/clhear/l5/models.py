# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L5 junction model.

An activity is what an organisation *does*, on one of two sides:

* ``compliance`` — the action a duty describes, in the duty's own words
  ("review user access rights"); it operates the L3 measures that duty needs;
* ``business``   — an activity of the organisation that texts in scope link to
  a duty. No catalogue of business activities is shipped.

An activity's ``action_type`` is the duty's own verb, not a fixed vocabulary.
"""
from __future__ import annotations

SIDES = ("business", "compliance")


def is_valid_side(side: str) -> bool:
    return side in SIDES
