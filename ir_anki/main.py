"""
Incremental Reading for Anki — saturating-curve topic scheduling.

Topics = review cards (type=2, queue=2) with ivl/due controlled by the
         saturating curve scheduler (effective-N based).
Items = normal Anki cards with FSRS (completely untouched).
Studying parent deck interleaves both. Studying Items alone = pure FSRS.

All shortcuts use Shift+ or Alt+ prefixes to avoid Anki default conflicts.
"""

from datetime import date
from typing import Optional
import json

from anki.cards import Card
from anki.hooks import addHook, wrap
from anki.notes import Note
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu, QToolBar, QToolButton
from aqt.reviewer import Reviewer
from aqt.utils import showInfo, tooltip, getText

from . import scheduler
from .ir_meta import get, has_field, is_topic, init_extract, init_source, save_meta, IR_FIELD
from .queue import build_queue, auto_postpone, mercy, clean_orphans
from .priority_dialog import ask_priority
from .settings_dialog import show_settings

_ADDON_NAME = __name__.split(".")[0]


def cfg(key):
    c = mw.addonManager.getConfig(_ADDON_NAME) or {}
    defaults = {
        "topics_deck": "Main::Topics", "items_deck": "Main::Items",
        "topic_note_type": "Extracts", "cloze_note_type": "Cloze",
        "initial_interval": 1, "default_priority": 50, "randomization_degree": 5,
        "auto_postpone": False, "postpone_protection": 30, "mercy_days": 14,
        "topic_item_ratio": 5,
        # Per-tag default interval caps (0 = no cap)
        "source_cap_default": 14,   # sources: cap at 14 days by default
        "extract_cap_default": 0,   # extracts: no cap by default
        "source_tag": "ir::source", "extract_tag": "ir::extract",
        "highlight_extract": "#5b9bd5", "highlight_cloze": "#c9a227",
        "key_extract": "x", "key_cloze": "z", "key_priority": "Shift+p",
        "key_priority_up": "Alt+Up", "key_priority_down": "Alt+Down",
        "key_reschedule": "Shift+j", "key_execute_rep": "Shift+r",
        "key_postpone": "Shift+w", "key_done": "Shift+d", "key_forget": "Shift+f",
        "key_later_today": "Shift+l", "key_advance_today": "Shift+a",
        "key_edit_last": "Shift+e", "key_undo_text": "Alt+z",
        "key_undo_answer": "Ctrl+Shift+z",
        "key_prepare": "Ctrl+Shift+p",
    }
    return c.get(key, defaults.get(key))


def _default_cap_for_note(note) -> int:
    """Return the default interval cap for a note based on its tags."""
    source_tag  = cfg("source_tag")
    extract_tag = cfg("extract_tag")
    if source_tag in note.tags:
        return int(cfg("source_cap_default") or 0)
    if extract_tag in note.tags:
        return int(cfg("extract_cap_default") or 0)
    return 0


_last_created_nid: Optional[int] = None
_ir_toolbar: Optional[QToolBar] = None
_text_history: dict = {}  # nid → list of previous Text field values (for undo)
_created_history: dict = {}  # nid → list of created note IDs (for undo — delete on undo)
_priority_history: dict = {}  # nid → list of (p, af) tuples for undo restoration

# Undo-answer stack: each entry is a snapshot of the state before answering a topic
# dict with keys: nid, cid, meta (IR-Data dict), card_ivl, card_due, card_type, card_queue,
#                 queue_position (index in _interleave_topic_queue where cid was),
#                 items_since (value of _interleave_items_since before answer)
_answer_history: list = []

# SM19 interleaving state
_interleave_topic_queue: list = []   # priority-sorted topic card IDs for this session
_interleave_items_since: int = 0     # items shown since last topic
_interleave_active: bool = False     # whether interleaving is active this session
_interleave_swapping: bool = False   # guard against recursive _showQuestion calls
_interleave_spacing: int = 5         # default spacing, overridden by cfg("topic_item_ratio")
_interleave_shown_topics: set = set()  # topic card IDs already shown this session
_postponed_today: bool = False       # track if auto-postpone already ran today
_prepare_done_for_session: bool = False  # prevent re-running _prepare_topics in same session


def _is_topic_card(card: Card) -> bool:
    try: return is_topic(card.note())
    except: return False


def _is_topic_card_fresh(card: Card) -> bool:
    """Like _is_topic_card but always fetches from DB.
    Use this in command handlers that modify the note, where the
    card.note() cache may be stale after a prior extract/cloze."""
    try: return is_topic(mw.col.get_note(card.nid))
    except: return False


def _update_extract_priorities_proportionally(source_note, old_p: float, new_p: float):
    """When a source's priority changes, scale all its extracts proportionally.

    Strategy:
    1. Preserve each extract's relative offset from its parent source.
    2. After scaling by ratio = new_p / old_p, re-apply the invariant
       "extract priority must be at least 5 points lower (better) than parent".
    3. Also update AF for each extract to match the new priority.
    """
    if not mw.col or abs(old_p - new_p) < 0.01 or old_p < 0.01:
        return
    ratio = new_p / old_p
    source_nid = source_note.id
    source_tag = cfg("source_tag")
    if source_tag not in source_note.tags:
        return
    deck = cfg("topics_deck")
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen = set()
    updated = 0
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen:
            continue
        seen.add(card.nid)
        note = mw.col.get_note(card.nid)
        if not is_topic(note):
            continue
        m = get(note)
        if m.get("pnid", 0) != source_nid:
            continue
        # Scale the extract's priority by the same ratio as the parent,
        # then enforce the "extract ≤ parent - 5" invariant.
        scaled = m["p"] * ratio
        capped = min(scaled, new_p - 5.0)
        new_extract_p = scheduler.clamp_priority(capped)
        m["p"]  = new_extract_p
        m["af"] = scheduler.af_from_priority(new_extract_p)
        save_meta(card.nid, m)
        updated += 1
    if updated:
        tooltip(f"Updated priority on {updated} extract(s) proportionally.")


def _col_day() -> int:
    return mw.col.sched.today if mw.col else 0


def _set_review(card: Card, ivl: int, due_days: int):
    """Set card as review card due in due_days from today."""
    card.type = 2; card.queue = 2; card.ivl = max(1, ivl)
    card.due = _col_day() + due_days; card.left = 0
    mw.col.update_card(card)


def _shelve(card: Card):
    """Remove card from today's queue (bury)."""
    card.queue = -2; mw.col.update_card(card)



def _ask_new_source_priority(sources):
    """Per-source priority dialog.
    - Each source has its own slider + number input + today checkbox
    - Preset buttons apply to the FOCUSED source only
    - Enter = save current input, move focus to next source
    - Ctrl+Enter = apply all and close
    - Esc / Cancel = abort, use defaults for all
    """
    from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                         QSlider, QPushButton, QCheckBox, QScrollArea, QWidget,
                         QGridLayout, Qt, QShortcut, QKeySequence)

    default_p = int(cfg("default_priority"))
    items = []
    for card, note in sources:
        title = note.fields[0][:80] if note.fields else "?"
        items.append({"card": card, "note": note, "title": title, "p": default_p, "today": False})

    dlg = QDialog(mw)
    dlg.setWindowTitle(f"New Sources ({len(items)})")
    dlg.setMinimumWidth(560)
    dlg.setMinimumHeight(min(500, 160 + len(items) * 45))
    main_layout = QVBoxLayout()
    main_layout.addWidget(QLabel(f"{len(items)} new source(s). Set priority per source:"))

    # Scrollable grid
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    container = QWidget(); grid = QGridLayout()
    grid.setColumnStretch(0, 3); grid.setColumnStretch(1, 2)
    grid.setColumnStretch(2, 0); grid.setColumnStretch(3, 0)
    grid.addWidget(QLabel("Source"), 0, 0)
    grid.addWidget(QLabel("Priority"), 0, 1)
    grid.addWidget(QLabel(""), 0, 2)
    grid.addWidget(QLabel("Today"), 0, 3)

    sliders = []; inputs = []; today_cbs = []
    focused_idx = [0]  # track which input is focused

    for i, item in enumerate(items):
        row = i + 1
        lbl = QLabel(item["title"]); lbl.setWordWrap(True)
        grid.addWidget(lbl, row, 0)

        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(0, 100); sl.setValue(default_p)
        grid.addWidget(sl, row, 1); sliders.append(sl)

        inp = QLineEdit(str(default_p)); inp.setFixedWidth(55)
        grid.addWidget(inp, row, 2); inputs.append(inp)

        cb = QCheckBox(); grid.addWidget(cb, row, 3); today_cbs.append(cb)

        # Sync slider → input (use default args to capture correctly)
        def _on_sl(val, _inp=inp): _inp.setText(str(val))
        sl.valueChanged.connect(_on_sl)

        # Sync input → slider
        def _on_inp(_t=None, _sl=sl, _inp=inp):
            try:
                v = int(float(_inp.text()))
                _sl.blockSignals(True); _sl.setValue(max(0, min(100, v))); _sl.blockSignals(False)
            except: pass
        inp.textChanged.connect(_on_inp)

        # Track focus — use a wrapper function, not lambda with tuple return
        def _make_focus_handler(_i, _inp):
            _original = _inp.__class__.focusInEvent
            def _handler(event):
                focused_idx[0] = _i
                _original(_inp, event)
            return _handler
        inp.focusInEvent = _make_focus_handler(i, inp)

        # Enter = move to next input
        def _on_enter(_i=i):
            if _i + 1 < len(inputs):
                inputs[_i + 1].setFocus()
                inputs[_i + 1].selectAll()
        inp.returnPressed.connect(_on_enter)

    container.setLayout(grid); scroll.setWidget(container)
    main_layout.addWidget(scroll)

    # Preset buttons — apply to the FOCUSED source
    preset_row = QHBoxLayout()
    preset_row.addWidget(QLabel("Quick set:"))
    for val in [10, 25, 50, 75, 90]:
        btn = QPushButton(f"{val}%")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # don't steal focus from inputs
        def _on_preset(checked, v=val):
            idx = focused_idx[0]
            sliders[idx].setValue(v)
            inputs[idx].setText(str(v))
        btn.clicked.connect(_on_preset)
        preset_row.addWidget(btn)
    main_layout.addLayout(preset_row)

    # Buttons — none should be default (prevents Enter from triggering them)
    btn_row = QHBoxLayout()
    apply_btn = QPushButton("Apply All (Ctrl+Enter)")
    apply_btn.setAutoDefault(False); apply_btn.setDefault(False)
    default_btn = QPushButton("Use Defaults")
    default_btn.setAutoDefault(False); default_btn.setDefault(False)
    cancel_btn = QPushButton("Cancel (Esc)")
    cancel_btn.setAutoDefault(False); cancel_btn.setDefault(False)
    btn_row.addStretch()
    btn_row.addWidget(apply_btn); btn_row.addWidget(default_btn); btn_row.addWidget(cancel_btn)
    main_layout.addLayout(btn_row)

    result = [None]  # "apply" | "defaults" | None (cancelled)

    def do_apply():
        for i in range(len(items)):
            try: items[i]["p"] = max(0, min(100, float(inputs[i].text())))
            except: items[i]["p"] = default_p
            items[i]["today"] = today_cbs[i].isChecked()
        result[0] = "apply"; dlg.accept()

    def do_defaults():
        result[0] = "defaults"; dlg.accept()

    def do_cancel():
        result[0] = None; dlg.reject()

    apply_btn.clicked.connect(do_apply)
    default_btn.clicked.connect(do_defaults)
    cancel_btn.clicked.connect(do_cancel)

    # Ctrl+Enter = apply
    sc = QShortcut(QKeySequence("Ctrl+Return"), dlg); sc.activated.connect(do_apply)

    dlg.setLayout(main_layout)
    if inputs: inputs[0].setFocus(); inputs[0].selectAll()
    dlg.exec()

    # Process results
    if result[0] is None:
        # Cancelled — don't init these sources at all, they stay uninitialised
        return

    for i, item in enumerate(items):
        card, note = item["card"], item["note"]
        if result[0] == "apply":
            p = scheduler.clamp_priority(item["p"])
        else:
            p = scheduler.clamp_priority(default_p)
        # NOW init the source with the chosen priority
        init_source(note, p, cap=_default_cap_for_note(note))
        mw.col.update_note(note)
        due_today = result[0] == "apply" and item["today"]
        if due_today:
            m = get(note)
            m["due"] = scheduler.today_str(); m["iv"] = 1
            save_meta(note.id, m)
        _set_review(card, 1, 0 if due_today else 1)


