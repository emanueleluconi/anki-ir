"""Queue engine for IR topics. Priority-sorted, auto-postpone, mercy, orphan cleanup."""

import math
import random
from datetime import date
from aqt import mw
from . import scheduler
from .ir_meta import get, is_topic, save_meta


def _iter_topic_notes(deck_name):
    """Yield (nid, note, meta) for all topic notes in deck. Deduplicates by nid."""
    if not mw.col:
        return
    cids = mw.col.find_cards(f'"deck:{deck_name}"')
    seen = set()
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen:
            continue
        seen.add(card.nid)
        note = card.note()
        if not is_topic(note):
            continue
        yield card.nid, note, get(note)


def build_queue(deck_name, randomization=0):
    """Return list of nids sorted by priority (ascending = most important first).

    randomization: 0-100 integer; higher values introduce more random swaps.
    """
    today = date.today().isoformat()
    cands = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active":
            continue
        due = m.get("due")
        if not due or due > today:
            continue
        cands.append((m["p"], nid))

    # Primary sort: priority ascending (0 = most important)
    # Secondary sort: nid for deterministic tie-breaking
    cands.sort(key=lambda x: (x[0], x[1]))

    if randomization > 0 and len(cands) > 1:
        deg = randomization / 100.0
        mx = max(1, int(deg * len(cands)))
        for i in range(len(cands)):
            if random.random() < deg:
                j = min(max(0, i + random.randint(-mx, mx)), len(cands) - 1)
                cands[i], cands[j] = cands[j], cands[i]

    return [c[1] for c in cands]


def auto_postpone(deck_name, protection_pct=10,
                  first=scheduler.DEFAULT_FIRST_REVIEW,
                  maxiv=scheduler.DEFAULT_MAX_INTERVAL,
                  k=scheduler.DEFAULT_K,
                  alpha=scheduler.DEFAULT_ALPHA):
    """Postpone overdue low-priority topics, protecting the top protection_pct%.

    Only topics from *previous* days are affected (SM19 behaviour).
    Returns the number of topics postponed.
    """
    if not mw.col:
        return 0
    today = date.today().isoformat()
    overdue = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active":
            continue
        due = m.get("due")
        if not due or due >= today:
            continue
        overdue.append((m["p"], nid))

    if not overdue:
        return 0

    overdue.sort(key=lambda x: x[0])  # best priority first
    prot = math.ceil(len(overdue) * (protection_pct / 100.0))
    n = 0
    for _, nid in overdue[prot:]:
        note = mw.col.get_note(nid)
        m = get(note)
        r = scheduler.postpone(m["en"], m["rc"], m["iv"],
                               first=first, maxiv=maxiv, k=k, alpha=alpha)
        m["due"] = r["due"]
        m["iv"]  = r["iv"]
        m["en"]  = r["en"]
        save_meta(nid, m)
        n += 1
    return n


def mercy(deck_name, mercy_days=14):
    """Spread all overdue/due topics evenly across mercy_days days.

    Returns the number of topics rescheduled.
    """
    if not mw.col:
        return 0
    today = date.today().isoformat()
    overdue = []
    for nid, _, m in _iter_topic_notes(deck_name):
        if m["st"] != "active":
            continue
        due = m.get("due")
        if not due or due > today:
            continue
        overdue.append((m["p"], nid))

    if not overdue or mercy_days <= 0:
        return 0

    overdue.sort(key=lambda x: x[0])  # best priority first
    per_day = max(1, math.ceil(len(overdue) / mercy_days))
    n = 0
    for i, (_, nid) in enumerate(overdue):
        note = mw.col.get_note(nid)
        m = get(note)
        m["due"] = scheduler.date_from_days(i // per_day)
        save_meta(nid, m)
        n += 1
    return n


def clean_orphans(deck_name):
    """Clear pnid on extracts whose parent note no longer exists.

    Returns the number of orphans cleaned.
    """
    if not mw.col:
        return 0
    all_nids = set()
    for nid, _, _ in _iter_topic_notes(deck_name):
        all_nids.add(nid)
    n = 0
    for nid, note, m in _iter_topic_notes(deck_name):
        pnid = m.get("pnid", 0)
        if pnid and pnid not in all_nids:
            m["pnid"] = 0
            save_meta(nid, m)
            n += 1
    return n
