"""Rendering a run as something a human reads at 8am.

Two surfaces: Markdown for the terminal and CI logs, Slack Block Kit for the
nightly message. Both lead with what broke and name the metric and the
number, because a report that only says a check failed gets muted.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional, Sequence

from assay.invariants.base import CheckResult, Status
from assay.run.runner import RunSummary, sort_for_report

_ICON = {
    Status.FAIL: "✖",
    Status.WARN: "⚠",
    Status.SKIP: "–",
    Status.PASS: "✓",
}


def markdown(summary: RunSummary, include_passing: bool = False) -> str:
    lines = [
        f"# Assay run {summary.run_id}",
        "",
        f"{summary.ran_at:%Y-%m-%d %H:%M} UTC · {summary.headline} "
        f"· {summary.scans} warehouse scans · {summary.duration_s:.2f}s",
        "",
    ]
    shown = [
        r
        for r in sort_for_report(summary.results)
        if include_passing or r.status is not Status.PASS
    ]
    if not shown:
        lines.append("All checks passed.")
    lines.extend(_section(shown, Status.FAIL, "Failed"))
    lines.extend(_section(shown, Status.WARN, "Warnings"))
    lines.extend(_section(shown, Status.SKIP, "Not run"))
    lines.extend(_section(shown, Status.PASS, "Passed"))
    return "\n".join(lines).rstrip() + "\n"


def _section(
    results: Sequence[CheckResult], status: Status, title: str
) -> list[str]:
    rows = [r for r in results if r.status is status]
    if not rows:
        return []
    lines = [f"## {title} ({len(rows)})", ""]
    lines.extend(
        f"- {_ICON[status]} `{r.invariant_id}` **{r.subject}** — {r.detail}"
        for r in rows
    )
    lines.append("")
    return lines


def slack_blocks(summary: RunSummary, limit: int = 10) -> dict[str, Any]:
    """Block Kit payload. Failures first; warnings only if there is room."""
    notable = [r for r in sort_for_report(summary.results) if r.violated][:limit]
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _title(summary)},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Run `{summary.run_id}` · {summary.headline}"}
            ],
        },
    ]
    blocks.extend(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{_ICON[r.status]} *{r.subject}* `{r.invariant_id}`\n{r.detail}",
            },
        }
        for r in notable
    )
    return {"text": _title(summary), "blocks": blocks}


def _title(summary: RunSummary) -> str:
    if summary.failures:
        return f"Assay: {len(summary.failures)} metric checks failed"
    if summary.warnings:
        return f"Assay: {len(summary.warnings)} warnings"
    return "Assay: all metric checks passed"


class SlackNotifier:
    """Posts a run to an incoming webhook.

    Never called unless the operator both sets the webhook and passes
    `--notify`: a verification tool that messages a channel by surprise on
    first run gets muted before it has said anything useful.
    """

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self._url = webhook_url or os.environ.get("ASSAY_SLACK_WEBHOOK")

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def send(self, payload: dict[str, Any]) -> None:
        if not self._url:
            raise RuntimeError("ASSAY_SLACK_WEBHOOK is not set")
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise urllib.error.HTTPError(
                    self._url, response.status, "webhook rejected", response.headers, None
                )
