"""Claim checks and the model loop around a failed call. No framework, no network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout_mcp.agents.claims import Claim, DraftOut, assemble, build_evidence, check_draft
from scout_mcp.agents.sla import Sla
from scout_mcp.agents.llm import (
    CallBudget, FinalLLMError, LLMUnavailable, classify, invoke_models, model_names, parse_json_lenient)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
CONTEXT = {
    "customer_id": "CUST-1001",
    "profile": {"name": "Ana Reyes", "tier": "gold", "created_at": "2024-02-03T00:00:00Z"},
    "orders": [{"id": "o_9021", "status": "refund_pending", "total_cents": 4999, "currency": "USD",
                "created_at": "2026-11-01T00:00:00Z"}],
    "tickets": [{"id": "TCK-7", "subject": "Refund delayed", "status": "open", "priority": "high",
                 "created_at": "2026-11-10T09:00:00Z"}],
    "docs": [],
}


def evidence():
    return build_evidence(CONTEXT, NOW)


def draft(*claims, **kw) -> DraftOut:
    return DraftOut(claims=[Claim(text=t, cites=c) for t, c in claims], **kw)


# ── evidence ───────────────────────────────────────────────────────────────
def test_evidence_is_numbered_in_a_fixed_order_with_ticket_age():
    ev = evidence()
    assert [e.id for e in ev] == [1, 2, 3]
    assert [e.kind for e in ev] == ["profile", "order", "ticket"]
    assert "49.99 USD" in ev[1].text and "6 days ago" in ev[2].text


def test_a_ticket_line_states_its_sla_when_there_is_one():
    sla = Sla({"default": {"high": 4}, "tiers": {"gold": {"high": 2}}}).for_tenant("acme")
    ticket = build_evidence(CONTEXT, NOW, sla)[2].text
    assert "SLA target 2h, breached (147h open)" in ticket
    assert "SLA" not in evidence()[2].text


def test_a_context_with_nothing_in_it_has_no_evidence():
    assert build_evidence({"customer_id": "C", "orders": [], "tickets": []}, NOW) == []


# ── checks ─────────────────────────────────────────────────────────────────
def test_a_claim_that_matches_its_evidence_passes():
    out = draft(("Order o_9021 totals 49.99 USD and is refund_pending.", [2]),
                ("Ticket TCK-7 has been open for 6 days.", [3]))
    assert check_draft(out, evidence()) == []


def test_a_number_the_cited_evidence_does_not_hold_fails():
    out = draft(("Order o_9021 totals 59.99 USD.", [2]))
    (problem,) = check_draft(out, evidence())
    assert "59.99" in problem


def test_a_number_held_only_by_other_evidence_fails():
    out = draft(("Ticket TCK-7 is about order 9021.", [3]))
    assert any("9021" in p for p in check_draft(out, evidence()))


def test_a_citation_to_missing_evidence_fails():
    assert any("[S9]" in p for p in check_draft(draft(("Refund pending.", [9])), evidence()))


def test_a_claim_with_no_citation_fails():
    assert any("cites no evidence" in p for p in check_draft(draft(("Refund pending.", [])), evidence()))


@pytest.mark.parametrize("text", [
    "We will refund the order.", "I will refund you today.", "We guarantee a reply.",
    "The refund has been approved.", "It will be refunded soon.", "We promise a fix.",
    "We shall refund this.", "We're going to credit your account.", "We are going to waive the fee.",
    "You will receive a full refund.", "You'll get your refund.", "I would be happy to waive the fee.",
    "We'll gladly replace it.", "We can also compensate you.",
])
def test_a_commitment_fails(text):
    assert any("commits" in p for p in check_draft(draft((text, [2])), evidence()))


@pytest.mark.parametrize("text", [
    "Order o_9021 is refund_pending and the refund needs human approval.",
    "You can ask for a refund through the portal.",
    "Order o_9021 is refund_pending.",
])
def test_saying_a_refund_needs_approval_is_not_a_commitment(text):
    assert check_draft(draft((text, [2])), evidence()) == []


def test_a_contact_detail_the_evidence_does_not_hold_fails():
    out = draft(("Write to ana@example.com.", [1]))
    assert any("contact detail" in p for p in check_draft(out, evidence()))


def test_a_draft_over_the_word_limit_fails():
    out = draft(*[(" ".join(["word"] * 45), [1])] * 4, ("word", [1]))
    assert any("181 words" in p for p in check_draft(out, evidence()))


def test_no_claims_is_a_problem_unless_the_model_says_the_evidence_is_insufficient():
    assert check_draft(draft(), evidence()) == ["the draft makes no claims"]
    assert check_draft(draft(insufficient=True), evidence()) == []


def test_claims_are_assembled_with_their_citations():
    out = draft(("Order is pending.", [2]), ("Ticket is open.", [3, 1]))
    assert assemble(out) == "Order is pending. [S2] Ticket is open. [S3] [S1]"
    assert "enough evidence" in assemble(draft(insufficient=True))


# ── a failed model call ────────────────────────────────────────────────────
class Boom(Exception):
    def __init__(self, status, body=""):
        super().__init__(f"HTTP {status}")
        self.status_code, self.body = status, body


class Msg:
    def __init__(self, content):
        self.content = content


class Model:
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return Msg(item)


def test_which_failures_are_final():
    assert [classify(Boom(s)) for s in (400, 401, 402, 403)] == ["final"] * 4
    assert classify(Boom(429, "free-models-per-day")) == "final"
    assert classify(Boom(429, "provider busy")) == "transient"
    assert classify(Boom(503)) == "transient" and classify(TimeoutError()) == "transient"


async def test_the_second_model_answers_when_the_first_fails_transiently():
    a, b, counter = Model(Boom(503)), Model("ok"), [0]
    assert await invoke_models([a, b], [], counter) == "ok" and counter == [2]


async def test_an_empty_reply_tries_the_next_model():
    a, b, counter = Model("  "), Model("ok"), [0]
    assert await invoke_models([a, b], [], counter) == "ok" and counter == [2]


async def test_a_final_failure_does_not_try_the_second_model():
    a, b, counter = Model(Boom(401)), Model("ok"), [0]
    with pytest.raises(FinalLLMError):
        await invoke_models([a, b], [], counter)
    assert b.calls == 0 and counter == [1]


async def test_a_daily_cap_does_not_try_the_second_model():
    a, b = Model(Boom(429, "free-models-per-day")), Model("ok")
    with pytest.raises(FinalLLMError):
        await invoke_models([a, b], [], [0])
    assert b.calls == 0


async def test_every_model_failing_raises_unavailable():
    with pytest.raises(LLMUnavailable):
        await invoke_models([Model(Boom(503)), Model(Boom(500))], [], [0])


async def test_the_limit_stops_a_fallback_attempt():
    a, b, counter = Model(Boom(503)), Model("ok"), [0]
    with pytest.raises(CallBudget):
        await invoke_models([a, b], [], counter, limit=1)
    assert b.calls == 0 and counter == [1]


# ── reading a reply ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    '{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go: {"a": 1} Hope that helps.', '  \n{"a": 1}\n',
])
def test_the_json_object_is_found_inside_a_fence_or_stray_prose(text):
    assert parse_json_lenient(text) == {"a": 1}


@pytest.mark.parametrize("text", ["", "no braces here", "{not json}", "[1, 2]"])
def test_a_reply_with_no_json_object_raises(text):
    with pytest.raises(ValueError):
        parse_json_lenient(text)


# ── which models ───────────────────────────────────────────────────────────
def test_the_primary_comes_first_and_the_fallback_second(monkeypatch):
    monkeypatch.setenv("SCOUT_LLM_MODEL", "a:free")
    monkeypatch.setenv("SCOUT_LLM_FALLBACK_MODEL", "b:free")
    assert model_names() == ["a:free", "b:free"]


@pytest.mark.parametrize("fallback", ["", "a:free"])
def test_an_empty_or_repeated_fallback_means_one_model(monkeypatch, fallback):
    monkeypatch.setenv("SCOUT_LLM_MODEL", "a:free")
    monkeypatch.setenv("SCOUT_LLM_FALLBACK_MODEL", fallback)
    assert model_names() == ["a:free"]


def test_the_defaults_are_two_different_free_models(monkeypatch):
    monkeypatch.delenv("SCOUT_LLM_MODEL", raising=False)
    monkeypatch.delenv("SCOUT_LLM_FALLBACK_MODEL", raising=False)
    names = model_names()
    assert len(names) == 2 and names[0] != names[1] and all(n.endswith(":free") for n in names)