# ============================================================
# Prepare Topics — the core sync between IR-Data and Anki cards
# ============================================================

def _prepare_topics():
    """
    Comprehensive topic preparation before review (SM19-faithful):
    1. Auto-init IR-Data on any topic notes that don't have it yet
    2. Auto-postpone overdue low-priority topics (previous days only, per SM19)
    3. Clean orphan parent references
    4. Sync every topic card's Anki due date with IR-Data scheduling
    5. ALL due topics are scheduled for today (no cap) — SM19 never caps topics.
       The sorting criteria (topic_proportion) controls interleaving order,
       not how many topics appear. If you don't finish, low-priority topics
       stay for tomorrow — exactly like SM19.
    """
    if not mw.col: return

    deck = cfg("topics_deck")
    did = mw.col.decks.id_for_name(deck)
    if did is None:
        tooltip(f"Deck '{deck}' not found."); return

    # Step 1: Auto-init any uninitialised topic notes
    # Sources: init at default priority (user can adjust later)
    # Extracts: auto-inherit priority from parent source (matched by Reference field)
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen = set()
    new_sources = []  # (card, note) pairs for new sources
    init_count = 0
    source_tag = cfg("source_tag")
    extract_tag = cfg("extract_tag")

    # First pass: build a map of Reference → source priority for parent matching
    ref_to_priority = {}
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if is_topic(note):
            m = get(note)
            fnames = [f["name"] for f in note.note_type()["flds"]]
            if "Reference" in fnames:
                ref = note["Reference"].strip()
                if ref and m["st"] == "active":
                    # Keep the lowest (best) priority for each reference
                    if ref not in ref_to_priority or m["p"] < ref_to_priority[ref]:
                        ref_to_priority[ref] = m["p"]

    # Second pass: init uninitialised notes
    seen.clear()
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = mw.col.get_note(card.nid)
        if not has_field(note) or is_topic(note): continue

        is_extract = extract_tag in note.tags
        is_source_note = source_tag in note.tags

        if is_extract:
            # Extract from Zotero: inherit priority from parent source (- 5 points)
            fnames = [f["name"] for f in note.note_type()["flds"]]
            parent_p = cfg("default_priority")
            if "Reference" in fnames:
                ref = note["Reference"].strip()
                if ref and ref in ref_to_priority:
                    parent_p = ref_to_priority[ref]
            init_extract(note, 0, parent_p, cap=_default_cap_for_note(note))
            mw.col.update_note(note)
            _set_review(card, 1, 1)
            init_count += 1
        else:
            # Source (or untagged): collect for batch priority dialog — DON'T init yet
            new_sources.append((card, note))

    # If there are new sources, show a dialog to set priority and schedule
    if new_sources:
        _ask_new_source_priority(new_sources)
        # Count how many were actually initialised (dialog may have been cancelled)
        # Also build a ref → priority map for sources that were just initialised,
        # so we can fix up any extracts that were initialised before the source
        # priority was known (they fell back to default_priority).
        new_source_ref_to_priority = {}
        for card, note in new_sources:
            fresh = mw.col.get_note(note.id)
            if is_topic(fresh):
                init_count += 1
                m = get(fresh)
                fnames = [f["name"] for f in fresh.note_type()["flds"]]
                if "Reference" in fnames:
                    ref = fresh["Reference"].strip()
                    if ref:
                        new_source_ref_to_priority[ref] = m["p"]

        # Fix up extracts that were just initialised with the wrong priority
        # because their source wasn't initialised yet when the second pass ran.
        if new_source_ref_to_priority:
            seen_fix = set()
            for cid in mw.col.find_cards(f'"deck:{deck}"'):
                card = mw.col.get_card(cid)
                if card.nid in seen_fix: continue
                seen_fix.add(card.nid)
                note = mw.col.get_note(card.nid)
                if not is_topic(note): continue
                if extract_tag not in note.tags: continue
                m = get(note)
                fnames = [f["name"] for f in note.note_type()["flds"]]
                if "Reference" not in fnames: continue
                ref = note["Reference"].strip()
                if ref not in new_source_ref_to_priority: continue
                parent_p = new_source_ref_to_priority[ref]
                correct_extract_p = scheduler.clamp_priority(parent_p - 5.0)
                if abs(m["p"] - correct_extract_p) < 0.01: continue
                fm = get(mw.col.get_note(card.nid))
                fm["p"]  = correct_extract_p
                fm["af"] = scheduler.af_from_priority(correct_extract_p)
                save_meta(card.nid, fm)

    # Step 1b: Link orphan extracts to parent sources and deprioritize parents
    # Handles Zotero-imported extracts (pnid=0). Match by Reference field.
    ref_to_source_nid = {}
    cids2 = mw.col.find_cards(f'"deck:{deck}"')
    seen2 = set()
    for cid in cids2:
        card = mw.col.get_card(cid)
        if card.nid in seen2: continue
        seen2.add(card.nid)
        note = mw.col.get_note(card.nid)
        if not is_topic(note): continue
        if source_tag in note.tags:
            m = get(note)
            if m["st"] == "active":
                fnames = [f["name"] for f in note.note_type()["flds"]]
                if "Reference" in fnames:
                    ref = note["Reference"].strip()
                    if ref: ref_to_source_nid[ref] = note.id

    new_extract_counts = {}  # source_nid → count of newly linked extracts
    seen2.clear()
    for cid in cids2:
        card = mw.col.get_card(cid)
        if card.nid in seen2: continue
        seen2.add(card.nid)
        note = mw.col.get_note(card.nid)
        if not is_topic(note): continue
        m = get(note)
        if extract_tag not in note.tags: continue
        if m.get("pnid", 0) != 0: continue  # already linked
        fnames = [f["name"] for f in note.note_type()["flds"]]
        if "Reference" not in fnames: continue
        ref = note["Reference"].strip()
        if not ref or ref not in ref_to_source_nid: continue
        parent_nid = ref_to_source_nid[ref]
        m["pnid"] = parent_nid
        save_meta(note.id, m)
        new_extract_counts[parent_nid] = new_extract_counts.get(parent_nid, 0) + 1

    # Log how many extracts were newly linked (no parent deprioritization —
    # priority is a stable user signal; the curve handles frequency naturally)
    if new_extract_counts:
        total_linked = sum(new_extract_counts.values())
        tooltip(f"IR: linked {total_linked} extract(s) to parent source(s).")

    # Step 2: Auto-postpone (only once per day to avoid re-postponing on resume)
    postpone_count = 0
    global _postponed_today
    if cfg("auto_postpone") and not _postponed_today:
        postpone_count = auto_postpone(deck, cfg("postpone_protection"))
        _postponed_today = True

    # Step 3: Clean orphans
    orphan_count = clean_orphans(deck)

    # Step 4: Build priority queue and sync card due dates
    # SM19: ALL due topics are scheduled for today. No cap.
    # The priority queue determines the ORDER, not a limit.
    queue = build_queue(deck, cfg("randomization_degree"))
    due_set = set(queue)
    topics_due = len(queue)

    # Step 5: Determine interleaving BEFORE setting topic due dates
    # We need to know if interleaving is active to decide due=today vs due=tomorrow
    global _interleave_topic_queue, _interleave_items_since, _interleave_active, _interleave_swapping
    _interleave_swapping = False

    # Count items
    items_deck = cfg("items_deck")
    try:
        items_did_val = mw.col.decks.id_for_name(items_deck)
        tree = mw.col.sched.deck_due_tree()
        def _find_deck_count(nodes, target_did):
            for n in nodes:
                if n.deck_id == target_did:
                    return n.new_count + n.learn_count + n.review_count
                r = _find_deck_count(n.children, target_did)
                if r is not None: return r
            return None
        items_due_count = _find_deck_count(tree.children, items_did_val) or 0
    except:
        items_due_count = 0

    # Detect if studying parent deck
    current_did = mw.col.decks.selected()
    topics_did = mw.col.decks.id_for_name(cfg("topics_deck"))
    items_did = mw.col.decks.id_for_name(cfg("items_deck"))
    studying_parent = current_did != topics_did and current_did != items_did
    will_interleave = studying_parent and topics_due > 0 and items_due_count > 0

    # Step 6: Sync all topic cards — set due based on interleaving decision
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen.clear()
    topic_cid_map = {}
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if not is_topic(note): continue
        m = get(note)

        if m["st"] in ("done", "dismissed", "forgotten"):
            card.type = 2; card.queue = -2; card.ivl = 9999
            card.due = _col_day() + 9999; mw.col.update_card(card)
            continue

        if card.nid in due_set:
            card.type = 2; card.queue = 2; card.ivl = max(1, m["iv"])
            if will_interleave:
                # Hide from Anki's queue — only our swap mechanism serves them
                card.due = _col_day() + 1; card.left = 0
            else:
                # No interleaving — due today for normal review
                card.due = _col_day(); card.left = 0
            mw.col.update_card(card)
            topic_cid_map[card.nid] = card.id
        else:
            if m["due"] and m["st"] == "active":
                try:
                    delta = max(1, (date.fromisoformat(m["due"]) - date.today()).days)
                except:
                    delta = max(1, m["iv"])
                _set_review(card, max(1, m["iv"]), delta)
            else:
                _set_review(card, max(1, m["iv"]), 30)

    # Build interleave queue
    _interleave_topic_queue = [topic_cid_map[nid] for nid in queue if nid in topic_cid_map]
    _interleave_active = will_interleave

    # Start with items_since = spacing so the first card triggers a topic
    # if Anki gives us an item. This ensures SM19 behavior: high-priority
    # topic first, then items, then next topic, etc.
    configured_ratio = int(cfg("topic_item_ratio") or 5)

    # Compute spacing for the session: items per topic
    # Always use configured ratio. When items run out, remaining topics
    # are served back-to-back (the swap logic handles this naturally
    # because _interleave_topic_queue still has entries).
    global _interleave_spacing
    _interleave_spacing = configured_ratio

    # Start with items_since = spacing so the first card triggers a topic
    _interleave_items_since = _interleave_spacing

    parts = []
    if init_count: parts.append(f"{init_count} new topics initialized")
    if len(new_sources) and not init_count: parts.append(f"{len(new_sources)} sources skipped (cancelled)")
    if postpone_count: parts.append(f"{postpone_count} postponed")
    if orphan_count: parts.append(f"{orphan_count} orphans cleaned")
    if items_due_count > 0 and topics_due > 0:
        parts.append(f"{topics_due} topics + {items_due_count} items ({_interleave_spacing} items/topic)")
    else:
        parts.append(f"{topics_due} topics due")
    tooltip(f"IR: {', '.join(parts)}")

    global _prepare_done_for_session
    _prepare_done_for_session = True


