"""Zotero Web API sync for Anki IR plugin.

Pulls sources (tagged items) and extracts (colored highlights) from Zotero
directly into Anki's Topics deck as topic cards.

Multi-device safe:
- Duplicate detection searches the Anki collection for Zotero keys in the
  Back Extra field (SourceID/NoteID). This works across AnkiWeb-synced devices.
- Local version tracking only used to minimize API calls.
- First sync uses current library version as baseline (no big initial pull).

Cards are created directly — no Prepare Topics needed after sync.
"""

import json
import os
import re
from datetime import date
from urllib.request import Request, urlopen
from urllib.error import URLError

from anki.notes import Note
from aqt import mw
from aqt.utils import showInfo, tooltip


_ADDON = "ir_anki"


def _cfg(key):
    c = mw.addonManager.getConfig(_ADDON) or {}
    return c.get(key, {
        "topics_deck": "Main::Topics", "topic_note_type": "Extracts",
        "source_tag": "ir::source", "extract_tag": "ir::extract",
        "default_priority": 50,
        "zotero_library_id": "", "zotero_api_key": "",
        "zotero_import_tag": "imported", "zotero_highlight_color": "#ffd400",
    }.get(key))


# ============================================================
# Zotero API
# ============================================================

_cache = {}


def _api(endpoint):
    lib = _cfg("zotero_library_id")
    key = _cfg("zotero_api_key")
    if not lib or not key:
        return None
    url = f"https://api.zotero.org/users/{lib}/{endpoint}"
    try:
        resp = urlopen(Request(url, headers={"Zotero-API-Key": key}), timeout=30)
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, resp.read().decode("utf-8")
    except Exception:
        return None


def _lib_version():
    r = _api("items?limit=1&format=keys")
    if not r or r[0] != 200:
        return None
    return int(r[1].get("last-modified-version", "0"))


def _fetch_since(ver):
    items, start = [], 0
    while True:
        r = _api(f"items?since={ver}&limit=100&start={start}")
        if not r or r[0] != 200:
            break
        batch = json.loads(r[2])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        start += 100
    return items


def _get_item(key):
    if key in _cache:
        return _cache[key]
    r = _api(f"items/{key}")
    if not r or r[0] != 200:
        return None
    item = json.loads(r[2])
    _cache[key] = item
    return item


def _bib_parent(key):
    cur = _get_item(key)
    for _ in range(5):
        if not cur:
            return None
        if cur["data"].get("itemType", "") not in ("attachment", "note", "annotation"):
            return cur
        pk = cur["data"].get("parentItem")
        if not pk:
            return None
        cur = _get_item(pk)
    return None


# ============================================================
# Formatting (matches the Google Apps Script)
# ============================================================

def _item_data(d):
    title = d.get("title", "No Title")
    year = ""
    m = re.search(r"\d{4}", d.get("date", ""))
    if m:
        year = m.group(0)
    authors = []
    for c in d.get("creators", []):
        # Handle both split (firstName/lastName) and single-field (name) creators.
        # Single-field names (e.g. YouTube channels, institutions) are kept whole;
        # split names use lastName for citation shortening.
        if c.get("name"):
            authors.append(c["name"].strip())
        else:
            last = c.get("lastName", "").strip()
            first = c.get("firstName", "").strip()
            n = f"{first} {last}".strip() if first else last
            if n:
                authors.append(n)
    return title, year, authors


def _last_name(author):
    """Extract citation-suitable last name from an author string.

    For institutional / channel names that contain common org words,
    return the full name rather than just the last word.
    """
    org_indicators = {"university", "institute", "college", "school", "lab",
                      "laboratory", "center", "centre", "foundation", "society",
                      "association", "department", "faculty", "academy", "channel"}
    words = author.strip().split()
    if not words:
        return "Unknown"
    # If any word in the name is an org indicator, use the full name
    if any(w.lower() in org_indicators for w in words):
        return author.strip()
    # Otherwise use the last word (standard surname extraction)
    return words[-1]


