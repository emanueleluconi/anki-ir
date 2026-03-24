"""
Incremental Reading for Anki — SuperMemo 19 topic scheduling.

Topics = review cards (type=2, queue=2) with ivl/due controlled by SM19.
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
from .ir_meta import get, put, has_field, is_topic, init_extract, init_source, IR_FIELD
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
        "auto_postpone": True, "postpone_protection": 10, "mercy_days": 14,
        "topic_item_ratio": 5,
        "source_tag": "ir::source", "extract_tag": "ir::extract",
        "highlight_extract": "#5b9bd5", "highlight_cloze": "#c9a227",
        "key_extract": "x", "key_cloze": "z", "key_priority": "Shift+p",
        "key_priority_up": "Alt+Up", "key_priority_down": "Alt+Down",
        "key_reschedule": "Shift+j", "key_execute_rep": "Shift+r",
        "key_postpone": "Shift+w", "key_done": "Shift+d", "key_forget": "Shift+f",
        "key_later_today": "Shift+l", "key_advance_today": "Shift+a",
        "key_edit_last": "Shift+e", "key_undo_text": "Alt+z",
        "key_prepare": "Ctrl+Shift+p",
    }
    return c.get(key, defaults.get(key))


_last_created_nid: Optional[int] = None
_ir_toolbar: Optional[QToolBar] = None
_text_history: dict = {}  # nid → list of previous Text field values (for undo)
_created_history: dict = {}  # nid → list of created note IDs (for undo — delete on undo)

# SM19 interleaving state
_interleave_topic_queue: list = []   # priority-sorted topic card IDs for this session
_interleave_items_since: int = 0     # items shown since last topic
_interleave_active: bool = False     # whether interleaving is active this session
_interleave_swapping: bool = False   # guard against recursive _showQuestion calls
_interleave_spacing: int = 5         # computed spacing: items per topic for this session
_interleave_shown_topics: set = set()  # topic card IDs already shown this session
_postponed_today: bool = False       # track if auto-postpone already ran today
_prepare_done_for_session: bool = False  # prevent re-running _prepare_topics in same session


def _is_topic_card(card: Card) -> bool:
    try: return is_topic(card.note())
    except: return False


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
        init_source(note, p)
        mw.col.update_note(note)
        due_today = result[0] == "apply" and item["today"]
        if due_today:
            m = get(note)
            m["due"] = scheduler.today_str(); m["iv"] = 1
            put(note, m); mw.col.update_note(note)
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
        note = card.note()
        if not has_field(note) or is_topic(note): continue

        is_extract = extract_tag in note.tags
        is_source_note = source_tag in note.tags

        if is_extract:
            # Extract from Zotero: inherit priority from parent source
            fnames = [f["name"] for f in note.note_type()["flds"]]
            parent_p = cfg("default_priority")
            if "Reference" in fnames:
                ref = note["Reference"].strip()
                if ref and ref in ref_to_priority:
                    parent_p = ref_to_priority[ref]
            text_len = len(note.fields[0]) if note.fields else 0
            init_extract(note, 0, parent_p, text_len)
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
        for card, note in new_sources:
            # Re-read note to check if it was initialised
            fresh = mw.col.get_note(note.id)
            if is_topic(fresh):
                init_count += 1

    # Step 1b: Link orphan extracts to parent sources and deprioritize parents
    # Handles Zotero-imported extracts (pnid=0). Match by Reference field.
    ref_to_source_nid = {}
    cids2 = mw.col.find_cards(f'"deck:{deck}"')
    seen2 = set()
    for cid in cids2:
        card = mw.col.get_card(cid)
        if card.nid in seen2: continue
        seen2.add(card.nid)
        note = card.note()
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
        note = card.note()
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
        put(note, m); mw.col.update_note(note)
        new_extract_counts[parent_nid] = new_extract_counts.get(parent_nid, 0) + 1

    # SM19: deprioritize parent sources for each new Zotero extract
    for parent_nid, count in new_extract_counts.items():
        try:
            pn = mw.col.get_note(parent_nid)
            pm = get(pn)
            for _ in range(count):
                pm["af"] = scheduler.parent_af_after_extract(pm["af"])
                pm["p"] = scheduler.parent_priority_after_extract(pm["p"])
            put(pn, pm); mw.col.update_note(pn)
        except: pass

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
    configured_ratio = cfg("topic_item_ratio") or 5

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
    if not _is_topic_card(card):
        _old(self, ease); return

    note = card.note(); m = get(note)
    today_iso = date.today().isoformat()
    is_due = not m["due"] or m["due"] <= today_iso

    if is_due:
        r = scheduler.execute_repetition(m["iv"], m["af"], m["rc"])
        m["rc"], m["lr"] = r["rc"], today_iso
        m["due"], m["iv"], m["af"] = r["due"], r["iv"], r["af"]
    else:
        r = scheduler.mid_interval_rep(m["af"], m["rc"])
        m["rc"], m["af"] = r["rc"], r["af"]

    put(note, m); mw.col.update_note(note)

    try:
        delta = max(1, (date.fromisoformat(m["due"]) - date.today()).days) if m["due"] else m["iv"]
    except: delta = m["iv"]
    _set_review(card, m["iv"], delta)
    self.nextCard()


def _custom_answer_buttons(self, _old):
    if self.card and _is_topic_card(self.card):
        m = get(self.card.note())
        next_iv = scheduler.compute_next_interval(max(1, m["iv"]), m["af"])
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
        # No interleaving (no topics due or studying sub-deck alone)
        if _is_topic_card(card):
            m = get(card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
        return

    if _is_topic_card(card):
        # Check if we already showed this topic (Anki serving a duplicate)
        if card.id in _interleave_shown_topics:
            # Already shown — skip to next card
            mw.reviewer.nextCard()
            return

        # Anki gave us a topic naturally. Check if it's the correct one
        if _interleave_topic_queue and card.id != _interleave_topic_queue[0]:
            next_topic_cid = _interleave_topic_queue.pop(0)
            _interleave_items_since = 0
            _interleave_shown_topics.add(next_topic_cid)
            try:
                topic_card = mw.col.get_card(next_topic_cid)
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
        try: _interleave_topic_queue.remove(card.id)
        except ValueError: pass
        m = get(card.note())
        tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
        return

    # Anki gave us an item
    _interleave_items_since += 1

    # Check if it's time for a topic
    if not _interleave_topic_queue:
        return  # no more topics to interleave

    if _interleave_items_since >= _interleave_spacing:
        # Time for a topic! Swap the current card with our next priority topic.
        next_topic_cid = _interleave_topic_queue.pop(0)
        _interleave_items_since = 0
        _interleave_shown_topics.add(next_topic_cid)
        try:
            topic_card = mw.col.get_card(next_topic_cid)
            mw.reviewer.card = topic_card
            mw.reviewer.card.start_timer()
            if _ir_toolbar: _ir_toolbar.setVisible(True)
            m = get(topic_card.note())
            tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)
            # Use _redraw_current_card which reloads and re-renders without
            # going through the full nextCard/v3 pipeline
            _interleave_swapping = True
            mw.reviewer._showQuestion()
            _interleave_swapping = False
        except Exception:
            _interleave_swapping = False


def _on_review_end():
    global _interleave_active, _interleave_topic_queue, _interleave_swapping, _interleave_spacing, _prepare_done_for_session
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
    _interleave_spacing = 5
    _interleave_shown_topics = set()
    _prepare_done_for_session = False
    # Tell Anki to recalculate deck counts (lighter than mw.reset())
    if mw.col and _prepare_done_for_session:
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
    ])


# ============================================================
# Commands
# ============================================================

def _cmd_extract():
    if mw.state != "review": return
    # Get HTML of selection (preserves math, formatting, etc.)
    js = """(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed) return '';
        var range=sel.getRangeAt(0);
        var div=document.createElement('div');
        div.appendChild(range.cloneContents());
        return div.innerHTML;
    })();"""
    mw.web.evalWithCallback(js, _do_extract)

def _do_extract(html):
    global _last_created_nid
    if not html or not html.strip(): tooltip("Select text first."); return
    html = html.strip()
    card = mw.reviewer.card
    if not card: return
    parent = card.note()
    pm = get(parent) if is_topic(parent) else None

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
    # Use plain text length for AF calculation (strip HTML tags)
    import re as _re
    plain_len = len(_re.sub(r'<[^>]+>', '', html))
    init_extract(nn, parent.id, pp, plain_len)
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    _last_created_nid = nn.id

    new_cards = nn.cards()
    if new_cards: _set_review(new_cards[0], 1, 1)

    # SM19: deprioritize parent after extraction
    if pm:
        pm["af"] = scheduler.parent_af_after_extract(pm["af"])
        pm["p"] = scheduler.parent_priority_after_extract(pm["p"])
        put(parent, pm); mw.col.update_note(parent)

    color = cfg("highlight_extract")
    # Highlight the selection visually in the webview (cosmetic only)
    mw.web.eval(f"""(function(){{
        var s=window.getSelection();if(s.rangeCount>0){{
            var r=s.getRangeAt(0);var sp=document.createElement('span');
            sp.style.backgroundColor='{color}';sp.style.color='#fff';
            r.surroundContents(sp);s.removeAllRanges();
        }}
    }})();""")
    # Save highlight to the note's Text field by replacing the extracted text
    # with a highlighted version. This avoids saving the entire card HTML
    # (which would include Reference, Back Extra, etc.)
    fnames = [f["name"] for f in parent.note_type()["flds"]]
    if "Text" in fnames:
        old_text = parent["Text"]
        # Wrap the extracted HTML in a highlight span within the Text field
        import re as _re
        plain_html = html.strip()
        if plain_html in old_text:
            highlighted = f'<span style="background-color:{color};color:#fff">{plain_html}</span>'
            new_text = old_text.replace(plain_html, highlighted, 1)
            # Save for undo
            nid = parent.id
            if nid not in _text_history: _text_history[nid] = []
            _text_history[nid].append(old_text)
            if nid not in _created_history: _created_history[nid] = []
            _created_history[nid].append(_last_created_nid)
            parent["Text"] = new_text
            mw.col.update_note(parent)
    tooltip("Extract created")


def _cmd_cloze():
    if mw.state != "review": return
    # Get ONLY the HTML of the selection
    js = """(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed)return JSON.stringify({err:1});
        var range=sel.getRangeAt(0);
        var div=document.createElement('div');
        div.appendChild(range.cloneContents());
        var selHtml=div.innerHTML;
        var selText=sel.toString().trim();
        return JSON.stringify({selHtml:selHtml,selText:selText});
    })();"""
    mw.web.evalWithCallback(js, _do_cloze)

def _do_cloze(result):
    global _last_created_nid
    try: data = json.loads(result) if isinstance(result, str) else result
    except: tooltip("Select text first."); return
    if not data or "err" in data: tooltip("Select text first."); return

    sel_html = data.get("selHtml", "").strip()
    sel_text = data.get("selText", "").strip()
    if not sel_html: tooltip("Select text first."); return

    card = mw.reviewer.card
    if not card: return
    parent = card.note()

    # Get the Text field content directly from the note (not rendered card)
    parent_fnames = [f["name"] for f in parent.note_type()["flds"]]
    parent_text = ""
    if "Text" in parent_fnames:
        parent_text = parent["Text"]
    elif parent.fields:
        parent_text = parent.fields[0]

    # SM19 approach: use the FULL text of the parent with the selection clozed.
    # This avoids sentence-truncation errors and matches SM19 behavior where
    # the cloze card contains the full extract context.
    import re as _re
    if sel_html in parent_text:
        cloze_text = parent_text.replace(sel_html, "{{c1::" + sel_html + "}}", 1)
    elif sel_text and sel_text in _re.sub(r'<[^>]+>', '', parent_text):
        # Plain text match — do replacement on the raw HTML by finding the text
        # within tags. Use a simple approach: replace in stripped text, then
        # reconstruct. Safer: just use the full parent text with plain cloze.
        plain = _re.sub(r'<[^>]+>', '', parent_text)
        cloze_text = plain.replace(sel_text, "{{c1::" + sel_text + "}}", 1)
    else:
        # Fallback: just the selection as cloze
        cloze_text = "{{c1::" + sel_html + "}}"

    card = mw.reviewer.card
    if not card: return
    parent = card.note()
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
    # Save highlight to the note's Text field
    parent_fnames2 = [f["name"] for f in parent.note_type()["flds"]]
    if "Text" in parent_fnames2:
        old_text = parent["Text"]
        if sel_html in old_text:
            highlighted = f'<span style="background-color:{color};color:#fff">{sel_html}</span>'
            new_text = old_text.replace(sel_html, highlighted, 1)
            nid = parent.id
            if nid not in _text_history: _text_history[nid] = []
            _text_history[nid].append(old_text)
            if nid not in _created_history: _created_history[nid] = []
            _created_history[nid].append(_last_created_nid)
            parent["Text"] = new_text
            mw.col.update_note(parent)
    tooltip("Cloze created (tomorrow)")



def _cmd_priority():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): tooltip("Not a topic."); return
    note = card.note(); m = get(note)
    result = ask_priority(m["p"], m["af"], m["iv"])
    if result is not None:
        m["p"] = scheduler.clamp_priority(result)
        m["af"] = scheduler.af_from_priority_and_length(m["p"], m["tl"])
        put(note, m); mw.col.update_note(note)
        tooltip(f"Priority: {m['p']:.1f}%, AF: {m['af']:.2f}")

def _cmd_quick_priority(delta):
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    m["p"] = scheduler.clamp_priority(m["p"] + delta)
    m["af"] = scheduler.af_from_priority_and_length(m["p"], m["tl"])
    put(note, m); mw.col.update_note(note)
    tooltip(f"Priority: {m['p']:.1f}%")

def _cmd_reschedule():
    """SM Ctrl+J: add days to interval. Last review unchanged."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    val, ok = getText(f"Add days to interval (current: {m['iv']}d)", title="Reschedule (+days)", default="3")
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    r = scheduler.reschedule_increment(m["iv"], days)
    m["af"] = scheduler.adjust_af_on_reschedule(m["iv"], m["af"], r["iv"])
    m["p"] = scheduler.adjust_priority_on_interval(m["p"], m["iv"], m["af"], r["iv"])
    m["due"], m["iv"] = r["due"], r["iv"]
    # Note: lr (last review) NOT updated — this is the key SM Ctrl+J behavior
    put(note, m); mw.col.update_note(note)
    _set_review(card, m["iv"], days)
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Reschedule: +{days}d → interval {r['iv']}d"); mw.reviewer.nextCard()

