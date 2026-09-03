"""The /api/ask endpoint: a question in, a plan or a refusal out.

Nothing here executes a plan yet - that is step 3. What this proves is the
path: a model resolves intent, the gate decides whether the result may run,
and the gate wins.

A public endpoint that calls a paid model is a denial-of-wallet primitive, so
the controls are here rather than deferred: a length cap, a per-caller rate
limit, a hard daily ceiling that degrades to cached answers, and a cache that
makes the prefilled questions free.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from assay.contracts.models import ContractSet
from assay.invariants.base import CheckResult
from assay.nlq.answer import Answer
from assay.nlq.plan import Query, Refusal
from assay.nlq.planner import Planner, PlanResult

MAX_CACHE = 512
RATE_PER_HOUR = 20
DAILY_BUDGET = 500


@dataclass
class Budget:
    """A hard ceiling. Past it the demo still answers cached questions."""

    limit: int = DAILY_BUDGET
    spent: int = 0
    day: str = ""

    def take(self, today: str) -> bool:
        if today != self.day:
            self.day, self.spent = today, 0
        if self.spent >= self.limit:
            return False
        self.spent += 1
        return True


@dataclass
class RateLimiter:
    """Per-caller sliding window. In-memory: one instance, one process."""

    per_hour: int = RATE_PER_HOUR
    seen: dict[str, deque] = field(default_factory=dict)

    def allow(self, caller: str, now: float) -> bool:
        window = self.seen.setdefault(caller, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= self.per_hour:
            return False
        window.append(now)
        return True


class AskService:
    """Planner plus the guards that make it safe to expose."""

    def __init__(self, contracts: ContractSet, token: str = "",
                 planner: Optional[Planner] = None,
                 executor: Optional[Callable[[Query], Answer]] = None) -> None:
        self._planner = planner or Planner(contracts, token=token)
        self._executor = executor
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._limiter = RateLimiter()
        self._budget = Budget()
        self._lock = threading.Lock()

    def ask(self, question: str, caller: str = "anon",
            now: Optional[float] = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        key = " ".join(question.lower().split())
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                cached = self._cache[key]
        if key in self._cache:
            return self._answered({**cached, "cached": True})
        with self._lock:
            if not self._limiter.allow(caller, now):
                return _refused("NLQ-04", "too many questions from here in the last hour",
                                "The demo limits each visitor to 20 questions an hour.")
            if not self._budget.take(time.strftime("%Y-%m-%d", time.gmtime(now))):
                return _refused("NLQ-05", "the demo has reached its daily budget",
                                "Questions already asked today still answer instantly.")
        rendered = _render(self._planner.plan(question))
        with self._lock:
            self._cache[key] = rendered
            while len(self._cache) > MAX_CACHE:
                self._cache.popitem(last=False)
        return self._answered({**rendered, "cached": False})

    def _answered(self, rendered: dict[str, Any]) -> dict[str, Any]:
        """Only the plan is cached; the number is computed every time.

        The plan is what costs money. The query costs milliseconds, and a
        cached figure would keep showing last night's answer after a
        restatement — which is the exact failure this project reports.
        """
        if self._executor is None or not rendered.get("answerable"):
            return rendered
        answer = self._executor(Query.model_validate(rendered["plan"]))
        return {**rendered, **_render_answer(answer)}

    @property
    def stats(self) -> dict[str, Any]:
        return {"cached_questions": len(self._cache), "spent_today": self._budget.spent,
                "daily_budget": self._budget.limit}


def _render(result: PlanResult) -> dict[str, Any]:
    return {
        "question": result.question,
        "answerable": result.answerable,
        "plan": result.plan.model_dump(mode="json") if result.plan else None,
        "refusals": [_refusal(r) for r in result.refusals],
        "latency_s": round(result.latency_s, 3),
        "attempts": result.attempts,
    }


def _render_answer(answer: Answer) -> dict[str, Any]:
    if answer.refusals:
        return {"answerable": False, "answer": None, "checks": [],
                "refusals": [_refusal(r) for r in answer.refusals]}
    result = answer.results[0]
    return {
        "answer": {
            "metric": result.metric,
            "dimension": result.dimension,
            "value": result.value,
            "rows": [{"label": k, "value": v} for k, v in result.rows],
            "window": {"start": str(answer.window.start) if answer.window.start else None,
                       "end": str(answer.window.end) if answer.window.end else None},
            "trustworthy": answer.trustworthy,
            "sql": result.sql,
            "scans": answer.scans,
            "duration_s": round(answer.duration_s, 3),
        },
        "checks": [_check(c) for c in answer.checks],
    }


def _check(c: CheckResult) -> dict[str, Any]:
    return {"rule": c.invariant_id, "subject": c.subject,
            "status": c.status.value, "detail": c.detail}


def _refusal(r: Refusal) -> dict[str, Any]:
    return {"rule": r.rule, "reason": r.reason, "repair": r.repair, "concept": r.concept}


def _refused(rule: str, reason: str, repair: str) -> dict[str, Any]:
    return {"answerable": False, "plan": None, "cached": False, "latency_s": 0.0,
            "refusals": [{"rule": rule, "reason": reason, "repair": repair, "concept": None}]}


def token_from_env() -> str:
    return os.environ.get("HF_TOKEN", "")