def _fmt_authors(authors, year, title):
    stop = {"a","an","the","and","but","or","for","nor","on","at","to","from","by","with","of","in","is","are","was","were","it","that","this","as"}
    tw = "Unknown"
    if title:
        for w in re.sub(r"[^\w\s]", "", title).split():
            if len(w) > 2 and w.lower() not in stop:
                tw = w[0].upper() + w[1:]
                break

    lns = [_last_name(a) for a in authors if a.strip()] or ["Unknown"]
    if len(lns) == 1:
        ra = lns[0]
    elif len(lns) == 2:
        ra = f"{lns[0]} & {lns[1]}"
    else:
        ra = f"{lns[0]} et al."

    ref = f'{ra} ({year}), "{title}"'

    if len(lns) == 1:
        tb = re.sub(r"[^a-zA-Z]", "", lns[0])
    elif len(lns) == 2:
        tb = re.sub(r"[^a-zA-Z]", "", lns[0]) + "&" + re.sub(r"[^a-zA-Z]", "", lns[1])
    else:
        tb = re.sub(r"[^a-zA-Z]", "", lns[0]) + "_et_al"

    tag = f"{tb}{year}-{tw}"
    return ref, tag, ra


def _fmt_math(text):
    if not text:
        return ""
    # Display math: $$...$$ → \[...\]  (must run before inline; require non-empty content)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: "\\[" + m.group(1) + "\\]", text, flags=re.DOTALL)
    # Inline math: $...$ → \(...\)  (non-empty, no $ or newline inside to prevent runaway matches)
    text = re.sub(r"\$([^$\n]+?)\$", lambda m: "\\(" + m.group(1) + "\\)", text)
    ctr = [1]
    def _repl(m):
        inner = m.group(1)
        nm = re.match(r"^(\d+):(.*)", inner)
        if nm:
            cid = int(nm.group(1))
            if cid >= ctr[0]:
                ctr[0] = cid + 1
            return "{{c" + str(cid) + "::" + nm.group(2) + "}}"
        c = ctr[0]; ctr[0] += 1
        return "{{c" + str(c) + "::" + inner + "}}"
    text = re.sub(r"\{(.*?)\}", _repl, text)
    return text


def _strip_html(h):
    return re.sub(r"<[^>]+>", "", h).replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _note_html_to_anki(html):
    """Convert Zotero note HTML to clean Anki HTML, preserving structure and math.

    Converts block elements (<p>, <h1-6>, <hr>) to line breaks, keeps <b>/<i>,
    and leaves $...$ math intact for _fmt_math to process.
    """
    if not html:
        return ""
    # Normalize self-closing <br />
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    # Block elements → newline
    html = re.sub(r"<(p|div|h[1-6]|hr|li|tr)[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", html, flags=re.IGNORECASE)
    # Normalize bold/italic to simple tags
    html = re.sub(r"<strong[^>]*>", "<b>", html, flags=re.IGNORECASE)
    html = re.sub(r"</strong>", "</b>", html, flags=re.IGNORECASE)
    html = re.sub(r"<em[^>]*>", "<i>", html, flags=re.IGNORECASE)
    html = re.sub(r"</em>", "</i>", html, flags=re.IGNORECASE)
    # Strip all remaining tags except <b> and <i>
    html = re.sub(r"<(?!/?[bi]>)[^>]+>", "", html)
    # Decode entities
    html = html.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Collapse 3+ newlines to 2
    html = re.sub(r"\n{3,}", "\n\n", html)
    # Convert newlines to <br>
    html = html.replace("\n\n", "<br><br>").replace("\n", "<br>")
    # Strip leading/trailing breaks
    html = re.sub(r"^(<br>)+", "", html)
    html = re.sub(r"(<br>)+$", "", html)
    return html.strip()


# ============================================================
# Duplicate detection (multi-device safe)
# ============================================================

def _exists(zotero_key):
    """Check if a note with this Zotero key exists anywhere in the collection.

    Uses two strategies for robustness:
    1. FTS search (fast, but may miss notes added in the same session before index flush)
    2. Direct SQL scan of the Back Extra field as a fallback
    """
    if not mw.col:
        return False
    try:
        nids = mw.col.find_notes(f'"{zotero_key}"')
        if nids:
            return True
    except Exception:
        pass
    # Fallback: direct SQL scan — catches notes added in the same session
    # whose FTS index hasn't been flushed yet
    try:
        rows = mw.col.db.list(
            "SELECT id FROM notes WHERE flds LIKE ?",
            f"%{zotero_key}%"
        )
        return len(rows) > 0
    except Exception:
        return False