# ============================================================
# Answer hook: SM19 for topics, FSRS for items
# ============================================================

def _custom_answer_card(self, ease, _old):
    card = self.card
    if not _is_topic_card_fresh(card):
        _old(self, ease); return

    note = mw.col.get_note(card.nid); m = get(note)

    # Save a snapshot for undo-answer BEFORE modifying anything
    _answer_history.append({
        "nid": card.nid,
        "cid": card.id,
        "meta": dict(m),
        "card_ivl": card.ivl,
        "card_due": card.due,
        "card_type": card.type,
        "card_queue": card.queue,
        "items_since": _interleave_items_since,
    })
    if len(_answer_history) > 50:
        _answer_history.pop(0)

    today_iso = date.today().isoformat()
    is_due = not m["due"] or m["due"] <= today_iso

    if is_due:
        r = scheduler.execute_repetition(m["iv"], m["af"], m["rc"],
                                         cap=m.get("cap", 0))
        m["iv"]  = r["iv"]
        m["af"]  = r["af"]
        m["rc"]  = r["rc"]
        m["lr"]  = r["lr"]
        m["due"] = r["due"]
    else:
        r = scheduler.mid_interval_rep(m["af"], m["rc"])
        m["rc"] = r["rc"]
        m["af"] = r["af"]

    save_meta(card.nid, m)

    try:
        delta = max(1, (date.fromisoformat(m["due"]) - date.today()).days) if m["due"] else m["iv"]
    except:
        delta = m["iv"]
    _set_review(card, m["iv"], delta)
    self.nextCard()


def _custom_answer_buttons(self, _old):
    if self.card and _is_topic_card(self.card):
        m = get(self.card.note())
        next_iv = scheduler.next_interval(m["iv"], m["af"], cap=m.get("cap", 0))
        return ((1, f"Next ({next_iv}d)"),)
    return _old(self)


def _custom_button_time(self, i, v3_labels, _old):
    try:
        if _is_topic_card(mw.reviewer.card): return "<div class=spacer></div>"
    except: pass
    return _old(self, i, v3_labels)



# ============================================================
# Toolbar
# ============================================================

def _setup_toolbar():
    global _ir_toolbar
    if _ir_toolbar: return
    _ir_toolbar = QToolBar("IR", mw)
    _ir_toolbar.setMovable(False); _ir_toolbar.setVisible(False)
    for label, fn in [
        (f"Extract [{cfg('key_extract')}]", _cmd_extract),
        (f"Cloze [{cfg('key_cloze')}]", _cmd_cloze),
        (f"Priority [{cfg('key_priority')}]", _cmd_priority),
        (f"P+ [{cfg('key_priority_up')}]", lambda: _cmd_quick_priority(-5)),
        (f"P- [{cfg('key_priority_down')}]", lambda: _cmd_quick_priority(5)),
        (f"Resched [{cfg('key_reschedule')}]", _cmd_reschedule),
        (f"ExecRep [{cfg('key_execute_rep')}]", _cmd_execute_rep),
        (f"Postpone [{cfg('key_postpone')}]", _cmd_postpone),
        (f"Later [{cfg('key_later_today')}]", _cmd_later_today),
        (f"Advance [{cfg('key_advance_today')}]", _cmd_advance_today),
        (f"Done [{cfg('key_done')}]", _cmd_done),
        (f"Forget [{cfg('key_forget')}]", _cmd_forget),
        (f"Undo [{cfg('key_undo_text')}]", _cmd_undo_text),
        (f"UndoAns [{cfg('key_undo_answer')}]", _cmd_undo_answer),
        (f"EditLast [{cfg('key_edit_last')}]", _cmd_edit_last),
    ]:
        btn = QToolButton(); btn.setText(label); btn.clicked.connect(fn)
        _ir_toolbar.addWidget(btn)
    mw.addToolBar(_ir_toolbar)


