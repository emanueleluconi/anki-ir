"""Topic scheduler: AF × iv with optional per-topic interval ceiling.

Faithful to SuperMemo's topic scheduling:
    next_interval = min(cap, max(1, ceil(iv × af)))

Key distinctions:
- execute_repetition / execute_rep_manual: "I reviewed this card" — updates
  lr, rc, and adjusts AF based on actual vs expected interval.
- reschedule_absolute / reschedule_increment: "Just move the card" — does
  NOT touch lr, rc, or AF. The scheduling state is preserved so next time
  you actually review, AF/interval math proceeds as if no reschedule
  happened.

AF is initialized from priority via a concave curve that keeps low-priority
(high-importance) topics in tight rotation:
    af = 1.2 + (p/100)^1.5 × 2.3

  p=0   → af=1.20  (slow: 1→2→3→4→5)
  p=25  → af=1.49  (slow-medium)
  p=50  → af=2.01  (medium)
  p=75  → af=2.70  (faster)
  p=100 → af=3.50  (fast)

On manual override, AF is nudged in the direction of the override:
    ratio  = actual / expected
    af_new = clamp(af × ratio^0.5, AF_MIN, AF_MAX)

This preserves some memory of past AF (square root dampens the adjustment)
while making the override meaningfully steer future growth.
"""

import math
from datetime import date, timedelta

AF_MIN = 1.2   # most-important / most-difficult topic: slowest growth
AF_MAX = 6.9   # least-important / easiest topic: fastest growth


def today_str() -> str:
    return date.today().isoformat()


def date_from_days(d: int) -> str:
    return (date.today() + timedelta(days=max(0, int(d)))).isoformat()


def clamp_priority(p: float) -> float:
    """Clamp priority to [0, 100] with 2 decimal precision (10,000 unique slots)."""
    return round(max(0.0, min(100.0, p)) * 100) / 100


def _clamp_af(af: float) -> float:
    return min(AF_MAX, max(AF_MIN, af))


# ── AF ↔ priority mapping ────────────────────────────────────────────────────

def af_from_priority(p: float) -> float:
    """Initial AF from priority. Concave curve — low-p topics stay in tight rotation.

    p=0 (most important) → AF=1.2  (intervals barely grow: 1→2→3→4)
    p=100 (least important) → AF=3.5 (intervals grow fast: 1→4→14→49)
    """
    p = max(0.0, min(100.0, p))
    af = 1.2 + ((p / 100.0) ** 1.5) * 2.3
    return _clamp_af(af)


# ── Interval computation ─────────────────────────────────────────────────────

def next_interval(iv: int, af: float, cap: int = 0) -> int:
    """Compute next interval from current iv and af, optionally capped.

    cap = 0 (or None) means no cap. Otherwise result is clamped to cap.
    """
    ni = max(1, int(math.ceil(max(1, iv) * af)))
    if cap and cap > 0:
        ni = min(cap, ni)
    return ni


def apply_cap(iv: int, cap: int) -> int:
    """Clamp an interval to the cap (0/None = no cap)."""
    if cap and cap > 0:
        return min(cap, max(1, iv))
    return max(1, iv)


# ── AF adjustment on manual override ─────────────────────────────────────────

def _adjust_af(iv: int, af: float, new_iv: int) -> float:
    """Nudge AF toward the ratio implied by the user's chosen new interval.

    Uses sqrt dampening so AF doesn't swing too hard on a single override.
    """
    expected = max(1, int(math.ceil(max(1, iv) * af)))
    if expected <= 0:
        return af
    ratio = new_iv / expected
    ratio = max(0.1, min(10.0, ratio))
    return _clamp_af(af * (ratio ** 0.5))


# ── Review actions ────────────────────────────────────────────────────────────

def execute_repetition(iv: int, af: float, rc: int, cap: int = 0) -> dict:
    """Natural queue review ("Next" button pressed on due card).

    Computes the next interval from AF, caps it, advances rc and lr.
    AF itself is NOT changed — the user just accepted the plugin's suggestion.
    """
    ni = next_interval(iv, af, cap)
    return {
        "iv":  ni,
        "af":  af,
        "rc":  rc + 1,
        "lr":  today_str(),
        "due": date_from_days(ni),
    }


def execute_rep_manual(iv: int, af: float, rc: int, new_days: int,
                       cap: int = 0) -> dict:
    """Manual execute-rep: user reviewed and chose an explicit interval.

    Updates lr, rc, AND adjusts AF to reflect the user's choice.
    Caps the stored iv so the cap is honored on the very next cycle.
    """
    capped = apply_cap(new_days, cap)
    new_af = _adjust_af(iv, af, capped)
    return {
        "iv":  capped,
        "af":  new_af,
        "rc":  rc + 1,
        "lr":  today_str(),
        "due": date_from_days(capped),
    }


def reschedule_absolute(iv: int, af: float, rc: int, new_days: int,
                        cap: int = 0) -> dict:
    """Reschedule: set due=today+new_days. Nothing else changes.

    lr, rc, af are NOT touched. Use when you haven't actually reviewed the
    card but want to change when it next appears (SM Ctrl+J semantics).
    """
    capped = apply_cap(new_days, cap)
    return {
        "iv":  capped,
        "af":  af,      # unchanged
        "rc":  rc,      # unchanged
        # lr NOT returned — caller keeps existing value
        "due": date_from_days(capped),
    }


def reschedule_increment(iv: int, af: float, rc: int, added_days: int,
                         cap: int = 0) -> dict:
    """Reschedule by adding days to current interval. No AF/lr/rc change.

    Useful for "push this a bit further" without signalling a review.
    """
    new_iv = apply_cap(max(1, iv) + max(1, added_days), cap)
    return {
        "iv":  new_iv,
        "af":  af,      # unchanged
        "rc":  rc,      # unchanged
        "due": date_from_days(new_iv),
    }


def mid_interval_rep(af: float, rc: int) -> dict:
    """Card shown before its due date: count the review, leave AF alone."""
    return {"rc": rc + 1, "af": af}


def postpone(iv: int, af: float, cap: int = 0, factor: float = 1.5) -> dict:
    """Postpone: multiply current interval by factor (default 1.5×).

    Pushes AF up slightly because the user signalled they want less
    frequent reviews, but dampened.
    """
    new_iv = apply_cap(max(1, int(math.ceil(max(1, iv) * factor))), cap)
    new_af = _adjust_af(iv, af, new_iv)
    return {
        "iv":  new_iv,
        "af":  new_af,
        "due": date_from_days(new_iv),
    }