# ============================================================
# Note creation
# ============================================================

def _create_note(text, reference, back_extra, tags_str):
    """Create a topic note in the Topics deck WITHOUT IR-Data.
    IR-Data will be initialized by Prepare Topics or Study Main."""
    if not mw.col:
        return False
    deck = _cfg("topics_deck")
    model = mw.col.models.by_name(_cfg("topic_note_type"))
    if not model:
        return False

    nn = Note(mw.col, model)
    fnames = [f["name"] for f in model["flds"]]
    if "Text" in fnames:
        nn["Text"] = text
    if "Reference" in fnames:
        nn["Reference"] = reference
    if "Back Extra" in fnames:
        nn["Back Extra"] = back_extra
    # IR-Data left empty — Prepare Topics will initialize it

    nn.tags = [t.strip() for t in tags_str.split() if t.strip()]

    did = mw.col.decks.id_for_name(deck)
    if did is None:
        did = mw.col.decks.id_for_name("Default")
    nn.note_type()["did"] = did
    mw.col.addNote(nn)
    return True


# ============================================================
# State management (local, version tracking only)
# ============================================================

def _state_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".zotero_state.json")


def _load_state():
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s):
    try:
        with open(_state_path(), "w") as f:
            json.dump(s, f)
    except Exception:
        pass


def _clean_annotation_text(text):
    """Remove duplicate words/phrases caused by Zotero PDF extraction artifacts.
    
    Zotero sometimes duplicates bold/italic text or margin labels into the
    annotation text, producing patterns like:
      "experimental descriptive models data" (should be "experimental data")
      "on the other mechanistic models hand" (should be "on the other hand")
    
    Strategy: find sequences of 2-4 words that appear twice in close proximity
    and remove the duplicate occurrence. Processed line-by-line to preserve
    newline structure.
    """
    if not text:
        return text

    def _dedup_line(line):
        words = line.split()
        if len(words) < 6:
            return line
        result = list(words)
        i = 0
        while i < len(result) - 3:
            for plen in range(2, 5):
                if i + plen * 2 > len(result):
                    break
                phrase = result[i:i + plen]
                for j in range(i + 1, min(i + plen + 3, len(result) - plen + 1)):
                    if result[j:j + plen] == phrase:
                        del result[j:j + plen]
                        break
            i += 1
        return " ".join(result)

    return "\n".join(_dedup_line(line) for line in text.split("\n"))


# ============================================================
# Main sync
# ============================================================

