"""
Incremental Reading — SuperMemo-style for Anki.
Main plugin entry point. Hooks into Anki's reviewer for IR workflow.

Shortcuts during review of IR cards:
  X — Extract selected text into a new extract card
  Z — Create cloze deletion from selected text
  P — Set priority
  J — Reschedule (increment interval)
  R — Execute repetition (set interval from today)
  D — Done (dismiss from IR)
  F — Forget (park for later)
  N — Next IR card (skip without scheduling)
  [ — Priority increase (quick -5%)
  ] — Priority decrease (quick +5%)
"""

from typing import Any, Optional
from datetime import date

from anki.cards import Card
from anki.hooks import addHook
from anki.notes import Note
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu, QInputDialog, QMessageBox
from aqt.utils import showInfo, tooltip, getText

from . import scheduler
from .ir_fields import (
    get_meta, set_meta, has_ir_data, is_ir_note,
    init_meta_for_extract, IR_DATA_FIELD, DEFAULT_META,
)
from .queue_engine import build_ir_queue, auto_postpone, mercy


# ============================================================
# Configuration
# ============================================================

IR_DECKS = ["Main::02-Extracts", "Main::03-Sources"]
EXTRACT_DECK = "Main::02-Extracts"
CLOZE_DECK = "Main::01-Items"
EXTRACT_NOTE_TYPE = "Extracts"
CLOZE_NOTE_TYPE = "Cloze"
INITIAL_INTERVAL = 1
DEFAULT_PRIORITY = 50.0
RANDOMIZATION = 0
POSTPONE_PROTECTION = 10
AUTO_POSTPONE = True
MERCY_DAYS = 14



