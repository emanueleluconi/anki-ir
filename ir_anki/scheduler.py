"""Saturating-curve topic scheduler with effective-N tracking.

Interval formula (from incremental-everything, adapted):
    interval = ⌈firstReview + (maxInterval - firstReview) × en / (en + k)⌉

Where `en` (effective N) is a float that accumulates on each review:
    weight = clamp(actual / expected, 0.1, 10) ** alpha
    en += weight

- actual < expected  → weight < 1  → en grows slowly  → curve stays low
  (you kept setting short intervals → topic stays in short-interval territory)
- actual ≈ expected  → weight ≈ 1  → en grows normally → curve saturates as designed
- actual > expected  → weight > 1  → en grows faster   → curve saturates sooner
  (you postponed → topic moves toward maxInterval faster)

Manual execute_rep with interval=X:
    expected = current curve value for current en
    weight   = (X / expected) ** alpha
    en      += weight
    interval = X  (as set by user)

This means repeated 1d overrides keep en low, so the next natural review
also gives a short interval — the curve "remembers" your reading frequency.
"""

import math
from datetime import date, timedelta

# ── Defaults (overridden by config in main.py) ──────────────────────────────
DEFAULT_FIRST_REVIEW = 3    # days after first review
DEFAULT_MAX_INTERVAL = 21   # asymptotic ceiling (days)
DEFAULT_K            = 4    # saturation speed (halfway at review k+1)
DEFAULT_ALPHA        = 0.5  # sensitivity of manual-override weighting


def today_str() -> str:
    return date.today().isoformat()


def date_from_days(d: int) -> str:
    return (date.today() + timedelta(days=max(0, int(d)))).isoformat()


def clamp_priority(p: float) -> float:
    return round(max(0.0, min(100.0, p)) * 100) / 100


# ── Core curve ───────────────────────────────────────────────────────────────

def _curve(en: float, first: float, maxiv: float, k: float) -> int:
    """Compute next interval from effective-N and curve parameters."""
    if en <= 0:
        return max(1, int(math.ceil(first)))
    return max(1, int(math.ceil(first + (maxiv - first) * en / (en + k))))


def next_interval(en: float, first: float = DEFAULT_FIRST_REVIEW,
                  maxiv: float = DEFAULT_MAX_INTERVAL,
                  k: float = DEFAULT_K) -> int:
    """Public: next interval in days given current effective-N."""
    return _curve(en, first, maxiv, k)


def _weight(actual: int, expected: int, alpha: float = DEFAULT_ALPHA) -> float:
    """Weight to add to en based on actual vs expected interval."""
    ratio = max(0.1, min(10.0, actual / max(1, expected)))
    return ratio ** alpha


# ── Review actions ────────────────────────────────────────────────────────────

def execute_repetition(en: float, rc: int,
                       first: float = DEFAULT_FIRST_REVIEW,
                       maxiv: float = DEFAULT_MAX_INTERVAL,
                       k: float = DEFAULT_K,
                       alpha: float = DEFAULT_ALPHA) -> dict:
    """Normal queue review: advance en by weight=1 (on-schedule review)."""
    # On a natural review we assume actual ≈ expected, so weight = 1.0
    new_en = en + 1.0
    ni = _curve(new_en, first, maxiv, k)
    return {
        "due": date_from_days(ni),
        "iv":  ni,
        "en":  new_en,
        "rc":  rc + 1,
    }


def execute_rep_manual(en: float, rc: int, new_days: int,
                       first: float = DEFAULT_FIRST_REVIEW,
                       maxiv: float = DEFAULT_MAX_INTERVAL,
                       k: float = DEFAULT_K,
                       alpha: float = DEFAULT_ALPHA) -> dict:
    """Manual execute_rep (Shift+R): user sets explicit interval.

    Weight reflects how the chosen interval compares to what the curve
    would have given — short overrides keep en low, long ones push it up.
    """
    expected = _curve(en, first, maxiv, k)
    w = _weight(new_days, expected, alpha)
    new_en = en + w
    return {
        "due": date_from_days(new_days),
        "iv":  new_days,
        "en":  new_en,
        "rc":  rc + 1,
    }


def reschedule_increment(en: float, rc: int, added_days: int,
                         first: float = DEFAULT_FIRST_REVIEW,
                         maxiv: float = DEFAULT_MAX_INTERVAL,
                         k: float = DEFAULT_K,
                         alpha: float = DEFAULT_ALPHA) -> dict:
    """Reschedule (+days, Shift+J): add days to current interval.

    lr (last review) is NOT updated — this is the SM Ctrl+J behaviour.
    en is adjusted by weight of the new total interval vs expected.
    """
    expected = _curve(en, first, maxiv, k)
    new_iv = max(1, expected + added_days)
    w = _weight(new_iv, expected, alpha)
    new_en = en + w
    return {
        "due": date_from_days(new_iv),
        "iv":  new_iv,
        "en":  new_en,
    }


def mid_interval_rep(en: float, rc: int) -> dict:
    """Mid-interval review (card shown before due date): en unchanged."""
    return {"rc": rc + 1, "en": en}


def postpone(en: float, rc: int, iv: int,
             factor: float = 1.5,
             first: float = DEFAULT_FIRST_REVIEW,
             maxiv: float = DEFAULT_MAX_INTERVAL,
             k: float = DEFAULT_K,
             alpha: float = DEFAULT_ALPHA) -> dict:
    """Postpone (Shift+W): multiply current interval by factor.

    en is adjusted upward because we're reviewing later than expected.
    """
    expected = _curve(en, first, maxiv, k)
    new_iv = max(1, int(math.ceil(max(1, iv) * factor)))
    w = _weight(new_iv, expected, alpha)
    new_en = en + w
    return {
        "due": date_from_days(new_iv),
        "iv":  new_iv,
        "en":  new_en,
    }
