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
        "topic_ratio": 20, "source_tag": "ir::source", "extract_tag": "ir::extract",
        "highlight_extract": "#5b9bd5", "highlight_cloze": "#c9a227",
        "key_extract": "x", "key_cloze": "z", "key_priority": "Shift+p",
        "key_priority_up": "Alt+Up", "key_priority_down": "Alt+Down",
        "key_reschedule": "Shift+j", "key_execute_rep": "Shift+r",
        "key_postpone": "Shift+w", "key_done": "Shift+d", "key_forget": "Shift+f",
        "key_later_today": "Shift+l", "key_advance_today": "Shift+a",
        "key_edit_last": "Shift+e", "key_prepare": "Ctrl+Shift+p",
    }
    return c.get(key, defaults.get(key))


_last_created_nid: Optional[int] = None
_ir_toolbar: Optional[QToolBar] = None


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



# ============================================================
# Prepare Topics — the core sync between IR-Data and Anki cards
# ============================================================

def _prepare_topics():
    """
    Comprehensive topic preparation before review:
    1. Auto-init IR-Data on any topic notes that don't have it yet
    2. Auto-postpone overdue low-priority topics
    3. Clean orphan parent references
    4. Sync every topic card's Anki due date with IR-Data scheduling
    5. Respect topic_ratio to limit how many topics show per session
    """
    if not mw.col: return

    deck = cfg("topics_deck")
    did = mw.col.decks.id_for_name(deck)
    if did is None:
        tooltip(f"Deck '{deck}' not found."); return

    # Step 1: Auto-init any uninitialised topic notes
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen = set()
    init_count = 0
    for cid in cids:
        card = mw.col.get_card(cid)
        if card.nid in seen: continue
        seen.add(card.nid)
        note = card.note()
        if has_field(note) and not is_topic(note):
            init_source(note, cfg("default_priority"))
            mw.col.update_note(note)
            _set_review(card, 1, 1)
            init_count += 1

    # Step 2: Auto-postpone
    postpone_count = 0
    if cfg("auto_postpone"):
        postpone_count = auto_postpone(deck, cfg("postpone_protection"))

    # Step 3: Clean orphans
    orphan_count = clean_orphans(deck)

    # Step 4: Build priority queue and sync card due dates
    queue = build_queue(deck, cfg("randomization_degree"))
    due_set = set(queue)

    # Compute topic limit from ratio
    items_deck = cfg("items_deck")
    try:
        items_due = len(mw.col.find_cards(f'"deck:{items_deck}" is:due'))
    except:
        items_due = 50
    ratio = cfg("topic_ratio") / 100.0
    if ratio > 0:
        max_topics = max(1, round(items_due * ratio / max(0.01, 1 - ratio)))
    else:
        max_topics = 0
    topics_to_show = min(len(queue), max_topics) if max_topics > 0 else len(queue)

    # Step 5: Sync all topic cards
    cids = mw.col.find_cards(f'"deck:{deck}"')
    seen.clear()
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
            pos = queue.index(card.nid)
            if pos < topics_to_show:
                _set_review(card, max(1, m["iv"]), 0)  # due today
            else:
                _set_review(card, max(1, m["iv"]), 1)  # overflow → tomorrow
        else:
            # Not due: sync from IR-Data
            if m["due"] and m["st"] == "active":
                try:
                    delta = max(1, (date.fromisoformat(m["due"]) - date.today()).days)
                except:
                    delta = max(1, m["iv"])
                _set_review(card, max(1, m["iv"]), delta)
            else:
                _set_review(card, max(1, m["iv"]), 30)

    parts = []
    if init_count: parts.append(f"{init_count} initialized")
    if postpone_count: parts.append(f"{postpone_count} postponed")
    if orphan_count: parts.append(f"{orphan_count} orphans cleaned")
    parts.append(f"{topics_to_show}/{len(queue)} topics ready")
    tooltip(f"IR: {', '.join(parts)}")


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
    if _is_topic_card(self.card):
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
        (f"EditLast [{cfg('key_edit_last')}]", _cmd_edit_last),
    ]:
        btn = QToolButton(); btn.setText(label); btn.clicked.connect(fn)
        _ir_toolbar.addWidget(btn)
    mw.addToolBar(_ir_toolbar)


