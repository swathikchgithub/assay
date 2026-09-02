"""Entry point for `python -m assay.run.doctor_cli`.

Kept separate from the check functions so they stay unit-testable without
argument parsing, and separate from `run.cli` so a diagnosis never needs the
thing it is diagnosing to work.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from assay.contracts.sources import DbtManifestSource, YamlSource
from assay.engine.targets import TARGETS, open_adapter
from assay.run import doctor
from assay.run.doctor import Finding

EXIT_WARN = 1
EXIT_BLOCKED = 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    findings: list[Finding] = []

    if args.target == "snowflake":
        findings.append(_config())
        if findings[-1].ok:
            findings.append(doctor.check_endpoint(_account()))
        if _blocked(findings):
            return _emit(findings, args.target)

    try:
        contracts = _contracts(args)
        adapter = open_adapter(args.target, doctor.now(), args.database, args.case_policy)
    except (ImportError, OSError, ValueError) as exc:
        findings.append(Finding("connection", False, str(exc), fatal=True))
        return _emit(findings, args.target)

    findings.append(Finding("connection", True, f"{args.target} reachable"))
    try:
        findings.extend(_warehouse_checks(adapter, contracts, args))
    finally:
        getattr(adapter, "close", lambda: None)()
    return _emit(findings, args.target)


def _warehouse_checks(adapter, contracts, args: argparse.Namespace) -> list[Finding]:
    findings: list[Finding] = []
    if args.target == "snowflake":
        # Identifier folding is a Snowflake behaviour; DuckDB preserves case.
        findings.append(doctor.check_session(adapter))
        findings.append(doctor.check_role(adapter))
        if _blocked(findings):
            return findings
        findings.append(doctor.check_case_policy(adapter, args.case_policy))
    findings.extend(doctor.check_objects(adapter, contracts))
    if not _blocked(findings):
        findings.append(doctor.check_row_counts(adapter, contracts))
    return findings


def _config() -> Finding:
    from assay.engine.snowflake_adapter import SnowflakeConfig

    try:
        config = SnowflakeConfig.from_env()
    except ValueError as exc:
        return Finding("configuration", False, str(exc), fatal=True)
    auth = "key-pair" if config.private_key_file else config.authenticator
    return Finding(
        "configuration", True, f"account {config.account} as {config.user} · {auth} auth"
    )


def _account() -> str:
    from assay.engine.snowflake_adapter import SnowflakeConfig

    return SnowflakeConfig.from_env().account


def _contracts(args: argparse.Namespace):
    from pathlib import Path

    if args.dbt_manifest:
        return DbtManifestSource(Path(args.dbt_manifest)).load()
    return YamlSource(Path(args.contracts)).load()


def _blocked(findings: Sequence[Finding]) -> bool:
    return any(f.fatal and not f.ok for f in findings)


def _emit(findings: list[Finding], target: str) -> int:
    """0 clean, 1 warnings only, 2 something blocking — so CI can tell them apart."""
    print(doctor.render(findings, target, doctor.now()))
    if _blocked(findings):
        return EXIT_BLOCKED
    return EXIT_WARN if any(not f.ok for f in findings) else 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assay doctor", description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--contracts")
    source.add_argument("--dbt-manifest")
    p.add_argument("--target", choices=TARGETS, default="duckdb")
    p.add_argument("--database")
    p.add_argument("--case-policy", choices=("upper", "exact"), default="upper")
    return p


if __name__ == "__main__":
    raise SystemExit(main())
