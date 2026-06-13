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
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import quote

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
        "zotero_import_tag": "IR", "zotero_highlight_color": "#ffd400",
        "zotero_extract_cutoff_date": "2026-06-13",
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


def _fetch_tagged(tag):
    """Fetch ALL items carrying a given tag (version-independent).

    This is the reliable way to detect items the user just tagged: it does not
    depend on library-version bookkeeping, so 'tag it → sync → imported' always
    works, even on a freshly reset state.
    """
    items, start = [], 0
    enc = quote(tag, safe="")
    while True:
        r = _api(f"items?tag={enc}&limit=100&start={start}")
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


def _children(key):
    """Fetch direct child items of an item (attachments, notes, annotations)."""
    items, start = [], 0
    while True:
        r = _api(f"items/{key}/children?limit=100&start={start}")
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


def _descendant_annotations(source_key):
    """All notes/annotations belonging to a source.

    Source → child notes (standalone notes) and child attachments;
    attachment → annotations (highlights, sticky notes). Walks two levels deep,
    which covers Zotero's PDF/HTML annotation structure.
    """
    out = []
    for ch in _children(source_key):
        t = ch["data"].get("itemType", "")
        if t in ("note", "annotation"):
            out.append(ch)
        elif t == "attachment":
            for gch in _children(ch["key"]):
                if gch["data"].get("itemType", "") in ("note", "annotation"):
                    out.append(gch)
    return out


def _fetch_recent(item_type, cutoff):
    """Fetch items of a type, newest first, stopping once we pass the cutoff.

    Because results are sorted by dateAdded descending, we can stop paginating
    as soon as we hit an item older than the cutoff. This keeps the sync fast:
    we only ever pull the annotations/notes created on or after the cutoff
    instead of the entire library.
    """
    items, start = [], 0
    while True:
        r = _api(f"items?itemType={item_type}&sort=dateAdded&direction=desc&limit=100&start={start}")
        if not r or r[0] != 200:
            break
        batch = json.loads(r[2])
        if not batch:
            break
        stop = False
        for it in batch:
            dadd = (it["data"].get("dateAdded") or "")[:10]
            if cutoff and dadd and dadd < cutoff:
                stop = True
                break
            items.append(it)
        if stop or len(batch) < 100:
            break
        start += 100
    return items


def _imported_keys(src_tag, ext_tag):
    """Collect, in ONE query, the Zotero keys already imported.

    Returns (source_keys, all_keys). Scans only IR notes (by tag) once and
    regex-extracts their SourceID/NoteID keys, instead of running a full-table
    'flds LIKE' scan per item — which is what made re-syncs extremely slow.
    """
    source_keys, all_keys = set(), set()
    if not mw.col:
        return source_keys, all_keys
    try:
        rows = mw.col.db.all(
            "SELECT flds, tags FROM notes WHERE tags LIKE ? OR tags LIKE ?",
            f"%{src_tag}%", f"%{ext_tag}%",
        )
    except Exception:
        rows = []
    pat = re.compile(r"(?:SourceID|NoteID):\s*([A-Za-z0-9]{6,})")
    for flds, tags in rows:
        found = pat.findall(flds or "")
        for k in found:
            all_keys.add(k)
        if src_tag in (tags or ""):
            for k in found:
                source_keys.add(k)
    return source_keys, all_keys


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


def _split_table_cells(row):
    """Split a markdown table row on | delimiters, protecting | inside $...$."""
    row = row.strip().strip("|")
    cells = []
    current = ""
    in_math = False
    for ch in row:
        if ch == "$" and not in_math:
            in_math = True
            current += ch
        elif ch == "$" and in_math:
            in_math = False
            current += ch
        elif ch == "|" and not in_math:
            cells.append(current.strip())
            current = ""
        else:
            current += ch
    cells.append(current.strip())
    return cells


def _md_to_html(text):
    """Convert common Markdown to HTML. Runs before _fmt_math.

    Handles: headings, bold, italic, hr, blockquotes, unordered lists, tables.
    Preserves $...$ and $$...$$ math untouched.
    """
    if not text:
        return ""
    lines = text.split("\n")
    out = []
    in_table = False
    for line in lines:
        stripped = line.strip()

        # Markdown table row: | col | col |
        if re.match(r"^\|.*\|$", stripped):
            # Skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", stripped):
                continue
            cells = _split_table_cells(stripped)
            if not in_table:
                in_table = True
                out.append("<table>")
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        else:
            if in_table:
                in_table = False
                out.append("</table>")

        # Headings: ## Title → <b>Title</b>
        hm = re.match(r"^(#{1,6})\s+(.+)", stripped)
        if hm:
            out.append(f"<b>{hm.group(2)}</b>")
            continue
        # Horizontal rule
        if re.match(r"^-{3,}$", stripped) or re.match(r"^\*{3,}$", stripped):
            out.append("")
            continue
        # Blockquote
        if stripped.startswith("> "):
            out.append(stripped[2:])
            continue
        # Unordered list item
        lm = re.match(r"^[-*]\s+(.+)", stripped)
        if lm:
            out.append(f"• {lm.group(1)}")
            continue
        out.append(line)

    if in_table:
        out.append("</table>")

    text = "\n".join(out)
    # Bold: **text** → <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic: *text* → <i>text</i>
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    return text


