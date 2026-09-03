"""The planner. Every test runs without a network call.

The load-bearing assertion is that the gate outranks the model: a plan the
model is confident about is still refused when the contract disagrees.
"""

import json

import pytest

from assay.contracts.models import ContractSet, Metric
from assay.nlq.planner import Planner, PlannerConfig, plan_schema


def _metric(**overrides) -> Metric:
    base = {
        "name": "net_revenue",
        "table": "orders",
        "measure": "sum(amount)",
        "time_column": "ts",
        "additivity": "additive",
        "dimensions": ({"name": "region", "column": "region"},),
    }
    return Metric(**{**base, **overrides})


@pytest.fixture
def contracts() -> ContractSet:
    return ContractSet(metrics=(_metric(), _metric(name="active_users", dimensions=())))


class Transport:
    """Returns canned replies in order and records every request."""

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, body):
        self.requests.append(body)
        return (self._replies.pop(0) if self._replies else "{}"), 0.5


def _reply(**fields) -> str:
    return json.dumps({"answerable": True, **fields})


# ---- input guards -------------------------------------------------------------


def test_an_empty_question_never_reaches_the_model(contracts):
    t = Transport()
    result = Planner(contracts, transport=t).plan("   ")
    assert result.refusals[0].rule == "NLQ-00"
    assert t.requests == []


def test_an_overlong_question_never_reaches_the_model(contracts):
    t = Transport()
    config = PlannerConfig(max_question_chars=20)
    result = Planner(contracts, config=config, transport=t).plan("x" * 21)
    assert result.refusals[0].rule == "NLQ-00"
    assert t.requests == []


# ---- the happy path -----------------------------------------------------------


def test_a_legal_plan_survives_the_gate(contracts):
    t = Transport(_reply(select=["net_revenue"], by=["region"]))
    result = Planner(contracts, transport=t).plan("net revenue by region")
    assert result.answerable
    assert result.plan.select == ("net_revenue",)
    assert result.plan.by == ("region",)


def test_a_filter_is_carried_through(contracts):
    t = Transport(_reply(select=["net_revenue"],
                         where=[{"dimension": "region", "op": "eq", "value": "EMEA"}]))
    result = Planner(contracts, transport=t).plan("net revenue in EMEA")
    assert result.plan.where[0].dimension == "region"


def test_absent_time_means_all_time_not_now(contracts):
    t = Transport(_reply(select=["net_revenue"]))
    assert Planner(contracts, transport=t).plan("net revenue").plan.time.kind == "all"


def test_a_relative_period_is_carried_through(contracts):
    t = Transport(_reply(select=["net_revenue"],
                         time={"kind": "relative", "anchor": "month", "offset": -1}))
    plan = Planner(contracts, transport=t).plan("net revenue last month").plan
    assert plan.time.kind == "relative" and plan.time.offset == -1


# ---- the gate outranks the model ---------------------------------------------


def test_the_gate_refuses_a_plan_the_model_was_confident_about(contracts):
    """The exact failure the spike measured: slicing a metric with no dimensions."""
    t = Transport(_reply(select=["active_users"], by=["region"]))
    result = Planner(contracts, transport=t).plan("active users by region")
    assert not result.answerable
    assert result.plan is None
    assert result.refusals[0].rule == "STR-02"


def test_a_hallucinated_metric_is_refused_by_the_gate(contracts):
    t = Transport(json.dumps({"answerable": True, "select": ["churn_rate"]}))
    result = Planner(contracts, transport=t).plan("what is churn")
    assert result.refusals[0].rule == "STR-01"


def test_the_models_refusal_stands_even_where_the_gate_would_allow(contracts):
    """Declining costs a retry. Answering wrongly costs the trust."""
    t = Transport(json.dumps({"answerable": False, "refusal_reason": "too vague"}))
    result = Planner(contracts, transport=t).plan("revenue?")
    assert result.refusals[0].rule == "NLQ-01"
    assert result.refusals[0].reason == "too vague"


# ---- malformed replies --------------------------------------------------------


def test_fenced_json_is_still_parsed(contracts):
    t = Transport('```json\n{"answerable": true, "select": ["net_revenue"]}\n```')
    assert Planner(contracts, transport=t).plan("net revenue").answerable


def test_prose_around_the_json_is_tolerated(contracts):
    t = Transport('Sure! {"answerable": true, "select": ["net_revenue"]} Hope that helps.')
    assert Planner(contracts, transport=t).plan("net revenue").answerable


def test_unparseable_output_gets_exactly_one_repair_attempt(contracts):
    t = Transport("not json at all", "still not json")
    result = Planner(contracts, transport=t).plan("net revenue")
    assert len(t.requests) == 2
    assert result.refusals[0].rule == "NLQ-02"


def test_a_repair_attempt_can_succeed(contracts):
    t = Transport("not json", _reply(select=["net_revenue"]))
    result = Planner(contracts, transport=t).plan("net revenue")
    assert result.answerable and result.attempts == 2


def test_the_repair_turn_shows_the_model_its_own_output(contracts):
    t = Transport("garbage", _reply(select=["net_revenue"]))
    Planner(contracts, transport=t).plan("net revenue")
    assert t.requests[1]["messages"][-2]["content"].startswith("garbage")


def test_a_plan_that_selects_nothing_is_malformed(contracts):
    t = Transport(json.dumps({"answerable": True, "select": []}))
    assert Planner(contracts, transport=t).plan("?").refusals[0].rule == "NLQ-02"


# ---- outage -------------------------------------------------------------------


def test_an_outage_becomes_a_refusal_not_a_crash(contracts):
    def dead(body):
        raise RuntimeError("503 upstream")

    result = Planner(contracts, transport=dead).plan("net revenue")
    assert result.refusals[0].rule == "NLQ-03"
    assert "503" in result.refusals[0].repair


# ---- the request itself -------------------------------------------------------


def test_legal_names_are_enums_in_the_schema(contracts):
    schema = plan_schema(contracts)
    assert schema["properties"]["select"]["items"]["enum"] == ["active_users", "net_revenue"]
    assert schema["properties"]["by"]["items"]["enum"] == ["region"]


def test_output_length_is_capped(contracts):
    t = Transport(_reply(select=["net_revenue"]))
    Planner(contracts, config=PlannerConfig(max_tokens=42), transport=t).plan("x")
    assert t.requests[0]["max_tokens"] == 42


def test_generation_is_deterministic(contracts):
    t = Transport(_reply(select=["net_revenue"]))
    Planner(contracts, transport=t).plan("x")
    assert t.requests[0]["temperature"] == 0