class IRManager:
    """Main incremental reading manager."""

    _queue: list = []
    _queue_idx: int = 0
    _session_active: bool = False
    _reviewed: int = 0

    def __init__(self):
        gui_hooks.profile_did_open.append(self._on_profile_loaded)
        gui_hooks.reviewer_did_show_question.append(self._on_show_question)
        addHook("reviewStateShortcuts", self._set_review_shortcuts)

    def _on_profile_loaded(self):
        self._ensure_ir_field()
        self._add_menu()

    def _ensure_ir_field(self):
        """Ensure the IR-Data field exists on the Extracts note type."""
        model = mw.col.models.by_name(EXTRACT_NOTE_TYPE)
        if not model:
            return
        field_names = [f["name"] for f in model["flds"]]
        if IR_DATA_FIELD not in field_names:
            field = mw.col.models.new_field(IR_DATA_FIELD)
            mw.col.models.add_field(model, field)
            mw.col.models.save(model)
            tooltip(f"Added '{IR_DATA_FIELD}' field to {EXTRACT_NOTE_TYPE} note type.")

    def _add_menu(self):
        """Add IR menu to Anki's menu bar."""
        menu = QMenu("IR", mw)
        mw.form.menubar.addMenu(menu)

        def _add(name, fn, shortcut=None):
            a = QAction(name, mw)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(fn)
            menu.addAction(a)

        _add("Start IR Session", self.start_session, "Ctrl+Shift+I")
        _add("End IR Session", self.end_session)
        menu.addSeparator()
        _add("Mercy (Spread Overdue)", self._mercy_cmd)
        _add("Auto-Postpone Now", self._auto_postpone_cmd)
        menu.addSeparator()
        _add("Init IR Data on Sources", self._init_sources_cmd)
        _add("Init IR Data on Extracts", self._init_extracts_cmd)

    # ============================================================
    # Review shortcuts
    # ============================================================

    def _set_review_shortcuts(self, shortcuts):
        shortcuts.extend([
            ("x", self._extract_selection),
            ("z", self._create_cloze),
            ("p", self._set_priority),
            ("j", self._reschedule_increment),
            ("Shift+r", self._execute_repetition_custom),
            ("d", self._mark_done),
            ("f", self._mark_forget),
            ("[", self._priority_increase),
            ("]", self._priority_decrease),
            ("Shift+p", self._postpone_current),
        ])

    def _on_show_question(self, card: Card):
        """When showing an IR card, display priority/interval info."""
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        info = f"P:{meta['ir_priority']:.1f}% | I:{meta['ir_interval']}d | AF:{meta['ir_af']:.2f} | Due:{meta.get('ir_next_due', '?')}"
        tooltip(info, period=2000)

    # ============================================================
    # Session management
    # ============================================================

    def start_session(self):
        """Start an IR learning session."""
        if AUTO_POSTPONE:
            count = auto_postpone(IR_DECKS, POSTPONE_PROTECTION)
            if count:
                tooltip(f"Auto-postponed {count} elements.")

        self._queue = build_ir_queue(IR_DECKS, RANDOMIZATION)
        self._queue_idx = 0
        self._reviewed = 0

        if not self._queue:
            showInfo("No IR elements due today.")
            return

        self._session_active = True
        tooltip(f"IR Session: {len(self._queue)} elements due.")
        self._open_current()

    def end_session(self):
        if not self._session_active:
            return
        self._session_active = False
        tooltip(f"IR Session ended. Reviewed {self._reviewed} elements.")

    def _open_current(self):
        """Open the current queue element in the reviewer."""
        if self._queue_idx >= len(self._queue):
            self.end_session()
            showInfo(f"Queue complete! Reviewed {self._reviewed} elements.")
            return

        nid = self._queue[self._queue_idx]
        cards = mw.col.get_note(nid).cards()
        if not cards:
            self._queue_idx += 1
            self._open_current()
            return

        # Set this card as the next to review
        card = cards[0]
        mw.reviewer.cardQueue.append(card)
        if mw.state == "review":
            mw.reviewer.nextCard()
        else:
            mw.moveToState("review")


    def _next_in_queue(self):
        """Move to next element, scheduling the current one."""
        if not self._session_active:
            return
        nid = self._queue[self._queue_idx] if self._queue_idx < len(self._queue) else None
        if nid:
            note = mw.col.get_note(nid)
            if is_ir_note(note):
                meta = get_meta(note)
                today = date.today().isoformat()
                is_due = not meta["ir_next_due"] or meta["ir_next_due"] <= today

                if is_due:
                    r = scheduler.execute_repetition(
                        meta["ir_interval"], meta["ir_af"], meta["ir_review_count"]
                    )
                    meta["ir_review_count"] = r["new_review_count"]
                    meta["ir_last_review"] = today
                    meta["ir_next_due"] = r["next_due"]
                    meta["ir_interval"] = r["new_interval"]
                    meta["ir_af"] = r["new_af"]
                else:
                    r = scheduler.mid_interval_repetition(meta["ir_af"], meta["ir_review_count"])
                    meta["ir_review_count"] = r["new_review_count"]
                    meta["ir_af"] = r["new_af"]

                set_meta(note, meta)
                mw.col.update_note(note)
                self._reviewed += 1

        self._queue_idx += 1
        self._open_current()

    # ============================================================
    # Extract (X key)
    # ============================================================

    def _extract_selection(self):
        """Extract selected text into a new extract card."""
        if mw.state != "review":
            return

        def _on_text(text):
            if not text or not text.strip():
                tooltip("Select text first.")
                return
            self._do_extract(text.strip())

        mw.web.evalWithCallback("window.getSelection().toString()", _on_text)

    def _do_extract(self, text: str):
        card = mw.reviewer.card
        if not card:
            return
        parent_note = card.note()
        parent_meta = get_meta(parent_note) if is_ir_note(parent_note) else dict(DEFAULT_META)

        # Create new extract note
        model = mw.col.models.by_name(EXTRACT_NOTE_TYPE)
        if not model:
            showInfo(f"Note type '{EXTRACT_NOTE_TYPE}' not found.")
            return

        new_note = Note(mw.col, model)
        # Set fields: Text field = extracted text
        field_names = [f["name"] for f in model["flds"]]
        if "Text" in field_names:
            new_note["Text"] = text
        elif len(field_names) > 0:
            new_note.fields[0] = text

        # Copy Reference from parent if available
        if "Reference" in field_names:
            try:
                new_note["Reference"] = parent_note["Reference"]
            except KeyError:
                pass

        # Copy Back Extra from parent if available
        if "Back Extra" in field_names:
            try:
                new_note["Back Extra"] = parent_note["Back Extra"]
            except KeyError:
                pass

        # Copy tags
        new_note.tags = list(parent_note.tags)

        # Set deck
        did = mw.col.decks.id_for_name(EXTRACT_DECK)
        if did is None:
            did = card.did

        # Initialize IR metadata
        init_meta_for_extract(new_note, parent_note.id, parent_meta["ir_priority"], len(text))

        # Add note
        new_note.note_type()["did"] = did
        mw.col.addNote(new_note)

        # Update parent: deprioritize
        if is_ir_note(parent_note):
            parent_meta["ir_af"] = scheduler.adjust_parent_after_extract(parent_meta["ir_af"])
            parent_meta["ir_priority"] = scheduler.adjust_parent_priority_after_extract(parent_meta["ir_priority"])
            set_meta(parent_note, parent_meta)
            mw.col.update_note(parent_note)

        # Highlight extracted text in the current card
        mw.web.eval("""
            (function() {
                var sel = window.getSelection();
                if (sel.rangeCount > 0) {
                    var range = sel.getRangeAt(0);
                    var span = document.createElement('span');
                    span.style.backgroundColor = '#a8d8ea';
                    range.surroundContents(span);
                    sel.removeAllRanges();
                }
            })();
        """)

        tooltip(f"Extract created (priority {parent_meta['ir_priority']:.1f}%)")

    # ============================================================
    # Cloze (Z key)
    # ============================================================

    def _create_cloze(self):
        """Create cloze deletion from selected text."""
        if mw.state != "review":
            return

        def _on_text(text):
            if not text or not text.strip():
                tooltip("Select text first.")
                return
            self._do_cloze(text.strip())

        mw.web.evalWithCallback("window.getSelection().toString()", _on_text)

    def _do_cloze(self, text: str):
        card = mw.reviewer.card
        if not card:
            return
        parent_note = card.note()

        model = mw.col.models.by_name(CLOZE_NOTE_TYPE)
        if not model:
            showInfo(f"Note type '{CLOZE_NOTE_TYPE}' not found.")
            return

        new_note = Note(mw.col, model)
        cloze_text = "{{c1::" + text + "}}"

        field_names = [f["name"] for f in model["flds"]]
        if "Text" in field_names:
            new_note["Text"] = cloze_text
        elif len(field_names) > 0:
            new_note.fields[0] = cloze_text

        # Copy Reference
        if "Reference" in field_names:
            try:
                new_note["Reference"] = parent_note["Reference"]
            except KeyError:
                pass

        # Copy Back Extra with link to parent
        if "Back Extra" in field_names:
            try:
                new_note["Back Extra"] = parent_note["Back Extra"]
            except KeyError:
                pass

        new_note.tags = list(parent_note.tags)

        did = mw.col.decks.id_for_name(CLOZE_DECK)
        if did is None:
            did = card.did

        new_note.note_type()["did"] = did
        mw.col.addNote(new_note)

        # Highlight
        mw.web.eval("""
            (function() {
                var sel = window.getSelection();
                if (sel.rangeCount > 0) {
                    var range = sel.getRangeAt(0);
                    var span = document.createElement('span');
                    span.style.backgroundColor = '#ffe0b2';
                    range.surroundContents(span);
                    sel.removeAllRanges();
                }
            })();
        """)

        tooltip("Cloze created")


    # ============================================================
    # Priority / Scheduling commands
    # ============================================================

    def _set_priority(self):
        """Set priority for current card (P key)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            tooltip("Not an IR card.")
            return
        meta = get_meta(note)
        val, ok = getText(
            f"Set priority (0=highest, 100=lowest)\nCurrent: {meta['ir_priority']:.2f}%",
            title="Priority",
            default=str(round(meta["ir_priority"]))
        )
        if ok and val:
            try:
                p = float(val)
                p = scheduler.clamp_priority(p)
                meta["ir_priority"] = p
                meta["ir_af"] = scheduler.af_from_priority_and_length(p, meta["ir_text_length"])
                set_meta(note, meta)
                mw.col.update_note(note)
                tooltip(f"Priority: {p:.2f}%, AF: {meta['ir_af']:.2f}")
            except ValueError:
                tooltip("Invalid number.")

    def _priority_increase(self):
        """Quick priority boost -5% ([ key)."""
        self._quick_priority_change(-5)

    def _priority_decrease(self):
        """Quick priority drop +5% (] key)."""
        self._quick_priority_change(5)

    def _quick_priority_change(self, delta: float):
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        new_p = scheduler.clamp_priority(meta["ir_priority"] + delta)
        meta["ir_priority"] = new_p
        meta["ir_af"] = scheduler.af_from_priority_and_length(new_p, meta["ir_text_length"])
        set_meta(note, meta)
        mw.col.update_note(note)
        tooltip(f"Priority: {new_p:.1f}% (AF: {meta['ir_af']:.2f})")

    def _reschedule_increment(self):
        """Reschedule: add days to interval (J key = SuperMemo Ctrl+J)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        val, ok = getText(
            f"Add days to interval (current: {meta['ir_interval']}d)",
            title="Reschedule (+days)",
            default="3"
        )
        if ok and val:
            try:
                days = int(val)
                if days < 1:
                    return
                r = scheduler.reschedule_increment(meta["ir_interval"], days)
                new_af = scheduler.adjust_af_on_reschedule(meta["ir_interval"], meta["ir_af"], r["new_interval"])
                new_p = scheduler.adjust_priority_on_interval_change(
                    meta["ir_priority"], meta["ir_interval"], meta["ir_af"], r["new_interval"]
                )
                meta["ir_next_due"] = r["next_due"]
                meta["ir_interval"] = r["new_interval"]
                meta["ir_af"] = new_af
                meta["ir_priority"] = new_p
                set_meta(note, meta)
                mw.col.update_note(note)
                tooltip(f"Reschedule: +{days}d → interval={r['new_interval']}d")
            except ValueError:
                tooltip("Invalid number.")

    def _execute_repetition_custom(self):
        """Execute repetition with custom interval (Shift+R = SuperMemo Ctrl+Shift+R)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        val, ok = getText(
            f"Set new interval from today (current: {meta['ir_interval']}d)",
            title="Execute Repetition",
            default=str(meta["ir_interval"])
        )
        if ok and val:
            try:
                days = int(val)
                if days < 1:
                    return
                new_af = scheduler.adjust_af_on_reschedule(meta["ir_interval"], meta["ir_af"], days)
                new_p = scheduler.adjust_priority_on_interval_change(
                    meta["ir_priority"], meta["ir_interval"], meta["ir_af"], days
                )
                meta["ir_next_due"] = scheduler.date_from_days(days)
                meta["ir_interval"] = days
                meta["ir_af"] = new_af
                meta["ir_priority"] = new_p
                meta["ir_last_review"] = scheduler.today_str()
                meta["ir_review_count"] += 1
                set_meta(note, meta)
                mw.col.update_note(note)
                tooltip(f"Execute rep: interval={days}d, AF={new_af:.2f}")
            except ValueError:
                tooltip("Invalid number.")

    def _postpone_current(self):
        """Postpone current element by 1.5x (Shift+P)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        r = scheduler.postpone_element(meta["ir_interval"], meta["ir_af"])
        meta["ir_next_due"] = r["next_due"]
        meta["ir_interval"] = r["new_interval"]
        meta["ir_af"] = r["new_af"]
        set_meta(note, meta)
        mw.col.update_note(note)
        tooltip(f"Postponed: interval={r['new_interval']}d")

    def _mark_done(self):
        """Mark current element as Done (D key)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        meta["ir_status"] = "done"
        set_meta(note, meta)
        mw.col.update_note(note)
        tooltip("Done ✓")
        if self._session_active:
            self._queue_idx += 1
            self._open_current()

    def _mark_forget(self):
        """Forget/park current element (F key)."""
        card = mw.reviewer.card
        if not card:
            return
        note = card.note()
        if not is_ir_note(note):
            return
        meta = get_meta(note)
        meta["ir_status"] = "forgotten"
        meta["ir_next_due"] = None
        set_meta(note, meta)
        mw.col.update_note(note)
        tooltip("Forgotten (parked). Use Remember to bring back.")
        if self._session_active:
            self._queue_idx += 1
            self._open_current()

    # ============================================================
    # Menu commands
    # ============================================================

    def _mercy_cmd(self):
        count = mercy(IR_DECKS, MERCY_DAYS)
        showInfo(f"Mercy: {count} elements spread over {MERCY_DAYS} days." if count else "No overdue elements.")

    def _auto_postpone_cmd(self):
        count = auto_postpone(IR_DECKS, POSTPONE_PROTECTION)
        showInfo(f"Auto-postponed {count} elements." if count else "Nothing to postpone.")

    def _init_sources_cmd(self):
        """Initialize IR-Data on all Source cards that don't have it yet."""
        count = 0
        nids = mw.col.find_notes(f'"deck:Main::03-Sources"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not has_ir_data(note):
                continue
            if is_ir_note(note):
                continue
            from .ir_fields import init_meta_for_source
            init_meta_for_source(note, DEFAULT_PRIORITY)
            mw.col.update_note(note)
            count += 1
        showInfo(f"Initialized IR data on {count} source notes.")

    def _init_extracts_cmd(self):
        """Initialize IR-Data on all Extract cards that don't have it yet."""
        count = 0
        nids = mw.col.find_notes(f'"deck:Main::02-Extracts"')
        for nid in nids:
            note = mw.col.get_note(nid)
            if not has_ir_data(note):
                continue
            if is_ir_note(note):
                continue
            init_meta_for_extract(note, 0, DEFAULT_PRIORITY, len(note.fields[0]) if note.fields else 0)
            mw.col.update_note(note)
            count += 1
        showInfo(f"Initialized IR data on {count} extract notes.")