def _on_show_question(card: Card):
    """SM19 interleaving: enforce topic/item alternation pattern."""
    global _interleave_items_since, _interleave_active, _interleave_swapping

    if _ir_toolbar: _ir_toolbar.setVisible(_is_topic_card(card))

    # Guard against recursive calls from our own _showQuestion swap
    if _interleave_swapping:
        if _is_topic_card(card):
            m = get(card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
        return

    # If interleaving hasn't been set up yet (first card shown before
    # _prepare_topics finished), run it now — but only once per session
    if not _prepare_done_for_session and mw.state == "review":
        _prepare_topics()

    if not _interleave_active:
        # No interleaving, but still enforce topic priority order if queue exists
        if _is_topic_card(card) and _interleave_topic_queue:
            # Find the next unshown topic from the queue
            next_cid = None
            while _interleave_topic_queue:
                candidate = _interleave_topic_queue[0]
                if candidate not in _interleave_shown_topics:
                    next_cid = candidate
                    break
                _interleave_topic_queue.pop(0)  # discard already-shown

            if next_cid is None:
                # Queue exhausted — let Anki show whatever it wants
                if card.id not in _interleave_shown_topics:
                    _interleave_shown_topics.add(card.id)
                    m = get(card.note())
                    tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
                return

            if card.id in _interleave_shown_topics or card.id != next_cid:
                # Wrong topic or duplicate — swap with correct one
                _interleave_topic_queue.pop(0)
                _interleave_shown_topics.add(next_cid)
                try:
                    topic_card = mw.col.get_card(next_cid)
                    mw.reviewer.card = topic_card
                    mw.reviewer.card.start_timer()
                    if _ir_toolbar: _ir_toolbar.setVisible(True)
                    m = get(topic_card.note())
                    tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
                    _interleave_swapping = True
                    mw.reviewer._showQuestion()
                    _interleave_swapping = False
                except Exception:
                    _interleave_swapping = False
                return
            # Correct topic
            _interleave_topic_queue.pop(0)
            _interleave_shown_topics.add(card.id)
            m = get(card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
            return
        if _is_topic_card(card):
            m = get(card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
        return

    if _is_topic_card(card):
        # Find the next unshown topic from the queue (skip duplicates safely)
        next_cid = None
        while _interleave_topic_queue:
            candidate = _interleave_topic_queue[0]
            if candidate not in _interleave_shown_topics:
                next_cid = candidate
                break
            _interleave_topic_queue.pop(0)  # discard already-shown

        if card.id in _interleave_shown_topics:
            # Already shown — swap to next unshown topic or let Anki continue
            if next_cid is not None:
                _interleave_topic_queue.pop(0)
                _interleave_items_since = 0
                _interleave_shown_topics.add(next_cid)
                try:
                    topic_card = mw.col.get_card(next_cid)
                    mw.reviewer.card = topic_card
                    mw.reviewer.card.start_timer()
                    if _ir_toolbar: _ir_toolbar.setVisible(True)
                    m = get(topic_card.note())
                    tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
                    _interleave_swapping = True
                    mw.reviewer._showQuestion()
                    _interleave_swapping = False
                except Exception:
                    _interleave_swapping = False
            # else: no more topics in queue, just let Anki show whatever it has
            return

        # Anki gave us a topic naturally. Check if it's the correct one
        if next_cid is not None and card.id != next_cid:
            _interleave_topic_queue.pop(0)
            _interleave_items_since = 0
            _interleave_shown_topics.add(next_cid)
            try:
                topic_card = mw.col.get_card(next_cid)
                mw.reviewer.card = topic_card
                mw.reviewer.card.start_timer()
                if _ir_toolbar: _ir_toolbar.setVisible(True)
                m = get(topic_card.note())
                tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
                _interleave_swapping = True
                mw.reviewer._showQuestion()
                _interleave_swapping = False
            except Exception:
                _interleave_swapping = False
            return
        # Correct topic or queue empty — accept it
        _interleave_items_since = 0
        _interleave_shown_topics.add(card.id)
        if _interleave_topic_queue and _interleave_topic_queue[0] == card.id:
            _interleave_topic_queue.pop(0)
        m = get(card.note())
        tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
        return

    # Anki gave us an item
    _interleave_items_since += 1

    # Check if it's time for a topic
    if not _interleave_topic_queue:
        return  # no more topics to interleave

    if _interleave_items_since >= _interleave_spacing:
        # Time for a topic! Find the next unshown topic from the queue.
        next_cid = None
        while _interleave_topic_queue:
            candidate = _interleave_topic_queue[0]
            if candidate not in _interleave_shown_topics:
                next_cid = candidate
                _interleave_topic_queue.pop(0)
                break
            _interleave_topic_queue.pop(0)  # discard already-shown

        if next_cid is None:
            return  # all topics already shown

        _interleave_items_since = 0
        _interleave_shown_topics.add(next_cid)
        try:
            topic_card = mw.col.get_card(next_cid)
            mw.reviewer.card = topic_card
            mw.reviewer.card.start_timer()
            if _ir_toolbar: _ir_toolbar.setVisible(True)
            m = get(topic_card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
            _interleave_swapping = True
            mw.reviewer._showQuestion()
            _interleave_swapping = False
        except Exception:
            _interleave_swapping = False


def _on_review_end():
    global _interleave_active, _interleave_topic_queue, _interleave_swapping, _interleave_spacing, _prepare_done_for_session, _interleave_shown_topics
    if _ir_toolbar: _ir_toolbar.setVisible(False)
    # Restore any remaining topics to due=today so they're available
    # if user studies Topics alone or comes back later
    if _interleave_topic_queue and mw.col:
        today = _col_day()
        for tcid in _interleave_topic_queue:
            try:
                tc = mw.col.get_card(tcid)
                tc.due = today
                mw.col.update_card(tc)
            except: pass
    _interleave_active = False
    _interleave_topic_queue = []
    _interleave_swapping = False
    _interleave_spacing = cfg("topic_item_ratio") or 5
    _interleave_shown_topics = set()
    _prepare_done_for_session = False
    _answer_history.clear()
    # Tell Anki to recalculate deck counts
    if mw.col:
        try: mw.col.reset()
        except: pass


# ============================================================
# Shortcuts
# ============================================================

def _set_shortcuts(shortcuts):
    shortcuts.extend([
        (cfg("key_extract"), _cmd_extract), (cfg("key_cloze"), _cmd_cloze),
        (cfg("key_priority"), _cmd_priority),
        (cfg("key_priority_up"), lambda: _cmd_quick_priority(-5)),
        (cfg("key_priority_down"), lambda: _cmd_quick_priority(5)),
        (cfg("key_reschedule"), _cmd_reschedule),
        (cfg("key_execute_rep"), _cmd_execute_rep),
        (cfg("key_postpone"), _cmd_postpone),
        (cfg("key_later_today"), _cmd_later_today),
        (cfg("key_advance_today"), _cmd_advance_today),
        (cfg("key_done"), _cmd_done), (cfg("key_forget"), _cmd_forget),
        (cfg("key_edit_last"), _cmd_edit_last),
        (cfg("key_undo_text"), _cmd_undo_text),
        (cfg("key_undo_answer"), _cmd_undo_answer),
    ])


# ============================================================
# Commands
# ============================================================

def _cmd_extract():
    if mw.state != "review": return
    # Get HTML of selection (preserves math, formatting, etc.)
    # After cloning, replace rendered MathJax containers with original LaTeX
    # so the child note stores renderable source, not opaque MathJax DOM.
    js = r"""(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed) return '';
        var range=sel.getRangeAt(0);
        var div=document.createElement('div');
        div.appendChild(range.cloneContents());

        // Build a map from rendered <mjx-container> → original LaTeX
        // MathJax 3 stores items in MathJax.startup.document.math
        var texMap=new Map();
        try {
            var mathDoc=MathJax.startup.document;
            if(mathDoc && mathDoc.math){
                for(var item of mathDoc.math){
                    if(item.typesetRoot){
                        var delim=item.display?['\\[','\\]']:['\\(','\\)'];
                        texMap.set(item.typesetRoot, delim[0]+item.math+delim[1]);
                    }
                }
            }
        } catch(e){}

        // Replace <mjx-container> in the clone with original LaTeX.
        // The clone's mjx-containers don't have MathJax refs, so we match
        // them to originals by position within the selection range.
        var origContainers=[];
        var walker=document.createTreeWalker(
            range.commonAncestorContainer.nodeType===1?range.commonAncestorContainer:range.commonAncestorContainer.parentNode,
            NodeFilter.SHOW_ELEMENT,
            {acceptNode:function(n){return n.tagName==='MJX-CONTAINER'?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_SKIP;}}
        );
        var n;
        while(n=walker.nextNode()){
            if(range.intersectsNode(n)) origContainers.push(n);
        }

        var cloneContainers=div.querySelectorAll('mjx-container');
        for(var i=0;i<cloneContainers.length;i++){
            var tex=null;
            if(i<origContainers.length) tex=texMap.get(origContainers[i]);
            if(!tex){
                // Fallback: extract from assistive MathML
                var mml=cloneContainers[i].querySelector('mjx-assistive-mml math');
                if(mml){
                    var isBlock=mml.getAttribute('display')==='block'||
                                cloneContainers[i].getAttribute('display')==='true';
                    // Can't perfectly recover LaTeX from MathML, so use a
                    // simplified text extraction as last resort
                    tex=(isBlock?'\\[':'\\(')+mml.textContent.trim()+(isBlock?'\\]':'\\)');
                }
            }
            if(tex){
                cloneContainers[i].replaceWith(document.createTextNode(tex));
            }
        }

        return div.innerHTML;
    })();"""
    mw.web.evalWithCallback(js, _do_extract)

def _do_extract(html):
    global _last_created_nid
    if not html or not html.strip(): tooltip("Select text first."); return
    html = html.strip()
    card = mw.reviewer.card
    if not card: return
    # Fetch parent via get_note for a guaranteed fresh object
    parent = mw.col.get_note(card.nid)
    pm = get(parent) if is_topic(parent) else None

    # Python-side fallback: if JS failed to convert MathJax containers back
    # to LaTeX, recover them from the parent note's Text field.
    parent_fnames_check = [f["name"] for f in parent.note_type()["flds"]]
    parent_text_for_math = parent["Text"] if "Text" in parent_fnames_check else ""
    html = _strip_mathjax_html(html, parent_text_for_math)

    model = mw.col.models.by_name(cfg("topic_note_type"))
    if not model: showInfo(f"Note type '{cfg('topic_note_type')}' not found."); return
    nn = Note(mw.col, model)
    fnames = [f["name"] for f in model["flds"]]
    if "Text" in fnames: nn["Text"] = html
    elif fnames: nn.fields[0] = html
    for f in ("Reference", "Back Extra"):
        if f in fnames:
            try: nn[f] = parent[f]
            except KeyError: pass
    nn.tags = list(parent.tags)
    st, et = cfg("source_tag"), cfg("extract_tag")
    if st in nn.tags: nn.tags.remove(st)
    if et not in nn.tags: nn.tags.append(et)

    did = mw.col.decks.id_for_name(cfg("topics_deck")) or card.did
    pp = pm["p"] if pm else cfg("default_priority")
    # Extract priority = parent priority - 5 (so extracts appear before parent)
    init_extract(nn, parent.id, pp, cap=_default_cap_for_note(nn))
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    _last_created_nid = nn.id

    new_cards = nn.cards()
    if new_cards: _set_review(new_cards[0], 1, 1)

    # Save parent priority+af for undo (no deprioritization)
    if pm:
        nid = parent.id
        if nid not in _priority_history: _priority_history[nid] = []
        _priority_history[nid].append((pm["p"], pm["af"]))

    color = cfg("highlight_extract")
    # Highlight the selection visually in the webview (cosmetic only)
    mw.web.eval(f"""(function(){{
        var s=window.getSelection();if(s.rangeCount>0){{
            var r=s.getRangeAt(0);var sp=document.createElement('span');
            sp.style.backgroundColor='{color}';sp.style.color='#fff';
            r.surroundContents(sp);s.removeAllRanges();
        }}
    }})();""")
    # Save highlight to the note's Text field.
    # Always re-fetch from DB right before writing to guarantee we have the
    # latest content (other code paths may have touched the note in between).
    nid = parent.id
    fresh = mw.col.get_note(nid)
    fnames = [f["name"] for f in fresh.note_type()["flds"]]
    if "Text" in fnames:
        old_text = fresh["Text"]
        import re as _re
        plain_html = html.strip()
        # Try exact HTML match first, then fall back to plain-text match
        search_text = plain_html
        if search_text not in old_text:
            plain_sel = _re.sub(r'<[^>]+>', '', plain_html).strip()
            if plain_sel and plain_sel in old_text:
                search_text = plain_sel
        if search_text in old_text:
            highlighted = f'<span style="background-color:{color};color:#fff">{search_text}</span>'
            new_text = old_text.replace(search_text, highlighted, 1)
            # Save for undo
            if nid not in _text_history: _text_history[nid] = []
            _text_history[nid].append(old_text)
            if nid not in _created_history: _created_history[nid] = []
            _created_history[nid].append(_last_created_nid)
            fresh["Text"] = new_text
            mw.col.update_note(fresh)
    tooltip("Extract created")


def _strip_mathjax_html(html, parent_text=""):
    """Replace leftover rendered MathJax DOM (<mjx-container>) with original LaTeX.

    The JS-side recovery handles most cases via MathJax.startup.document.math,
    but if that fails (e.g. MathJax not loaded yet, partial selection) this
    Python fallback kicks in.

    Strategy:
    1. Extract plain-text anchors around each <mjx-container> in the HTML.
    2. Find the corresponding region in parent_text (which has the original
       \\(...\\) or \\[...\\] LaTeX).
    3. Replace the <mjx-container>...</mjx-container> with the original LaTeX.
    """
    import re as _re
    if '<mjx-container' not in html:
        return html  # nothing to do

    # Pattern to match an entire <mjx-container ...>...</mjx-container>
    mjx_pat = _re.compile(r'<mjx-container[^>]*>.*?</mjx-container>', _re.DOTALL)

    if not parent_text:
        # No parent text to recover from — strip MathJax containers entirely
        # and leave a placeholder so the user knows something was there
        return mjx_pat.sub('[math]', html)

    # Build a list of LaTeX expressions in the parent text
    # Matches \(...\) and \[...\] (non-greedy)
    latex_inline = list(_re.finditer(r'\\\(.*?\\\)', parent_text, _re.DOTALL))
    latex_display = list(_re.finditer(r'\\\[.*?\\\]', parent_text, _re.DOTALL))
    all_latex = sorted(latex_inline + latex_display, key=lambda m: m.start())
    if not all_latex:
        return mjx_pat.sub('[math]', html)

    # For each <mjx-container>, try to match it to a LaTeX expression
    # by looking at the plain text before/after it in the HTML
    result = html
    used = set()
    for mjx_match in list(mjx_pat.finditer(html)):
        # Get plain text before and after this mjx-container in the HTML
        before_html = html[:mjx_match.start()]
        after_html = html[mjx_match.end():]
        before_plain = _re.sub(r'<[^>]+>', '', before_html).strip()[-40:]
        after_plain = _re.sub(r'<[^>]+>', '', after_html).strip()[:40]

        # Also check if it's display math from the container attributes
        is_display = 'display="true"' in mjx_match.group() or 'display="block"' in mjx_match.group()

        best_match = None
        best_score = -1
        for i, lm in enumerate(all_latex):
            if i in used:
                continue
            # Check type match
            is_lm_display = lm.group().startswith('\\[')
            if is_display != is_lm_display:
                continue
            score = 0
            # Check surrounding text in parent
            pt_before = parent_text[max(0, lm.start()-50):lm.start()]
            pt_before_plain = _re.sub(r'<[^>]+>', '', pt_before).strip()[-40:]
            pt_after = parent_text[lm.end():lm.end()+50]
            pt_after_plain = _re.sub(r'<[^>]+>', '', pt_after).strip()[:40]
            if before_plain and pt_before_plain:
                overlap = min(len(before_plain), len(pt_before_plain), 20)
                if before_plain[-overlap:] == pt_before_plain[-overlap:]:
                    score += overlap * 2
            if after_plain and pt_after_plain:
                overlap = min(len(after_plain), len(pt_after_plain), 20)
                if after_plain[:overlap] == pt_after_plain[:overlap]:
                    score += overlap * 2
            if score > best_score:
                best_score = score
                best_match = (i, lm)

        if best_match and best_score > 0:
            used.add(best_match[0])
            result = result.replace(mjx_match.group(), best_match[1].group(), 1)
        elif not used and len(all_latex) == 1:
            # Only one LaTeX expression and one mjx-container — safe to match
            used.add(0)
            result = result.replace(mjx_match.group(), all_latex[0].group(), 1)

    # If any mjx-containers remain, strip them as last resort
    result = mjx_pat.sub('[math]', result)
    return result


def _replace_at_context(text, needle, replacement, before, after):
    """Replace the occurrence of needle closest to the given surrounding context.
    Used for cloze creation and highlight saving when text appears multiple times."""
    import re as _re
    # Strip HTML from context for comparison against both HTML and plain text
    before_plain = _re.sub(r'<[^>]+>', '', before).strip() if before else ""
    after_plain = _re.sub(r'<[^>]+>', '', after).strip() if after else ""
    # Use longer context windows for better matching
    before_match = before_plain[-30:] if before_plain else ""
    after_match = after_plain[:30] if after_plain else ""

    # Also prepare a plain-text version of the haystack for context matching
    text_plain = _re.sub(r'<[^>]+>', '', text)

    # Find all occurrences of needle in the original text
    start = 0
    positions = []
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    if not positions:
        return None
    if len(positions) == 1:
        return text[:positions[0]] + replacement + text[positions[0] + len(needle):]

    # For each position, compute a context match score
    # We check context in BOTH the raw HTML and the plain-text version
    needle_plain = _re.sub(r'<[^>]+>', '', needle)
    best_idx = positions[0]
    best_score = -1
    for pos in positions:
        score = 0
        # Check context in raw HTML around this position
        if before_match:
            chunk_html = text[max(0, pos - 80):pos]
            chunk_plain = _re.sub(r'<[^>]+>', '', chunk_html)
            if before_match in chunk_plain:
                score += len(before_match) * 2  # strong match
            elif before_match[-10:] in chunk_plain:
                score += 5  # partial match
        if after_match:
            chunk_html = text[pos + len(needle):pos + len(needle) + 80]
            chunk_plain = _re.sub(r'<[^>]+>', '', chunk_html)
            if after_match in chunk_plain:
                score += len(after_match) * 2
            elif after_match[:10] in chunk_plain:
                score += 5

        # Also try matching in the plain-text version to find the corresponding position
        if before_match or after_match:
            # Find where this HTML position maps to in plain text
            plain_before_pos = len(_re.sub(r'<[^>]+>', '', text[:pos]))
            plain_chunk_before = text_plain[max(0, plain_before_pos - 60):plain_before_pos]
            plain_chunk_after = text_plain[plain_before_pos + len(needle_plain):plain_before_pos + len(needle_plain) + 60]
            if before_match and before_match in plain_chunk_before:
                score += len(before_match)
            if after_match and after_match in plain_chunk_after:
                score += len(after_match)

        if score > best_score:
            best_score = score
            best_idx = pos
    return text[:best_idx] + replacement + text[best_idx + len(needle):]


def _cmd_cloze():
    if mw.state != "review": return
    # Get selection text, HTML, context, AND character offset within the card content.
    # After cloning, replace rendered MathJax containers with original LaTeX
    # so the child cloze note stores renderable source, not opaque MathJax DOM.
    js = r"""(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed)return JSON.stringify({err:1});
        var range=sel.getRangeAt(0);
        var div=document.createElement('div');
        div.appendChild(range.cloneContents());

        // Build a map from rendered <mjx-container> → original LaTeX
        var texMap=new Map();
        try {
            var mathDoc=MathJax.startup.document;
            if(mathDoc && mathDoc.math){
                for(var item of mathDoc.math){
                    if(item.typesetRoot){
                        var delim=item.display?['\\[','\\]']:['\\(','\\)'];
                        texMap.set(item.typesetRoot, delim[0]+item.math+delim[1]);
                    }
                }
            }
        } catch(e){}

        // Match cloned mjx-containers to originals by position in selection
        var origContainers=[];
        var tw=document.createTreeWalker(
            range.commonAncestorContainer.nodeType===1?range.commonAncestorContainer:range.commonAncestorContainer.parentNode,
            NodeFilter.SHOW_ELEMENT,
            {acceptNode:function(n){return n.tagName==='MJX-CONTAINER'?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_SKIP;}}
        );
        var n;
        while(n=tw.nextNode()){
            if(range.intersectsNode(n)) origContainers.push(n);
        }
        var cloneContainers=div.querySelectorAll('mjx-container');
        for(var i=0;i<cloneContainers.length;i++){
            var tex=null;
            if(i<origContainers.length) tex=texMap.get(origContainers[i]);
            if(!tex){
                var mml=cloneContainers[i].querySelector('mjx-assistive-mml math');
                if(mml){
                    var isBlock=mml.getAttribute('display')==='block'||
                                cloneContainers[i].getAttribute('display')==='true';
                    tex=(isBlock?'\\[':'\\(')+mml.textContent.trim()+(isBlock?'\\]':'\\)');
                }
            }
            if(tex){
                cloneContainers[i].replaceWith(document.createTextNode(tex));
            }
        }

        var selHtml=div.innerHTML;
        var selText=sel.toString().trim();
        
        // Compute character offset of selection start within the card content
        // This is the key to finding the correct position in the source text
        var container=document.getElementById('qa')||document.body;
        var charOffset=0;
        var walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT);
        var node;
        var foundStart=false;
        while(node=walker.nextNode()){
            if(node===range.startContainer){
                charOffset+=range.startOffset;
                foundStart=true;
                break;
            }
            charOffset+=(node.textContent||'').length;
        }
        var startOffset=foundStart?charOffset:0;
        
        // Also compute end offset
        charOffset=0;
        walker=document.createTreeWalker(container,NodeFilter.SHOW_TEXT);
        var foundEnd=false;
        while(node=walker.nextNode()){
            if(node===range.endContainer){
                charOffset+=range.endOffset;
                foundEnd=true;
                break;
            }
            charOffset+=(node.textContent||'').length;
        }
        var endOffset=foundEnd?charOffset:startOffset+selText.length;
        
        // Get context
        var beforeCtx='', afterCtx='';
        try {
            var sn=range.startContainer;
            var so=range.startOffset;
            var before='';
            if(sn.nodeType===3) before=sn.textContent.substring(Math.max(0,so-60),so);
            if(before.length<30){
                var prev=sn.previousSibling||sn.parentNode?.previousSibling;
                for(var i=0;i<5&&prev&&before.length<60;i++){
                    before=(prev.textContent||'').slice(-60)+before;
                    prev=prev.previousSibling||prev.parentNode?.previousSibling;
                }
            }
            beforeCtx=before.slice(-60);
            var en=range.endContainer;
            var eo=range.endOffset;
            var after='';
            if(en.nodeType===3) after=en.textContent.substring(eo,eo+60);
            if(after.length<30){
                var next=en.nextSibling||en.parentNode?.nextSibling;
                for(var i=0;i<5&&next&&after.length<60;i++){
                    after=after+(next.textContent||'').substring(0,60);
                    next=next.nextSibling||next.parentNode?.nextSibling;
                }
            }
            afterCtx=after.substring(0,60);
        } catch(e){}
        return JSON.stringify({selHtml:selHtml,selText:selText,before:beforeCtx,after:afterCtx,startOffset:startOffset,endOffset:endOffset});
    })();"""
    mw.web.evalWithCallback(js, _do_cloze)

def _do_cloze(result):
    global _last_created_nid
    try: data = json.loads(result) if isinstance(result, str) else result
    except: tooltip("Select text first."); return
    if not data or "err" in data: tooltip("Select text first."); return

    sel_html = data.get("selHtml", "").strip()
    sel_text = data.get("selText", "").strip()
    before_ctx = data.get("before", "").strip()
    after_ctx = data.get("after", "").strip()
    start_offset = data.get("startOffset", 0)
    end_offset = data.get("endOffset", 0)
    if not sel_html and not sel_text: tooltip("Select text first."); return

    card = mw.reviewer.card
    if not card: return
    # Fetch parent once — always from DB, never from card.note() cache
    parent = mw.col.get_note(card.nid)

    parent_fnames = [f["name"] for f in parent.note_type()["flds"]]
    parent_text = ""
    if "Text" in parent_fnames:
        parent_text = parent["Text"]
    elif parent.fields:
        parent_text = parent.fields[0]

    # Python-side fallback: if JS failed to convert MathJax containers back
    # to LaTeX, recover them from the parent note's Text field.
    sel_html = _strip_mathjax_html(sel_html, parent_text)

    import re as _re
    
    # Strip HTML to get plain text for matching
    plain_parent = _re.sub(r'<[^>]+>', '', parent_text)
    
    # Determine the keyword to use for the cloze
    keyword = None
    
    if sel_html in parent_text:
        keyword = sel_html
    elif sel_text and sel_text in plain_parent:
        keyword = sel_text
    elif sel_text and sel_text in parent_text:
        keyword = sel_text
    else:
        # Selection likely contains rendered math — the sel_text has Unicode symbols
        # but the source has \(...\) LaTeX. We need to find the original source text.
        #
        # Strategy: split sel_text into plain-text fragments (non-math parts) and
        # use them as anchors to find the region in the source text.
        # Then expand to include any \(...\) or \[...\] blocks in between.
        
        # Extract plain text fragments from the selection (words that aren't math symbols)
        plain_fragments = [w for w in sel_text.split() if all(ord(c) < 128 for c in w) and len(w) >= 2]
        
        if plain_fragments:
            # Find the first fragment in the source to get approximate start
            first_frag = plain_fragments[0]
            last_frag = plain_fragments[-1] if len(plain_fragments) > 1 else first_frag
            
            # Use before_ctx to find the right occurrence of the first fragment
            start_pos = -1
            search_from = 0
            while True:
                idx = parent_text.find(first_frag, search_from)
                if idx == -1: break
                # Check if before_ctx matches
                chunk_before = parent_text[max(0, idx-60):idx]
                plain_chunk = _re.sub(r'<[^>]+>', '', chunk_before)
                if before_ctx and before_ctx[-5:] in plain_chunk:
                    start_pos = idx
                    break
                if start_pos == -1:
                    start_pos = idx  # fallback to first occurrence
                search_from = idx + 1
            
            if start_pos == -1:
                start_pos = parent_text.find(first_frag)
            
            # Find the last fragment after start_pos to get approximate end
            end_pos = parent_text.find(last_frag, start_pos)
            if end_pos >= 0:
                end_pos += len(last_frag)
            else:
                end_pos = start_pos + len(first_frag)
            
            # Now expand start_pos backwards and end_pos forwards to include
            # any \(...\) or \[...\] math blocks that are part of the selection
            
            # Expand backwards: if there's a \( before start_pos with no matching \) between
            before_text = parent_text[:start_pos]
            last_open = before_text.rfind('\\(')
            last_close = before_text.rfind('\\)')
            if last_open > last_close and last_open >= 0:
                start_pos = last_open
            
            # Expand forwards: check if there's a math block starting near end_pos
            # (the selection included rendered math that follows the plain text)
            # Skip whitespace, punctuation, AND HTML tags to find nearby math
            after_text = parent_text[end_pos:]
            skip = 0
            while skip < len(after_text):
                if after_text[skip] == '<':
                    # Skip HTML tag
                    close_tag = after_text.find('>', skip)
                    if close_tag >= 0:
                        skip = close_tag + 1
                    else:
                        break
                elif after_text[skip] in ' \t\n\r.,;:!?)&':
                    # Skip whitespace, punctuation, HTML entities start
                    if after_text[skip] == '&':
                        # Skip HTML entity like &nbsp;
                        semi = after_text.find(';', skip)
                        if semi >= 0 and semi - skip < 10:
                            skip = semi + 1
                        else:
                            skip += 1
                    else:
                        skip += 1
                else:
                    break
            nearby = after_text[skip:skip+10]
            if nearby.startswith('\\(') or nearby.startswith('\\['):
                delim = '\\)' if nearby.startswith('\\(') else '\\]'
                close_idx = after_text.find(delim, skip)
                if close_idx >= 0:
                    end_pos = end_pos + close_idx + 2
            
            # Also expand backwards: check if there's a math block ending near start_pos
            before_text_b = parent_text[:start_pos]
            rskip = len(before_text_b) - 1
            while rskip >= 0:
                if before_text_b[rskip] == '>':
                    open_tag = before_text_b.rfind('<', 0, rskip)
                    if open_tag >= 0:
                        rskip = open_tag - 1
                    else:
                        break
                elif before_text_b[rskip] in ' \t\n\r.,;:!?(':
                    rskip -= 1
                else:
                    break
            # Check if text before start ends with \) or \]
            if rskip >= 1:
                two_chars = before_text_b[rskip-1:rskip+1]
                if two_chars == '\\)' or two_chars == '\\]':
                    delim = '\\(' if two_chars == '\\)' else '\\['
                    open_idx = before_text_b.rfind(delim, 0, rskip)
                    if open_idx >= 0:
                        start_pos = open_idx
            
            # Also check for unmatched \( within the current region
            region = parent_text[start_pos:end_pos]
            open_count = region.count('\\(') - region.count('\\)')
            if open_count > 0:
                rest = parent_text[end_pos:]
                close_idx = rest.find('\\)')
                if close_idx >= 0:
                    end_pos = end_pos + close_idx + 2
            
            # Same for \[...\]
            region = parent_text[start_pos:end_pos]
            open_d = region.count('\\[') - region.count('\\]')
            if open_d > 0:
                after_text = parent_text[end_pos:]
                close_d = after_text.find('\\]')
                if close_d >= 0:
                    end_pos = end_pos + close_d + 2
            
            keyword = parent_text[start_pos:end_pos].strip()
        
        if not keyword:
            # Last resort: use the character offsets
            src_start = min(start_offset, len(parent_text))
            src_end = min(end_offset, len(parent_text))
            keyword = parent_text[src_start:src_end].strip()
    
    if not keyword:
        keyword = sel_html or sel_text
    
    cloze_marker = "{{c1::" + keyword + "}}"
    
    # Build cloze text: the FULL parent text with the keyword wrapped in cloze
    # This ensures all surrounding context is preserved in the cloze card
    if keyword in parent_text:
        cloze_text = _replace_at_context(parent_text, keyword, cloze_marker, before_ctx, after_ctx)
        if not cloze_text:
            cloze_text = parent_text.replace(keyword, cloze_marker, 1)
    else:
        # Keyword not found in parent_text — use the full parent text with cloze appended
        cloze_text = parent_text + "\n" + cloze_marker

    card = mw.reviewer.card
    if not card: return
    model = mw.col.models.by_name(cfg("cloze_note_type"))
    if not model: showInfo(f"Note type '{cfg('cloze_note_type')}' not found."); return
    nn = Note(mw.col, model)
    fnames = [f["name"] for f in model["flds"]]
    if "Text" in fnames: nn["Text"] = cloze_text
    elif fnames: nn.fields[0] = cloze_text
    for f in ("Reference", "Back Extra"):
        if f in fnames:
            try: nn[f] = parent[f]
            except KeyError: pass
    nn.tags = list(parent.tags)
    # Clozes are items, not topics — strip IR topic tags
    st, et = cfg("source_tag"), cfg("extract_tag")
    if st in nn.tags: nn.tags.remove(st)
    if et in nn.tags: nn.tags.remove(et)
    did = mw.col.decks.id_for_name(cfg("items_deck")) or card.did
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    _last_created_nid = nn.id

    # Bury new cloze so it appears tomorrow via FSRS, not today
    new_cids = [nc.id for nc in nn.cards()]
    if new_cids:
        mw.col.sched.bury_cards(new_cids)

    color = cfg("highlight_cloze")
    # Highlight the selection visually in the webview (cosmetic only)
    mw.web.eval(f"""(function(){{
        var s=window.getSelection();if(s.rangeCount>0){{
            var r=s.getRangeAt(0);var sp=document.createElement('span');
            sp.style.backgroundColor='{color}';sp.style.color='#fff';
            r.surroundContents(sp);s.removeAllRanges();
        }}
    }})();""")
    # Save highlight to the note's Text field using the resolved keyword.
    # Always re-fetch from DB right before writing to guarantee we have the
    # latest content (other code paths may have touched the note in between).
    nid = parent.id
    fresh = mw.col.get_note(nid)
    fresh_fnames = [f["name"] for f in fresh.note_type()["flds"]]
    if "Text" in fresh_fnames:
        old_text = fresh["Text"]
        import re as _re
        # Resolve the keyword to search for in old_text.
        # If keyword contains HTML (e.g. already-highlighted spans from a previous
        # cloze on the same note), strip it to get the plain searchable form.
        search_keyword = keyword
        if search_keyword not in old_text:
            plain_kw = _re.sub(r'<[^>]+>', '', keyword).strip()
            if plain_kw and plain_kw in old_text:
                search_keyword = plain_kw
        if search_keyword in old_text:
            highlighted = f'<span style="background-color:{color};color:#fff">{search_keyword}</span>'
            new_text = _replace_at_context(old_text, search_keyword, highlighted, before_ctx, after_ctx)
            if not new_text:
                new_text = old_text.replace(search_keyword, highlighted, 1)
            if nid not in _text_history: _text_history[nid] = []
            _text_history[nid].append(old_text)
            if nid not in _created_history: _created_history[nid] = []
            _created_history[nid].append(_last_created_nid)
            fresh["Text"] = new_text
            mw.col.update_note(fresh)
    tooltip("Cloze created (tomorrow)")



def _cmd_priority():
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): tooltip("Not a topic."); return
    note = mw.col.get_note(card.nid); m = get(note)
    old_p = m["p"]
    result = ask_priority(m["p"], m["af"], m["iv"])
    if result is not None:
        m["p"]  = scheduler.clamp_priority(result)
        m["af"] = scheduler.af_from_priority(m["p"])
        save_meta(card.nid, m)
        _update_extract_priorities_proportionally(note, old_p, m["p"])
        tooltip(f"Priority: {m['p']:.1f}%, AF: {m['af']:.2f}")

def _cmd_quick_priority(delta):
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): return
    note = mw.col.get_note(card.nid); m = get(note)
    old_p = m["p"]
    m["p"]  = scheduler.clamp_priority(m["p"] + delta)
    m["af"] = scheduler.af_from_priority(m["p"])
    save_meta(card.nid, m)
    _update_extract_priorities_proportionally(note, old_p, m["p"])
    tooltip(f"Priority: {m['p']:.1f}%, AF: {m['af']:.2f}")

def _cmd_reschedule():
    """SM Ctrl+J: set next review date absolutely. No AF/lr/rc change.

    Use this when you haven't actually reviewed the card but want to
    change when it appears next. The scheduling state is preserved.
    """
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): return
    note = mw.col.get_note(card.nid); m = get(note)
    val, ok = getText(f"Reschedule: days from today (current iv: {m['iv']}d)",
                      title="Reschedule (no review)", default=str(m["iv"]))
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    r = scheduler.reschedule_absolute(m["iv"], m["af"], m["rc"], days,
                                      cap=m.get("cap", 0))
    m["iv"]  = r["iv"]
    m["due"] = r["due"]
    # af, rc, lr intentionally NOT touched
    save_meta(card.nid, m)
    _set_review(card, m["iv"], days)
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Rescheduled: {r['iv']}d (no review)"); mw.reviewer.nextCard()

def _cmd_execute_rep():
    """SM Ctrl+Shift+R: review the card and set new interval.

    Updates lr, rc, and adjusts AF based on the interval you chose vs
    what AF expected. Use this when you actually reviewed the card.
    """
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): return
    note = mw.col.get_note(card.nid); m = get(note)
    val, ok = getText(f"Set new interval from today (current: {m['iv']}d, AF: {m['af']:.2f})",
                      title="Execute Repetition", default=str(m["iv"]))
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    r = scheduler.execute_rep_manual(m["iv"], m["af"], m["rc"], days,
                                     cap=m.get("cap", 0))
    m["iv"]  = r["iv"]
    m["af"]  = r["af"]
    m["rc"]  = r["rc"]
    m["lr"]  = r["lr"]
    m["due"] = r["due"]
    save_meta(card.nid, m)
    _set_review(card, m["iv"], r["iv"])
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Execute rep: {r['iv']}d, AF={m['af']:.2f}"); mw.reviewer.nextCard()

