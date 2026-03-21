"""
IR metadata stored in a dedicated 'IR-Data' field on Extracts/Sources notes.
Format: JSON string with all scheduling parameters.
This avoids needing extra Anki fields — one hidden field holds everything.
"""

import json
from datetime import date
from . import scheduler


DEFAULT_META = {
    "ir_type": "extract",       # "source" | "extract"
    "ir_priority": 50.0,        # 0-100, lower = higher priority
    "ir_interval": 1,           # days
    "ir_af": 2.0,               # A-Factor 1.2-6.9
    "ir_next_due": None,        # ISO date string
    "ir_last_review": None,     # ISO date string
    "ir_review_count": 0,
    "ir_status": "active",      # active | done | dismissed | forgotten
    "ir_parent_nid": None,      # note id of parent
    "ir_text_length": 0,
}

IR_DATA_FIELD = "IR-Data"


def get_meta(note) -> dict:
    """Read IR metadata from note's IR-Data field."""
    try:
        raw = note[IR_DATA_FIELD]
        if raw and raw.strip():
            meta = json.loads(raw)
            # Merge with defaults for any missing keys
            for k, v in DEFAULT_META.items():
                if k not in meta:
                    meta[k] = v
            return meta
    except (KeyError, json.JSONDecodeError):
        pass
    return dict(DEFAULT_META)


def set_meta(note, meta: dict):
    """Write IR metadata to note's IR-Data field."""
    try:
        note[IR_DATA_FIELD] = json.dumps(meta, ensure_ascii=False)
    except KeyError:
        pass


def has_ir_data(note) -> bool:
    """Check if note has the IR-Data field."""
    try:
        _ = note[IR_DATA_FIELD]
        return True
    except KeyError:
        return False


def is_ir_note(note) -> bool:
    """Check if this is an IR-managed note (has IR-Data with content)."""
    try:
        raw = note[IR_DATA_FIELD]
        return bool(raw and raw.strip())
    except KeyError:
        return False


def init_meta_for_source(note, priority: float = 50.0):
    """Initialize IR metadata for a new source note."""
    text_len = len(note.fields[0]) if note.fields else 0
    meta = dict(DEFAULT_META)
    meta["ir_type"] = "source"
    meta["ir_priority"] = priority
    meta["ir_af"] = scheduler.af_from_priority_and_length(priority, text_len)
    meta["ir_next_due"] = scheduler.date_from_days(1)
    meta["ir_interval"] = 1
    meta["ir_text_length"] = text_len
    set_meta(note, meta)


def init_meta_for_extract(note, parent_nid: int, parent_priority: float, text_length: int):
    """Initialize IR metadata for a new extract note."""
    meta = dict(DEFAULT_META)
    meta["ir_type"] = "extract"
    meta["ir_priority"] = parent_priority
    meta["ir_af"] = scheduler.af_from_priority_and_length(parent_priority, text_length)
    meta["ir_next_due"] = scheduler.date_from_days(1)
    meta["ir_interval"] = 1
    meta["ir_parent_nid"] = parent_nid
    meta["ir_text_length"] = text_length
    set_meta(note, meta)
