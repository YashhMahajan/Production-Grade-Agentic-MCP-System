"""The statement that tells Postgres which tenant a connection is acting for.

Row-level security reads the ``app.tenant_id`` setting. It used to be set with
``set_config()``, which takes a bind parameter but is also a function any query could call
to point the setting at another tenant. The database now refuses ``set_config`` to every
role that is not a superuser (``deploy/sql/init.sql``), so the server sets the tenant with
``SET``, which a ``SELECT`` cannot issue. ``SET`` cannot take a bind parameter, so the name
is checked against the one shape a tenant may have before it goes into the statement.
"""

from __future__ import annotations

import re

# The same shape ``scout-mcp tenant add`` accepts. Nothing in it needs quoting.
_TENANT = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")


def set_tenant_sql(tenant: str, *, local: bool = True) -> str:
    """``SET LOCAL`` for one transaction, or ``SET`` for the life of a connection."""
    if not _TENANT.fullmatch(tenant):
        raise ValueError("a tenant id may only hold lower case letters, digits, - and _")
    return f"SET {'LOCAL ' if local else ''}app.tenant_id = '{tenant}'"
