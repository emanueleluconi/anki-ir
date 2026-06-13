"""Queue engine for IR topics. Priority-sorted, auto-postpone, mercy, orphan cleanup."""

import math
import random
from datetime import date
from aqt import mw
from . import scheduler
from .ir_meta import get, is_topic, save_meta


def _cfg(key, default=None):
    c = mw.addonManager.getConfig("ir_anki") or {}
    return c.get(key, default)


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
    Priority ties are broken by nid for deterministic ordering.
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

    cands.sort(key=lambda x: (x[0], x[1]))

    if randomization > 0 and len(cands) > 1:
        deg = randomization / 100.0
        mx = max(1, int(deg * len(cands)))
        for i in range(len(cands)):
            if random.random() < deg:
                j = min(max(0, i + random.randint(-mx, mx)), len(cands) - 1)
                cands[i], cands[j] = cands[j], cands[i]

    return [c[1] for c in cands]


def auto_postpone(deck_name, protection_pct=10):
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
    source_tag = _cfg("source_tag", "ir::source")
    n = 0
    for _, nid in overdue[prot:]:
        note = mw.col.get_note(nid)
        m = get(note)
        if source_tag in note.tags:
            # Sources keep a fixed cadence: push to the next slot (today + iv).
            # Interval and AF are left untouched so the user-set rhythm holds.
            m["due"] = scheduler.date_from_days(m["iv"])
        else:
            r = scheduler.postpone(m["iv"], m["af"], cap=m.get("cap", 0))
            m["iv"]  = r["iv"]
            m["af"]  = r["af"]
            m["due"] = r["due"]
        save_meta(nid, m)
        n += 1
    return n


def mercy(deck_name, mercy_days=14):
    """Spread all overdue/due topics evenly across mercy_days days.

    Returns the number of topics rescheduled. Does NOT touch AF/lr/rc
    because mercy is a bulk reschedule, not a bulk review.
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

    overdue.sort(key=lambda x: x[0])
    per_day = max(1, math.ceil(len(overdue) / mercy_days))
    n = 0
    for i, (_, nid) in enumerate(overdue):
        note = mw.col.get_note(nid)
        m = get(note)
        m["due"] = scheduler.date_from_days(i // per_day)
        save_meta(nid, m)
        n += 1
    return n


def priority_protection(deck_name):
    """SuperMemo 'Priority protection' for topics.

    Definition (Help: Toolkit : Statistics : Analysis : Use : Priority protection):
    "the highest priority item (with the lowest %) that was missed in repetitions"
    on a given day — i.e. your actual processing capacity for high-priority
    material. "If your graph oscillates around priority of 3%, only the top 3% of
    your learning material is guaranteed a timely repetition."

    We compute it for topics as the priority percent of the most important
    (lowest %) topic that is still outstanding (scheduled on/before today and not
    yet reviewed today). Everything more important than this cutoff has been
    protected; everything in [cutoff, 100%] is at risk. If nothing is outstanding,
    protection is full (cutoff = 100.0, fully_protected = True).

    Items are excluded — they are scheduled by Anki/FSRS, not by IR priority.
    """
    today = date.today().isoformat()
    src_tag = _cfg("source_tag", "ir::source")
    ext_tag = _cfg("extract_tag", "ir::extract")
    outstanding = []          # priorities of due-but-unreviewed active topics
    reviewed_today = 0
    out_sources = out_extracts = 0
    for _, note, m in _iter_topic_notes(deck_name):
        if m["st"] != "active":
            continue
        due = m.get("due")
        if due and due <= today:
            outstanding.append(m["p"])
            if src_tag in note.tags:
                out_sources += 1
            elif ext_tag in note.tags:
                out_extracts += 1
        elif m.get("lr") == today:
            reviewed_today += 1
    if outstanding:
        cutoff = min(outstanding)
        fully_protected = False
    else:
        cutoff = 100.0
        fully_protected = True
    return {
        "cutoff": cutoff,
        "fully_protected": fully_protected,
        "outstanding": len(outstanding),
        "outstanding_sources": out_sources,
        "outstanding_extracts": out_extracts,
        "reviewed_today": reviewed_today,
    }


def clean_orphans(deck_name):
    """Clear pnid on extracts whose parent note no longer exists."""
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