def _fmt_math(text):
    if not text:
        return ""
    # Display math: $$...$$ → \[...\]  (must run before inline; require non-empty content)
    text = re.sub(r"\$\$(.+?)\$\$", lambda m: "\\[" + m.group(1) + "\\]", text, flags=re.DOTALL)
    # Inline math: $...$ → \(...\)  (non-empty, no $ or newline inside to prevent runaway matches)
    # Inline math: $...$ → \(...\)
    # Heuristic to avoid matching currency ($100 ... $200):
    # - Contains a LaTeX command (\word) → definitely math
    # - Starts with digit(s) then space/comma → likely currency ($100 in...)
    # - No spaces → single token like $x$, $3$, $u(w)$
    # - Short with math operators → expression like $x^2 + y^2$
    def _inline_math(m):
        inner = m.group(1)
        if "\\" in inner:
            return "\\(" + inner + "\\)"
        if re.match(r"^\d+[\s,]", inner):
            return m.group(0)
        if " " not in inner.strip():
            return "\\(" + inner + "\\)"
        if len(inner) <= 30 and re.search(r"[+\-*/^_=<>()]", inner):
            return "\\(" + inner + "\\)"
        return m.group(0)

    text = re.sub(r"\$([^$\n]+?)\$", _inline_math, text)
    return text


def _strip_html(h):
    return re.sub(r"<[^>]+>", "", h).replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _newlines_to_br(text):
    """Convert newlines to HTML <br> tags and strip leading/trailing breaks."""
    text = text.replace("\n\n", "<br><br>").replace("\n", "<br>")
    text = re.sub(r"^(<br>)+", "", text)
    text = re.sub(r"(<br>)+$", "", text)
    return text.strip()


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
    # Strip leading/trailing whitespace (leave \n intact for _fmt_math)
    html = re.sub(r"^\n+", "", html)
    html = re.sub(r"\n+$", "", html)
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


