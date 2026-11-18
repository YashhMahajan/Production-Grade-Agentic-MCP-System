"""Response targets: which layer wins, where a ticket stands against its target, and a bad file."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout_mcp.agents.sla import Sla, SlaError, age_hours

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
DATA = {
    "default": {"high": 4, "normal": 24, "low": 72},
    "tiers": {"gold": {"high": 2, "normal": 12}},
    "tenants": {"acme": {"default": {"low": 48}, "tiers": {"platinum": {"high": 1}, "gold": {"high": 3}}}},
}
sla = Sla(DATA)


def ticket(hours_ago, priority="high", status="open"):
    created = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"id": "T", "priority": priority, "status": status, "created_at": created}


# ── which target applies ───────────────────────────────────────────────────
@pytest.mark.parametrize("tenant, tier, priority, hours", [
    ("globex", None, "high", 4),          # the default
    ("globex", "gold", "high", 2),        # a tier beats the default
    ("globex", "gold", "low", 72),        # a priority the tier leaves out falls through
    ("acme", None, "low", 48),            # a tenant's default beats the global one
    ("acme", "platinum", "high", 1),      # a tenant's tier is the most specific
    ("acme", "gold", "high", 3),          # and beats the global tier
    ("acme", "gold", "normal", 12),       # while a priority it leaves out comes from the next layer
    ("globex", None, "urgent", None),     # no target for an unknown priority
])
def test_the_most_specific_target_wins(tenant, tier, priority, hours):
    assert sla.hours(tenant, tier, priority) == hours


# ── where a ticket stands ──────────────────────────────────────────────────
def status(t, tier=None, tenant="globex"):
    return sla.for_tenant(tenant).status(t, tier, NOW)


def test_a_ticket_is_ok_nearing_or_past_its_target():
    assert status(ticket(2)).state == "ok"           # 2h of 4h
    assert status(ticket(3)).state == "at_risk"      # 75% of 4h
    assert status(ticket(4)).state == "at_risk"      # exactly on the target is not yet past it
    assert status(ticket(5)).state == "breached"


def test_the_description_names_the_target_and_the_hours_open():
    assert status(ticket(5)).describe() == "SLA target 4h, breached (5h open)"
    assert status(ticket(3)).describe() == "SLA target 4h, at risk (3h open)"


def test_a_closed_ticket_a_missing_target_or_a_bad_date_has_no_status():
    assert status(ticket(100, status="closed")) is None
    assert status(ticket(100, priority="urgent")) is None
    assert status({**ticket(1), "created_at": "not a date"}) is None
    assert status({**ticket(1), "created_at": None}) is None


def test_a_pending_ticket_counts_and_a_date_with_no_zone_is_read_as_utc():
    assert status(ticket(9, status="pending")).state == "breached"
    assert age_hours("2026-11-16T06:00:00", NOW) == 6.0
    assert age_hours("2026-11-16T06:00:00Z", NOW) == 6.0
    assert age_hours("2099-01-01T00:00:00Z", NOW) == 0.0  # a time in the future is not negative


# ── the file ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("data, message", [
    ({"defualt": {"high": 4}}, "unknown key"),
    ({"default": {"high": 0}}, "default.high"),
    ({"default": {"high": "4"}}, "default.high"),
    ({"default": {"high": True}}, "default.high"),
    ({"default": [4]}, "must map"),
    ({"tiers": {"gold": {"high": -1}}}, "tiers.gold.high"),
    ({"tenants": {"acme": {"tiers": {"gold": {"low": 0}}}}}, "tenants.acme.tiers.gold.low"),
])
def test_a_file_with_the_wrong_shape_is_refused_and_says_where(data, message):
    with pytest.raises(SlaError, match=message):
        Sla(data)


def test_the_file_in_the_repository_loads_and_gives_every_seeded_priority_a_target():
    real = Sla.load("config/sla.yaml")
    assert real.hours("acme", None, "high") is not None and real.hours("acme", "gold", "normal") is not None
    assert real.hours("acme", None, "low") is not None


def test_a_missing_file_is_not_hidden(tmp_path):
    with pytest.raises(FileNotFoundError):
        Sla.load(tmp_path / "nope.yaml")
