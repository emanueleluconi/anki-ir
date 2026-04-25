"""IR metadata stored as compact JSON in 'IR-Data' field on topic notes.

Schema (all keys always present):
    p    float  priority 0-100 (0 = most important)
    iv   int    last set interval in days
    en   float  effective-N for the saturating curve (accumulates on each review)
    due  str    ISO date of next review, or null
    lr   str    ISO date of last review, or null
    rc   int    total review count
    st   str    "active" | "done" | "dismissed" | "forgotten"
    pnid int    parent note ID (0 = no parent / orphan)

AF and tl (text length) have been removed.  The saturating curve uses en
instead of AF, and interval growth is no longer length-dependent.
"""

import json
from . import scheduler

IR_FIELD = "IR-Data"
DEFAULT = {
    "p":    50.0,
    "iv":   1,
    "en":   0.0,
    "due":  None,
    "lr":   None,
    "rc":   0,
    "st":   "active",
    "pnid": 0,
}

# Keys that existed in the old schema but are no longer used.
# We silently drop them when reading old notes so they don't linger.
_OBSOLETE_KEYS = {"af", "tl"}


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
    # Never write obsolete keys
    clean = {k: v for k, v in m.items() if k not in _OBSOLETE_KEYS}
    try:
        note[IR_FIELD] = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    except KeyError:
        pass


def save_meta(nid: int, m: dict):
    """Safely persist IR metadata without touching any other field.

    Always fetches a fresh note from the DB, writes ONLY the IR-Data field,
    and saves.  This prevents stale Python objects from overwriting content
    fields (e.g. highlights in Text) that were modified by another code path.
    """
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

def init_source(note, priority: float = 50.0):
    """Initialise a brand-new source note with the given priority."""
    m = dict(DEFAULT)
    m["p"]   = scheduler.clamp_priority(priority)
    m["iv"]  = 1
    m["en"]  = 0.0
    m["due"] = scheduler.date_from_days(1)
    put(note, m)


def init_extract(note, parent_nid: int, parent_priority: float):
    """Initialise a new extract note.

    Priority = parent_priority - 5 (clamped to [0, 100]) so extracts
    appear before their parent in the priority queue.
    Both Zotero-imported and manually created extracts use this function.
    """
    m = dict(DEFAULT)
    m["p"]    = scheduler.clamp_priority(parent_priority - 5.0)
    m["iv"]   = 1
    m["en"]   = 0.0
    m["due"]  = scheduler.date_from_days(1)
    m["pnid"] = parent_nid
    put(note, m)
