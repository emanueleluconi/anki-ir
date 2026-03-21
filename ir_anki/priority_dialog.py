"""Priority dialog with slider + text input. Enter confirms. Keyboard-first."""

from aqt import mw
from aqt.qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                     QSlider, QPushButton, Qt)


def ask_priority(current: float, af: float, interval: int) -> float | None:
    """Show priority dialog. Returns new priority or None if cancelled."""
    dlg = QDialog(mw)
    dlg.setWindowTitle("Set Priority")
    dlg.setMinimumWidth(350)
    layout = QVBoxLayout()

    info = QLabel(f"Current: {current:.1f}%  |  AF: {af:.2f}  |  Interval: {interval}d")
    layout.addWidget(info)

    # Slider
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 10000)  # 0.00 to 100.00
    slider.setValue(int(current * 100))
    layout.addWidget(slider)

    # Text input (focused by default for keyboard-first)
    row = QHBoxLayout()
    row.addWidget(QLabel("Priority (0-100):"))
    inp = QLineEdit(f"{current:.1f}")
    inp.selectAll()
    row.addWidget(inp)
    layout.addLayout(row)

    result = [None]

    def on_slider(val):
        p = val / 100
        inp.setText(f"{p:.1f}")

    def on_text():
        try:
            v = float(inp.text())
            slider.blockSignals(True)
            slider.setValue(int(max(0, min(100, v)) * 100))
            slider.blockSignals(False)
        except ValueError:
            pass

    slider.valueChanged.connect(on_slider)
    inp.textChanged.connect(on_text)

    def accept():
        try:
            result[0] = max(0.0, min(100.0, float(inp.text())))
        except ValueError:
            result[0] = None
        dlg.accept()

    # Enter confirms
    inp.returnPressed.connect(accept)

    btn_row = QHBoxLayout()
    ok = QPushButton("OK")
    ok.clicked.connect(accept)
    cancel = QPushButton("Cancel")
    cancel.clicked.connect(dlg.reject)
    btn_row.addStretch()
    btn_row.addWidget(ok)
    btn_row.addWidget(cancel)
    layout.addLayout(btn_row)

    dlg.setLayout(layout)
    inp.setFocus()
    dlg.exec()
    return result[0]
