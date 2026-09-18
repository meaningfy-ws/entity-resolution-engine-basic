"""Unit tests: the adaptive bite sizer (services).

Spec: batched-resolution — "Bites are sized by processing capacity, not by arrival".
"""

from ere.services.bite_sizer import BiteSizer
from ere.services.resolver_config import BatchSettings


def test_first_bite_uses_the_maximum():
    assert BiteSizer(BatchSettings(max_mentions=300)).limit() == 300


def test_limit_follows_moving_average_of_rate():
    sizer = BiteSizer(BatchSettings(target_seconds=2, max_mentions=500))

    sizer.observe(100, 1.0)  # 100 mentions/s → 200
    first = sizer.limit()
    sizer.observe(10, 1.0)  # EMA: 0.3 × 10 + 0.7 × 100 = 73 mentions/s → 146
    second = sizer.limit()

    assert first == 200
    assert second == 146


def test_limit_never_below_one_even_when_nothing_was_processed():
    sizer = BiteSizer(BatchSettings(target_seconds=2, max_mentions=500))

    sizer.observe(0, 5.0)

    assert sizer.limit() == 1


def test_limit_never_above_the_maximum():
    sizer = BiteSizer(BatchSettings(target_seconds=2, max_mentions=500))

    sizer.observe(10_000, 1.0)

    assert sizer.limit() == 500


def test_bites_without_measurable_duration_are_ignored():
    sizer = BiteSizer(BatchSettings(target_seconds=2, max_mentions=500))

    sizer.observe(100, 0.0)

    assert sizer.limit() == 500
