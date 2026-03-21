"""Minimal settings dialog. Keyboard-friendly, no fancy UI."""

from aqt import mw
from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                     QSpinBox, QCheckBox, QPushButton, QGroupBox, QFormLayout)


def show_settings():
    conf = mw.addonManager.getConfig(__name__.split(".")[0]) or {}
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
        "key_edit_last": "Shift+e", "key_undo_text": "Ctrl+z",
        "key_prepare": "Ctrl+Shift+p",
    }
    for k, v in defaults.items():
        if k not in conf: conf[k] = v

    dlg = QDialog(mw)
    dlg.setWindowTitle("Incremental Reading — Settings")
    dlg.setMinimumWidth(480)
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
        ("topic_ratio", "Topic ratio in IR session (%)", "int"),
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
    ])

    add_group("Highlight Colors", [
        ("highlight_extract", "Extract highlight", "str"),
        ("highlight_cloze", "Cloze highlight", "str"),
    ])

    # Buttons
    btn_row = QHBoxLayout()
    save_btn = QPushButton("Save")
    cancel_btn = QPushButton("Cancel")
    btn_row.addStretch()
    btn_row.addWidget(save_btn)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)

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

    save_btn.clicked.connect(save)
    cancel_btn.clicked.connect(dlg.reject)
    dlg.setLayout(layout)
    dlg.exec()
