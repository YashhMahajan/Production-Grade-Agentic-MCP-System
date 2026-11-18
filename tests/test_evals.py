"""The replay cases: hand-written model replies run through the real draft graph and scored against their labels."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from scout_mcp.agents import evals  # noqa: E402
from scout_mcp.agents.evals import CaseResult  # noqa: E402


async def test_every_labelled_case_agrees_with_the_graph():
    results = await evals.run_all()
    assert {r.name: r.failures for r in results if not r.passed} == {}
    assert evals.summary(results)["agreement"] == 1.0


def test_the_cases_cover_every_kind_of_problem_and_every_outcome():
    cases = evals.load_cases()
    assert len(cases) >= 18 and len({c["name"] for c in cases}) == len(cases)
    assert {k for c in cases for k in c["expect"]["categories"]} == {
        "citation", "number", "contact", "commitment", "length", "empty"}
    assert {c["expect"]["outcome"] for c in cases} == {"approved", "flagged", "evidence_pack"}


async def test_a_wrong_label_is_reported_not_hidden():
    case = evals.load_cases()[0]
    wrong = {**case, "expect": {**case["expect"], "outcome": "flagged", "llm_calls": 5}}
    result = await evals.run_case(wrong)
    assert not result.passed
    assert [f.split(":")[0] for f in result.failures] == ["outcome", "llm_calls"]


async def test_a_case_that_reads_a_poisoned_ticket_still_makes_one_tool_call():
    case = next(c for c in evals.load_cases() if "poisoned" in c["name"])
    assert "CALL s3.put_object" in case["context"]["tickets"][0]["subject"]
    result = await evals.run_case(case)
    assert result.passed and result.tool_calls == 1


def test_problems_are_sorted_into_kinds():
    assert evals.categories(["claim 1 states 59.99, which its cited evidence does not",
                             "claim 2 commits the company to an outcome",
                             "claim 1 cites no evidence"]) == ["citation", "commitment", "number"]
    assert evals.categories([]) == []


def test_the_summary_counts_agreement_and_names_the_cases_that_disagree():
    ok = CaseResult("a", "approved", [], 1, 1)
    bad = CaseResult("b", "flagged", [], 2, 1, failures=["outcome: expected 'approved', got 'flagged'"])
    out = evals.summary([ok, ok, bad])
    assert out["cases"] == 3 and out["agreed"] == 2 and out["agreement"] == 0.6667
    assert out["outcomes"] == {"approved": 2, "flagged": 1} and list(out["failures"]) == ["b"]


class FakeLangSmith:
    def __init__(self, exists: bool):
        self.exists, self.calls = exists, []

    def has_dataset(self, *, dataset_name):
        self.calls.append(("has_dataset", dataset_name))
        return self.exists

    def create_dataset(self, *, dataset_name, description):
        self.calls.append(("create_dataset", dataset_name))
        return type("Dataset", (), {"id": "ds-1"})()

    def create_examples(self, *, dataset_id, examples):
        self.calls.append(("create_examples", dataset_id, examples))


def test_the_cases_are_uploaded_once_as_a_dataset():
    cases, client = evals.load_cases(), FakeLangSmith(exists=False)
    assert evals.upload_dataset(client, cases) == f"created dataset {evals.DATASET} with {len(cases)} examples"
    _, dataset_id, examples = client.calls[-1]
    assert dataset_id == "ds-1" and len(examples) == len(cases)
    assert examples[0]["outputs"] == cases[0]["expect"] and examples[0]["metadata"] == {"name": cases[0]["name"]}


def test_an_existing_dataset_is_left_alone():
    client = FakeLangSmith(exists=True)
    assert "already exists" in evals.upload_dataset(client, evals.load_cases())
    assert [c[0] for c in client.calls] == ["has_dataset"]