def _cmd_postpone():
    """Postpone: multiply interval by 1.5x. Nudges AF up."""
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): return
    note = mw.col.get_note(card.nid); m = get(note)
    r = scheduler.postpone(m["iv"], m["af"], cap=m.get("cap", 0))
    m["iv"]  = r["iv"]
    m["af"]  = r["af"]
    m["due"] = r["due"]
    save_meta(card.nid, m)
    _set_review(card, r["iv"], r["iv"])
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Postponed: {r['iv']}d, AF={m['af']:.2f}"); mw.reviewer.nextCard()

def _cmd_later_today():
    """SM Ctrl+Shift+J: put back in today's queue without changing interval.
    The card will reappear later in today's session."""
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card): return
    note = mw.col.get_note(card.nid); m = get(note)
    m["due"] = scheduler.today_str()
    # Interval, AF, priority all stay unchanged
    save_meta(card.nid, m)
    _set_review(card, m["iv"], 0)
    # If interleaving is active, re-hide the topic so it comes back via swap mechanism
    if _interleave_active:
        card.due = _col_day() + 1
        mw.col.update_card(card)
    tooltip("Later today (interval unchanged)")
    mw.reviewer.nextCard()

def _cmd_advance_today():
    """SM Advance: move to today + boost priority by 10%."""
    card = mw.reviewer.card
    if not card or not _is_topic_card_fresh(card):
        return
    note = mw.col.get_note(card.nid); m = get(note)
    m["due"] = scheduler.today_str()
    m["p"]   = scheduler.clamp_priority(max(0, m["p"] - 10))
    m["af"]  = scheduler.af_from_priority(m["p"])
    m["iv"]  = 1
    save_meta(card.nid, m)
    _set_review(card, 1, 0)
    if _interleave_active:
        card.due = _col_day() + 1
        mw.col.update_card(card)
    tooltip(f"Advanced to today. Priority: {m['p']:.1f}%, AF: {m['af']:.2f}")
    mw.reviewer.nextCard()

