"""The /api/ask guards. A public endpoint calling a paid model needs them."""

import pytest

from assay.contracts.models import ContractSet, Metric
from assay.nlq.planner import Planner
from deploy.railway.ask import AskService, Budget, RateLimiter


class Transport:
    def __init__(self, reply='{"answerable": true, "select": ["net_revenue"]}'):
        self.reply, self.calls = reply, 0

    def __call__(self, body):
        self.calls += 1
        return self.reply, 0.5


@pytest.fixture
def contracts() -> ContractSet:
    return ContractSet(metrics=(Metric(
        name="net_revenue", table="orders", measure="sum(amount)",
        time_column="ts", additivity="additive"),))


def _service(contracts, transport=None):
    transport = transport or Transport()
    return AskService(contracts, planner=Planner(contracts, transport=transport)), transport


def test_a_question_is_planned(contracts):
    service, t = _service(contracts)
    assert service.ask("net revenue")["answerable"] is True
    assert t.calls == 1


def test_the_same_question_is_not_paid_for_twice(contracts):
    service, t = _service(contracts)
    service.ask("net revenue")
    again = service.ask("  NET   Revenue  ")
    assert again["cached"] is True
    assert t.calls == 1


def test_the_cache_is_bounded(contracts):
    from deploy.railway import ask as module

    service, _ = _service(contracts)
    for i in range(module.MAX_CACHE + 5):
        service.ask(f"question {i}", caller=f"c{i}")
    assert len(service._cache) <= module.MAX_CACHE


def test_a_caller_is_rate_limited(contracts):
    service, t = _service(contracts)
    for i in range(25):
        result = service.ask(f"q{i}", caller="same")
    assert result["refusals"][0]["rule"] == "NLQ-04"
    assert t.calls == 20


def test_the_window_slides(contracts):
    limiter = RateLimiter(per_hour=2)
    assert limiter.allow("a", 0) and limiter.allow("a", 1)
    assert not limiter.allow("a", 2)
    assert limiter.allow("a", 3700)


def test_callers_are_limited_independently(contracts):
    limiter = RateLimiter(per_hour=1)
    assert limiter.allow("a", 0) and limiter.allow("b", 0)


def test_the_daily_budget_is_a_hard_ceiling(contracts):
    budget = Budget(limit=2)
    assert budget.take("2026-09-03") and budget.take("2026-09-03")
    assert not budget.take("2026-09-03")


def test_the_budget_resets_the_next_day(contracts):
    budget = Budget(limit=1)
    budget.take("2026-09-03")
    assert budget.take("2026-09-04")


def test_cached_questions_survive_an_exhausted_budget(contracts):
    """The page degrades to its prefilled examples rather than going dark."""
    service, t = _service(contracts)
    service.ask("net revenue")
    service._budget.limit = 0
    assert service.ask("net revenue")["cached"] is True
    assert service.ask("something new")["refusals"][0]["rule"] == "NLQ-05"


def test_a_refusal_is_rendered_with_its_repair(contracts):
    service, _ = _service(contracts, Transport('{"answerable": true, "select": ["nope"]}'))
    refusal = service.ask("what is nope")["refusals"][0]
    assert refusal["rule"] == "STR-01"
    assert refusal["concept"] == "nope"
