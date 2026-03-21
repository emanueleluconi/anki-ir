"""
Incremental Reading for Anki.

Topics = review cards with intervals controlled by SM19 via IR-Data field.
Items = normal Anki cards with FSRS scheduling (untouched).
Topic cards: type=2, queue=2, ivl/due set by our scheduler.
When answering a topic, SM19 computes the next interval and we write it directly.
"""

from datetime import date
from typing import Optional

from anki.cards import Card
from anki.hooks import addHook, wrap
from anki.notes import Note
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu, QToolBar, QToolButton
from aqt.reviewer import Reviewer
from aqt.utils import showInfo, tooltip

from . import scheduler
from .ir_meta import get, put, has_field, is_topic, init_extract, init_source, IR_FIELD
from .queue import build_queue, auto_postpone, mercy, clean_orphans
from .priority_dialog import ask_priority
from .settings_dialog import show_settings


def cfg(key):
    c = mw.addonManager.getConfig(__name__.split(".")[0]) or {}
    defaults = {
        "topics_deck": "Main::Topics", "items_deck": "Main::Items",
        "topic_note_type": "Extracts", "cloze_note_type": "Cloze",
        "initial_interval": 1, "default_priority": 50, "randomization_degree": 5,
        "auto_postpone": True, "postpone_protection": 10, "mercy_days": 14,
        "topic_ratio": 20, "source_tag": "ir::source", "extract_tag": "ir::extract",
        "key_extract": "x", "key_cloze": "z", "key_priority": "p",
        "key_priority_up": "9", "key_priority_down": "0", "key_reschedule": "j",
        "key_execute_rep": "e", "key_postpone": "w", "key_done": "d",
        "key_forget": "f", "key_edit_last": "Shift+e",
    }
    return c.get(key, defaults.get(key))


_last_created_nid: Optional[int] = None
_ir_toolbar: Optional[QToolBar] = None


def _is_topic_card(card: Card) -> bool:
    try: return is_topic(card.note())
    except: return False


def _col_day_offset() -> int:
    """Get Anki's day number for today (days since col creation)."""
    if not mw.col: return 0
    return mw.col.sched.today


def _set_card_as_review(card: Card, ivl: int, due_in_days: int):
    """Set a card as a review card due in `due_in_days` from today."""
    card.type = 2  # review
    card.queue = 2  # review queue
    card.ivl = ivl
    card.due = _col_day_offset() + due_in_days
    card.left = 0
    card.lapses = 0
    mw.col.update_card(card)


def _bury_card(card: Card):
    """Push card out of today's queue."""
    card.queue = -2  # buried manually
    mw.col.update_card(card)



# ============================================================
# Prepare topics: set card.due based on IR-Data scheduling
# ============================================================

def _prepare_topics():
    """
    Before studying, sync topic cards' due dates with IR-Data.
    Due topics → card.due = today (shown in review).
    Non-due topics → card.due = future day (not shown).
    This is the closest to SuperMemo: our scheduler controls WHEN topics appear,
    Anki just serves them as review cards on the scheduled day.
    """
    if not mw.col: return

    deck = cfg("topics_deck")
    did = mw.col.decks.id_for_name(deck)
    if did is None: return

    if cfg("auto_postpone"):
        n = auto_postpone(deck, cfg("postpone_protection"))
        if n: tooltip(f"IR: postponed {n} topics")

    clean_orphans(deck)

    today_iso = date.today().isoformat()
    today_day = _col_day_offset()
    queue = build_queue(deck, cfg("randomization_degree"))
    due_set = set(queue)

    # Compute how many topics to show based on ratio
    # Count items due today in items deck
    items_deck = cfg("items_deck")
    items_due = len(mw.col.find_cards(f'"deck:{items_deck}" is:due'))
    ratio = cfg("topic_ratio") / 100.0
    max_topics = max(1, round(items_due * ratio / max(0.01, 1 - ratio))) if ratio > 0 else len(queue)
    topics_to_show = min(len(queue), max_topics)

    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen = set()
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if not is_topic(note): continue
        m = get(note)

        if card.nid in due_set:
            pos = queue.index(card.nid)
            if pos < topics_to_show:
                # Due and within ratio limit: show today as review card
                # Use position for ordering (lower position = earlier in review)
                _set_card_as_review(card, max(1, m["iv"]), 0)
            else:
                # Due but over ratio: push to tomorrow
                _set_card_as_review(card, max(1, m["iv"]), 1)
        else:
            # Not due: set due date from IR-Data
            if m["due"] and m["st"] == "active":
                try:
                    due_date = date.fromisoformat(m["due"])
                    delta = (due_date - date.today()).days
                    _set_card_as_review(card, max(1, m["iv"]), max(0, delta))
                except:
                    _set_card_as_review(card, max(1, m["iv"]), 30)
            elif m["st"] in ("done", "dismissed", "forgotten"):
                # Inactive: bury indefinitely
                card.type = 2; card.queue = -2; card.ivl = 9999
                card.due = today_day + 9999
                mw.col.update_card(card)

    tooltip(f"IR: {topics_to_show} topics ready ({len(queue)} due)")


