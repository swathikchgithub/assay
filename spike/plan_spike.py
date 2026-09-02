"""Step 0 of the NLQ planner: choose a model on evidence, not on vibes.

Runs a fixed question set against candidate models on the Hugging Face
router and measures the three things that decide the choice:

  valid    — did it return a plan the schema and the contract both accept
  correct  — did it pick the metric and dimensions a human would
  refused  — did it decline the questions that have no legal answer

The third matters most. A model that answers everything scores well on the
first two and is useless, because inventing a number is the failure this
project exists to prevent.

    python -m spike.plan_spike --models Qwen/Qwen2.5-72B-Instruct
    python -m spike.plan_spike --dry-run     # exercise the harness, no calls
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from assay.contracts.models import ContractSet, Metric
from assay.contracts.sources import YamlSource

ROUTER = "https://router.huggingface.co/v1/chat/completions"
CONTRACTS = Path(__file__).resolve().parents[1] / "demo" / "contracts.yml"
QUESTIONS = Path(__file__).resolve().parent / "questions.yml"

DEFAULT_MODELS = [
    "Qwen/Qwen2.5-72B-Instruct",
    "meta-llama/Llama-3.3-70B-Instruct",
    "mistralai/Mistral-Small-24B-Instruct-2501",
]


# ---- the catalogue the model is allowed to see -------------------------------


def catalogue(contracts: ContractSet) -> str:
    """What the model knows. Nothing outside this is a legal answer."""
    lines = []
    for m in contracts.metrics:
        dims = ", ".join(d.name for d in m.dimensions) or "(none — cannot be sliced)"
        lines.append(
            f"- {m.name}: {m.unit}, {m.additivity.value}, "
            f"finest grain {m.min_grain.value}\n    dimensions: {dims}"
        )
    return "\n".join(lines)


def plan_schema(contracts: ContractSet) -> dict[str, Any]:
    """Legal names are enums, so a compliant response cannot invent one."""
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
                    "anchor": {
                        "type": "string",
                        "enum": ["day", "week", "month", "quarter", "year", "all"],
                    },
                    "offset": {"type": "integer"},
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
1. A metric can only be sliced by, or filtered on, a dimension listed under that \
metric. If the question asks to slice or filter a metric by something not listed \
for it, the question is not answerable.
1b. "by X" means group: put X in "by". "for X" or "in X" means restrict: put a \
clause in "where" and leave "by" empty.
2. If no metric in the catalogue measures what was asked, the question is not \
answerable.
3. When not answerable, set "answerable": false and give a one-line \
"refusal_reason". Do not guess a near-miss metric.
4. Reply with JSON only, matching this schema:
{schema}"""


# ---- results ------------------------------------------------------------------


@dataclass
class Outcome:
    question: str
    expected_refusal: bool
    latency_s: float
    raw: str = ""
    plan: Optional[dict[str, Any]] = None
    valid: bool = False
    correct: bool = False
    note: str = ""


@dataclass
class Scorecard:
    model: str
    outcomes: list[Outcome] = field(default_factory=list)

    def _subset(self, refusals: bool) -> list[Outcome]:
        return [o for o in self.outcomes if o.expected_refusal is refusals]

    @property
    def valid_rate(self) -> float:
        return _rate([o.valid for o in self.outcomes])

    @property
    def answer_accuracy(self) -> float:
        return _rate([o.correct for o in self._subset(False)])

    @property
    def refusal_accuracy(self) -> float:
        return _rate([o.correct for o in self._subset(True)])

    @property
    def latencies(self) -> list[float]:
        return sorted(o.latency_s for o in self.outcomes if o.latency_s > 0)

    def percentile(self, p: float) -> float:
        xs = self.latencies
        return xs[min(int(len(xs) * p), len(xs) - 1)] if xs else 0.0


def _rate(flags: list[bool]) -> float:
    return (sum(flags) / len(flags)) if flags else 0.0


# ---- the gate the spike scores against ---------------------------------------


def validate(plan: dict[str, Any], contracts: ContractSet) -> tuple[bool, str]:
    """A miniature type gate: STR-01 and STR-02 only, which is what a plan
    can violate before any time or filter handling exists."""
    if not isinstance(plan, dict) or "answerable" not in plan:
        return False, "missing 'answerable'"
    if not plan.get("answerable"):
        return True, "refused"
    selected = plan.get("select") or []
    if not selected:
        return False, "answerable but selected no metric"
    for name in selected:
        if not contracts.has(name):
            return False, f"STR-01: no metric named {name!r}"
    filtered = [c.get("dimension") for c in plan.get("where") or [] if isinstance(c, dict)]
    for dim in (plan.get("by") or []) + filtered:
        for name in selected:
            if not _reachable(contracts.metric(name), dim):
                return False, f"STR-02: {name} cannot be sliced by {dim!r}"
    return True, "ok"


def _reachable(metric: Metric, dimension: str) -> bool:
    return any(d.name == dimension for d in metric.dimensions)