def _on_show_question(card: Card):
    if _ir_toolbar: _ir_toolbar.setVisible(_is_topic_card(card))
    if _is_topic_card(card):
        m = get(card.note())
        tooltip(f"P:{m['p']:.1f}% | I:{m['iv']}d | AF:{m['af']:.2f} | Due:{m.get('due','?')}", period=2000)


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
        (cfg("key_reschedule"), _cmd_reschedule),
        (cfg("key_execute_rep"), _cmd_execute_rep),
        (cfg("key_postpone"), _cmd_postpone),
        (cfg("key_later_today"), _cmd_later_today),
        (cfg("key_advance_today"), _cmd_advance_today),
        (cfg("key_done"), _cmd_done), (cfg("key_forget"), _cmd_forget),
        (cfg("key_edit_last"), _cmd_edit_last),
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

    new_cards = nn.cards()
    if new_cards: _set_review(new_cards[0], 1, 1)

    # SM19: deprioritize parent after extraction
    if pm:
        pm["af"] = scheduler.parent_af_after_extract(pm["af"])
        pm["p"] = scheduler.parent_priority_after_extract(pm["p"])
        put(parent, pm); mw.col.update_note(parent)

    color = cfg("highlight_extract")
    mw.web.eval(f"""(function(){{
        var s=window.getSelection();if(s.rangeCount>0){{
            var r=s.getRangeAt(0);var sp=document.createElement('span');
            sp.style.backgroundColor='{color}';sp.style.color='#fff';
            r.surroundContents(sp);s.removeAllRanges();
        }}
        // Save modified HTML back to note
        var el=document.querySelector('.ir-text')||document.querySelector('.card');
        if(el){{pycmd('ir_save_html:'+el.innerHTML);}}
    }})();""")
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

    # Bury new cloze so it appears tomorrow via FSRS, not today
    for nc in nn.cards(): _shelve(nc)

    color = cfg("highlight_cloze")
    mw.web.eval(f"""(function(){{
        var s=window.getSelection();if(s.rangeCount>0){{
            var r=s.getRangeAt(0);var sp=document.createElement('span');
            sp.style.backgroundColor='{color}';sp.style.color='#fff';
            r.surroundContents(sp);s.removeAllRanges();
        }}
        var el=document.querySelector('.ir-text')||document.querySelector('.card');
        if(el){{pycmd('ir_save_html:'+el.innerHTML);}}
    }})();""")
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
    tooltip(f"Postponed: {r['iv']}d"); mw.reviewer.nextCard()

def _cmd_later_today():
    """SM Ctrl+Shift+J: due=today, interval unchanged."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note)
    m["due"] = scheduler.today_str()
    # Interval, AF, priority all stay unchanged
    put(note, m); mw.col.update_note(note)
    _set_review(card, m["iv"], 0)
    tooltip("Later today (interval unchanged)")

def _cmd_advance_today():
    """SM Advance: move to today + boost priority by 10%."""
    card = mw.reviewer.card
    if not card or not _is_topic_card(card):
        # Also works outside review: advance by note ID from browser
        return
    note = card.note(); m = get(note)
    m["due"] = scheduler.today_str()
    m["p"] = scheduler.clamp_priority(max(0, m["p"] - 10))
    m["af"] = scheduler.af_from_priority(m["p"])
    m["iv"] = 1
    put(note, m); mw.col.update_note(note)
    _set_review(card, 1, 0)
    tooltip(f"Advanced to today. Priority: {m['p']:.1f}%")

def _cmd_done():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "done"
    put(note, m); mw.col.update_note(note)
    # Suspend all cards of this note (Anki suspend = permanently out of review)
    cids = [c.id for c in note.cards()]
    mw.col.sched.suspend_cards(cids)
    tooltip("Done (suspended)"); mw.reviewer.nextCard()

def _cmd_forget():
    card = mw.reviewer.card
    if not card or not _is_topic_card(card): return
    note = card.note(); m = get(note); m["st"] = "forgotten"; m["due"] = None
    put(note, m); mw.col.update_note(note)
    card.type = 2; card.queue = -2; card.due = _col_day() + 9999
    mw.col.update_card(card)
    tooltip("Forgotten"); mw.reviewer.nextCard()

def _cmd_edit_last():
    global _last_created_nid
    if not _last_created_nid: tooltip("No recent card."); return
    try:
        from aqt.browser.browser import Browser
        b = Browser(mw)
        b.form.searchEdit.lineEdit().setText(f"nid:{_last_created_nid}")
        b.onSearchActivated(); b.show()
    except Exception as ex:
        tooltip(f"Error: {ex}")



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
    showInfo(f"IR Queue Stats\n\nTopics: {total}\nActive: {active}\nDue today: {due}\nDone: {done}\nForgotten: {forgotten}")


# ============================================================
# Plugin init
# ============================================================

class IRManager:
    def __init__(self):
        gui_hooks.profile_did_open.append(self._on_profile)
        gui_hooks.reviewer_did_show_question.append(_on_show_question)
        gui_hooks.state_did_change.append(self._on_state_change)
        gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)
        gui_hooks.webview_did_receive_js_message.append(self._on_js_message)
        addHook("reviewStateShortcuts", _set_shortcuts)

    def _on_js_message(self, handled, message, context):
        """Handle pycmd messages from the webview (e.g., saving highlights)."""
        if not isinstance(context, Reviewer): return handled
        if not message.startswith("ir_save_html:"): return handled
        html = message[len("ir_save_html:"):]
        card = mw.reviewer.card
        if card:
            note = card.note()
            # Save the modified HTML to the Text field
            fnames = [f["name"] for f in note.note_type()["flds"]]
            if "Text" in fnames:
                note["Text"] = html
                mw.col.update_note(note)
        return (True, None)

    def _on_profile(self):
        if not mw.col: return
        self._ensure_field()
        _add_menu()
        _setup_toolbar()
        mw.addonManager.setConfigAction(_ADDON_NAME, show_settings)

    def _on_state_change(self, new_state, old_state):
        if new_state == "review": _prepare_topics()
        if old_state == "review": _on_review_end()

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
