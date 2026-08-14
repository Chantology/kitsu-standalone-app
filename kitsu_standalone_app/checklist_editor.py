"""Editable checklist for a comment being written.

Kitsu lets a comment carry a checklist, which is what makes a review pass
actionable — "these four things, then it's approved" rather than a paragraph
someone has to re-read. `gazu.task.add_comment` takes it directly as
`[{"text": ..., "checked": bool}]`, so this widget's only job is to produce
that list.

The same shape comes back on an existing comment, so this also serves as an
editor for one already posted (see CommentPanel's checklist display, which
syncs a single item at a time through `session.update_comment_checklist`).
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class ChecklistEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []  # list of {"checkbox", "line_edit", "container"}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)
        layout.addLayout(self._rows_layout)

        self._empty_label = QLabel("No checklist items")
        self._empty_label.setStyleSheet("color: #666666;")
        layout.addWidget(self._empty_label)

        add_row = QHBoxLayout()
        self.add_button = QPushButton("+ Add item")
        self.add_button.clicked.connect(lambda: self.add_item(focus=True))
        add_row.addWidget(self.add_button)
        add_row.addStretch(1)
        layout.addLayout(add_row)

    # ---- contents ----

    def items(self):
        """The checklist in the shape Kitsu wants, blank rows dropped."""
        return [
            {"text": row["line_edit"].text().strip(), "checked": row["checkbox"].isChecked()}
            for row in self._rows
            if row["line_edit"].text().strip()
        ]

    def set_items(self, items):
        self.clear()
        for item in items or []:
            self.add_item(text=item.get("text", ""), checked=bool(item.get("checked")))

    def clear(self):
        while self._rows:
            self._remove_row(self._rows[0])
        self._update_empty_label()

    def add_item(self, text="", checked=False, focus=False):
        checkbox = QCheckBox()
        checkbox.setChecked(checked)
        checkbox.stateChanged.connect(lambda _state: self.changed.emit())

        line_edit = QLineEdit(text)
        line_edit.setPlaceholderText("Checklist item")
        line_edit.textChanged.connect(lambda _text: self.changed.emit())
        # Enter at the end of a row starts the next one, so a list can be typed
        # straight through without reaching for the mouse.
        line_edit.returnPressed.connect(lambda: self.add_item(focus=True))

        remove_button = QToolButton()
        remove_button.setText("✕")
        remove_button.setAutoRaise(True)
        remove_button.setToolTip("Remove this item")

        container = QWidget()
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(checkbox)
        row_layout.addWidget(line_edit, 1)
        row_layout.addWidget(remove_button)

        row = {"checkbox": checkbox, "line_edit": line_edit, "container": container}
        remove_button.clicked.connect(lambda _checked=False, r=row: self._on_remove_clicked(r))

        self._rows_layout.addWidget(container)
        self._rows.append(row)

        self._update_empty_label()
        if focus:
            line_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.changed.emit()

    # ---- rows ----

    def _on_remove_clicked(self, row):
        self._remove_row(row)
        self._update_empty_label()
        self.changed.emit()

    def _remove_row(self, row):
        self._rows_layout.removeWidget(row["container"])
        row["container"].deleteLater()
        if row in self._rows:
            self._rows.remove(row)

    def _update_empty_label(self):
        self._empty_label.setVisible(not self._rows)
