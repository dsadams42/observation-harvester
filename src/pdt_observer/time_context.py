from __future__ import annotations

import re

from pdt_observer.models import (
    DaylightState,
    DayPart,
    TimeContext,
    TimePrecision,
)

_CLOCK_RE = re.compile(
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<meridiem>a\.m\.|p\.m\.|am|pm|AM|PM)"
)
_APPROXIMATE_RE = re.compile(r"\b(?:about|around|approximately|approx\.?)\b", re.IGNORECASE)


def day_part_for_local_time(value: str) -> DayPart:
    hour = int(value.split(":", maxsplit=1)[0])
    if 4 <= hour <= 6:
        return DayPart.EARLY_MORNING
    if 7 <= hour <= 11:
        return DayPart.MORNING
    if 12 <= hour <= 16:
        return DayPart.AFTERNOON
    if 17 <= hour <= 20:
        return DayPart.EVENING
    return DayPart.NIGHT


def normalize_observed_time_text(observed_time_text: str | None) -> TimeContext | None:
    if observed_time_text is None:
        return None

    text = observed_time_text.strip()
    if not text:
        return None

    clock_match = _CLOCK_RE.search(text)
    if clock_match is not None:
        hour = int(clock_match.group("hour"))
        minute = int(clock_match.group("minute") or "0")
        meridiem = clock_match.group("meridiem").replace(".", "").casefold()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

        local_time = f"{hour:02d}:{minute:02d}"
        precision = (
            TimePrecision.APPROXIMATE
            if _APPROXIMATE_RE.search(text)
            else TimePrecision.EXACT
        )
        return TimeContext(
            observed_time_local=local_time,
            time_precision=precision,
            day_part=day_part_for_local_time(local_time),
            daylight_state=DaylightState.UNKNOWN,
        )

    lowered = text.casefold()
    if "early morning" in lowered:
        day_part = DayPart.EARLY_MORNING
    elif "morning" in lowered:
        day_part = DayPart.MORNING
    elif "afternoon" in lowered:
        day_part = DayPart.AFTERNOON
    elif "evening" in lowered:
        day_part = DayPart.EVENING
    elif "night" in lowered or "overnight" in lowered:
        day_part = DayPart.NIGHT
    elif "daytime" in lowered or re.search(r"\bday\b", lowered) is not None:
        day_part = DayPart.DAY
    else:
        day_part = DayPart.UNKNOWN

    if day_part == DayPart.UNKNOWN:
        return TimeContext()
    return TimeContext(time_precision=TimePrecision.DAY_PART_ONLY, day_part=day_part)