def judge(plan: dict[str, Any], expected: dict[str, Any], refusal: bool) -> bool:
    """Correct means: declined what must be declined, or picked what a human would.

    An ambiguous question accepts either, because declining to guess is not a
    defect - it is the behaviour the rest of the system is built around.
    """
    if refusal:
        return not plan.get("answerable", True)
    if not plan.get("answerable"):
        return bool(expected.get("ambiguous"))
    if set(plan.get("select") or []) != {expected["metric"]}:
        return False
    if set(plan.get("by") or []) != set(expected["by"]):
        return False
    got = {c.get("dimension") for c in plan.get("where") or [] if isinstance(c, dict)}
    return got == set(expected.get("where") or [])


# ---- running ------------------------------------------------------------------


def ask(model: str, question: str, system: str, schema: dict, token: str) -> tuple[str, float]:
    body = {
        "model": model,
        "max_tokens": 300,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "plan", "schema": schema, "strict": False},
        },
    }
    started = time.time()
    response = httpx.post(
        ROUTER, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=90
    )
    elapsed = time.time() - started
    if response.status_code != 200:
        raise RuntimeError(f"{response.status_code}: {response.text[:160]}")
    message = response.json()["choices"][0]["message"]
    # Reasoning-tuned models return content=None and put the text elsewhere.
    text = message.get("content") or message.get("reasoning_content") or ""
    return text, elapsed


def parse(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """Models fence JSON or prepend prose often enough to be worth handling."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def run_model(model: str, cases: list[tuple[str, dict, bool]], contracts: ContractSet,
              token: str, stub=None) -> Scorecard:
    system = SYSTEM.format(
        catalogue=catalogue(contracts),
        schema=json.dumps(plan_schema(contracts), indent=2),
    )
    schema = plan_schema(contracts)
    card = Scorecard(model=model)
    for question, expected, refusal in cases:
        try:
            raw, elapsed = stub(question) if stub else ask(model, question, system, schema, token)
        except Exception as exc:  # noqa: BLE001 - a dead model is a result
            card.outcomes.append(Outcome(question, refusal, 0.0, note=str(exc)[:70]))
            continue
        plan = parse(raw)
        outcome = Outcome(question, refusal, elapsed, raw=raw[:200], plan=plan)
        if plan is None:
            outcome.note = "unparseable"
        else:
            outcome.valid, outcome.note = validate(plan, contracts)
            outcome.correct = outcome.valid and judge(plan, expected, refusal)
        card.outcomes.append(outcome)
    return card


def load_cases() -> list[tuple[str, dict, bool]]:
    spec = yaml.safe_load(QUESTIONS.read_text())
    cases = [(c["q"], c, False) for c in spec["answerable"]]
    cases += [(c["q"], {**c, "ambiguous": True}, False) for c in spec.get("ambiguous", [])]
    cases += [(c["q"], c, True) for c in spec["refuse"]]
    return cases


def report(cards: list[Scorecard]) -> str:
    lines = [
        "",
        f"{'model':<44} {'valid':>7} {'answers':>8} {'refusals':>9} {'p50':>7} {'p95':>7}",
        "-" * 86,
    ]
    for c in cards:
        lines.append(
            f"{c.model:<44} {c.valid_rate:>6.0%} {c.answer_accuracy:>8.0%} "
            f"{c.refusal_accuracy:>9.0%} {c.percentile(0.5):>6.2f}s {c.percentile(0.95):>6.2f}s"
        )
    lines.append("")
    for c in cards:
        misses = [o for o in c.outcomes if not o.correct]
        if not misses:
            continue
        lines.append(f"  {c.model} — {len(misses)} miss(es):")
        for o in misses[:8]:
            kind = "should have refused" if o.expected_refusal else "wrong plan"
            picked = ""
            if o.plan and o.plan.get("answerable"):
                picked = f" picked {o.plan.get('select')} by {o.plan.get('by') or []}"
            lines.append(f"    {kind:<20} {o.question[:42]:<44}{picked or ' ' + o.note}")
        lines.append("")
    return "\n".join(lines)


def _stub(question: str) -> tuple[str, float]:
    """Offline harness exercise: answers correctly except one deliberate miss."""
    spec = yaml.safe_load(QUESTIONS.read_text())
    for case in spec["answerable"]:
        if case["q"] == question:
            return json.dumps({"answerable": True, "select": [case["metric"]], "by": case["by"]}), 0.4
    if question == "active users by region":  # the classic STR-02 failure
        return json.dumps({"answerable": True, "select": ["active_users"], "by": ["region"]}), 0.4
    return json.dumps({"answerable": False, "refusal_reason": "not in the catalogue"}), 0.3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    parser.add_argument("--dry-run", action="store_true", help="stub the model")
    args = parser.parse_args()

    contracts = YamlSource(CONTRACTS).load()
    cases = load_cases()
    token = ""
    if not args.dry_run:
        from huggingface_hub import get_token

        token = get_token() or ""
        if not token:
            print("no Hugging Face token found")
            return 2

    models = ["stub"] if args.dry_run else args.models
    cards = [
        run_model(m, cases, contracts, token, stub=_stub if args.dry_run else None)
        for m in models
    ]
    print(report(cards))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
