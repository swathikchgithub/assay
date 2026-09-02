"""`assay run` — the whole of P0's interface.

No dashboard, no query surface, no language model. Point it at contracts and a
warehouse; it tells you what is wrong with numbers you already ship.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from assay.contracts.models import ContractSet
from assay.contracts.sources import DbtManifestSource, YamlSource
from assay.engine.duckdb_adapter import DuckDBAdapter
from assay.engine.sql import Window
from assay.run.history import History
from assay.run.report import SlackNotifier, markdown, slack_blocks
from assay.run.runner import run

EXIT_CONFIG = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contracts = _load_contracts(args)
    except (OSError, ValueError) as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    as_of = _as_of(args.as_of)
    adapter = DuckDBAdapter(args.database, as_of=as_of, read_only=True)
    history = History(args.history)
    try:
        summary = run(
            contracts,
            adapter,
            Window(start=(as_of - timedelta(days=args.since_days)).date()),
            history=history,
        )
        history.record_checks(summary.run_id, summary.results, summary.ran_at)
        _emit(summary, args)
    finally:
        adapter.close()
        history.close()
    return summary.exit_code


def _emit(summary, args: argparse.Namespace) -> None:
    if args.format == "json":
        print(json.dumps(slack_blocks(summary), indent=2))
    else:
        print(markdown(summary, include_passing=args.verbose))
    if args.notify:
        _notify(summary)


def _notify(summary) -> None:
    notifier = SlackNotifier()
    if not notifier.configured:
        print("ASSAY_SLACK_WEBHOOK is not set — nothing sent", file=sys.stderr)
        return
    notifier.send(slack_blocks(summary))
    print(f"posted run {summary.run_id} to Slack", file=sys.stderr)


def _load_contracts(args: argparse.Namespace) -> ContractSet:
    if args.dbt_manifest:
        return DbtManifestSource(Path(args.dbt_manifest)).load()
    return YamlSource(Path(args.contracts)).load()


def _as_of(raw: Optional[str]) -> datetime:
    if raw is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assay", description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--contracts", help="path to a contracts YAML file")
    source.add_argument("--dbt-manifest", help="path to dbt semantic_manifest.json")
    p.add_argument("--database", required=True, help="DuckDB file to check")
    p.add_argument("--history", default=".assay/history.db", help="observation history")
    p.add_argument("--since-days", type=int, default=540, help="lookback window")
    p.add_argument("--as-of", help="ISO timestamp to treat as now (testing)")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    p.add_argument("--verbose", action="store_true", help="include passing checks")
    p.add_argument(
        "--notify",
        action="store_true",
        help="post to ASSAY_SLACK_WEBHOOK (off by default)",
    )
    return p


if __name__ == "__main__":
    raise SystemExit(main())
