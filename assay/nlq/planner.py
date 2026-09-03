"""The planner — a language model resolving intent, and nothing more.

It emits a plan, never SQL, and the plan is not trusted. Everything it returns
goes through `gate.check` before anything touches a warehouse, and the gate is
authoritative: if the model says a question is answerable and the gate
disagrees, the gate wins.

The bias runs one way. When the model declines a question the gate would have
allowed, the refusal stands. Declining to answer costs a visitor a retry;
answering wrongly costs the trust the whole project is about.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import ValidationError

from assay.contracts.models import ContractSet
from assay.nlq.gate import check
from assay.nlq.plan import Query, Refusal

ROUTER = "https://router.huggingface.co/v1/chat/completions"
MODEL = "Qwen/Qwen2.5-72B-Instruct"  # chosen by spike/plan_spike.py, see docs/11


@dataclass(frozen=True)
class PlannerConfig:
    model: str = MODEL
    endpoint: str = ROUTER
    max_tokens: int = 300
    timeout_s: float = 30.0
    max_question_chars: int = 240
    repair_attempts: int = 1


@dataclass(frozen=True)
class PlanResult:
    question: str
    plan: Optional[Query] = None
    refusals: tuple[Refusal, ...] = ()
    latency_s: float = 0.0
    attempts: int = 0
    raw: str = ""

    @property
    def answerable(self) -> bool:
        return self.plan is not None and not self.refusals


def catalogue(contracts: ContractSet) -> str:
    """Everything the model is allowed to know. Nothing else is a legal answer."""
    lines = []
    for metric in contracts.metrics:
        dims = ", ".join(_describe(d) for d in metric.dimensions)
        aka = f" (aka {', '.join(metric.synonyms)})" if metric.synonyms else ""
        lines.append(
            f"- {metric.name}{aka}: {metric.unit}, {metric.additivity.value}, "
            f"finest grain {metric.min_grain.value}\n"
            f"    dimensions: {dims or '(none - this metric cannot be sliced)'}"
        )
    return "\n".join(lines)


def _describe(dimension: Any) -> str:
    parts = [dimension.name]
    if dimension.synonyms:
        parts.append(f"(aka {', '.join(dimension.synonyms)})")
    if dimension.domain:
        parts.append(f"[{', '.join(dimension.domain)}]")
    return " ".join(parts)


def plan_schema(contracts: ContractSet) -> dict[str, Any]:
    """Legal names become enums, so a compliant reply cannot invent one."""
    metrics = sorted(m.name for m in contracts.metrics)
    dimensions = sorted({d.name for m in contracts.metrics for d in m.dimensions})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["answerable"],
        "properties": {
            "answerable": {"type": "boolean"},
            "refusal_reason": {"type": ["string", "null"]},
            "select": {"type": "array", "items": {"type": "string", "enum": metrics}},
            "by": {"type": "array", "items": {"type": "string", "enum": dimensions}},
            "where": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["dimension", "op", "value"],
                    "properties": {
                        "dimension": {"type": "string", "enum": dimensions},
                        "op": {"type": "string", "enum": ["eq", "neq", "in"]},
                        "value": {},
                    },
                },
            },
            "time": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["all", "relative"]},
                    "anchor": {
                        "type": "string",
                        "enum": ["day", "week", "month", "quarter", "year"],
                    },
                    "offset": {"type": "integer"},
                    "calendar": {"type": "string", "enum": ["gregorian", "fiscal"]},
                },
            },
        },
    }


SYSTEM = """You translate a question about a data warehouse into a query plan.

You may only use the metrics and dimensions in the catalogue below. You never \
write SQL. You never invent a metric or a dimension.

CATALOGUE
{catalogue}

