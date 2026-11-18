"""The statement that sets the tenant is built from a validated name, never from raw input."""

from __future__ import annotations

import pytest

from scout_mcp.tenant_sql import set_tenant_sql


def test_a_transaction_local_and_a_session_statement():
    assert set_tenant_sql("acme") == "SET LOCAL app.tenant_id = 'acme'"
    assert set_tenant_sql("globex-2_x", local=False) == "SET app.tenant_id = 'globex-2_x'"


@pytest.mark.parametrize("tenant", [
    "", "Acme", "ac me", "acme'; DROP TABLE orders; --", "acme'", 'acme"', "acme\n", "acme\\", "-acme",
    "a" * 64, "acme;", "acme--x", "ac/me", "acmé", "acme\x00",
])
def test_a_name_with_anything_a_quote_could_hide_is_refused(tenant):
    with pytest.raises(ValueError):
        set_tenant_sql(tenant)


def test_the_longest_name_is_accepted():
    assert set_tenant_sql("a" * 63).endswith(f"'{'a' * 63}'")