# ============================================================
# Answer hook: SM19 for topics, FSRS for items
# ============================================================

def _custom_answer_card(self, ease, _old):
    """Intercept answering for topic cards to apply SM19 scheduling."""
    card = self.card
    if not _is_topic_card(card):
        # Normal item: let FSRS handle it
        _old(self, ease)
        return

    # Topic card: apply SM19 scheduling
    note = card.note()
    m = get(note)
    today_iso = date.today().isoformat()
    is_due = not m["due"] or m["due"] <= today_iso

    if is_due:
        r = scheduler.execute_repetition(m["iv"], m["af"], m["rc"])
        m["rc"] = r["rc"]
        m["lr"] = today_iso
        m["due"] = r["due"]
        m["iv"] = r["iv"]
        m["af"] = r["af"]
    else:
        r = scheduler.mid_interval_rep(m["af"], m["rc"])
        m["rc"] = r["rc"]
        m["af"] = r["af"]

    put(note, m)
    mw.col.update_note(note)

    # Set card's Anki-level scheduling to match
    try:
        due_date = date.fromisoformat(m["due"]) if m["due"] else date.today()
        delta = max(1, (due_date - date.today()).days)
    except:
        delta = max(1, m["iv"])

    _set_card_as_review(card, m["iv"], delta)

    # Move to next card (don't call _old which would apply FSRS)
    self.nextCard()


def _custom_answer_buttons(self, _old):
    if _is_topic_card(self.card):
        return ((1, "Next"),)
    return _old(self)


def _custom_button_time(self, i, v3_labels, _old):
    try:
        if _is_topic_card(mw.reviewer.card):
            return "<div class=spacer></div>"
    except: pass
    return _old(self, i, v3_labels)



# ============================================================
# IR Toolbar (visible during review when topic card is shown)
# ============================================================

def _setup_toolbar():
    global _ir_toolbar
    if _ir_toolbar: return
    _ir_toolbar = QToolBar("IR Commands", mw)
    _ir_toolbar.setMovable(False)
    _ir_toolbar.setVisible(False)
    for label, fn in [
        ("Extract[X]", _cmd_extract), ("Cloze[Z]", _cmd_cloze),
        ("Priority[P]", _cmd_priority), ("P+[9]", lambda: _cmd_quick_priority(-5)),
        ("P-[0]", lambda: _cmd_quick_priority(5)), ("Resched[J]", _cmd_reschedule),
        ("ExecRep[E]", _cmd_execute_rep), ("Postpone[W]", _cmd_postpone),
        ("Done[D]", _cmd_done), ("Forget[F]", _cmd_forget),
        ("EditLast[Shift+E]", _cmd_edit_last),
    ]:
        btn = QToolButton(); btn.setText(label); btn.clicked.connect(fn)
        _ir_toolbar.addWidget(btn)
    mw.addToolBar(_ir_toolbar)


def _on_show_question(card: Card):
    if _ir_toolbar:
        _ir_toolbar.setVisible(_is_topic_card(card))
    if _is_topic_card(card):
        m = get(card.note())
        tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f}", period=2000)


def _on_review_end():
    if _ir_toolbar: _ir_toolbar.setVisible(False)


# ============================================================
# Shortcuts
# ============================================================

def _set_shortcuts(shortcuts):
    shortcuts.extend([
        (cfg("key_extract"), _cmd_extract), (cfg("key_cloze"), _cmd_cloze),
        (cfg("key_priority"), _cmd_priority),
        (cfg("key_priority_up"), lambda: _cmd_quick_priority(-5)),
        (cfg("key_priority_down"), lambda: _cmd_quick_priority(5)),
        (cfg("key_reschedule"), _cmd_reschedule), (cfg("key_execute_rep"), _cmd_execute_rep),
        (cfg("key_postpone"), _cmd_postpone), (cfg("key_done"), _cmd_done),
        (cfg("key_forget"), _cmd_forget), (cfg("key_edit_last"), _cmd_edit_last),
    ])


# ============================================================
# Commands
# ============================================================

def _cmd_extract():
    if mw.state != "review": return
    mw.web.evalWithCallback("window.getSelection().toString()", _do_extract)

