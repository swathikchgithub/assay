"""Robust statistics — mean/stdev would let one outage hide the next."""

from assay.invariants.stats import envelope, mad


def test_mad_of_a_flat_series_is_zero():
    assert mad([5.0, 5.0, 5.0]) == 0.0


def test_mad_ignores_a_single_extreme_value():
    assert mad([10.0, 10.0, 10.0, 10.0, 900.0]) == 0.0


def test_envelope_brackets_the_median():
    low, high = envelope([10.0, 11.0, 9.0, 10.0, 12.0], k=3.0)
    assert low < 10.0 < high


def test_envelope_falls_back_to_a_proportional_band_when_flat():
    low, high = envelope([100.0] * 5, k=1.0)
    assert (low, high) == (95.0, 105.0)


def test_envelope_of_an_empty_series_is_degenerate():
    assert envelope([], k=3.0) == (0.0, 0.0)