def _source_exists(zotero_key, src_tag):
    """Check whether a SOURCE note for this Zotero key already exists.

    A source's key (SourceID) is also embedded in every one of its extracts, so a
    plain key search would report the source as 'existing' whenever any extract
    survives. We therefore require the note to also carry the source tag, which
    extracts never have. This keeps source import independent of extracts.
    """
    if not mw.col:
        return False
    try:
        rows = mw.col.db.list(
            "SELECT id FROM notes WHERE flds LIKE ? AND tags LIKE ?",
            f"%{zotero_key}%", f"%{src_tag}%"
        )
        return len(rows) > 0
    except Exception:
        # Conservative fallback: treat as missing so we don't silently skip it.
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
    """Sync from Zotero. Returns (sources_created, extracts_created).

    Tag-driven and version-independent: every item carrying the import tag is
    scanned (and its annotations/notes), importing whatever is not already in
    the collection. This makes 'tag an item → sync → imported' reliable, even
    right after resetting the sync state.
    """
    lib_id = _cfg("zotero_library_id")
    api_key = _cfg("zotero_api_key")
    if not lib_id or not api_key:
        showInfo("Zotero: Set Library ID and API Key in IR → Zotero Settings first.")
        return 0, 0

    _cache.clear()
    state = _load_state()

    # Validate credentials / connectivity (also used to stamp last_sync).
    cur_ver = _lib_version()
    if cur_ver is None:
        showInfo("Zotero: Failed to connect. Check your Library ID and API Key.")
        return 0, 0

    import_tag = _cfg("zotero_import_tag") or "IR"
    hl_color = (_cfg("zotero_highlight_color") or "#ffd400").lower()
    src_tag = _cfg("source_tag") or "ir::source"
    ext_tag = _cfg("extract_tag") or "ir::extract"
    # Extracts (annotations/notes) are only imported if created on/after this
    # cutoff date. Sources always import. This guards against re-importing old
    # highlights after a wipe-and-resync.
    cutoff = (_cfg("zotero_extract_cutoff_date") or "").strip()

    # Prefetch already-imported keys ONCE (fast), instead of scanning per item.
    source_keys, existing_keys = _imported_keys(src_tag, ext_tag)

    tagged = _fetch_tagged(import_tag)
    # Sources = bibliographic (top-level) items carrying the tag.
    source_items = [it for it in tagged
                    if it["data"].get("itemType", "") not in ("attachment", "note", "annotation")]

    if not source_items:
        state["last_sync"] = cur_ver
        _save_state(state)
        showInfo(f"Zotero: no items tagged '{import_tag}' were found.\n"
                 f"Make sure the tag matches exactly (it is case-sensitive) and that "
                 f"the item is tagged in Zotero, then sync.")
        return 0, 0

    sources, extracts = 0, 0
    tagged_source_keys = set()

    # Pass 1: sources (always imported if missing).
    for src in source_items:
        skey = src["key"]
        _cache[skey] = src
        tagged_source_keys.add(skey)
        d = src["data"]
        if skey in source_keys:
            continue
        title, year, authors = _item_data(d)
        ref, tag, ra = _fmt_authors(authors, year, title)
        yr = f" ({year})" if year else ""
        text = _fmt_math(f"{title}, {ra}{yr}")
        back = f'<a href="zotero://select/library/items/{skey}">SourceID: {skey}</a>'
        url = d.get("url", "").strip()
        if url:
            back += f'<br><a href="{url}">{url}</a>'
        if _create_note(text, ref, back, f"{tag} {src_tag}"):
            sources += 1
            source_keys.add(skey)

    # Pass 2: extracts. Only recent annotations/notes (>= cutoff) are pulled,
    # in bulk, then matched to a tagged source via their parent chain. This is
    # far faster than enumerating every source's children every sync.
    recent = _fetch_recent("annotation", cutoff) + _fetch_recent("note", cutoff)
    for it in recent:
        ckey = it["key"]
        cd = it["data"]
        itype = cd.get("itemType", "")
        if ckey in existing_keys:
            continue
        created = (cd.get("dateAdded") or cd.get("dateModified") or "")
        if cutoff and created and created[:10] < cutoff:
            continue

        _cache[ckey] = it
        parent = _bib_parent(ckey)
        if not parent or parent["key"] not in tagged_source_keys:
            continue
        skey = parent["key"]
        title, year, authors = _item_data(parent["data"])
        ref, tag, _ = _fmt_authors(authors, year, title)

        if itype == "annotation":
            ann_type = cd.get("annotationType", "")
            ann_color = (cd.get("annotationColor") or "").lower()
            hl_text = (cd.get("annotationText") or "").strip()
            comment = (cd.get("annotationComment") or "").strip()
            is_highlight = hl_text and ann_color == hl_color
            is_sticky_note = ann_type == "note" and comment
            if not is_highlight and not is_sticky_note:
                continue

            if is_highlight:
                combined = _clean_annotation_text(hl_text)
                if comment:
                    combined += f"<br><br>{comment}"
            else:
                combined = comment
            combined = combined.replace("\xa0\n", "\n").replace("\xa0", " ")
            combined = _md_to_html(combined)
            combined = _fmt_math(combined)
            combined = _newlines_to_br(combined)

            att_key = cd.get("parentItem", "")
            page = cd.get("annotationPageLabel", "")
            note_url = (f"zotero://open-pdf/library/items/{att_key}?page={page}&annotation={ckey}"
                        if att_key else f"zotero://select/library/items/{ckey}")
            back = (f'<a href="zotero://select/library/items/{skey}">SourceID: {skey}</a>'
                    f'<br><a href="{note_url}">NoteID: {ckey}</a>')
            if _create_note(combined, ref, back, f"{tag} {ext_tag}"):
                extracts += 1
                existing_keys.add(ckey)

        elif itype == "note":
            plain = _note_html_to_anki(cd.get("note", ""))
            if not plain:
                continue
            back = (f'<a href="zotero://select/library/items/{skey}">SourceID: {skey}</a>'
                    f'<br><a href="zotero://select/library/items/{ckey}">NoteID: {ckey}</a>')
            if _create_note(_newlines_to_br(_fmt_math(plain)), ref, back, f"{tag} {ext_tag}"):
                extracts += 1
                existing_keys.add(ckey)

    state["last_sync"] = cur_ver
    _save_state(state)
    return sources, extracts


def reset_state():
    """Reset sync state. Sync is tag-driven, so this only clears bookkeeping;
    the next sync re-scans all tagged items and imports anything missing."""
    _save_state({})
    tooltip("Zotero sync state reset. Next sync re-scans all tagged items.")
