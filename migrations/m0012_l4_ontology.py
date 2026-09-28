# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0012 — HLD v2 §4.4 L4 profile space.

* ontology tables: ``licences`` (register-backed authorisations), ``products_services``,
  ``client_types``, ``channels``; edges ``permits`` (licence -> product) and
  ``validity_rules`` ("no impossible permutation")
* ``profiles`` (validated attribute sets, PRF-000001) and ``applies_to``
  (obligation -> profile predicate, the L4 applicability edge)
* new ``channels`` attribute on the L4 attribute schema
* first ontology build from the reviewed register snapshot (no network: the
  nightly fleet re-checks the live registers) and the curated sample profiles
  stored as validated ``profiles`` rows (source = sample).
"""
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.derived_models import (
    applies_to,
    channels,
    client_types,
    licences,
    permits,
    products_services,
    profiles,
    validity_rules,
)
from app.clhear.platform.shared_schema import ensure_shared_columns

_TABLES = (licences, products_services, client_types, channels, permits, validity_rules, profiles, applies_to)


def upgrade(conn: Connection) -> None:
    for table in _TABLES:
        table.create(conn, checkfirst=True)
        ensure_shared_columns(conn, table)

    # The retired reviewed catalog (attribute schema, finance ontology, sample
    # profiles) is no longer seeded: a host's own sources define the questions.
