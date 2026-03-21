"""SuperMemo 19-style topic scheduler. Ported from Obsidian plugin."""

import math
from datetime import date, timedelta

DEFAULT_AF = 2.0
MIN_AF = 1.2
MAX_AF = 6.9


def today_str(): return date.today().isoformat()
def date_from_days(d): return (date.today() + timedelta(days=max(0, d))).isoformat()
def _clamp_af(af): return min(MAX_AF, max(MIN_AF, af))
def clamp_priority(p): return round(max(0.0, min(100.0, p)) * 100) / 100
def compute_next_interval(cur, af): return max(1, math.ceil(cur * af)) if cur > 0 else 1


def af_from_priority_and_length(priority, text_length=0):
    af = 1.2 + (priority / 50) * 0.8 if priority <= 50 else 2.0 + ((priority - 50) / 50) * 3.0
    if text_length > 10000: af *= 0.75
    elif text_length > 2000: af *= 0.85
    elif text_length > 500: af *= 0.95
    return _clamp_af(af)


def af_from_priority(p): return af_from_priority_and_length(p, 0)


def execute_repetition(iv, af, rc):
    ni = compute_next_interval(max(1, iv), af)
    return {"due": date_from_days(ni), "iv": ni, "rc": rc + 1, "af": af}


def reschedule_increment(iv, added):
    cur = max(1, iv)
    return {"due": date_from_days(added), "iv": cur + added}


def mid_interval_rep(af, rc):
    return {"rc": rc + 1, "af": _clamp_af(af * 0.97)}


def adjust_af_on_reschedule(iv, af, new_days):
    expected = compute_next_interval(max(1, iv), af)
    if new_days < expected:
        return _clamp_af(af * math.pow(max(0.01, new_days / max(1, expected)), 0.4))
    if new_days > expected:
        return _clamp_af(af * (1 + math.log(min(5, new_days / max(1, expected))) * 0.22))
    return af


def adjust_priority_on_interval(p, iv, af, new_days):
    expected = compute_next_interval(max(1, iv), af)
    if new_days < expected:
        boost = -math.log(max(0.01, new_days / max(1, expected))) * 5
        return clamp_priority(p - min(boost, 20))
    if new_days > expected:
        drop = math.log(min(10, new_days / max(1, expected))) * 5
        return clamp_priority(p + min(drop, 15))
    return p


def parent_af_after_extract(af): return _clamp_af(af * 1.05)
def parent_priority_after_extract(p): return clamp_priority(p + 2)
def af_on_postpone(af): return _clamp_af(af * 1.03)


def postpone(iv, af, factor=1.5):
    ni = max(1, math.ceil(max(1, iv) * factor))
    return {"due": date_from_days(ni), "iv": ni, "af": af_on_postpone(af)}
