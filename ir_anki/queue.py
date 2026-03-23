"""Queue engine for IR topics. Priority-sorted, auto-postpone, mercy, orphan cleanup."""

import math, random
from datetime import date
from typing import List, Tuple
from aqt import mw
from . import scheduler
from .ir_meta import get, put, is_topic


def _iter_topic_notes(deck_name):
    """Yield (nid, note, meta) for all topic notes in deck. Deduplicates by nid."""
    if not mw.col: return
    cids = mw.col.find_cards(f'"deck:{deck_name}"')
    seen = set()
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if not is_topic(note): continue
        yield card.nid, note, get(note)


def build_queue(deck_name, randomization=0):
    today = date.today().isoformat()
    cands = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active": continue
        due = m.get("due")
        if not due or due > today: continue
        cands.append((m["p"], nid))
    cands.sort(key=lambda x: (x[0], x[1]))  # sort by priority, then nid for stability
    if randomization > 0 and len(cands) > 1:
        deg = randomization / 100
        mx = max(1, int(deg * len(cands)))
        for i in range(len(cands)):
            if random.random() < deg:
                j = min(max(0, i + random.randint(-mx, mx)), len(cands) - 1)
                cands[i], cands[j] = cands[j], cands[i]
    return [c[1] for c in cands]


def auto_postpone(deck_name, protection_pct=10):
    if not mw.col: return 0
    today = date.today().isoformat()
    overdue = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active": continue
        due = m.get("due")
        if not due or due >= today: continue
        overdue.append((m["p"], nid))
    if not overdue: return 0
    overdue.sort(key=lambda x: x[0])
    prot = math.ceil(len(overdue) * (protection_pct / 100))
    n = 0
    for _, nid in overdue[prot:]:
        note = mw.col.get_note(nid)
        m = get(note)
        r = scheduler.postpone(m["iv"], m["af"])
        m["due"], m["iv"], m["af"] = r["due"], r["iv"], r["af"]
        put(note, m); mw.col.update_note(note); n += 1
    return n


def mercy(deck_name, mercy_days=14):
    if not mw.col: return 0
    today = date.today().isoformat()
    overdue = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active": continue
        due = m.get("due")
        if not due or due > today: continue
        overdue.append((m["p"], nid))
    if not overdue or mercy_days <= 0: return 0
    overdue.sort(key=lambda x: x[0])
    per_day = max(1, math.ceil(len(overdue) / mercy_days))
    n = 0
    for i, (_, nid) in enumerate(overdue):
        note = mw.col.get_note(nid)
        m = get(note)
        m["due"] = scheduler.date_from_days(i // per_day)
        put(note, m); mw.col.update_note(note); n += 1
    return n


def clean_orphans(deck_name):
    """Remove IR-Data from notes whose parent no longer exists."""
    if not mw.col: return 0
    all_nids = set()
    for nid, _, _ in _iter_topic_notes(deck_name):
        all_nids.add(nid)
    n = 0
    for nid, note, m in _iter_topic_notes(deck_name):
        pnid = m.get("pnid", 0)
        if pnid and pnid not in all_nids:
            m["pnid"] = 0  # clear orphan parent ref
            put(note, m); mw.col.update_note(note); n += 1
    return n
