"""Minimal settings dialog. Keyboard-friendly, scrollable."""

from aqt import mw
from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                     QSpinBox, QCheckBox, QPushButton, QGroupBox, QFormLayout,
                     QScrollArea, QWidget)


def show_settings():
    conf = mw.addonManager.getConfig(__name__.split(".")[0]) or {}
    defaults = {
        "topics_deck": "Main::Topics", "items_deck": "Main::Items",
        "topic_note_type": "Extracts", "cloze_note_type": "Cloze",
        "initial_interval": 1, "default_priority": 50, "randomization_degree": 5,
        "auto_postpone": True, "postpone_protection": 10, "mercy_days": 14,
        "topic_item_ratio": 5,
        "source_cap_default": 0, "extract_cap_default": 0,
        "source_default_interval": 3, "extract_priority_offset": 2,
        "source_tag": "ir::source", "extract_tag": "ir::extract",
        "highlight_extract": "#5b9bd5", "highlight_cloze": "#c9a227",
        "key_extract": "x", "key_cloze": "z", "key_priority": "Shift+p",
        "key_priority_up": "Alt+Up", "key_priority_down": "Alt+Down",
        "key_reschedule": "Shift+j", "key_execute_rep": "Shift+r",
        "key_postpone": "Shift+w", "key_done": "Shift+d", "key_forget": "Shift+f",
        "key_later_today": "Shift+l", "key_advance_today": "Shift+a",
        "key_edit_last": "Shift+e", "key_undo_text": "Alt+z",
        "key_prepare": "Ctrl+Shift+p", "key_zotero_sync": "Ctrl+Shift+y",
        "zotero_library_id": "", "zotero_api_key": "",
        "zotero_import_tag": "IR", "zotero_highlight_color": "#ffd400",
        "zotero_extract_cutoff_date": "2026-06-13",
    }
    for k, v in defaults.items():
        if k not in conf: conf[k] = v

    dlg = QDialog(mw)
    dlg.setWindowTitle("Incremental Reading — Settings")
    dlg.setMinimumWidth(480)
    dlg.setMinimumHeight(400)
    dlg_layout = QVBoxLayout()

    # Scrollable content area
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    container = QWidget()
    layout = QVBoxLayout()

    widgets = {}

    def add_group(title, fields):
        grp = QGroupBox(title)
        form = QFormLayout()
        for key, label, typ in fields:
            if typ == "str":
                w = QLineEdit(str(conf[key]))
                widgets[key] = w
                form.addRow(label, w)
            elif typ == "int":
                w = QSpinBox()
                w.setRange(0, 9999)
                w.setValue(int(conf[key]))
                widgets[key] = w
                form.addRow(label, w)
            elif typ == "bool":
                w = QCheckBox()
                w.setChecked(bool(conf[key]))
                widgets[key] = w
                form.addRow(label, w)
        grp.setLayout(form)
        layout.addWidget(grp)

    add_group("Decks & Note Types", [
        ("topics_deck", "Topics deck", "str"),
        ("items_deck", "Items deck", "str"),
        ("topic_note_type", "Topic note type", "str"),
        ("cloze_note_type", "Cloze note type", "str"),
    ])
    add_group("Scheduling", [
        ("initial_interval", "Initial interval (days)", "int"),
        ("default_priority", "Default priority (0-100)", "int"),
        ("randomization_degree", "Randomization (0-100)", "int"),
        ("topic_item_ratio", "Items per topic (interleave ratio)", "int"),
        ("source_default_interval", "Source interval (days, fixed cadence)", "int"),
        ("extract_priority_offset", "Extract priority offset (parent − N)", "int"),
        ("source_cap_default", "Source default interval cap (days, 0=none)", "int"),
        ("extract_cap_default", "Extract default interval cap (days, 0=none)", "int"),
    ])
    add_group("Overload", [
        ("auto_postpone", "Auto-postpone on session start", "bool"),
        ("postpone_protection", "Protection (top N%)", "int"),
        ("mercy_days", "Mercy days", "int"),
    ])
    add_group("Tags", [
        ("source_tag", "Source tag", "str"),
        ("extract_tag", "Extract tag", "str"),
    ])
    add_group("Shortcuts (during topic review)", [
        ("key_extract", "Extract selection", "str"),
        ("key_cloze", "Create cloze", "str"),
        ("key_priority", "Set priority", "str"),
        ("key_priority_up", "Priority up (-5%)", "str"),
        ("key_priority_down", "Priority down (+5%)", "str"),
        ("key_reschedule", "Reschedule (+days)", "str"),
        ("key_execute_rep", "Execute repetition", "str"),
        ("key_postpone", "Postpone (1.5x)", "str"),
        ("key_later_today", "Later today", "str"),
        ("key_advance_today", "Advance to today", "str"),
        ("key_done", "Done", "str"),
        ("key_forget", "Forget/park", "str"),
        ("key_edit_last", "Edit last created", "str"),
        ("key_undo_text", "Undo text change", "str"),
        ("key_prepare", "Prepare topics", "str"),
        ("key_zotero_sync", "Sync from Zotero", "str"),
    ])
    add_group("Highlight Colors", [
        ("highlight_extract", "Extract highlight", "str"),
        ("highlight_cloze", "Cloze highlight", "str"),
    ])
    add_group("Zotero Integration", [
        ("zotero_library_id", "Library ID", "str"),
        ("zotero_api_key", "API Key", "str"),
        ("zotero_import_tag", "Import tag (sources)", "str"),
        ("zotero_highlight_color", "Highlight color (extracts)", "str"),
        ("zotero_extract_cutoff_date", "Extract cutoff date (YYYY-MM-DD)", "str"),
    ])

    container.setLayout(layout)
    scroll.setWidget(container)
    dlg_layout.addWidget(scroll)

    # Zotero maintenance actions (kept separate from Save/Cancel)
    zotero_resync_btn = QPushButton("Full Re-scan from Zotero")
    zotero_resync_btn.setToolTip("Ignore incremental state and re-scan the whole library: "
                                 "imports any missing tagged sources and extracts created on/after "
                                 "the cutoff date. Additive — never deletes. Use after a wipe or to backfill.")
    zotero_reset_btn = QPushButton("Reset Zotero Sync State")
    zotero_reset_btn.setToolTip("Clears the sync version baseline. The next sync becomes a full re-scan.")
    zotero_reset_row = QHBoxLayout()
    zotero_reset_row.addWidget(zotero_resync_btn)
    zotero_reset_row.addWidget(zotero_reset_btn)
    zotero_reset_row.addStretch()
    dlg_layout.addLayout(zotero_reset_row)

    def do_zotero_resync():
        from aqt.utils import tooltip
        from .zotero_sync import full_resync
        try:
            tooltip("Zotero: Full re-scan…")
            s, e = full_resync()
            tooltip(f"Zotero (full): {s} sources, {e} extracts created.")
        except Exception as ex:
            tooltip(f"Zotero error: {ex}")

    def do_zotero_reset():
        from .zotero_sync import reset_state
        reset_state()

    zotero_resync_btn.clicked.connect(do_zotero_resync)
    zotero_reset_btn.clicked.connect(do_zotero_reset)

    # Buttons (outside scroll area, always visible)
    btn_row = QHBoxLayout()
    restore_btn = QPushButton("Restore Defaults")
    restore_btn.setToolTip("Reset all settings to their default values.")
    save_btn = QPushButton("Save")
    cancel_btn = QPushButton("Cancel")
    btn_row.addWidget(restore_btn)
    btn_row.addStretch()
    btn_row.addWidget(save_btn)
    btn_row.addWidget(cancel_btn)
    dlg_layout.addLayout(btn_row)

    def save():
        for k, w in widgets.items():
            if isinstance(w, QLineEdit):
                conf[k] = w.text()
            elif isinstance(w, QSpinBox):
                conf[k] = w.value()
            elif isinstance(w, QCheckBox):
                conf[k] = w.isChecked()
        mw.addonManager.writeConfig(__name__.split(".")[0], conf)
        dlg.accept()

    def do_restore_defaults():
        from aqt.utils import askUser
        if not askUser("Restore all Incremental Reading settings to their defaults?\n"
                       "This overwrites your current settings."):
            return
        addon = __name__.split(".")[0]
        # Prefer the shipped config.json defaults; fall back to the built-in map.
        try:
            shipped = mw.addonManager.addonConfigDefaults(addon)
        except Exception:
            shipped = None
        new_conf = dict(shipped) if shipped else {**conf, **defaults}
        mw.addonManager.writeConfig(addon, new_conf)
        dlg.accept()
        show_settings()  # reopen with the restored values

    restore_btn.clicked.connect(do_restore_defaults)
    save_btn.clicked.connect(save)
    cancel_btn.clicked.connect(dlg.reject)
    dlg.setLayout(dlg_layout)
    dlg.exec()
