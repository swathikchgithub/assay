"""`assay doctor` — diagnose a setup before it costs anyone an evening.

Every check here exists because getting Assay pointed at a real warehouse for
the first time failed in a way whose error message named the symptom and not
the cause. A wrong account identifier surfaced as a TLS hostname mismatch; a
missing SAML provider surfaced as a stack trace forty frames deep; empty
tables would surface as a report where every check quietly skipped.

Checks run cheapest-first and stop at the first one that makes the rest
meaningless, so a typo in an environment variable never reaches the point of
opening a browser window.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from assay.contracts.models import ContractSet, Metric
from assay.engine.adapter import Query, WarehouseAdapter

PRIVILEGED_ROLES = {"ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "ORGADMIN"}


@dataclass(frozen=True)
class Finding:
    name: str
    ok: bool
    detail: str
    fix: Optional[str] = None
    fatal: bool = False

    @property
    def icon(self) -> str:
        if self.ok:
            return "✓"
        return "✖" if self.fatal else "⚠"


def verify_certificate(host: str, port: int = 443, timeout: float = 8.0) -> None:
    """Raise if the certificate served for `host` does not match it.

    This is the check that would have caught a wrong account identifier:
    Snowflake wildcards its DNS, so a bad identifier resolves happily and only
    the certificate reveals that it landed on someone else's deployment.
    """
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host):
            return


def check_endpoint(
    account: str, verifier: Callable[[str], None] = verify_certificate
) -> Finding:
    host = f"{account}.snowflakecomputing.com"
    try:
        verifier(host)
    except ssl.SSLCertVerificationError:
        return Finding(
            "endpoint",
            False,
            f"{host} served a certificate that does not match it",
            "SNOWFLAKE_ACCOUNT is wrong. In Snowsight run "
            "SELECT SYSTEM$ALLOWLIST() and use the SNOWFLAKE_DEPLOYMENT_REGIONLESS "
            "host with .snowflakecomputing.com removed.",
            fatal=True,
        )
    except (OSError, socket.timeout) as exc:
        return Finding("endpoint", False, f"cannot reach {host}: {exc}", fatal=True)
    return Finding("endpoint", True, f"{host} certificate valid")


def check_session(adapter: WarehouseAdapter) -> Finding:
    rows = adapter.fetch(
        Query(
            "SELECT CURRENT_ROLE(), CURRENT_WAREHOUSE(), "
            "CURRENT_DATABASE(), CURRENT_SCHEMA()"
        )
    )
    role, warehouse, database, schema = (r or "-" for r in rows[0])
    if database == "-" or schema == "-":
        return Finding(
            "session",
            False,
            f"connected as {role}, but no database or schema is selected",
            "Set SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA.",
            fatal=True,
        )
    return Finding("session", True, f"{role} @ {warehouse} · {database}.{schema}")


def check_role(adapter: WarehouseAdapter) -> Finding:
    role = str(adapter.fetch(Query("SELECT CURRENT_ROLE()"))[0][0] or "")
    if role.upper() in PRIVILEGED_ROLES:
        return Finding(
            "role",
            False,
            f"{role} can write; Assay only ever reads",
            "Create a SELECT-only role (see the README) and set SNOWFLAKE_ROLE. "
            "The adapter refuses non-queries, but that is defence in depth, "
            "not the primary control.",
        )
    return Finding("role", True, f"{role} is not a privileged built-in role")


def check_objects(adapter: WarehouseAdapter, contracts: ContractSet) -> list[Finding]:
    """Resolve every table and column the contracts name, before a run does."""
    present = _catalog(adapter)
    if not present:
        return [
            Finding(
                "contract objects",
                False,
                "the selected schema contains no tables",
                "Check SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA, or load data first.",
                fatal=True,
            )
        ]
    missing = sorted({m for metric in contracts.metrics for m in _missing(metric, present)})
    if missing:
        return [
            Finding(
                "contract objects",
                False,
                f"{len(missing)} referenced object(s) not found: {', '.join(missing[:6])}",
                "Either the contracts name objects that do not exist, or the "
                "case policy is wrong — see the next check.",
                fatal=True,
            )
        ]
    columns = sum(len(cols) for cols in present.values())
    return [
        Finding(
            "contract objects",
            True,
            f"{len(contracts.metrics)} metrics resolved against "
            f"{len(present)} tables, {columns} columns",
        )
    ]


def check_case_policy(adapter: WarehouseAdapter, configured: str) -> Finding:
    names = list(_catalog(adapter))
    if not names:
        return Finding("case policy", True, "no tables to judge from")
    upper = sum(1 for n in names if n.isupper())
    actual = "upper" if upper > len(names) / 2 else "exact"
    if actual != configured:
        return Finding(
            "case policy",
            False,
            f"objects are stored {'UPPER CASE' if actual == 'upper' else 'lower case'}, "
            f"but --case-policy {configured} is configured",
            f"Re-run with --case-policy {actual}.",
            fatal=True,
        )
    return Finding(
        "case policy",
        True,
        f"objects are {'UPPER CASE' if actual == 'upper' else 'lower case'}; "
        f"--case-policy {configured} is correct",
    )


def check_row_counts(adapter: WarehouseAdapter, contracts: ContractSet) -> Finding:
    """An empty table produces a report of skips, which reads like success.

    Counts every table the contracts touch, joined ones included: a metric
    reads fine while the dimension table it joins to is empty, and that
    failure looks like a decomposition problem rather than a missing load.
    """
    counts = {t: _count(adapter, t) for t in sorted(_tables(contracts))}
    empty = [t for t, n in counts.items() if n == 0]
    if empty:
        return Finding(
            "row counts",
            False,
            f"{len(empty)} of {len(counts)} tables are empty: {', '.join(empty)}",
            "Checks against an empty table skip rather than fail, so a run "
            "would look healthy while testing nothing.",
        )
    return Finding(
        "row counts", True, f"{sum(counts.values()):,} rows across {len(counts)} tables"
    )


def _tables(contracts: ContractSet) -> set[str]:
    """Base tables plus every table reachable by a declared join."""
    return {
        table
        for metric in contracts.metrics
        for table in ({metric.table} | {j.table for j in metric.joins})
    }


def _catalog(adapter: WarehouseAdapter) -> dict[str, set[str]]:
    rows = adapter.fetch(
        Query(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema NOT IN ('information_schema', 'INFORMATION_SCHEMA')"
        )
    )
    catalog: dict[str, set[str]] = {}
    for table, column in rows:
        catalog.setdefault(str(table), set()).add(str(column))
    return catalog


def _missing(metric: Metric, present: dict[str, set[str]]) -> set[str]:
    """Objects this metric names that the warehouse does not have."""
    missing: set[str] = set()
    for table, columns in _referenced(metric):
        available = present.get(table) or present.get(table.upper())
        if available is None:
            missing.add(table)
            continue
        missing |= {
            f"{table}.{c}"
            for c in columns
            if c not in available and c.upper() not in available
        }
    return missing


def _referenced(metric: Metric) -> list[tuple[str, set[str]]]:
    refs: dict[str, set[str]] = {metric.table: {metric.time_column}}
    for join in metric.joins:
        refs.setdefault(metric.table, set()).add(join.left_key)
        refs.setdefault(join.table, set()).add(join.right_key)
    for dim in metric.dimensions:
        refs.setdefault(dim.table or metric.table, set()).add(dim.column)
    return list(refs.items())


def _count(adapter: WarehouseAdapter, table: str) -> int:
    quoted = adapter.dialect.quote(table)
    return int(adapter.fetch(Query(f"SELECT count(*) FROM {quoted}"))[0][0] or 0)


def render(findings: Sequence[Finding], target: str, at: datetime) -> str:
    lines = [f"Assay doctor · target={target} · {at:%Y-%m-%d %H:%M} UTC", ""]
    for finding in findings:
        lines.append(f"  {finding.icon}  {finding.name:<18} {finding.detail}")
        if finding.fix:
            lines.append(f"     fix: {finding.fix}")
    problems = [f for f in findings if not f.ok]
    lines.append("")
    if not problems:
        lines.append("All clear. `assay run` will check the metrics themselves.")
    else:
        fatal = sum(1 for f in problems if f.fatal)
        lines.append(f"{len(problems)} problem(s), {fatal} of them blocking.")
    return "\n".join(lines) + "\n"


def now() -> datetime:
    return datetime.now(timezone.utc)
