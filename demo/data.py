"""The demo dataset, generated once and independent of where it is written.

Seven defects, each one an analytics team actually ships, each one invisible
in a dashboard:

    1  region lookup table is missing a newer region  -> CON-01
    2  6% of accounts have no segment                 -> CON-02
    3  order_items fans out the order grain           -> CON-04 (and CON-01)
    4  a promo double-counted in discounts            -> IDN-01
    5  distinct users mislabelled as additive         -> IDN-03
    6  the ticket pipeline stopped 40 hours ago       -> TMP-01
    7  backfill_orders() restates a closed month      -> TMP-03

Generation is separated from persistence so the same rows land in DuckDB for
the local demo and in Snowflake for an end-to-end run against a real
warehouse. Both must see identical data or the two runs are not comparable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

# Naive UTC, which is how warehouses almost always store timestamps.
AS_OF = datetime(2026, 9, 1, 9, 0)
SEED = 20260901

REGIONS = ["EMEA", "NA", "APAC", "LATAM"]  # LATAM is missing from `regions`
SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
ACCOUNT_COUNT = 400
EVENTS_PER_DAY = 460
USER_POOL = 900

# Types chosen from the intersection of DuckDB and Snowflake spellings, so one
# set of DDL builds the demo on either warehouse.
TABLES: dict[str, str] = {
    "regions": "code VARCHAR, name VARCHAR",
    "accounts": "id INTEGER, segment VARCHAR",
    "orders": (
        "id INTEGER, account_id INTEGER, region_code VARCHAR, "
        "ordered_at TIMESTAMP, amount DOUBLE, discount DOUBLE, status VARCHAR"
    ),
    "order_items": "order_id INTEGER, sku VARCHAR, qty INTEGER",
    "discounts": "order_id INTEGER, amount DOUBLE, applied_at TIMESTAMP",
    "events": "user_id INTEGER, occurred_at TIMESTAMP",
    "tickets": "id INTEGER, opened_at TIMESTAMP, priority VARCHAR",
}

COLUMNS: dict[str, tuple[str, ...]] = {
    "regions": ("code", "name"),
    "accounts": ("id", "segment"),
    "orders": (
        "id", "account_id", "region_code", "ordered_at", "amount", "discount", "status",
    ),
    "order_items": ("order_id", "sku", "qty"),
    "discounts": ("order_id", "amount", "applied_at"),
    "events": ("user_id", "occurred_at"),
    "tickets": ("id", "opened_at", "priority"),
}


def start(days: int) -> datetime:
    return AS_OF - timedelta(days=days)


@dataclass(frozen=True)
class DemoData:
    """Every table except `events`, which is generated in-warehouse."""

    regions: list[tuple]
    accounts: list[tuple]
    orders: list[tuple]
    order_items: list[tuple]
    discounts: list[tuple]
    tickets: list[tuple]

    def rows(self, table: str) -> list[tuple]:
        return getattr(self, table)


def generate(days: int = 540) -> DemoData:
    """Deterministic for a given `days`. Time: O(days). Space: O(rows)."""
    rng = random.Random(SEED)
    accounts = _accounts(rng)
    orders, items, discounts = _orders(rng, days)
    return DemoData(
        regions=_regions(),
        accounts=accounts,
        orders=orders,
        order_items=items,
        discounts=discounts,
        tickets=_tickets(rng),
    )


def _regions() -> list[tuple]:
    # Defect 1: LATAM opened last year and nobody updated the lookup table.
    return [("EMEA", "Europe"), ("NA", "North America"), ("APAC", "Asia Pacific")]


def _accounts(rng: random.Random) -> list[tuple]:
    # Defect 2: ~6% of accounts came from an integration that never set
    # segment, so any "revenue by segment" slice quietly omits them.
    return [
        (i, None if rng.random() < 0.06 else rng.choice(SEGMENTS))
        for i in range(1, ACCOUNT_COUNT + 1)
    ]


def _orders(rng: random.Random, days: int) -> tuple[list, list, list]:
    orders, items, discounts = [], [], []
    order_id = 0
    for day in range(days):
        stamp = start(days) + timedelta(days=day)
        for _ in range(rng.randint(26, 34)):
            order_id += 1
            promo = 200 <= day < 260
            amount = round(rng.uniform(120, 2600), 2)
            rate = rng.uniform(0.20, 0.30) if promo else rng.uniform(0.0, 0.12)
            discount = round(amount * rate, 2)
            orders.append(_order(rng, order_id, stamp, amount, discount))
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
    return orders, items, discounts


def _order(
    rng: random.Random, order_id: int, stamp: datetime, amount: float, discount: float
) -> tuple:
    return (
        order_id,
        rng.randint(1, ACCOUNT_COUNT),
        rng.choices(REGIONS, weights=[35, 40, 15, 10])[0],
        stamp + timedelta(hours=rng.randint(0, 23)),
        amount,
        discount,
        "cancelled" if rng.random() < 0.03 else "complete",
    )


def _tickets(rng: random.Random) -> list[tuple]:
    # Defect 6: the ticket pipeline died 40 hours ago against a 24h SLA.
    latest = AS_OF - timedelta(hours=40)
    rows = [
        (i, latest - timedelta(hours=rng.randint(0, 24 * 500)), rng.choice(["P1", "P2", "P3"]))
        for i in range(1, 6000)
    ]
    rows.append((6000, latest, "P3"))
    return rows


def backfill_orders(next_id: int, count: int = 320) -> list[tuple]:
    """Defect 7: a late CRM sync adds orders to a month everyone calls closed."""
    rng = random.Random(7)
    target = datetime(2026, 4, 15, 12)
    return [
        (
            next_id + i,
            rng.randint(1, ACCOUNT_COUNT),
            "NA",
            target + timedelta(days=rng.randint(-14, 14)),
            round(rng.uniform(4000, 9000), 2),
            0.0,
            "complete",
        )
        for i in range(count)
    ]


def ddl(table: str) -> tuple[str, str]:
    """(drop, create) for one table, valid on both DuckDB and Snowflake."""
    return (
        f"DROP TABLE IF EXISTS {table}",
        f"CREATE TABLE {table} ({TABLES[table]})",
    )


def insert(table: str) -> str:
    """A parameterised single-row INSERT, batched by each sink."""
    placeholders = ", ".join("?" * len(COLUMNS[table]))
    return (
        f"INSERT INTO {table} ({', '.join(COLUMNS[table])}) VALUES ({placeholders})"
    )
