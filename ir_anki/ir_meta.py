"""IR metadata stored as compact JSON in 'IR-Data' field on topic notes."""

import json
from . import scheduler

IR_FIELD = "IR-Data"
DEFAULT = {"p": 50.0, "iv": 1, "af": 2.0, "due": None, "lr": None, "rc": 0, "st": "active", "tl": 0, "pnid": 0}


def get(note):
    try:
        raw = note[IR_FIELD]
        if raw and raw.strip():
            m = json.loads(raw)
            for k, v in DEFAULT.items():
                if k not in m: m[k] = v
            return m
    except (KeyError, json.JSONDecodeError):
        pass
    return dict(DEFAULT)


def put(note, m):
    try:
        note[IR_FIELD] = json.dumps(m, ensure_ascii=False, separators=(",", ":"))
    except KeyError:
        pass


def has_field(note):
    try:
        _ = note[IR_FIELD]; return True
    except KeyError:
        return False


def is_topic(note):
    try:
        raw = note[IR_FIELD]
        return bool(raw and raw.strip() and raw.startswith("{"))
    except KeyError:
        return False


def init_source(note, priority=50.0):
    tl = len(note.fields[0]) if note.fields else 0
    m = dict(DEFAULT)
    m["p"] = priority
    m["af"] = scheduler.af_from_priority_and_length(priority, tl)
    m["due"] = scheduler.date_from_days(1)
    m["tl"] = tl
    put(note, m)


def init_extract(note, parent_nid, parent_priority, text_length):
    m = dict(DEFAULT)
    m["p"] = parent_priority
    m["af"] = scheduler.af_from_priority_and_length(parent_priority, text_length)
    m["due"] = scheduler.date_from_days(1)
    m["tl"] = text_length
    m["pnid"] = parent_nid
    put(note, m)