def _do_extract(text):
    global _last_created_nid
    if not text or not text.strip(): tooltip("Select text first."); return
    text = text.strip()
    card = mw.reviewer.card
    if not card: return
    parent = card.note()
    pm = get(parent) if is_topic(parent) else None

    model = mw.col.models.by_name(cfg("topic_note_type"))
    if not model: showInfo(f"Note type '{cfg('topic_note_type')}' not found."); return
    nn = Note(mw.col, model)
    fnames = [f["name"] for f in model["flds"]]
    if "Text" in fnames: nn["Text"] = text
    elif fnames: nn.fields[0] = text
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
    init_extract(nn, parent.id, pp, len(text))
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    _last_created_nid = nn.id

    # Set the new card as a review card due tomorrow
    new_cards = nn.cards()
    if new_cards:
        _set_card_as_review(new_cards[0], 1, 1)

    # Deprioritize parent (SM19)
    if pm:
        pm["af"] = scheduler.parent_af_after_extract(pm["af"])
        pm["p"] = scheduler.parent_priority_after_extract(pm["p"])
        put(parent, pm); mw.col.update_note(parent)

    mw.web.eval("(function(){var s=window.getSelection();if(s.rangeCount>0){var r=s.getRangeAt(0);var sp=document.createElement('span');sp.style.backgroundColor='#a8d8ea';r.surroundContents(sp);s.removeAllRanges();}})();")
    tooltip("Extract created")


def _cmd_cloze():
    if mw.state != "review": return
    js = """(function(){
        var sel=window.getSelection();
        if(!sel||sel.isCollapsed)return JSON.stringify({err:1});
        var st=sel.toString(),r=sel.getRangeAt(0),n=r.startContainer;
        var b=n.nodeType===3?n.parentElement:n;
        while(b&&b!==document.body){var t=b.tagName?b.tagName.toLowerCase():"";
        if(["p","div","li","td","th","blockquote","h1","h2","h3","h4","h5","h6"].indexOf(t)>=0)break;b=b.parentElement;}
        var ft=b?b.innerText:st,ls=ft.split("\\n"),bl=ls[0]||ft;
        var sub=st.substring(0,Math.min(20,st.length));
        for(var i=0;i<ls.length;i++){if(ls[i].indexOf(sub)>=0){bl=ls[i];break;}}
        return JSON.stringify({sel:st.trim(),line:bl.trim()});})();"""
    mw.web.evalWithCallback(js, _do_cloze)

def _do_cloze(result):
    global _last_created_nid
    import json
    try: data = json.loads(result) if isinstance(result, str) else result
    except: tooltip("Select text first."); return
    if not data or "err" in data: tooltip("Select text first."); return
    sel = data.get("sel", "").strip()
    line = data.get("line", "").strip()
    if not sel: tooltip("Select text first."); return
    cloze_text = line.replace(sel, "{{c1::" + sel + "}}", 1) if sel in line else "{{c1::" + sel + "}}"

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
    did = mw.col.decks.id_for_name(cfg("items_deck")) or card.did
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    _last_created_nid = nn.id

    # Bury the new cloze card so it shows tomorrow, not today
    new_cards = nn.cards()
    for nc in new_cards:
        _bury_card(nc)

    mw.web.eval("(function(){var s=window.getSelection();if(s.rangeCount>0){var r=s.getRangeAt(0);var sp=document.createElement('span');sp.style.backgroundColor='#ffe0b2';r.surroundContents(sp);s.removeAllRanges();}})();")
    tooltip("Cloze created (buried for tomorrow)")



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
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    from aqt.utils import getText
    val, ok = getText(f"Add days (interval: {m['iv']}d)", title="Reschedule", default="3")
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    r = scheduler.reschedule_increment(m["iv"], days)
    m["af"] = scheduler.adjust_af_on_reschedule(m["iv"], m["af"], r["iv"])
    m["p"] = scheduler.adjust_priority_on_interval(m["p"], m["iv"], m["af"], r["iv"])
    m["due"], m["iv"] = r["due"], r["iv"]
    put(note, m); mw.col.update_note(note)
    _set_card_as_review(card, m["iv"], days)
    tooltip(f"+{days}d → {r['iv']}d"); mw.reviewer.nextCard()

