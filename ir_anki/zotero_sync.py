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
import http.client
import threading
from concurrent.futures import ThreadPoolExecutor

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
# Zotero API  (keep-alive, thread-local connection per worker)
# ============================================================

_cache = {}
_tls = threading.local()
_ZHOST = "api.zotero.org"


def _conn():
    c = getattr(_tls, "conn", None)
    if c is None:
        c = http.client.HTTPSConnection(_ZHOST, timeout=30)
        _tls.conn = c
    return c


def _api(endpoint, _retry=True):
    """GET /users/{lib}/{endpoint}, reusing a keep-alive HTTPS connection.

    Reusing the connection avoids a TLS handshake per request, which is a large
    share of per-call latency. On any connection error we drop the socket and
    retry once with a fresh one.
    """
    lib = _cfg("zotero_library_id")
    key = _cfg("zotero_api_key")
    if not lib or not key:
        return None
    path = f"/users/{lib}/{endpoint}"
    try:
        c = _conn()
        c.request("GET", path, headers={
            "Zotero-API-Key": key,
            "Zotero-API-Version": "3",
            "Connection": "keep-alive",
        })
        resp = c.getresponse()
        status = resp.status
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read().decode("utf-8")  # must drain before next request
        return status, hdrs, body
    except Exception:
        try:
            if getattr(_tls, "conn", None) is not None:
                _tls.conn.close()
        except Exception:
            pass
        _tls.conn = None
        if _retry:
            return _api(endpoint, _retry=False)
        return None


def _close_conns():
    try:
        if getattr(_tls, "conn", None) is not None:
            _tls.conn.close()
            _tls.conn = None
    except Exception:
        pass


def _lib_version():
    r = _api("items?limit=1&format=keys")
    if not r or r[0] != 200:
        return None
    return int(r[1].get("last-modified-version", "0"))


def _fetch_items_since(since):
    """Fetch all items changed since a library version, in bulk.

    Uses the Total-Results header from the first page to fetch the remaining
    pages in parallel. On an incremental sync this is a single small request;
    on a full pull (since=0) it pages through the library efficiently.
    """
    r = _api(f"items?since={since}&limit=100&start=0")
    if not r or r[0] != 200:
        return []
    items = json.loads(r[2])
    try:
        total = int(r[1].get("total-results", len(items)))
    except (TypeError, ValueError):
        total = len(items)
    if total <= len(items) or len(items) < 100:
        return items
    starts = list(range(100, total, 100))

    def _page(s):
        rr = _api(f"items?since={since}&limit=100&start={s}")
        if not rr or rr[0] != 200:
            return []
        try:
            return json.loads(rr[2])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=min(6, len(starts))) as ex:
        for chunk in ex.map(_page, starts):
            items.extend(chunk)
    return items


def _items_by_keys(keys):
    """Fetch items by key in batches of 50 (parallel). Returns {key: item}."""
    out = {}
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return out
    batches = [keys[i:i + 50] for i in range(0, len(keys), 50)]

    def _b(batch):
        r = _api(f"items?itemKey={','.join(batch)}&limit=50")
        if not r or r[0] != 200:
            return []
        try:
            return json.loads(r[2])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=min(6, len(batches))) as ex:
        for items in ex.map(_b, batches):
            for it in items:
                out[it["key"]] = it
    return out


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

