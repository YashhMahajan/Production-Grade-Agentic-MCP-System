"""Policy engine — deny-by-default, condition matching, deny-beats-allow."""

from __future__ import annotations

import pytest

from atlas_mcp.auth.policy import PolicyEngine, Rule
from atlas_mcp.errors.framework import PolicyError


def _engine(rules: list[Rule], default_deny: bool = True) -> PolicyEngine:
    return PolicyEngine(rules=rules, default_deny=default_deny)


def test_default_deny_when_no_rules_match():
    engine = _engine([Rule(id="r1", subjects=("role:nobody",), actions=("*",), resources=("*",))])
    with pytest.raises(PolicyError):
        engine.check(
            subject="role:analyst", tenant="acme", action="tool:postgres:read",
            resource="analytics/orders", context={},
        )


def test_allow_rule_permits_action():
    engine = _engine([
        Rule(id="r1", subjects=("role:analyst",),
             actions=("tool:postgres:read",), resources=("tenant:acme/*",))
    ])
    # Should not raise.
    engine.check(
        subject="role:analyst", tenant="acme", action="tool:postgres:read",
        resource="analytics/orders", context={},
    )


def test_deny_rule_overrides_allow():
    engine = _engine([
        Rule(id="allow", subjects=("*",), actions=("tool:postgres:read",),
             resources=("*",), effect="allow"),
        Rule(id="deny", subjects=("*",), actions=("tool:postgres:read",),
             resources=("*",), effect="deny",
             conditions={"sql_contains_any": ["DROP"]}),
    ])
    with pytest.raises(PolicyError) as exc:
        engine.check(
            subject="role:analyst", tenant="acme", action="tool:postgres:read",
            resource="orders", context={"sql": "SELECT * FROM orders; DROP TABLE orders"},
        )
    assert "deny" in exc.value.hint.lower() or "policy" in exc.value.code


def test_glob_resource_match():
    engine = _engine([
        Rule(id="r1", subjects=("role:analyst",), actions=("tool:postgres:read",),
             resources=("tenant:acme/*",))
    ])
    # Matching prefix succeeds.
    engine.check(
        subject="role:analyst", tenant="acme", action="tool:postgres:read",
        resource="anything/else", context={},
    )
    # Different tenant does not match.
    with pytest.raises(PolicyError):
        engine.check(
            subject="role:analyst", tenant="globex", action="tool:postgres:read",
            resource="anything", context={},
        )


def test_pii_condition_blocks_access():
    engine = _engine([
        Rule(id="allow", subjects=("*",), actions=("tool:postgres:read",),
             resources=("*",), effect="allow"),
        Rule(id="pii", subjects=("*",), actions=("tool:postgres:read",),
             resources=("*",), effect="deny",
             conditions={"pii_fields": ["ssn", "credit_card"]}),
    ])
    with pytest.raises(PolicyError):
        engine.check(
            subject="role:analyst", tenant="acme", action="tool:postgres:read",
            resource="customers", context={"columns": ["id", "name", "ssn"]},
        )
