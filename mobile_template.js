// ============================================================
// AnkiMobile IR — User Actions for Extract & Cloze creation
// ============================================================
//
// Paste this script into your "Extracts" card template (Back Template)
// inside a <script> tag. Then in AnkiMobile:
//   Settings → Review → User Action 1 → assign to a button/swipe/tap
//   Settings → Review → User Action 2 → assign to a button/swipe/tap
//
// User Action 1 = Extract (creates new topic from selected text)
// User Action 2 = Cloze (creates new cloze item from selected text)
//
// Requirements:
//   - Note types "Extracts" and "Cloze" must exist
//   - Decks "Main::Topics" and "Main::Items" must exist
//   - Fields: Text, Reference, Back Extra
// ============================================================

// --- Configuration (must match your Anki setup) ---
var IR_TOPICS_DECK = "Main::Topics";
var IR_ITEMS_DECK = "Main::Items";
var IR_EXTRACT_TYPE = "Extracts";
var IR_CLOZE_TYPE = "Cloze";
var IR_SOURCE_TAG = "ir::source";
var IR_EXTRACT_TAG = "ir::extract";
var IR_HIGHLIGHT_COLOR = "#5b9bd5";
var IR_CLOZE_COLOR = "#c9a227";

// --- Helper: get selected text/HTML ---
function irGetSelection() {
  var sel = window.getSelection();
  if (!sel || sel.isCollapsed) return null;
  var range = sel.getRangeAt(0);
  var div = document.createElement("div");
  div.appendChild(range.cloneContents());
  return {
    html: div.innerHTML.trim(),
    text: sel.toString().trim(),
    range: range
  };
}

// --- Helper: get field content from the rendered card ---
// Anki renders fields into the card HTML. We read them from the DOM.
function irGetField(fieldName) {
  // Try data attribute approach (some templates use this)
  var el = document.querySelector('[data-field="' + fieldName + '"]');
  if (el) return el.innerHTML.trim();
  // Fallback: for standard templates, fields are in specific divs
  return "";
}

// --- Helper: URL-encode ---
function irEncode(str) {
  return encodeURIComponent(str || "");
}

// --- Helper: get current card's reference and back extra ---
// These are stored in template replacement tags. We use a hidden div trick.
// The card template should include hidden spans with field content:
//   <span id="ir-ref" style="display:none">{{Reference}}</span>
//   <span id="ir-back" style="display:none">{{Back Extra}}</span>
//   <span id="ir-tags" style="display:none">{{Tags}}</span>
function irFieldFromHidden(id) {
  var el = document.getElementById(id);
  return el ? el.innerHTML.trim() : "";
}

// --- Helper: highlight selection visually (cosmetic, session-only) ---
function irHighlight(range, color) {
  try {
    var span = document.createElement("span");
    span.style.backgroundColor = color;
    span.style.color = "#fff";
    span.style.borderRadius = "2px";
    range.surroundContents(span);
    window.getSelection().removeAllRanges();
  } catch (e) {
    // surroundContents fails if selection crosses element boundaries
  }
}

// --- User Action 1: Extract ---
var userJs1 = function() {
  var sel = irGetSelection();
  if (!sel) { alert("Select text first"); return; }

  var reference = irFieldFromHidden("ir-ref");
  var backExtra = irFieldFromHidden("ir-back");
  var tags = irFieldFromHidden("ir-tags") || "";

  // Remove source tag, add extract tag
  var tagList = tags.split(/\s+/).filter(function(t) { return t && t !== IR_SOURCE_TAG; });
  if (tagList.indexOf(IR_EXTRACT_TAG) === -1) tagList.push(IR_EXTRACT_TAG);
  var tagStr = tagList.join(" ");

  // Build the addnote URL
  var url = "anki://x-callback-url/addnote?"
    + "type=" + irEncode(IR_EXTRACT_TYPE)
    + "&deck=" + irEncode(IR_TOPICS_DECK)
    + "&fldText=" + irEncode(sel.html)
    + "&fldReference=" + irEncode(reference)
    + "&fldBack%20Extra=" + irEncode(backExtra)
    + "&tags=" + irEncode(tagStr)
    + "&dupes=1";

  // Cosmetic highlight
  irHighlight(sel.range, IR_HIGHLIGHT_COLOR);

  // Create the note
  window.location.href = url;
};

// --- User Action 2: Cloze ---
var userJs2 = function() {
  var sel = irGetSelection();
  if (!sel) { alert("Select keyword first"); return; }

  var reference = irFieldFromHidden("ir-ref");
  var backExtra = irFieldFromHidden("ir-back");

  // Build cloze text: get the full line/sentence containing the selection
  // For simplicity on mobile, use the selected text as the cloze keyword
  // and the visible text content as context
  var parentEl = sel.range.startContainer.parentElement;
  var context = "";
  if (parentEl) {
    // Walk up to find a block-level element for context
    var block = parentEl;
    while (block && block !== document.body) {
      var display = window.getComputedStyle(block).display;
      if (display === "block" || display === "list-item" || block.tagName === "P" || block.tagName === "DIV" || block.tagName === "LI") break;
      block = block.parentElement;
    }
    if (block && block !== document.body) {
      context = block.textContent.trim();
    }
  }
  // If no block context found, use a wider text grab
  if (!context) context = sel.text;

  // Create the cloze: replace the selected keyword in the context
  var clozeText = context.replace(sel.text, "{{c1::" + sel.text + "}}");
  // If replacement didn't work (e.g., HTML entities), just wrap it
  if (clozeText === context) clozeText = "{{c1::" + sel.text + "}}";

  // No IR tags for clozes (they're items, not topics)
  var tags = irFieldFromHidden("ir-tags") || "";
  var tagList = tags.split(/\s+/).filter(function(t) {
    return t && t !== IR_SOURCE_TAG && t !== IR_EXTRACT_TAG;
  });
  var tagStr = tagList.join(" ");

  var url = "anki://x-callback-url/addnote?"
    + "type=" + irEncode(IR_CLOZE_TYPE)
    + "&deck=" + irEncode(IR_ITEMS_DECK)
    + "&fldText=" + irEncode(clozeText)
    + "&fldReference=" + irEncode(reference)
    + "&fldBack%20Extra=" + irEncode(backExtra)
    + "&tags=" + irEncode(tagStr)
    + "&dupes=1";

  // Cosmetic highlight
  irHighlight(sel.range, IR_CLOZE_COLOR);

  // Create the note
  window.location.href = url;
};
