"""Loads the demo dataset into Snowflake, to prove the adapter end to end.

This is the one part of Assay that writes. The checking path is read-only by
construction — `SnowflakeAdapter` refuses any statement that is not a query —
so this loader talks to the connector directly and lives in `demo/`, well
away from anything the nightly run imports.

It plans first and writes only with `--yes`, because it drops and recreates
seven tables in a schema you name, and a loader that does that on a mistyped
argument is not one you should run against an account with real data in it.

    python -m demo.load_snowflake --database ASSAY_DEMO --schema DEMO
    python -m demo.load_snowflake --database ASSAY_DEMO --schema DEMO --yes
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional, Sequence

from assay.engine.snowflake_adapter import SnowflakeConfig
from demo import data

LOADED = ("regions", "accounts", "orders", "order_items", "discounts", "tickets")


def load(
    database: str,
    schema: str,
    days: int = 540,
    backfill: bool = False,
    connector: Any = None,
) -> dict[str, int]:
    """Create the demo tables and fill them. Returns rows written per table."""
    connector = connector or _import_connector()
    connector.paramstyle = "qmark"
    config = SnowflakeConfig.from_env()
    conn = connector.connect(**{**config.connect_kwargs(), "database": database})
    try:
        cursor = conn.cursor()
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {_qualify(database, schema)}")
        cursor.execute(f"USE SCHEMA {_qualify(database, schema)}")
        written = _backfill(cursor) if backfill else _load_all(cursor, days)
        cursor.close()
        return written
    finally:
        conn.close()


def _load_all(cursor: Any, days: int) -> dict[str, int]:
    dataset = data.generate(days)
    for table in data.TABLES:
        drop, create = data.ddl(table)
        cursor.execute(drop)
        cursor.execute(create)
    written = {}
    for table in LOADED:
        rows = dataset.rows(table)
        cursor.executemany(data.insert(table), rows)
        written[table] = len(rows)
    written["events"] = _events(cursor, days)
    return written


def _events(cursor: Any, days: int) -> int:
    """Snowflake's GENERATOR, in place of DuckDB's generate_series.

    `SEQ4()` is aliased in a subquery rather than referenced repeatedly, so
    every column in a row is computed from the same sequence value.
    """
    rowcount = days * data.EVENTS_PER_DAY
    cursor.execute(
        f"""
        INSERT INTO events (user_id, occurred_at)
        SELECT ((i * 7919) % {data.USER_POOL}) + 1,
               DATEADD(minute, (i * 37) % 1440,
                 DATEADD(day, FLOOR(i / {data.EVENTS_PER_DAY}),
                   TO_TIMESTAMP_NTZ('{data.start(days):%Y-%m-%d %H:%M:%S}')))
        FROM (SELECT SEQ4() AS i FROM TABLE(GENERATOR(ROWCOUNT => {rowcount})))
        """
    )
    return rowcount


def _backfill(cursor: Any) -> dict[str, int]:
    cursor.execute("SELECT max(id) + 1 FROM orders")
    next_id = cursor.fetchall()[0][0]
    rows = data.backfill_orders(int(next_id))
    cursor.executemany(data.insert("orders"), rows)
    return {"orders": len(rows)}


def _qualify(database: str, schema: str) -> str:
    for part in (database, schema):
        if not part.replace("_", "").replace("$", "").isalnum():
            raise ValueError(f"not a bare identifier: {part!r}")
    return f"{database}.{schema}"


def _plan(args: argparse.Namespace) -> str:
    target = _qualify(args.database, args.schema)
    if args.backfill:
        return f"Would add 320 restating orders to {target}.ORDERS."
    tables = ", ".join(data.TABLES)
    return (
        f"Would DROP and recreate {len(data.TABLES)} tables in {target}\n"
        f"  {tables}\n"
        f"and load {args.days} days of demo data "
        f"(~{args.days * data.EVENTS_PER_DAY:,} event rows)."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--schema", default="DEMO")
    parser.add_argument("--days", type=int, default=540)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--yes", action="store_true", help="actually write")
    args = parser.parse_args(argv)

    try:
        print(_plan(args))
        if not args.yes:
            print("\nDry run. Re-run with --yes to write.")
            return 0
        written = load(args.database, args.schema, args.days, args.backfill)
    except (ImportError, ValueError) as exc:
        print(f"cannot load: {exc}", file=sys.stderr)
        return 2
    for table, count in written.items():
        print(f"  {table:<12} {count:>8,} rows")
    return 0


def _import_connector() -> Any:
    try:
        import snowflake.connector as connector
    except ImportError as exc:  # pragma: no cover
        raise ImportError("needs `pip install 'assay[snowflake]'`") from exc
    return connector


if __name__ == "__main__":
    raise SystemExit(main())
