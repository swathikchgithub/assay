"""The dataset must be identical wherever it is written.

DuckDB and Snowflake runs are only comparable if they see the same rows, so
generation is deterministic and asserted here rather than trusted.
"""

from demo import data


def test_generation_is_deterministic():
    assert data.generate(90).orders == data.generate(90).orders


def test_every_declared_table_has_columns_and_ddl():
    assert set(data.TABLES) == set(data.COLUMNS)


def test_ddl_is_produced_for_each_table():
    for table in data.TABLES:
        drop, create = data.ddl(table)
        assert drop == f"DROP TABLE IF EXISTS {table}"
        assert create.startswith(f"CREATE TABLE {table} (")


def test_insert_binds_one_placeholder_per_column():
    for table, columns in data.COLUMNS.items():
        assert data.insert(table).count("?") == len(columns)


def test_row_width_matches_the_column_list():
    dataset = data.generate(30)
    for table in ("regions", "accounts", "orders", "order_items", "discounts", "tickets"):
        assert len(dataset.rows(table)[0]) == len(data.COLUMNS[table])


def test_the_region_lookup_is_missing_latam():
    """Defect 1 — the join in the contract is inner, so this loses rows."""
    codes = {code for code, _ in data.generate(30).regions}
    assert "LATAM" in data.REGIONS and "LATAM" not in codes


def test_some_accounts_have_no_segment():
    """Defect 2."""
    segments = [segment for _, segment in data.generate(30).accounts]
    assert 0.02 < segments.count(None) / len(segments) < 0.12


def test_orders_have_more_line_items_than_orders():
    """Defect 3 — this ratio is the fan-out CON-04 reports."""
    dataset = data.generate(30)
    assert len(dataset.order_items) > 2 * len(dataset.orders)


def test_promo_orders_get_a_second_discount_row():
    """Defect 4 — outside the promo window there is one row per order."""
    dataset = data.generate(30)
    assert len(dataset.discounts) == len(dataset.orders)
    promo = data.generate(260)
    assert len(promo.discounts) > len(promo.orders)


def test_the_backfill_lands_entirely_in_one_closed_month():
    """Defect 7 — every restating order falls inside April 2026, which is why
    TMP-03 reports exactly one moved period rather than a smear across three."""
    months = {row[3].strftime("%Y-%m") for row in data.backfill_orders(1)}
    assert months == {"2026-04"}
