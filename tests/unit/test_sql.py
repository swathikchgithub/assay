"""Generated SQL: identifiers quoted, values bound, joins only where earned."""

from datetime import date

from assay.engine.sql import Window


def test_total_omits_joins_the_measure_does_not_need(revenue_sql):
    sql = revenue_sql.total(Window()).sql
    assert "regions" not in sql
    assert 'FROM "orders" AS b' in sql


def test_grouped_adds_the_join_needed_to_reach_the_dimension(revenue, revenue_sql):
    sql = revenue_sql.grouped(revenue.dimension("region"), Window()).sql
    assert 'INNER JOIN "regions" AS j0 ON b."region_code" = j0."code"' in sql
    assert 'j0."name" AS dim_value' in sql


def test_window_bounds_are_bound_parameters_not_interpolated(revenue_sql):
    query = revenue_sql.total(Window(date(2026, 1, 1), date(2026, 7, 1)))
    assert query.params == (date(2026, 1, 1), date(2026, 7, 1))
    assert "2026" not in query.sql
    assert query.sql.count("?") == 2


def test_window_is_half_open(revenue_sql):
    sql = revenue_sql.total(Window(date(2026, 1, 1), date(2026, 7, 1))).sql
    assert 'b."ordered_at" >= ?' in sql and 'b."ordered_at" < ?' in sql


def test_row_counts_compares_base_against_traversed(revenue, revenue_sql):
    sql = revenue_sql.row_counts(revenue.joins[0], Window()).sql
    assert sql.count("count(*)") == 2
    assert sql.count("INNER JOIN") == 1


def test_identifiers_with_quotes_are_escaped(dialect):
    assert dialect.quote('we"ird') == '"we""ird"'
