from __future__ import annotations

from pdt_observer.models import DayPart, TimePrecision
from pdt_observer.time_context import normalize_observed_time_text


def test_normalize_approximate_pm_clock_time() -> None:
    context = normalize_observed_time_text("approximately 9:10 p.m.")

    assert context is not None
    assert context.observed_time_local == "21:10"
    assert context.time_precision == TimePrecision.APPROXIMATE
    assert context.day_part == DayPart.NIGHT


def test_normalize_day_part_only_phrase() -> None:
    context = normalize_observed_time_text("Friday night")

    assert context is not None
    assert context.observed_time_local is None
    assert context.time_precision == TimePrecision.DAY_PART_ONLY
    assert context.day_part == DayPart.NIGHT


def test_unknown_time_text_stays_unknown() -> None:
    context = normalize_observed_time_text("soon after opening")

    assert context is not None
    assert context.observed_time_local is None
    assert context.time_precision == TimePrecision.UNKNOWN
    assert context.day_part == DayPart.UNKNOWN