def _cmd_execute_rep():
    """SM Ctrl+Shift+R: set new interval from today. Last review = today."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    val, ok = getText(f"Set new interval from today (current: {m['iv']}d)", title="Execute Repetition", default=str(m["iv"]))
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    m["af"] = scheduler.adjust_af_on_reschedule(m["iv"], m["af"], days)
    m["p"] = scheduler.adjust_priority_on_interval(m["p"], m["iv"], m["af"], days)
    m["due"] = scheduler.date_from_days(days); m["iv"] = days
    m["lr"] = scheduler.today_str(); m["rc"] += 1
    put(note, m); mw.col.update_note(note)
    _set_review(card, days, days)
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Execute rep: interval={days}d, AF={m['af']:.2f}"); mw.reviewer.nextCard()

def _cmd_postpone():
    """SM Postpone: multiply interval by 1.5x."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    r = scheduler.postpone(m["iv"], m["af"])
    m["due"], m["iv"], m["af"] = r["due"], r["iv"], r["af"]
    put(note, m); mw.col.update_note(note)
    _set_review(card, r["iv"], r["iv"])
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip(f"Postponed: {r['iv']}d"); mw.reviewer.nextCard()

def _cmd_later_today():
    """SM Ctrl+Shift+J: put back in today's queue without changing interval.
    The card will reappear later in today's session."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    m["due"] = scheduler.today_str()
    # Interval, AF, priority all stay unchanged
    put(note, m); mw.col.update_note(note)
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
    if not card or not _is_topic_card(card):
        return
    note = card.note(); m = get(note)
    m["due"] = scheduler.today_str()
    m["p"] = scheduler.clamp_priority(max(0, m["p"] - 10))
    m["af"] = scheduler.af_from_priority(m["p"])
    m["iv"] = 1
    put(note, m); mw.col.update_note(note)
    _set_review(card, 1, 0)
    # If interleaving is active, re-hide the topic so it comes back via swap mechanism
    if _interleave_active:
        card.due = _col_day() + 1
        mw.col.update_card(card)
    # Don't remove from queue — card is due today and should reappear
    tooltip(f"Advanced to today. Priority: {m['p']:.1f}%")
    mw.reviewer.nextCard()

def _cmd_done():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "done"
    put(note, m); mw.col.update_note(note)
    # Suspend all cards of this note (Anki suspend = permanently out of review)
    cids = [c.id for c in note.cards()]
    mw.col.sched.suspend_cards(cids)
    # Remove from interleave queue
    try: _interleave_topic_queue.remove(card.id)
    except ValueError: pass
    tooltip("Done (suspended)"); mw.reviewer.nextCard()

def _cmd_forget():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "forgotten"; m["due"] = None
    put(note, m); mw.col.update_note(note)
    card.type = 2; card.queue = -2; card.due = _col_day() + 9999
    mw.col.update_card(card)
    # Remove from interleave queue
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
    """Undo the last text modification (highlight) AND delete the created note."""
    card = mw.reviewer.card
    if not card: return
    note = card.note()
    nid = note.id
    if nid not in _text_history or not _text_history[nid]:
        tooltip("Nothing to undo."); return
    fnames = [f["name"] for f in note.note_type()["flds"]]
    if "Text" not in fnames: tooltip("No Text field."); return

    # Restore previous text
    prev = _text_history[nid].pop()
    note["Text"] = prev
    mw.col.update_note(note)

    # Delete the created note (cloze or extract) if tracked
    if nid in _created_history and _created_history[nid]:
        created_nid = _created_history[nid].pop()
        if created_nid:
            try:
                created_note = mw.col.get_note(created_nid)
                # Remove from parent's children list
                parent_children = note["ir-children"] if "ir-children" in [f["name"] for f in note.note_type()["flds"]] else None
                # Remove the child link from the parent's IR-Data
                el_data = None
                try:
                    import json as _json
                    ir_field = note["IR-Data"]
                    if ir_field:
                        el_data = _json.loads(ir_field)
                except: pass
                # Delete all cards of the created note, then the note itself
                cids = [c.id for c in created_note.cards()]
                mw.col.remove_notes([created_nid])
                tooltip("Undone (note deleted)")
            except Exception:
                tooltip("Undone (text restored, note deletion failed)")
            # Refresh the displayed card to show restored text
            mw.reviewer._redraw_current_card()
            return

    # Refresh the displayed card
    mw.reviewer._redraw_current_card()
    tooltip("Undone")



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

    _a("Settings...", show_settings)
    menu.addSeparator()
    _a("Prepare Topics", _prepare_topics, cfg("key_prepare"))
    _a("Mercy (Spread Overdue)", lambda: showInfo(f"Mercy: {mercy(cfg('topics_deck'), cfg('mercy_days'))} topics spread."))
    _a("Auto-Postpone Now", lambda: showInfo(f"Postponed {auto_postpone(cfg('topics_deck'), cfg('postpone_protection'))} topics."))
    _a("Clean Orphans", lambda: showInfo(f"Cleaned {clean_orphans(cfg('topics_deck'))} orphans."))
    menu.addSeparator()
    _a("Queue Stats", _show_stats)
    menu.addSeparator()
    _a("Sync from Zotero", _zotero_sync)
    _a("Reset Zotero Sync", _zotero_reset)


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
        init_source(note, cfg("default_priority"))
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
                f"{pos+1}. [{m['p']:.0f}%] {title}  (I:{m['iv']}d AF:{m['af']:.2f})"
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
        global _interleave_active
        if old_state == "review":
            # If items ran out but topics remain, unhide them and continue
            if _interleave_active and _interleave_topic_queue and new_state == "overview":
                today = _col_day()
                for tcid in _interleave_topic_queue:
                    try:
                        tc = mw.col.get_card(tcid)
                        tc.due = today
                        mw.col.update_card(tc)
                    except: pass
                # Disable interleaving — remaining topics will show naturally
                _interleave_active = False
                # Go back to review to show the remaining topics
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
    # Use first selected topic's current priority as default
    m0 = get(topics[0][1])
    result = ask_priority(m0["p"], m0["af"], m0["iv"])
    if result is None: return
    for nid, note in topics:
        m = get(note)
        m["p"] = scheduler.clamp_priority(result)
        m["af"] = scheduler.af_from_priority_and_length(m["p"], m["tl"])
        put(note, m); mw.col.update_note(note)
    tooltip(f"Priority set to {result:.1f}% on {len(topics)} topic(s).")


def _browser_advance_today(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        m["due"] = scheduler.today_str()
        m["p"] = scheduler.clamp_priority(max(0, m["p"] - 10))
        m["af"] = scheduler.af_from_priority(m["p"])
        m["iv"] = 1
        put(note, m); mw.col.update_note(note)
        # Also sync the card
        cards = note.cards()
        if cards: _set_review(cards[0], 1, 0)
    tooltip(f"Advanced {len(topics)} topic(s) to today.")


def _browser_later_today(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        m["due"] = scheduler.today_str()
        put(note, m); mw.col.update_note(note)
        cards = note.cards()
        if cards: _set_review(cards[0], m["iv"], 0)
    tooltip(f"Scheduled {len(topics)} topic(s) for today.")


def _browser_reschedule(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    val, ok = getText("Add days to interval", title="Reschedule", default="3")
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    for nid, note in topics:
        m = get(note)
        r = scheduler.reschedule_increment(m["iv"], days)
        m["af"] = scheduler.adjust_af_on_reschedule(m["iv"], m["af"], r["iv"])
        m["p"] = scheduler.adjust_priority_on_interval(m["p"], m["iv"], m["af"], r["iv"])
        m["due"], m["iv"] = r["due"], r["iv"]
        put(note, m); mw.col.update_note(note)
        cards = note.cards()
        if cards: _set_review(cards[0], m["iv"], days)
    tooltip(f"Rescheduled {len(topics)} topic(s) +{days}d.")


def _browser_postpone(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note)
        r = scheduler.postpone(m["iv"], m["af"])
        m["due"], m["iv"], m["af"] = r["due"], r["iv"], r["af"]
        put(note, m); mw.col.update_note(note)
        cards = note.cards()
        if cards: _set_review(cards[0], r["iv"], r["iv"])
    tooltip(f"Postponed {len(topics)} topic(s).")


def _browser_done(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note); m["st"] = "done"
        put(note, m); mw.col.update_note(note)
        cids = [c.id for c in note.cards()]
        mw.col.sched.suspend_cards(cids)
    tooltip(f"Done (suspended) {len(topics)} topic(s).")


def _browser_forget(browser):
    topics = _browser_get_topic_notes(browser)
    if not topics: tooltip("No IR topics selected."); return
    for nid, note in topics:
        m = get(note); m["st"] = "forgotten"; m["due"] = None
        put(note, m); mw.col.update_note(note)
        cards = note.cards()
        if cards:
            c = cards[0]; c.type = 2; c.queue = -2
            c.due = _col_day() + 9999; mw.col.update_card(c)
    tooltip(f"Forgot {len(topics)} topic(s).")


# Patch answer handling for topic cards
Reviewer._answerButtonList = wrap(Reviewer._answerButtonList, _custom_answer_buttons, "around")
Reviewer._answerCard = wrap(Reviewer._answerCard, _custom_answer_card, "around")
Reviewer._buttonTime = wrap(Reviewer._buttonTime, _custom_button_time, "around")
