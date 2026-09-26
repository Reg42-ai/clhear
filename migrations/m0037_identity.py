# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0037 — identity schema for the web tier, and its least-privilege role.

The role can read and write only the identity tables, ``community.users``,
``community.api_keys`` and the id high-water marks the key ids use. Its
password is read from SSM; without it the tables are still created and the
web tier keeps its previous behavior.
"""
import logging
import re

from sqlalchemy.engine import Connection

from app.clhear.identity import PASSWORD_PARAMETER, ROLE
from app.clhear.identity_models import IDENTITY_SCHEMA, TABLES

log = logging.getLogger("clhear.migrations")
_PASSWORD_RE = re.compile(r"^[A-Za-z0-9]{32,128}$")


def _password() -> str | None:
    try:
        import boto3

        from app.clhear.settings import get_settings

        value = boto3.client("ssm", region_name=get_settings().aws_region).get_parameter(
            Name=PASSWORD_PARAMETER, WithDecryption=True)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 — no parameter means no role yet, not a failed migration
        return None
    return value if _PASSWORD_RE.match(value or "") else None


def upgrade(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {IDENTITY_SCHEMA}")
    for table in TABLES:
        table.create(conn, checkfirst=True)
    if conn.dialect.name != "postgresql":
        return
    password = _password()
    if password is None:
        log.warning("identity role %s not configured: password parameter unavailable", ROLE)
        return
    conn.exec_driver_sql(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') "
        f"THEN CREATE ROLE {ROLE} LOGIN; END IF; END $$")
    # The password matches [A-Za-z0-9]{32,128}, so it is safe inside a literal.
    conn.exec_driver_sql(f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD '{password}'")
    conn.exec_driver_sql(f"GRANT CONNECT ON DATABASE {conn.exec_driver_sql('SELECT current_database()').scalar()} TO {ROLE}")
    for schema in (IDENTITY_SCHEMA, "community", "l0_platform"):
        conn.exec_driver_sql(f"GRANT USAGE ON SCHEMA {schema} TO {ROLE}")
    conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {IDENTITY_SCHEMA} TO {ROLE}")
    conn.exec_driver_sql(f"GRANT DELETE ON {IDENTITY_SCHEMA}.rate_windows TO {ROLE}")
    conn.exec_driver_sql(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {IDENTITY_SCHEMA} TO {ROLE}")
    conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE ON community.users, community.api_keys TO {ROLE}")
    conn.exec_driver_sql(f"GRANT SELECT, INSERT, UPDATE ON l0_platform.id_sequences TO {ROLE}")
