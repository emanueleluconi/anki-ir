"""
QueueEngine — builds and manages the priority-sorted IR learning queue.
Operates on Anki notes with IR-Data fields.
"""

import math
import random
from datetime import date
from typing import List, Tuple, Optional

from aqt import mw

from . import scheduler
from .ir_fields import get_meta, set_meta, is_ir_note, IR_DATA_FIELD


def build_ir_queue(
    deck_names: List[str],
    randomization_degree: int = 0,
) -> List[int]:
    """Build a priority-sorted queue of note IDs that are due today.
    Returns list of note IDs sorted by priority (lowest number first).
    """
    today = date.today().isoformat()
    candidates: List[Tuple[float, int, int]] = []  # (priority, split_order_proxy, nid)

    for deck_name in deck_names:
        did = mw.col.decks.id_for_name(deck_name)
        if did is None:
            continue
        nids = mw.col.find_notes(f'"deck:{deck_name}"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not is_ir_note(note):
                continue
            meta = get_meta(note)
            if meta["ir_status"] != "active":
                continue
            due = meta.get("ir_next_due")
            if not due or due > today:
                continue
            candidates.append((meta["ir_priority"], nid, nid))

    # Sort by priority (lower = higher priority)
    candidates.sort(key=lambda x: x[0])

    # Apply randomization
    if randomization_degree > 0 and len(candidates) > 1:
        deg = randomization_degree / 100
        mx = max(1, int(deg * len(candidates)))
        for i in range(len(candidates)):
            if random.random() < deg:
                j = min(max(0, i + random.randint(-mx, mx)), len(candidates) - 1)
                candidates[i], candidates[j] = candidates[j], candidates[i]

    return [c[1] for c in candidates]


def auto_postpone(
    deck_names: List[str],
    protection_pct: int = 10,
) -> int:
    """Auto-postpone overdue low-priority material. Returns count of postponed."""
    today = date.today().isoformat()
    overdue: List[Tuple[float, int]] = []  # (priority, nid)

    for deck_name in deck_names:
        nids = mw.col.find_notes(f'"deck:{deck_name}"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not is_ir_note(note):
                continue
            meta = get_meta(note)
            if meta["ir_status"] != "active":
                continue
            due = meta.get("ir_next_due")
            if not due or due >= today:
                continue
            overdue.append((meta["ir_priority"], nid))

    if not overdue:
        return 0

    overdue.sort(key=lambda x: x[0])
    prot = math.ceil(len(overdue) * (protection_pct / 100))
    count = 0

    for _, nid in overdue[prot:]:
        note = mw.col.get_note(nid)
        meta = get_meta(note)
        r = scheduler.postpone_element(meta["ir_interval"], meta["ir_af"])
        meta["ir_next_due"] = r["next_due"]
        meta["ir_interval"] = r["new_interval"]
        meta["ir_af"] = r["new_af"]
        set_meta(note, meta)
        mw.col.update_note(note)
        count += 1

    return count



def mercy(deck_names: List[str], mercy_days: int = 14) -> int:
    """Mercy: spread ALL overdue material over N days."""
    today = date.today().isoformat()
    overdue: List[Tuple[float, int]] = []

    for deck_name in deck_names:
        nids = mw.col.find_notes(f'"deck:{deck_name}"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not is_ir_note(note):
                continue
            meta = get_meta(note)
            if meta["ir_status"] != "active":
                continue
            due = meta.get("ir_next_due")
            if not due or due > today:
                continue
            overdue.append((meta["ir_priority"], nid))

    if not overdue or mercy_days <= 0:
        return 0

    overdue.sort(key=lambda x: x[0])
    per_day = max(1, math.ceil(len(overdue) / mercy_days))
    count = 0

    for i, (_, nid) in enumerate(overdue):
        day_offset = i // per_day
        note = mw.col.get_note(nid)
        meta = get_meta(note)
        meta["ir_next_due"] = scheduler.date_from_days(day_offset)
        set_meta(note, meta)
        mw.col.update_note(note)
        count += 1

    return count


def get_priority_protection(deck_names: List[str], threshold: int = 10) -> int:
    """Compute priority protection: % of top-priority material that is due today."""
    today = date.today().isoformat()
    top_total = 0
    top_due = 0

    for deck_name in deck_names:
        nids = mw.col.find_notes(f'"deck:{deck_name}"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not is_ir_note(note):
                continue
            meta = get_meta(note)
            if meta["ir_status"] != "active":
                continue
            if meta["ir_priority"] <= threshold:
                top_total += 1
                due = meta.get("ir_next_due")
                if due and due <= today:
                    top_due += 1

    return round((top_due / top_total) * 100) if top_total > 0 else 100