def _cmd_done():
    card = mw.reviewer.card
    if not card: return
    # Use fresh DB fetch for the topic check — card.note() cache can be stale
    # after _do_extract/_do_cloze modified the note via a different object.
    note = mw.col.get_note(card.nid)
    if not is_topic(note):
        tooltip("Not a topic."); return
    m = get(note); m["st"] = "done"
    save_meta(card.nid, m)
    # Suspend all cards of this note (Anki suspend = permanently out of review)
    cids = [c.id for c in note.cards()]
    if cids:
        mw.col.sched.suspend_cards(cids)
    # Remove from interleave queue
    _interleave_shown_topics.discard(card.id)
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip("Done (suspended)"); mw.reviewer.nextCard()

def _cmd_forget():
    card = mw.reviewer.card
    if not card: return
    note = mw.col.get_note(card.nid)
    if not is_topic(note):
        tooltip("Not a topic."); return
    m = get(note); m["st"] = "forgotten"; m["due"] = None
    save_meta(card.nid, m)
    card.type = 2; card.queue = -2; card.due = _col_day() + 9999
    mw.col.update_card(card)
    # Remove from interleave queue
    _interleave_shown_topics.discard(card.id)
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip("Forgotten"); mw.reviewer.nextCard()

def _cmd_edit_last():
    """Open the last created note for editing using Anki's built-in editor."""
    global _last_created_nid
    if not _last_created_nid: tooltip("No recent card."); return
    try:
        note = mw.col.get_note(_last_created_nid)
        cards = note.cards()
        if not cards: tooltip("Card not found."); return
        # Use Anki's EditCurrent which is designed to work during review
        # It opens a non-modal editor that doesn't break the reviewer
        from aqt.editcurrent import EditCurrent
        # Temporarily set the reviewer's card to the target card so EditCurrent edits it
        saved_card = mw.reviewer.card
        mw.reviewer.card = cards[0]
        ec = EditCurrent(mw)
        # Restore the original card after the editor opens
        mw.reviewer.card = saved_card
        # Re-bury all cards of the edited note when editor closes
        def _on_editor_close():
            if _last_created_nid:
                try:
                    edited_note = mw.col.get_note(_last_created_nid)
                    new_cids = [c.id for c in edited_note.cards()]
                    if new_cids:
                        mw.col.sched.bury_cards(new_cids)
                except: pass
            # Redraw the current review card
            if mw.state == "review" and saved_card:
                try:
                    saved_card.load()
                    mw.reviewer.card = saved_card
                    mw.reviewer._redraw_current_card()
                except: pass
        ec.form.buttonBox.accepted.connect(_on_editor_close)
        ec.form.buttonBox.rejected.connect(_on_editor_close)
    except Exception as ex:
        tooltip(f"Error: {ex}")