RULES
1. A metric can only be sliced by, or filtered on, a dimension listed under \
that metric. If the question asks for something not listed for it, the question \
is not answerable.
2. If no metric in the catalogue measures what was asked, it is not answerable.
3. "by X" means group: put X in "by". "for X" or "in X" means restrict: put a \
clause in "where" and leave "by" empty.
4. When not answerable, set "answerable": false and give a one-line \
"refusal_reason". Never guess a near-miss metric.
5. Reply with JSON only. Keys: answerable, refusal_reason, select, by, where, \
time. A "where" clause is {{"dimension": ..., "op": "eq"|"neq"|"in", "value": ...}}. \
A "time" is {{"kind": "relative", "anchor": "day"|"week"|"month"|"quarter"|"year", \
"offset": 0, "calendar": "fiscal"|"gregorian"}} or omitted for all time."""


# The full JSON schema goes in `response_format`, not in the prompt. Sending it
# twice cost 55% of the prompt and bought nothing measurable - see docs/11.


class Planner:
    """Question in, validated plan or refusals out.

    `transport` is injectable so the whole class is testable without a network
    call: it takes the request body and returns (text, latency).
    """

    def __init__(
        self,
        contracts: ContractSet,
        token: str = "",
        config: PlannerConfig = PlannerConfig(),
        transport: Optional[Callable[[dict[str, Any]], tuple[str, float]]] = None,
    ) -> None:
        self._contracts = contracts
        self._token = token
        self._config = config
        self._transport = transport or self._http
        self._system = SYSTEM.format(catalogue=catalogue(contracts))

    def plan(self, question: str) -> PlanResult:
        question = question.strip()
        if too_long := self._too_long(question):
            return PlanResult(question, refusals=(too_long,))

        messages = [
            {"role": "system", "content": self._system},
            {"role": "user", "content": question},
        ]
        elapsed = 0.0
        for attempt in range(1, self._config.repair_attempts + 2):
            try:
                raw, took = self._transport(self._body(messages))
            except Exception as exc:  # noqa: BLE001 - an outage is a refusal
                return PlanResult(question, refusals=(_unavailable(exc),), attempts=attempt)
            elapsed += took
            parsed = _parse(raw)
            if parsed is None:
                messages = self._repair(messages, raw, "reply with JSON only")
                continue
            return self._interpret(question, parsed, raw, elapsed, attempt)
        return PlanResult(question, refusals=(_uninterpretable(),), latency_s=elapsed,
                          attempts=self._config.repair_attempts + 1)

    # ---- internals -----------------------------------------------------------

    def _interpret(self, question: str, parsed: dict, raw: str, elapsed: float,
                   attempt: int) -> PlanResult:
        if not parsed.get("answerable", False):
            reason = parsed.get("refusal_reason") or "no metric in the catalogue answers this"
            return PlanResult(question, refusals=(_declined(reason),), latency_s=elapsed,
                              attempts=attempt, raw=raw)
        try:
            plan = Query.model_validate(_to_plan(parsed))
        except ValidationError as exc:
            return PlanResult(question, refusals=(_malformed(exc),), latency_s=elapsed,
                              attempts=attempt, raw=raw)
        refusals = check(plan, self._contracts)
        return PlanResult(question, plan=None if refusals else plan, refusals=refusals,
                          latency_s=elapsed, attempts=attempt, raw=raw)

    def _too_long(self, question: str) -> Optional[Refusal]:
        if not question:
            return Refusal(rule="NLQ-00", reason="the question is empty")
        if len(question) <= self._config.max_question_chars:
            return None
        return Refusal(
            rule="NLQ-00",
            reason=f"questions are limited to {self._config.max_question_chars} characters",
            repair="Ask for one metric and one slice at a time.",
        )

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "plan",
                    "schema": plan_schema(self._contracts),
                    "strict": False,
                },
            },
        }

    @staticmethod
    def _repair(messages: list[dict], raw: str, instruction: str) -> list[dict]:
        return messages + [
            {"role": "assistant", "content": raw[:400]},
            {"role": "user", "content": f"That was not valid JSON. {instruction}."},
        ]

    def _http(self, body: dict[str, Any]) -> tuple[str, float]:
        import httpx

        started = time.time()
        response = httpx.post(
            self._config.endpoint,
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
            timeout=self._config.timeout_s,
        )
        if response.status_code != 200:
            raise RuntimeError(f"{response.status_code}: {response.text[:120]}")
        message = response.json()["choices"][0]["message"]
        text = message.get("content") or message.get("reasoning_content") or ""
        return text, time.time() - started


# ---- helpers -------------------------------------------------------------------


def _to_plan(parsed: dict[str, Any]) -> dict[str, Any]:
    """Model JSON -> plan fields. Absent time means all time, not now."""
    time_spec = parsed.get("time") or {}
    if time_spec.get("kind") == "relative" and time_spec.get("anchor"):
        spec = {k: v for k, v in time_spec.items() if v is not None}
    else:
        spec = {"kind": "all"}
    return {
        "select": tuple(parsed.get("select") or ()),
        "by": tuple(parsed.get("by") or ()),
        "where": tuple(parsed.get("where") or ()),
        "time": spec,
    }


def _parse(raw: Optional[str]) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _declined(reason: str) -> Refusal:
    return Refusal(rule="NLQ-01", reason=reason,
                   repair="Try naming a metric from the catalogue.")


def _malformed(exc: ValidationError) -> Refusal:
    return Refusal(rule="NLQ-02", reason="the plan was not well formed",
                   repair=str(exc.errors()[0].get("msg", ""))[:120] if exc.errors() else None)


def _uninterpretable() -> Refusal:
    return Refusal(rule="NLQ-02", reason="could not interpret that question",
                   repair="Try naming a metric and one way to slice it.")


def _unavailable(exc: Exception) -> Refusal:
    return Refusal(rule="NLQ-03", reason="the planner is unavailable right now",
                   repair=f"{type(exc).__name__}: {str(exc)[:90]}")