def sync():
    """Sync from Zotero. Returns (sources_created, extracts_created)."""
    lib_id = _cfg("zotero_library_id")
    api_key = _cfg("zotero_api_key")
    if not lib_id or not api_key:
        showInfo("Zotero: Set Library ID and API Key in IR → Zotero Settings first.")
        return 0, 0

    state = _load_state()
    _cache.clear()

    cur_ver = _lib_version()
    if cur_ver is None:
        showInfo("Zotero: Failed to connect. Check your Library ID and API Key.")
        return 0, 0

    # First run: set baseline to current version (no big initial pull)
    if "last_sync" not in state:
        state["last_sync"] = cur_ver
        _save_state(state)
        showInfo(f"Zotero: First run — baseline set to version {cur_ver}.\nFuture syncs will pull new changes.")
        return 0, 0

    start_ver = state["last_sync"]
    if start_ver >= cur_ver:
        tooltip("Zotero: Already up to date.")
        return 0, 0

    items = _fetch_since(start_ver)
    if not items:
        state["last_sync"] = cur_ver
        _save_state(state)
        tooltip("Zotero: No new items.")
        return 0, 0

    for it in items:
        _cache[it["key"]] = it

    import_tag = (_cfg("zotero_import_tag") or "imported").lower()
    hl_color = (_cfg("zotero_highlight_color") or "#ffd400").lower()
    src_tag = _cfg("source_tag") or "ir::source"
    ext_tag = _cfg("extract_tag") or "ir::extract"
    sources, extracts = 0, 0

    # Pass 1: Sources
    for it in items:
        d = it["data"]
        key = it["key"]
        if d.get("itemType", "") in ("attachment", "note", "annotation"):
            continue
        if not any(t.get("tag", "").lower() == import_tag for t in d.get("tags", [])):
            continue
        if _exists(key):
            continue

        title, year, authors = _item_data(d)
        ref, tag, ra = _fmt_authors(authors, year, title)
        yr = f" ({year})" if year else ""
        text = _fmt_math(f"{title}, {ra}{yr}")
        back = f'<a href="zotero://select/library/items/{key}">SourceID: {key}</a>'
        url = d.get("url", "").strip()
        if url:
            back += f'<br><a href="{url}">{url}</a>'
        tags = f"{tag} {src_tag}"

        if _create_note(text, ref, back, tags):
            sources += 1

    # Pass 2: Annotations → extracts
    # Handles: (a) colored highlights, (b) sticky note annotations
    for it in items:
        d = it["data"]
        key = it["key"]
        itype = d.get("itemType", "")

        if itype == "annotation":
            ann_type = d.get("annotationType", "")
            ann_color = (d.get("annotationColor") or "").lower()
            hl_text = (d.get("annotationText") or "").strip()
            comment = (d.get("annotationComment") or "").strip()

            # Determine if this annotation should be imported:
            # (a) Highlight with matching color and text
            # (b) Sticky note annotation (annotationType="note") with comment
            is_highlight = hl_text and ann_color == hl_color
            is_sticky_note = ann_type == "note" and comment

            if not is_highlight and not is_sticky_note:
                continue
            if _exists(key):
                continue

            parent = _bib_parent(key)
            if not parent:
                continue
            # Only import extracts whose parent article has the import tag
            if not any(t.get("tag", "").lower() == import_tag for t in parent["data"].get("tags", [])):
                continue
            title, year, authors = _item_data(parent["data"])
            ref, tag, _ = _fmt_authors(authors, year, title)

            # Build the text content
            if is_highlight:
                combined = _clean_annotation_text(hl_text)
                if comment:
                    combined += f"<br><br>{comment}"
            else:
                # Sticky note: comment is the main content
                combined = comment
            # \xa0 (non-breaking space) is used by Zotero as a line separator;
            # strip it before converting newlines so it doesn't swallow breaks
            combined = combined.replace("\xa0\n", "\n").replace("\xa0", " ")
            combined = combined.replace("\n\n", "<br><br>").replace("\n", "<br>")

            pk = parent["key"]
            back = (f'<a href="zotero://select/library/items/{pk}">SourceID: {pk}</a>'
                    f'<br><a href="zotero://select/library/items/{key}">NoteID: {key}</a>')
            tags = f"{tag} {ext_tag}"

            if _create_note(_fmt_math(combined), ref, back, tags):
                extracts += 1

        elif itype == "note":
            note_html = d.get("note", "")
            plain = _note_html_to_anki(note_html)
            if not plain:
                continue
            if _exists(key):
                continue

            parent = _bib_parent(key)
            if not parent:
                continue
            # Only import notes whose parent article has the import tag
            if not any(t.get("tag", "").lower() == import_tag for t in parent["data"].get("tags", [])):
                continue
            title, year, authors = _item_data(parent["data"])
            ref, tag, _ = _fmt_authors(authors, year, title)

            pk = parent["key"]
            back = (f'<a href="zotero://select/library/items/{pk}">SourceID: {pk}</a>'
                    f'<br><a href="zotero://select/library/items/{key}">NoteID: {key}</a>')
            tags = f"{tag} {ext_tag}"

            if _create_note(_fmt_math(plain), ref, back, tags):
                extracts += 1

    state["last_sync"] = cur_ver
    _save_state(state)
    return sources, extracts


def reset_state():
    """Reset sync state. Next sync sets a new baseline."""
    _save_state({})
    tooltip("Zotero sync state reset. Next sync will set a new baseline.")