def _cmd_undo_text():
    """Undo the last text modification (highlight) AND delete the created note.
    Also restores parent priority/AF if it was changed by extraction."""
    card = mw.reviewer.card
    if not card: return
    nid = card.nid
    if nid not in _text_history or not _text_history[nid]:
        tooltip("Nothing to undo."); return
    # Always fetch fresh from DB to avoid stale cache
    note = mw.col.get_note(nid)
    fnames = [f["name"] for f in note.note_type()["flds"]]
    if "Text" not in fnames: tooltip("No Text field."); return

    # Restore previous text
    prev = _text_history[nid].pop()
    note["Text"] = prev
    mw.col.update_note(note)

    # Restore priority+af if saved
    if nid in _priority_history and _priority_history[nid]:
        old_p, old_af = _priority_history[nid].pop()
        if is_topic(note):
            m = get(note)
            m["p"]  = old_p
            m["af"] = old_af
            save_meta(nid, m)

    # Delete the created note (cloze or extract) if tracked
    if nid in _created_history and _created_history[nid]:
        created_nid = _created_history[nid].pop()
        if created_nid:
            try:
                mw.col.remove_notes([created_nid])
                tooltip("Undone (note deleted, priority restored)")
            except Exception:
                tooltip("Undone (text restored, note deletion failed)")
            mw.reviewer._redraw_current_card()
            return

    # Refresh the displayed card
    mw.reviewer._redraw_current_card()
    tooltip("Undone")


def _cmd_undo_answer():
    """Undo the last topic answer: restore IR metadata, card scheduling,
    and re-insert the topic at the front of the interleave queue so it
    appears as if the answer never happened."""
    global _interleave_items_since, _interleave_swapping
    if not _answer_history:
        tooltip("No topic answer to undo."); return

    snap = _answer_history.pop()
    nid = snap["nid"]
    cid = snap["cid"]

    # 1. Restore IR metadata to pre-answer state
    save_meta(nid, snap["meta"])

    # 2. Restore Anki card scheduling to pre-answer state
    try:
        card = mw.col.get_card(cid)
        card.ivl = snap["card_ivl"]
        card.due = snap["card_due"]
        card.type = snap["card_type"]
        card.queue = snap["card_queue"]
        mw.col.update_card(card)
    except Exception:
        tooltip("Undo failed: card not found."); return

    # 3. Restore interleave queue state
    #    Put the topic back at the front of the queue and mark it as unshown
    _interleave_shown_topics.discard(cid)
    if cid not in _interleave_topic_queue:
        _interleave_topic_queue.insert(0, cid)
    _interleave_items_since = snap.get("items_since", 0)

    # 4. Navigate back to the restored topic
    try:
        mw.reviewer.card = card
        mw.reviewer.card.start_timer()
        if _ir_toolbar: _ir_toolbar.setVisible(True)
        m = snap["meta"]
        tooltip(f"Answer undone — P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f}")
        _interleave_swapping = True
        mw.reviewer._showQuestion()
        _interleave_swapping = False
    except Exception:
        _interleave_swapping = False
        tooltip("Answer undone (metadata restored)")


# ============================================================
# Zotero sync
# ============================================================

def _zotero_sync():
    try:
        from .zotero_sync import sync
        tooltip("Zotero: Syncing...")
        s, e = sync()
        tooltip(f"Zotero: {s} sources, {e} extracts created.")
    except Exception as ex:
        tooltip(f"Zotero error: {ex}")
        import traceback; traceback.print_exc()

def _zotero_reset():
    from .zotero_sync import reset_state
    reset_state()


def _import_markdown_source():
    """Dialog to paste markdown text and create a source topic note."""
    from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                         QPlainTextEdit, QPushButton, QShortcut, QKeySequence)
    from .zotero_sync import _md_to_html, _fmt_math, _newlines_to_br

    if not mw.col:
        tooltip("No collection open."); return

    model = mw.col.models.by_name(cfg("topic_note_type"))
    if not model:
        showInfo(f"Note type '{cfg('topic_note_type')}' not found."); return

    dlg = QDialog(mw)
    dlg.setWindowTitle("Import Markdown as Source")
    dlg.setMinimumWidth(600)
    dlg.setMinimumHeight(500)
    layout = QVBoxLayout()

    layout.addWidget(QLabel("Reference / Source:"))
    ref_input = QLineEdit()
    layout.addWidget(ref_input)

    layout.addWidget(QLabel("Markdown text:"))
    text_input = QPlainTextEdit()
    text_input.setPlaceholderText("Paste markdown here...")
    layout.addWidget(text_input)

    btn_row = QHBoxLayout()
    import_btn = QPushButton("Import as Source")
    import_btn.setAutoDefault(False)
    cancel_btn = QPushButton("Cancel")
    cancel_btn.setAutoDefault(False)
    btn_row.addStretch()
    btn_row.addWidget(import_btn)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)

    def do_import():
        raw = text_input.toPlainText().strip()
        ref = ref_input.text().strip() or "Unknown"
        if not raw:
            tooltip("No text to import."); return

        # Convert markdown → HTML → math → br
        html = _md_to_html(raw)
        html = _fmt_math(html)
        html = _newlines_to_br(html)

        deck = cfg("topics_deck")
        did = mw.col.decks.id_for_name(deck)
        if did is None:
            did = mw.col.decks.id_for_name("Default")

        nn = Note(mw.col, model)
        fnames = [f["name"] for f in model["flds"]]
        if "Text" in fnames:
            nn["Text"] = html
        if "Reference" in fnames:
            nn["Reference"] = ref
        # IR-Data left empty — Prepare Topics will initialize it
        nn.tags = [cfg("source_tag")]
        nn.note_type()["did"] = did
        mw.col.addNote(nn)
        tooltip(f"Source imported. Run Prepare Topics to set priority.")
        dlg.accept()

    import_btn.clicked.connect(do_import)
    cancel_btn.clicked.connect(dlg.reject)
    sc = QShortcut(QKeySequence("Ctrl+Return"), dlg)
    sc.activated.connect(do_import)

    dlg.setLayout(layout)
    text_input.setFocus()
    dlg.exec()


# ============================================================
# Menu
# ============================================================

def _add_menu():
    menu = QMenu("IR", mw)
    mw.form.menubar.addMenu(menu)
    def _a(name, fn, shortcut=None):
        a = QAction(name, mw)
        if shortcut: a.setShortcut(shortcut)
        a.triggered.connect(fn); menu.addAction(a)

    _a("IR Settings", show_settings)
    menu.addSeparator()
    _a("Prepare Topics", _prepare_topics, cfg("key_prepare"))
    _a("Mercy (Spread Overdue)", lambda: showInfo(f"Mercy: {mercy(cfg('topics_deck'), cfg('mercy_days'))} topics spread."))
    _a("Auto-Postpone Now", lambda: showInfo(f"Postponed {auto_postpone(cfg('topics_deck'), cfg('postpone_protection'))} topics."))
    _a("Clean Orphans", lambda: showInfo(f"Cleaned {clean_orphans(cfg('topics_deck'))} orphans."))
    menu.addSeparator()
    _a("Queue Stats", _show_stats)
    menu.addSeparator()
    _a("Sync from Zotero", _zotero_sync)
    _a("Import Markdown as Source", _import_markdown_source)