def sync(full=False):
    """Sync from Zotero. Returns (sources_created, extracts_created).

    Incremental: pulls only items changed since the last successful sync via the
    Zotero version feed (``items?since=...``) — usually a single small request.
    A full pull (``full=True``, or first run / after reset) pages the whole
    library in bulk. Sources are detected by the import tag; extracts
    (annotations/notes) are imported only if created on/after the cutoff date.
    """
    lib_id = _cfg("zotero_library_id")
    api_key = _cfg("zotero_api_key")
    if not lib_id or not api_key:
        showInfo("Zotero: Set Library ID and API Key in IR → Zotero Settings first.")
        return 0, 0

    _cache.clear()
    state = _load_state()

    cur_ver = _lib_version()
    if cur_ver is None:
        _close_conns()
        showInfo("Zotero: Failed to connect. Check your Library ID and API Key.")
        return 0, 0

    import_tag = _cfg("zotero_import_tag") or "IR"
    hl_color = (_cfg("zotero_highlight_color") or "#ffd400").lower()
    src_tag = _cfg("source_tag") or "ir::source"
    ext_tag = _cfg("extract_tag") or "ir::extract"
    cutoff = (_cfg("zotero_extract_cutoff_date") or "").strip()

    last_sync = 0 if full else int(state.get("last_sync", 0) or 0)
    source_keys, existing_keys = _imported_keys(src_tag, ext_tag)

    # Incremental fast path: nothing changed since the last sync.
    if not full and last_sync and last_sync >= cur_ver:
        _close_conns()
        tooltip("Zotero: already up to date.")
        return 0, 0

    changed = _fetch_items_since(last_sync)
    idx = {it["key"]: it for it in changed}

    def _is_tagged_source(it):
        d = it["data"]
        if d.get("itemType", "") in ("attachment", "note", "annotation"):
            return False
        return any(t.get("tag", "") == import_tag for t in d.get("tags", []))

    # Tagged sources = already-imported ones + newly-tagged ones in the feed.
    tagged_source_keys = set(source_keys)
    feed_sources = {}
    for it in changed:
        if _is_tagged_source(it):
            tagged_source_keys.add(it["key"])
            feed_sources[it["key"]] = it

    sources, extracts = 0, 0
    source_meta = {}  # skey -> (ref, tag)

    # Create any missing sources.
    for skey, src in feed_sources.items():
        d = src["data"]
        title, year, authors = _item_data(d)
        ref, tag, ra = _fmt_authors(authors, year, title)
        source_meta[skey] = (ref, tag)
        if skey in source_keys:
            continue
        yr = f" ({year})" if year else ""
        text = _fmt_math(f"{title}, {ra}{yr}")
        back = f'<a href="zotero://select/library/items/{skey}">SourceID: {skey}</a>'
        url = d.get("url", "").strip()
        if url:
            back += f'<br><a href="{url}">{url}</a>'
        if _create_note(text, ref, back, f"{tag} {src_tag}"):
            sources += 1
            source_keys.add(skey)

    # Candidate extracts in the feed.
    annotations = [it for it in changed if it["data"].get("itemType") == "annotation"]
    notes = [it for it in changed if it["data"].get("itemType") == "note"]

    # Resolve attachments referenced by annotations but absent from the feed.
    need_att = {a["data"].get("parentItem") for a in annotations}
    need_att = {k for k in need_att if k and k not in idx}
    fetched_att = _items_by_keys(need_att)

    def _att(k):
        return idx.get(k) or fetched_att.get(k)

    def _source_of_note(n):
        pk = n["data"].get("parentItem")
        item = idx.get(pk) or fetched_att.get(pk)
        if item and item["data"].get("itemType") == "attachment":
            return item["data"].get("parentItem")
        return pk

    # Resolve source items we still need (to check the tag + build references).
    need_src = set()
    for a in annotations:
        att = _att(a["data"].get("parentItem"))
        sk = att["data"].get("parentItem") if att else None
        if sk and sk not in idx and sk not in feed_sources and sk not in source_meta:
            need_src.add(sk)
    for n in notes:
        sk = _source_of_note(n)
        if sk and sk not in idx and sk not in feed_sources and sk not in source_meta:
            need_src.add(sk)
    fetched_src = _items_by_keys(need_src)
    for sk, it in fetched_src.items():
        d = it["data"]
        if d.get("itemType", "") in ("attachment", "note", "annotation"):
            continue
        if sk in source_keys or any(t.get("tag", "") == import_tag for t in d.get("tags", [])):
            tagged_source_keys.add(sk)
        title, year, authors = _item_data(d)
        ref, tag, _ = _fmt_authors(authors, year, title)
        source_meta.setdefault(sk, (ref, tag))

    # Build the work list (item, source_key) for extracts under tagged sources.
    work = []
    for a in annotations:
        att = _att(a["data"].get("parentItem"))
        sk = att["data"].get("parentItem") if att else None
        if sk and sk in tagged_source_keys:
            work.append((a, sk))
    for n in notes:
        sk = _source_of_note(n)
        if sk and sk in tagged_source_keys:
            work.append((n, sk))

    for ch, skey in work:
        ckey = ch["key"]
        cd = ch["data"]
        itype = cd.get("itemType", "")
        if ckey in existing_keys:
            continue
        created = (cd.get("dateAdded") or cd.get("dateModified") or "")
        if cutoff and created and created[:10] < cutoff:
            continue
        ref, tag = source_meta.get(skey, ("", ""))

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
    _close_conns()
    return sources, extracts


def full_resync():
    """Force a full re-scan of the whole library (bulk), honouring the cutoff."""
    return sync(full=True)


def reset_state():
    """Reset sync state. The next sync becomes a full re-scan (since=0),
    importing tagged sources and extracts created on/after the cutoff date."""
    _save_state({})
    tooltip("Zotero sync state reset. Next sync does a full re-scan.")
