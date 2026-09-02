"""Writes the demo dataset (demo/data.py) into a local DuckDB file."""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from demo import data
from demo.data import AS_OF  # re-exported: the integration suite pins its clock to it

__all__ = ["AS_OF", "seed"]


def seed(path: Path, backfill: bool = False, days: int = 540) -> None:
    conn = duckdb.connect(str(path))
    try:
        if backfill:
            _backfill(conn)
            return
        dataset = data.generate(days)
        for table in data.TABLES:
            drop, create = data.ddl(table)
            conn.execute(drop)
            conn.execute(create)
        for table in ("regions", "accounts", "orders", "order_items", "discounts", "tickets"):
            conn.executemany(data.insert(table), dataset.rows(table))
        _events(conn, days)
    finally:
        conn.close()


def _events(conn: duckdb.DuckDBPyConnection, days: int) -> None:
    """Generated in-warehouse: a quarter of a million rows is not worth binding.

    460 distinct users are active on any given day out of a pool of 900, so a
    month of daily distinct counts summed together is roughly fifteen times
    the true monthly figure — which is exactly what `active_users` being
    declared additive tells every consumer to do.
    """
    conn.execute(
        f"""
        INSERT INTO events
        SELECT ((i * 7919) % {data.USER_POOL}) + 1 AS user_id,
               TIMESTAMP '{data.start(days):%Y-%m-%d %H:%M:%S}'
                 + INTERVAL ((i // {data.EVENTS_PER_DAY})) DAY
                 + INTERVAL ((i * 37) % 1440) MINUTE
        FROM generate_series(0, {days * data.EVENTS_PER_DAY - 1}) AS t(i)
        """
    )


def _backfill(conn: duckdb.DuckDBPyConnection) -> None:
    next_id = conn.execute("SELECT max(id) + 1 FROM orders").fetchone()[0]
    conn.executemany(data.insert("orders"), data.backfill_orders(next_id))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--backfill", action="store_true", help="restate a closed month")
    parser.add_argument("--days", type=int, default=540)
    args = parser.parse_args()
    seed(args.path, backfill=args.backfill, days=args.days)
    print(f"{'backfilled' if args.backfill else 'seeded'} {args.path}")
