"""The policy engine says which rule decided, and `scout-mcp policy` lets anyone ask it questions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scout_mcp.auth.policy import PolicyEngine
from scout_mcp.cli import main

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "config" / "policy.yaml"
CASES = ROOT / "config" / "policy_test.yaml"
COPILOT = ("agent:x", "role:support_copilot")


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine.from_file(POLICY)


# ── The deciding rule ──────────────────────────────────────────────────────
def test_an_allow_names_the_rule_that_allowed_it(engine):
    d = engine.decide(COPILOT, "acme", "tool:postgres:read", "orders", {})
    assert d.allowed and d.rule == "support-agents-build-context"


def test_a_deny_rule_is_named_and_beats_the_allow(engine):
    d = engine.decide(COPILOT, "acme", "tool:postgres:read", "orders",
                      {"sql": "DROP TABLE orders", "columns": []})
    assert not d.allowed and d.rule == "no-one-can-drop-tables" and d.code == "denied_by_policy"


def test_no_matching_rule_is_a_denial_with_no_rule(engine):
    d = engine.decide(("agent:x", "role:nobody"), "acme", "tool:postgres:read", "orders", {})
    assert not d.allowed and d.rule is None and d.code == "not_authorized"


# ── The glob weakness: a segment name must be the top-level folder ─────────
@pytest.mark.parametrize("key", ["docs/a.txt", "docs/faq/refunds.md", "tickets/1001.json"])
def test_support_agents_read_keys_under_docs_and_tickets(engine, key):
    assert engine.decide(COPILOT, "acme", "tool:s3:read", key, {}).allowed


@pytest.mark.parametrize("key", ["x/docs/y", "finance/tickets/payroll.csv", "../globex/docs/a.txt", "docs/../finance/payroll.csv"])
def test_a_key_that_only_contains_docs_or_tickets_deeper_down_is_refused(engine, key):
    d = engine.decide(COPILOT, "acme", "tool:s3:read", key, {})
    assert not d.allowed and d.code == "not_authorized"


def test_a_rule_pinned_to_one_tenant_does_not_match_another():
    from scout_mcp.auth.policy import Rule
    only_acme = PolicyEngine([Rule("r", ("role:a",), ("tool:t:read",), ("tenant:acme/*",))])
    assert only_acme.decide(("role:a",), "acme", "tool:t:read", "orders", {}).allowed
    assert not only_acme.decide(("role:a",), "globex", "tool:t:read", "orders", {}).allowed


def test_check_still_raises_with_the_same_codes(engine):
    from scout_mcp.errors.framework import PolicyError
    with pytest.raises(PolicyError) as exc:
        engine.check(COPILOT, "acme", "tool:s3:read", "x/docs/y", {})
    assert exc.value.code == "not_authorized"


# ── The case file ──────────────────────────────────────────────────────────
def test_every_rule_in_policy_yaml_is_the_deciding_rule_of_some_case(engine):
    rules = {r["id"] for r in yaml.safe_load(POLICY.read_text())["rules"]}
    cases = yaml.safe_load(CASES.read_text())["cases"]
    decided = set()
    for c in cases:
        d = engine.decide(tuple(c["subjects"]), c["tenant"], c["action"], c["resource"], c.get("context") or {})
        decided.add(d.rule)
    assert rules - decided == set(), f"no case is decided by: {sorted(rules - decided)}"


# ── The commands ───────────────────────────────────────────────────────────
def test_policy_test_passes_on_the_shipped_files_and_says_what_it_covered(capsys):
    assert main(["policy", "test", "--policy", str(POLICY), "--cases", str(CASES)]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and "cases passed" in out and "rules exercised" in out


def test_policy_test_fails_and_names_the_case_when_a_rule_changes(tmp_path, capsys):
    loosened = yaml.safe_load(POLICY.read_text())
    loosened["rules"][1]["resources"] = ["tenant:*/*"]  # support agents can now read any S3 key
    changed = tmp_path / "policy.yaml"
    changed.write_text(yaml.safe_dump(loosened))
    assert main(["policy", "test", "--policy", str(changed), "--cases", str(CASES)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_policy_check_prints_the_decision_and_the_deciding_rule(capsys):
    code = main(["policy", "check", "--policy", str(POLICY), "--subject", "role:support_copilot",
                 "--tenant", "acme", "--action", "tool:s3:read", "--resource", "x/docs/y"])
    out = capsys.readouterr().out
    assert code == 1 and "DENY" in out and "no policy rule allows" in out
    code = main(["policy", "check", "--policy", str(POLICY), "--subject", "role:support_copilot",
                 "--tenant", "acme", "--action", "tool:s3:read", "--resource", "docs/a.txt"])
    out = capsys.readouterr().out
    assert code == 0 and "ALLOW" in out and "support-agents-can-read-s3" in out


def test_no_arguments_still_means_start_the_server(monkeypatch):
    started = []
    monkeypatch.setattr("scout_mcp.server.main", lambda: started.append(True))
    assert main([]) == 0 and started == [True]
