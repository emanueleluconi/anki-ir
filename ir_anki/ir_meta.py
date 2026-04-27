"""IR metadata stored as compact JSON in 'IR-Data' field on topic notes.

Schema (all keys always present):
    p    float  priority 0-100 (0 = most important), 2-decimal precision
    iv   int    current interval in days
    af   float  A-Factor (1.2-6.9): next_interval = ceil(iv × af)
    cap  int    per-topic interval ceiling (0 = no cap)
    due  str    ISO date of next review, or null
    lr   str    ISO date of last review, or null
    rc   int    total review count
    st   str    "active" | "done" | "dismissed" | "forgotten"
    pnid int    parent note ID (0 = no parent / orphan)
"""

import json
from . import scheduler

IR_FIELD = "IR-Data"
DEFAULT = {
    "p":    50.0,
    "iv":   1,
    "af":   2.0,
    "cap":  0,
    "due":  None,
    "lr":   None,
    "rc":   0,
    "st":   "active",
    "pnid": 0,
}

# Keys from older schema versions that we silently drop on read.
_OBSOLETE_KEYS = {"en", "tl"}


def get(note) -> dict:
    try:
        raw = note[IR_FIELD]
        if raw and raw.strip():
            m = json.loads(raw)
            # Migrate: fill in any missing keys from DEFAULT
            for k, v in DEFAULT.items():
                if k not in m:
                    m[k] = v
            # Migrate: drop obsolete keys
            for k in _OBSOLETE_KEYS:
                m.pop(k, None)
            return m
    except (KeyError, json.JSONDecodeError):
        pass
    return dict(DEFAULT)


def put(note, m: dict):
    clean = {k: v for k, v in m.items() if k not in _OBSOLETE_KEYS}
    try:
        note[IR_FIELD] = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    except KeyError:
        pass


def save_meta(nid: int, m: dict):
    """Safely persist IR metadata without touching any other field."""
    from aqt import mw
    note = mw.col.get_note(nid)
    put(note, m)
    mw.col.update_note(note)


def has_field(note) -> bool:
    try:
        _ = note[IR_FIELD]
        return True
    except KeyError:
        return False


def is_topic(note) -> bool:
    try:
        raw = note[IR_FIELD]
        return bool(raw and raw.strip() and raw.startswith("{"))
    except KeyError:
        return False


# ── Initialisation helpers ────────────────────────────────────────────────────

def init_source(note, priority: float = 50.0, cap: int = 0):
    """Initialise a brand-new source note with the given priority and cap."""
    p = scheduler.clamp_priority(priority)
    m = dict(DEFAULT)
    m["p"]   = p
    m["iv"]  = 1
    m["af"]  = scheduler.af_from_priority(p)
    m["cap"] = int(cap) if cap else 0
    m["due"] = scheduler.date_from_days(1)
    put(note, m)


def init_extract(note, parent_nid: int, parent_priority: float, cap: int = 0):
    """Initialise a new extract note.

    Priority = parent - 5 so extracts appear before their parent.
    AF derived from the extract's own (lower) priority.
    """
    p = scheduler.clamp_priority(parent_priority - 5.0)
    m = dict(DEFAULT)
    m["p"]    = p
    m["iv"]   = 1
    m["af"]   = scheduler.af_from_priority(p)
    m["cap"]  = int(cap) if cap else 0
    m["due"]  = scheduler.date_from_days(1)
    m["pnid"] = parent_nid
    put(note, m)
