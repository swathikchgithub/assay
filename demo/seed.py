"""Builds a demo warehouse with deliberately planted defects.

Every defect below is one an analytics team actually ships. The point of the
demo is not that Assay finds bugs in a toy — it is that each of these looks
completely normal in a dashboard.

    1  region lookup table is missing a newer region  -> CON-01
    2  6% of accounts have no segment                 -> CON-02
    3  order_items fans out the order grain           -> CON-04 (and CON-01)
    4  a promo double-counted in discounts            -> IDN-01
    5  distinct users mislabelled as additive         -> IDN-03
    6  the ticket pipeline stopped 40 hours ago       -> TMP-01
    7  --backfill restates a closed month             -> TMP-03
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

# Naive UTC, which is how warehouses almost always store timestamps.
AS_OF = datetime(2026, 9, 1, 9, 0)


def _start(days: int) -> datetime:
    return AS_OF - timedelta(days=days)


START = _start(540)
REGIONS = ["EMEA", "NA", "APAC", "LATAM"]  # LATAM is missing from `regions`
SEGMENTS = ["Enterprise", "Mid-Market", "SMB", None]  # None -> defect 2

DDL = """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS regions;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS discounts;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS tickets;
CREATE TABLE regions   (code VARCHAR, name VARCHAR);
CREATE TABLE accounts  (id INTEGER, segment VARCHAR);
CREATE TABLE orders    (id INTEGER, account_id INTEGER, region_code VARCHAR,
                        ordered_at TIMESTAMP, amount DOUBLE, discount DOUBLE,
                        status VARCHAR);
CREATE TABLE order_items (order_id INTEGER, sku VARCHAR, qty INTEGER);
CREATE TABLE discounts (order_id INTEGER, amount DOUBLE, applied_at TIMESTAMP);
CREATE TABLE events    (user_id INTEGER, occurred_at TIMESTAMP);
CREATE TABLE tickets   (id INTEGER, opened_at TIMESTAMP, priority VARCHAR);
"""


def seed(path: Path, backfill: bool = False, days: int = 540) -> None:
    conn = duckdb.connect(str(path))
    if backfill:
        _restate_a_closed_month(conn)
        conn.close()
        return
    rng = random.Random(20260901)
    for statement in filter(str.strip, DDL.split(";")):
        conn.execute(statement)
    _regions(conn)
    _accounts(conn, rng)
    _orders(conn, rng, days)
    _events(conn, rng, days)
    _tickets(conn, rng)
    conn.close()


def _regions(conn: duckdb.DuckDBPyConnection) -> None:
    # Defect 1: LATAM opened last year and nobody updated the lookup table.
    conn.executemany(
        "INSERT INTO regions VALUES (?, ?)",
        [("EMEA", "Europe"), ("NA", "North America"), ("APAC", "Asia Pacific")],
    )


def _accounts(conn: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    # Defect 2: ~6% of accounts were created by an integration that never set
    # segment, so any "revenue by segment" slice quietly omits them.
    rows = [
        (i, None if rng.random() < 0.06 else rng.choice(SEGMENTS[:3]))
        for i in range(1, 401)
    ]
    conn.executemany("INSERT INTO accounts VALUES (?, ?)", rows)


def _orders(conn: duckdb.DuckDBPyConnection, rng: random.Random, days: int) -> None:
    orders, items, discounts = [], [], []
    order_id = 0
    for day in range(days):
        stamp = _start(days) + timedelta(days=day)
        for _ in range(rng.randint(26, 34)):
            order_id += 1
            amount = round(rng.uniform(120, 2600), 2)
            promo = 200 <= day < 260
            rate = rng.uniform(0.20, 0.30) if promo else rng.uniform(0.0, 0.12)
            discount = round(amount * rate, 2)
            status = "cancelled" if rng.random() < 0.03 else "complete"
            orders.append(
                (
                    order_id,
                    rng.randint(1, 400),
                    rng.choices(REGIONS, weights=[35, 40, 15, 10])[0],
                    stamp + timedelta(hours=rng.randint(0, 23)),
                    amount,
                    discount,
                    status,
                )
            )
            # Defect 3: an order has many line items; joining them multiplies rows.
            items.extend(
                (order_id, f"SKU-{rng.randint(100, 140)}", rng.randint(1, 4))
                for _ in range(rng.randint(1, 4))
            )
            discounts.append((order_id, discount, stamp))
            # Defect 4: the spring promo batch file was loaded twice, so every
            # promo discount is counted against revenue a second time.
            if promo:
                discounts.append((order_id, discount, stamp))
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?, ?, ?)", items)
    conn.executemany("INSERT INTO discounts VALUES (?, ?, ?)", discounts)


def _events(conn: duckdb.DuckDBPyConnection, rng: random.Random, days: int) -> None:
    """Defect 5 lives in the contract, not here.

    460 distinct users are active on any given day out of a pool of 900, so a
    month of daily distinct counts summed together is roughly fifteen times
    the true monthly figure — which is exactly what `active_users` being
    declared additive tells every consumer to do.
    """
    del rng  # generated in SQL so the 250k rows land in one statement
    conn.execute(
        f"""
        INSERT INTO events
        SELECT ((i * 7919) % 900) + 1 AS user_id,
               TIMESTAMP '{_start(days):%Y-%m-%d %H:%M:%S}'
                 + INTERVAL ((i // 460)) DAY
                 + INTERVAL ((i * 37) % 1440) MINUTE
        FROM generate_series(0, {days * 460 - 1}) AS t(i)
        """
    )


def _tickets(conn: duckdb.DuckDBPyConnection, rng: random.Random) -> None:
    # Defect 6: the ticket pipeline died 40 hours ago against a 24h SLA.
    latest = AS_OF - timedelta(hours=40)
    rows = [
        (i, latest - timedelta(hours=rng.randint(0, 24 * 500)), rng.choice(["P1", "P2", "P3"]))
        for i in range(1, 6000)
    ]
    rows.append((6000, latest, "P3"))
    conn.executemany("INSERT INTO tickets VALUES (?, ?, ?)", rows)


def _restate_a_closed_month(conn: duckdb.DuckDBPyConnection) -> None:
    """Defect 7: a late CRM sync adds orders to a month everyone considers closed."""
    rng = random.Random(7)
    target = datetime(2026, 4, 15, 12)
    next_id = conn.execute("SELECT max(id) + 1 FROM orders").fetchone()[0]
    rows = [
        (
            next_id + i,
            rng.randint(1, 400),
            "NA",
            target + timedelta(days=rng.randint(-14, 14)),
            round(rng.uniform(4000, 9000), 2),
            0.0,
            "complete",
        )
        for i in range(320)
    ]
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--backfill", action="store_true", help="restate a closed month")
    parser.add_argument("--days", type=int, default=540)
    args = parser.parse_args()
    seed(args.path, backfill=args.backfill, days=args.days)
    print(f"{'backfilled' if args.backfill else 'seeded'} {args.path}")