def _cmd_execute_rep():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    from aqt.utils import getText
    val, ok = getText(f"Set interval (current: {m['iv']}d)", title="Execute Rep", default=str(m["iv"]))
    if not ok or not val: return
    try: days = int(val)
    except ValueError: return
    if days < 1: return
    m["af"] = scheduler.adjust_af_on_reschedule(m["iv"], m["af"], days)
    m["p"] = scheduler.adjust_priority_on_interval(m["p"], m["iv"], m["af"], days)
    m["due"] = scheduler.date_from_days(days); m["iv"] = days
    m["lr"] = scheduler.today_str(); m["rc"] += 1
    put(note, m); mw.col.update_note(note)
    _set_card_as_review(card, days, days)
    tooltip(f"Exec rep: {days}d"); mw.reviewer.nextCard()

def _cmd_postpone():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    r = scheduler.postpone(m["iv"], m["af"])
    m["due"], m["iv"], m["af"] = r["due"], r["iv"], r["af"]
    put(note, m); mw.col.update_note(note)
    _set_card_as_review(card, r["iv"], r["iv"])
    tooltip(f"Postponed: {r['iv']}d"); mw.reviewer.nextCard()

def _cmd_done():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "done"
    put(note, m); mw.col.update_note(note)
    card.type = 2; card.queue = -2; card.due = _col_day_offset() + 9999
    mw.col.update_card(card)
    tooltip("Done"); mw.reviewer.nextCard()

def _cmd_forget():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "forgotten"; m["due"] = None
    put(note, m); mw.col.update_note(note)
    card.type = 2; card.queue = -2; card.due = _col_day_offset() + 9999
    mw.col.update_card(card)
    tooltip("Forgotten"); mw.reviewer.nextCard()

def _cmd_edit_last():
    global _last_created_nid
    if not _last_created_nid: tooltip("No recent card."); return
    try:
        from aqt.browser.browser import Browser
        browser = Browser(mw)
        browser.form.searchEdit.lineEdit().setText(f"nid:{_last_created_nid}")
        browser.onSearchActivated()
        browser.show()
    except Exception as ex:
        tooltip(f"Error: {ex}")


# ============================================================
# Menu
# ============================================================

def _add_menu():
    menu = QMenu("IR", mw)
    mw.form.menubar.addMenu(menu)
    def _a(name, fn):
        a = QAction(name, mw); a.triggered.connect(fn); menu.addAction(a)
    _a("Settings...", show_settings)
    menu.addSeparator()
    _a("Prepare Topics", _prepare_topics)
    _a("Mercy (Spread Overdue)", lambda: showInfo(f"Mercy: {mercy(cfg('topics_deck'), cfg('mercy_days'))} topics."))
    _a("Auto-Postpone Now", lambda: showInfo(f"Postponed {auto_postpone(cfg('topics_deck'), cfg('postpone_protection'))} topics."))
    _a("Clean Orphans", lambda: showInfo(f"Cleaned {clean_orphans(cfg('topics_deck'))} orphans."))
    menu.addSeparator()
    _a("Init IR Data on Topics", _init_topics)
    _a("Queue Stats", _show_stats)


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
        # Set card as review due tomorrow
        _set_card_as_review(card, 1, 1)
        n += 1
    showInfo(f"Initialized {n} topics.")


def _show_stats():
    if not mw.col: return
    today = date.today().isoformat()
    total = due = active = done = forgotten = 0
    from .queue import _iter_topic_notes
    for _, _, m in _iter_topic_notes(cfg("topics_deck")):
        total += 1
        if m["st"] == "active":
            active += 1
            if m.get("due") and m["due"] <= today: due += 1
        elif m["st"] == "done": done += 1
        elif m["st"] == "forgotten": forgotten += 1
    showInfo(f"Topics: {total}\nActive: {active}\nDue: {due}\nDone: {done}\nForgotten: {forgotten}")


# ============================================================
# Plugin init
# ============================================================

class IRManager:
    def __init__(self):
        gui_hooks.profile_did_open.append(self._on_profile)
        gui_hooks.reviewer_did_show_question.append(_on_show_question)
        gui_hooks.state_did_change.append(self._on_state_change)
        addHook("reviewStateShortcuts", _set_shortcuts)

    def _on_profile(self):
        if not mw.col: return
        self._ensure_field()
        _add_menu()
        _setup_toolbar()
        mw.addonManager.setConfigAction(__name__.split(".")[0], show_settings)

    def _on_state_change(self, new_state, old_state):
        if new_state == "review":
            _prepare_topics()
        if old_state == "review":
            _on_review_end()

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


# Patch answer handling for topic cards
Reviewer._answerButtonList = wrap(Reviewer._answerButtonList, _custom_answer_buttons, "around")
Reviewer._answerCard = wrap(Reviewer._answerCard, _custom_answer_card, "around")
Reviewer._buttonTime = wrap(Reviewer._buttonTime, _custom_button_time, "around")