def _init_topics():
    if not mw.col: return
    deck = cfg("topics_deck")
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen, n = set(), 0
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if not has_field(note): continue
        if is_topic(note): continue
        init_source(note, cfg("default_priority"), cap=_default_cap_for_note(note))
        mw.col.update_note(note)
        _set_review(card, 1, 1)
        n += 1
    showInfo(f"Initialized {n} topics.")


def _show_stats():
    if not mw.col: return
    from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                         QListWidget, QListWidgetItem, QPushButton, Qt)
    from .queue import _iter_topic_notes, build_queue

    today = date.today().isoformat()
    total = due = active = done = forgotten = 0
    avg_af = 0.0; avg_iv = 0.0; avg_p = 0.0
    for _, _, m in _iter_topic_notes(cfg("topics_deck")):
        total += 1
        if m["st"] == "active":
            active += 1
            avg_af += m["af"]; avg_iv += m["iv"]; avg_p += m["p"]
            if m.get("due") and m["due"] <= today: due += 1
        elif m["st"] == "done": done += 1
        elif m["st"] == "forgotten": forgotten += 1
    if active > 0:
        avg_af /= active; avg_iv /= active; avg_p /= active

    queue = build_queue(cfg("topics_deck"), cfg("randomization_degree"))

    # Also count items due
    items_deck = cfg("items_deck")
    try:
        items_did = mw.col.decks.id_for_name(items_deck)
        tree = mw.col.sched.deck_due_tree()
        def _find_items(nodes, target_did):
            for n in nodes:
                if n.deck_id == target_did:
                    return n.new_count + n.learn_count + n.review_count
                r = _find_items(n.children, target_did)
                if r is not None: return r
            return None
        items_due_count = _find_items(tree.children, items_did) or 0
    except:
        items_due_count = 0

    dlg = QDialog(mw)
    dlg.setWindowTitle("IR Queue Stats")
    dlg.setMinimumWidth(550); dlg.setMinimumHeight(400)
    layout = QVBoxLayout()

    # Stats summary
    stats = (
        f"Total: {total}  |  Active: {active}  |  Due today: {due}  |  "
        f"Done: {done}  |  Forgotten: {forgotten}\n"
        f"Avg priority: {avg_p:.1f}%  |  Avg AF: {avg_af:.2f}  |  Avg interval: {avg_iv:.0f}d\n"
        f"Queue: {len(queue)} topics + {items_due_count} items due"
    )
    lbl = QLabel(stats); lbl.setWordWrap(True)
    layout.addWidget(lbl)

    # Queue list
    layout.addWidget(QLabel(f"Today's queue ({len(queue)} topics, sorted by priority):"))
    lst = QListWidget()
    for pos, nid in enumerate(queue):
        try:
            note = mw.col.get_note(nid)
            m = get(note)
            title = note.fields[0][:70] if note.fields else "?"
            # Strip HTML for display
            import re
            title = re.sub(r'<[^>]+>', '', title).strip()[:70]
            item = QListWidgetItem(
                f"{pos+1}. [{m['p']:.1f}%] {title}  (I:{m['iv']}d AF:{m['af']:.2f})"
            )
            lst.addItem(item)
        except:
            continue
    layout.addWidget(lst)

    # Close button
    close_btn = QPushButton("Close")
    close_btn.clicked.connect(dlg.accept)
    close_btn.setAutoDefault(False)
    row = QHBoxLayout(); row.addStretch(); row.addWidget(close_btn)
    layout.addLayout(row)

    dlg.setLayout(layout)
    dlg.exec()


# ============================================================
# Plugin init
# ============================================================

class IRManager:
    def __init__(self):
        gui_hooks.profile_did_open.append(self._on_profile)
        gui_hooks.profile_will_close.append(self._on_profile_close)
        gui_hooks.reviewer_did_show_question.append(_on_show_question)
        gui_hooks.state_did_change.append(self._on_state_change)
        gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)
        gui_hooks.webview_did_receive_js_message.append(self._on_js_message)
        addHook("reviewStateShortcuts", _set_shortcuts)

    def _on_js_message(self, handled, message, context):
        """Handle pycmd messages from the webview."""
        # No longer saving card HTML — highlights are saved directly to note fields
        return handled

    def _on_profile(self):
        if not mw.col: return
        self._ensure_field()
        _add_menu()
        _setup_toolbar()
        mw.addonManager.setConfigAction(_ADDON_NAME, show_settings)

    def _on_profile_close(self):
        """Restore topics to due=today on shutdown so they're not stuck at tomorrow."""
        _on_review_end()

    def _on_state_change(self, new_state, old_state):
        global _interleave_active, _interleave_topic_queue, _prepare_done_for_session
        if old_state == "review":
            # If items ran out but topics remain, unhide them and continue
            if _interleave_active and _interleave_topic_queue and new_state == "overview":
                today = _col_day()
                # Unhide remaining topics at due=today (no offsets needed —
                # _prepare_topics will handle ordering via swap on re-entry)
                for tcid in _interleave_topic_queue:
                    try:
                        tc = mw.col.get_card(tcid)
                        tc.due = today
                        mw.col.update_card(tc)
                    except: pass
                _interleave_active = False
                _interleave_topic_queue = []
                # Reset session flag so _prepare_topics runs on re-entry
                _prepare_done_for_session = False
                try: mw.col.reset()
                except: pass
                mw.moveToState("review")
                return
            _on_review_end()
        # Note: _prepare_topics is NOT called here. It's called from
        # _on_show_question on the first card, which ensures topics are
        # hidden BEFORE Anki shows any card. This avoids the race condition
        # where Anki grabs cards before we hide topics.

    def _ensure_field(self):
        if not mw.col: return
        model = mw.col.models.by_name(cfg("topic_note_type"))
        if not model: return
        fnames = [f["name"] for f in model["flds"]]
        if IR_FIELD not in fnames:
            field = mw.col.models.new_field(IR_FIELD)
            mw.col.models.add_field(model, field)
            mw.col.models.save(model)
            tooltip(f"Added '{IR_FIELD}' field to {cfg('topic_note_type')}")


# ============================================================
# Browser context menu: Set Priority, Advance, Later Today
# ============================================================

def _on_browser_context_menu(browser, menu):
    """Add IR actions to the browser's right-click context menu."""
    sel = browser.selectedNotes()
    if not sel: return

    ir_menu = menu.addMenu("IR")

    a1 = ir_menu.addAction("Set Priority...")
    a1.triggered.connect(lambda: _browser_set_priority(browser))

    a2 = ir_menu.addAction("Advance to Today")
    a2.triggered.connect(lambda: _browser_advance_today(browser))

    a3 = ir_menu.addAction("Later Today")
    a3.triggered.connect(lambda: _browser_later_today(browser))

    a4 = ir_menu.addAction("Reschedule (+days)...")
    a4.triggered.connect(lambda: _browser_reschedule(browser))

    a5 = ir_menu.addAction("Postpone (1.5x)")
    a5.triggered.connect(lambda: _browser_postpone(browser))

    a6 = ir_menu.addAction("Done")
    a6.triggered.connect(lambda: _browser_done(browser))

    a7 = ir_menu.addAction("Forget (park)")
    a7.triggered.connect(lambda: _browser_forget(browser))


def _browser_get_topic_notes(browser):
    """Get selected notes that are IR topics."""
    results = []
    for nid in browser.selectedNotes():
        note = mw.col.get_note(nid)
        if is_topic(note):
            results.append((nid, note))
    return results


def _browser_set_priority(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    m0 = get(topics[0][1])
    result = ask_priority(m0["p"], m0["af"], m0["iv"])
    if result is None: return
    for nid, note in topics:
        m = get(note)
        old_p = m["p"]
        m["p"]  = scheduler.clamp_priority(result)
        m["af"] = scheduler.af_from_priority(m["p"])
        save_meta(nid, m)
        _update_extract_priorities_proportionally(note, old_p, m["p"])
    tooltip(f"Priority set to {result:.1f}% on {len(topics)} topic(s).")


def _browser_advance_today(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        m["due"] = scheduler.today_str()
        m["p"]   = scheduler.clamp_priority(max(0, m["p"] - 10))
        m["af"]  = scheduler.af_from_priority(m["p"])
        m["iv"]  = 1
        save_meta(nid, m)
        cards = note.cards()
        if cards: _set_review(cards[0], 1, 0)
    tooltip(f"Advanced {len(topics)} topic(s) to today.")


def _browser_later_today(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        m["due"] = scheduler.today_str()
        save_meta(nid, m)
        cards = note.cards()
        if cards: _set_review(cards[0], m["iv"], 0)
    tooltip(f"Scheduled {len(topics)} topic(s) for today.")


def _browser_reschedule(browser):
    """Browser Reschedule: set absolute interval. No AF/lr/rc change."""
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    val, ok = getText("Reschedule: days from today", title="Reschedule (no review)", default="3")
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    for nid, note in topics:
        m = get(note)
        r = scheduler.reschedule_absolute(m["iv"], m["af"], m["rc"], days,
                                          cap=m.get("cap", 0))
        m["iv"]  = r["iv"]
        m["due"] = r["due"]
        # af, rc, lr unchanged
        save_meta(nid, m)
        cards = note.cards()
        if cards: _set_review(cards[0], m["iv"], days)
    tooltip(f"Rescheduled {len(topics)} topic(s) → {days}d.")


def _browser_postpone(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        r = scheduler.postpone(m["iv"], m["af"], cap=m.get("cap", 0))
        m["iv"]  = r["iv"]
        m["af"]  = r["af"]
        m["due"] = r["due"]
        save_meta(nid, m)
        cards = note.cards()
        if cards: _set_review(cards[0], r["iv"], r["iv"])
    tooltip(f"Postponed {len(topics)} topic(s).")


def _browser_done(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note); m["st"] = "done"
        save_meta(nid, m)
        cids = [c.id for c in note.cards()]
        mw.col.sched.suspend_cards(cids)
    tooltip(f"Done (suspended) {len(topics)} topic(s).")


def _browser_forget(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note); m["st"] = "forgotten"; m["due"] = None
        save_meta(nid, m)
        cards = note.cards()
        if cards:
            c = cards[0]; c.type = 2; c.queue = -2
            c.due = _col_day() + 9999; mw.col.update_card(c)
    tooltip(f"Forgot {len(topics)} topic(s).")


# Patch answer handling for topic cards
Reviewer._answerButtonList = wrap(Reviewer._answerButtonList, _custom_answer_buttons, "around")
Reviewer._answerCard = wrap(Reviewer._answerCard, _custom_answer_card, "around")
Reviewer._buttonTime = wrap(Reviewer._buttonTime, _custom_button_time, "around")
