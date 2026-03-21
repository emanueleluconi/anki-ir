"""
SuperMemo 19-style topic scheduler for Anki incremental reading.
Core formula: next_interval = current_interval × A-Factor

Ported from the Obsidian incremental-reading-plugin Scheduler.ts.
"""

import math
from datetime import date, timedelta

DEFAULT_AF = 2.0
MIN_AF = 1.2
MAX_AF = 6.9
CHARS_PER_PAGE = 2000


def today_str() -> str:
    return date.today().isoformat()


def date_from_days(days: int) -> str:
    return (date.today() + timedelta(days=max(0, days))).isoformat()


def _clamp_af(af: float) -> float:
    return min(MAX_AF, max(MIN_AF, af))


def clamp_priority(p: float) -> float:
    """Clamp priority to 0-100 with 2 decimal precision."""
    return round(max(0.0, min(100.0, p)) * 100) / 100


def compute_next_interval(current: int, af: float) -> int:
    if current <= 0:
        return 1
    return max(1, math.ceil(current * af))


def af_from_priority_and_length(priority: float, text_length: int) -> float:
    """Compute initial A-Factor from priority AND text length."""
    if priority <= 50:
        af = 1.2 + (priority / 50) * 0.8
    else:
        af = 2.0 + ((priority - 50) / 50) * 3.0
    if text_length > 10000:
        af *= 0.75
    elif text_length > 2000:
        af *= 0.85
    elif text_length > 500:
        af *= 0.95
    return _clamp_af(af)


def af_from_priority(priority: float) -> float:
    return af_from_priority_and_length(priority, 0)



def execute_repetition(interval: int, af: float, review_count: int, initial_interval: int = 1):
    """Execute Repetition (SuperMemo Ctrl+Shift+R): new interval from today."""
    cur = interval if interval > 0 else initial_interval
    new_interval = compute_next_interval(cur, af)
    return {
        "next_due": date_from_days(new_interval),
        "new_interval": new_interval,
        "new_review_count": review_count + 1,
        "new_af": af,
    }


def reschedule_increment(interval: int, added_days: int, initial_interval: int = 1):
    """Reschedule (SuperMemo Ctrl+J): add days to current interval."""
    cur = interval if interval > 0 else initial_interval
    new_interval = cur + added_days
    return {"next_due": date_from_days(added_days), "new_interval": new_interval}


def mid_interval_repetition(af: float, review_count: int):
    """Mid-interval repetition: review before due without disrupting schedule."""
    return {"new_review_count": review_count + 1, "new_af": _clamp_af(af * 0.97)}


def adjust_af_on_reschedule(interval: int, af: float, new_days: int) -> float:
    """Adjust AF when user manually reschedules. Log scale for aggressive response."""
    cur = max(1, interval)
    expected = compute_next_interval(cur, af)
    if new_days < expected:
        ratio = new_days / max(1, expected)
        factor = math.pow(max(0.01, ratio), 0.4)
        return _clamp_af(af * factor)
    if new_days > expected:
        ratio = min(5, new_days / max(1, expected))
        factor = 1 + math.log(ratio) * 0.22
        return _clamp_af(af * factor)
    return af


def adjust_priority_on_interval_change(priority: float, interval: int, af: float, new_days: int) -> float:
    """Bidirectional priority adjustment when interval changes."""
    cur = max(1, interval)
    expected = compute_next_interval(cur, af)
    if new_days < expected:
        ratio = new_days / max(1, expected)
        boost = -math.log(max(0.01, ratio)) * 5
        return clamp_priority(priority - min(boost, 20))
    if new_days > expected:
        ratio = min(10, new_days / max(1, expected))
        drop = math.log(ratio) * 5
        return clamp_priority(priority + min(drop, 15))
    return priority


def adjust_parent_after_extract(parent_af: float) -> float:
    return _clamp_af(parent_af * 1.05)


def adjust_parent_priority_after_extract(parent_priority: float) -> float:
    return clamp_priority(parent_priority + 2)


def adjust_af_on_postpone(af: float) -> float:
    return _clamp_af(af * 1.03)


def postpone_element(interval: int, af: float, factor: float = 1.5):
    """Postpone: multiply interval by factor."""
    cur = max(1, interval)
    new_interval = max(1, math.ceil(cur * factor))
    new_af = adjust_af_on_postpone(af)
    return {"next_due": date_from_days(new_interval), "new_interval": new_interval, "new_af": new_af}


def spread_priorities(parent_priority: float, child_count: int) -> list:
    if child_count <= 1:
        return [parent_priority]
    half_range = min(5, child_count // 2)
    result = []
    for i in range(child_count):
        offset = -half_range + (i / (child_count - 1)) * (half_range * 2)
        result.append(clamp_priority(parent_priority + offset))
    return result
