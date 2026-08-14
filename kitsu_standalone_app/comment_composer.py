"""Kitsu's comment composer: Post Comment or Publish Revision.

Kitsu's own task page offers two side-by-side forms — "Post Comment" (text,
emoji, attachments, checklist, client visibility) and "Publish Revision" (the
same, plus a preview file or a URL to fetch it from). This is the same thing as
one form with a mode switch, which is what fits a narrow side panel.

All of it maps onto arguments `gazu` already accepts and this app simply never
passed (see KitsuSession.add_comment/publish_preview): `checklist`,
`attachments` (local file paths), `links` (URLs stored on the comment),
`for_client`, `preview_file_url` (the server fetches the file itself) and
`set_thumbnail`.

This widget only collects input and hands it over as one payload dict —
`CommentPanel` keeps ownership of the network call, the draft store and the
result message, so nothing in here touches the session.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import emoji_picker, media_types
from .checklist_editor import ChecklistEditor

MODE_COMMENT = "comment"
MODE_REVISION = "revision"

_LIST_MAX_HEIGHT = 72
_TEXT_MIN_HEIGHT = 70


class _FileListBox(QWidget):
    """A short list of file paths or URLs with add/remove buttons. Used for
    comment attachments and comment links, which behave identically apart from
    what "add" means."""

    changed = Signal()

    def __init__(self, empty_text, parent=None):
        super().__init__(parent)
        self._empty_text = empty_text

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(_LIST_MAX_HEIGHT)
        self.list_widget.setVisible(False)
        layout.addWidget(self.list_widget)

        self.empty_label = QLabel(empty_text)
        self.empty_label.setStyleSheet("color: #666666;")
        layout.addWidget(self.empty_label)

        self.buttons_row = QHBoxLayout()
        layout.addLayout(self.buttons_row)

        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove_clicked)
        self.remove_button.setVisible(False)

    def finish_buttons(self):
        """Called once the owner has added its own add-buttons, so Remove sits
        last on the row."""
        self.buttons_row.addWidget(self.remove_button)
        self.buttons_row.addStretch(1)

    def values(self):
        return [
            self.list_widget.item(index).data(Qt.UserRole)
            for index in range(self.list_widget.count())
        ]

    def add_value(self, value, label=None):
        if not value or value in self.values():
            return
        self.list_widget.addItem(label or value)
        item = self.list_widget.item(self.list_widget.count() - 1)
        item.setData(Qt.UserRole, value)
        item.setToolTip(value)
        self._update_visibility()
        self.changed.emit()

    def set_values(self, values):
        self.list_widget.clear()
        for value in values or []:
            self.add_value(value, label=self._label_for(value))
        self._update_visibility()

    def clear(self):
        self.list_widget.clear()
        self._update_visibility()
        self.changed.emit()

    def _label_for(self, value):
        return value

    def _on_remove_clicked(self):
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self):
        has_items = self.list_widget.count() > 0
        self.list_widget.setVisible(has_items)
        self.empty_label.setVisible(not has_items)
        self.remove_button.setVisible(has_items)


class _AttachmentListBox(_FileListBox):
    def _label_for(self, value):
        return os.path.basename(value)

    def add_value(self, value, label=None):
        super().add_value(value, label=label or os.path.basename(value))


class CommentComposer(QWidget):
    post_requested = Signal(dict)
    save_draft_requested = Signal(dict)
    discard_draft_requested = Signal()
    # A file was picked that the owner may want to offer paint-over for.
    file_chosen = Signal(str)
    attach_url_requested = Signal(str)  # a URL to download and attach (comment mode)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_path = None
        self._preview_url = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        layout.addLayout(self._build_mode_row())
        layout.addLayout(self._build_status_row())
        layout.addLayout(self._build_text_toolbar())

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Write a comment... (markdown is supported)")
        self.text_edit.setMinimumHeight(_TEXT_MIN_HEIGHT)
        layout.addWidget(self.text_edit, 1)

        self.for_client_check = QCheckBox("Visible to clients")
        self.for_client_check.setToolTip(
            "Off (the default) keeps the comment internal to the production "
            "team. On publishes it to clients — Kitsu only allows this for "
            "managers."
        )
        layout.addWidget(self.for_client_check)

        layout.addWidget(self._section_label("Checklist"))
        self.checklist_editor = ChecklistEditor()
        layout.addWidget(self.checklist_editor)

        self._comment_box = self._build_comment_box()
        layout.addWidget(self._comment_box)

        self._revision_box = self._build_revision_box()
        layout.addWidget(self._revision_box)

        layout.addLayout(self._build_buttons_row())

        self.draft_label = QLabel("")
        self.draft_label.setStyleSheet("color: #8a6d00;")
        self.draft_label.setWordWrap(True)
        self.draft_label.setVisible(False)
        layout.addWidget(self.draft_label)

        self._set_mode(MODE_COMMENT)

    # ---- construction ----

    def _section_label(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        return label

    def _build_mode_row(self):
        row = QHBoxLayout()
        self.comment_mode_button = QPushButton("Post Comment")
        self.comment_mode_button.setCheckable(True)
        self.comment_mode_button.setChecked(True)
        self.revision_mode_button = QPushButton("Publish Revision")
        self.revision_mode_button.setCheckable(True)

        # Exclusive group so the pair reads as one segmented control, the way
        # Kitsu's two side-by-side panels do.
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.comment_mode_button)
        self._mode_group.addButton(self.revision_mode_button)
        self.comment_mode_button.clicked.connect(lambda: self._set_mode(MODE_COMMENT))
        self.revision_mode_button.clicked.connect(lambda: self._set_mode(MODE_REVISION))

        row.addWidget(self.comment_mode_button)
        row.addWidget(self.revision_mode_button)
        row.addStretch(1)
        return row

    def _build_status_row(self):
        row = QHBoxLayout()
        row.addWidget(QLabel("Status"))
        self.status_combo = QComboBox()
        self.status_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        row.addWidget(self.status_combo)
        row.addStretch(1)
        return row

    def _build_text_toolbar(self):
        row = QHBoxLayout()
        row.setSpacing(2)

        for text, tooltip, handler in (
            ("B", "Bold (**text**)", lambda: self._wrap_selection("**", "**")),
            ("I", "Italic (*text*)", lambda: self._wrap_selection("*", "*")),
            ("• —", "Bullet list", lambda: self._prefix_lines("- ")),
            ("</>", "Inline code (`text`)", lambda: self._wrap_selection("`", "`")),
        ):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tooltip)
            button.setAutoRaise(True)
            button.clicked.connect(handler)
            row.addWidget(button)

        self.emoji_button = QToolButton()
        self.emoji_button.setText("🙂")
        self.emoji_button.setToolTip("Insert an emoji")
        self.emoji_button.setAutoRaise(True)
        # Kept on self so the menu isn't garbage-collected.
        self._emoji_menu = emoji_picker.attach_to_button(self.emoji_button, self._insert_text)
        row.addWidget(self.emoji_button)

        row.addStretch(1)
        return row

    def _build_comment_box(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(self._section_label("Attachments"))
        self.attachments_box = _AttachmentListBox("No files attached")
        attach_button = QPushButton("Attach Files...")
        attach_button.clicked.connect(self._on_attach_files_clicked)
        self.attachments_box.buttons_row.addWidget(attach_button)
        attach_url_button = QPushButton("From URL...")
        attach_url_button.setToolTip(
            "Download a file from a web address and attach it. For just linking "
            "to a page, use Links below — Kitsu stores those on the comment."
        )
        attach_url_button.clicked.connect(self._on_attach_url_clicked)
        self.attachments_box.buttons_row.addWidget(attach_url_button)
        self.attachments_box.finish_buttons()
        layout.addWidget(self.attachments_box)

        layout.addWidget(self._section_label("Links"))
        self.links_box = _FileListBox("No links")
        add_link_button = QPushButton("Add Link...")
        add_link_button.clicked.connect(self._on_add_link_clicked)
        self.links_box.buttons_row.addWidget(add_link_button)
        self.links_box.finish_buttons()
        layout.addWidget(self.links_box)

        return box

    def _build_revision_box(self):
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(self._section_label("Preview to publish"))

        file_row = QHBoxLayout()
        choose_button = QPushButton("Choose File...")
        choose_button.clicked.connect(self._on_choose_preview_clicked)
        file_row.addWidget(choose_button)
        url_button = QPushButton("From URL...")
        url_button.setToolTip(
            "Download the file from a web address and publish it as the "
            "revision. Kitsu only stores uploaded files, so the download "
            "happens here first."
        )
        url_button.clicked.connect(self._on_preview_url_clicked)
        file_row.addWidget(url_button)
        file_row.addStretch(1)
        layout.addLayout(file_row)

        preview_row = QHBoxLayout()
        self.preview_label = QLabel("Nothing selected")
        self.preview_label.setStyleSheet("color: #666666;")
        self.preview_label.setWordWrap(True)
        preview_row.addWidget(self.preview_label, 1)
        self.paint_over_button = QPushButton("Paint Over...")
        self.paint_over_button.setToolTip("Draw on top of the selected image before publishing it")
        self.paint_over_button.setVisible(False)
        self.paint_over_button.clicked.connect(self._on_paint_over_clicked)
        preview_row.addWidget(self.paint_over_button)
        self.clear_preview_button = QPushButton("Clear")
        self.clear_preview_button.setVisible(False)
        self.clear_preview_button.clicked.connect(self.clear_preview)
        preview_row.addWidget(self.clear_preview_button)
        layout.addLayout(preview_row)

        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("Version"))
        self.version_spin = QSpinBox()
        self.version_spin.setRange(0, 9999)
        self.version_spin.setSpecialValueText("Auto")
        self.version_spin.setToolTip(
            "Revision number for the published preview — pins it to a specific "
            "version instead of letting Kitsu auto-increment."
        )
        version_row.addWidget(self.version_spin)
        version_row.addStretch(1)
        layout.addLayout(version_row)

        self.set_thumbnail_check = QCheckBox("Set as the asset/shot thumbnail")
        layout.addWidget(self.set_thumbnail_check)

        return box

    def _build_buttons_row(self):
        row = QHBoxLayout()
        self.post_button = QPushButton("Publish")
        self.post_button.clicked.connect(self._on_post_clicked)
        row.addWidget(self.post_button, 1)

        self.save_draft_button = QPushButton("Save as Draft")
        self.save_draft_button.setToolTip(
            "Save this comment locally without posting it — e.g. write feedback "
            "now, attach the render once it's done, and post later."
        )
        self.save_draft_button.clicked.connect(
            lambda: self.save_draft_requested.emit(self.payload())
        )
        row.addWidget(self.save_draft_button)

        self.discard_draft_button = QPushButton("Discard Draft")
        self.discard_draft_button.setVisible(False)
        # Wrapped, not connected straight through: clicked carries a bool that
        # a no-argument signal would choke on.
        self.discard_draft_button.clicked.connect(lambda: self.discard_draft_requested.emit())
        row.addWidget(self.discard_draft_button)
        return row

    # ---- mode ----

    def mode(self):
        return MODE_REVISION if self.revision_mode_button.isChecked() else MODE_COMMENT

    def _set_mode(self, mode):
        is_revision = mode == MODE_REVISION
        self.revision_mode_button.setChecked(is_revision)
        self.comment_mode_button.setChecked(not is_revision)
        self._comment_box.setVisible(not is_revision)
        self._revision_box.setVisible(is_revision)
        self.post_button.setText("Publish Revision" if is_revision else "Post Comment")

    # ---- text helpers ----

    def _insert_text(self, text):
        self.text_edit.insertPlainText(text)
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _wrap_selection(self, prefix, suffix):
        cursor = self.text_edit.textCursor()
        selected = cursor.selectedText()
        cursor.insertText(f"{prefix}{selected}{suffix}")
        if not selected:
            # Nothing selected — drop the caret between the markers so typing
            # continues inside them.
            position = cursor.position() - len(suffix)
            cursor.setPosition(position)
            self.text_edit.setTextCursor(cursor)
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _prefix_lines(self, prefix):
        cursor = self.text_edit.textCursor()
        selected = cursor.selectedText()
        if selected:
            # QTextEdit uses U+2029 between selected paragraphs, not \n.
            lines = selected.replace(" ", "\n").split("\n")
            cursor.insertText("\n".join(f"{prefix}{line}" for line in lines))
        else:
            cursor.insertText(prefix)
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---- statuses ----

    def set_statuses(self, statuses, current_status_id=None):
        self.status_combo.clear()
        for status in statuses:
            self.status_combo.addItem(status.get("name", status.get("short_name", "?")), status)
        if current_status_id:
            for index in range(self.status_combo.count()):
                if (self.status_combo.itemData(index) or {}).get("id") == current_status_id:
                    self.status_combo.setCurrentIndex(index)
                    break

    def selected_status(self):
        return self.status_combo.currentData()

    def clear_statuses(self):
        self.status_combo.clear()

    # ---- files ----

    def _on_attach_files_clicked(self):
        paths, _filter = QFileDialog.getOpenFileNames(self, "Attach Files")
        for path in paths:
            self.attachments_box.add_value(path)
        if paths:
            self.file_chosen.emit(paths[-1])

    def _on_attach_url_clicked(self):
        url, accepted = QInputDialog.getText(
            self, "Attach from URL", "File URL (http:// or https://):"
        )
        url = (url or "").strip()
        if accepted and url:
            self.attach_url_requested.emit(url)

    def _on_add_link_clicked(self):
        url, accepted = QInputDialog.getText(self, "Add Link", "URL to store on the comment:")
        url = (url or "").strip()
        if accepted and url:
            self.links_box.add_value(url)

    def _on_choose_preview_clicked(self):
        path, _filter = QFileDialog.getOpenFileName(self, "Choose Preview File")
        if path:
            self.set_preview_path(path)
            self.file_chosen.emit(path)

    def _on_preview_url_clicked(self):
        url, accepted = QInputDialog.getText(
            self, "Publish Revision from URL", "File URL (http:// or https://):"
        )
        url = (url or "").strip()
        if accepted and url:
            self.set_preview_url(url)

    def _on_paint_over_clicked(self):
        if self._preview_path:
            self.file_chosen.emit(self._preview_path)

    def set_preview_path(self, path):
        self._preview_path = path
        self._preview_url = None
        self.preview_label.setText(os.path.basename(path))
        self.preview_label.setStyleSheet("")
        self.clear_preview_button.setVisible(True)
        self.paint_over_button.setVisible(media_types.is_image_path(path))

    def set_preview_url(self, url):
        self._preview_path = None
        self._preview_url = url
        self.preview_label.setText(f"{url}  (downloaded when posted)")
        self.preview_label.setStyleSheet("")
        self.clear_preview_button.setVisible(True)
        self.paint_over_button.setVisible(False)

    def clear_preview(self):
        self._preview_path = None
        self._preview_url = None
        self.preview_label.setText("Nothing selected")
        self.preview_label.setStyleSheet("color: #666666;")
        self.clear_preview_button.setVisible(False)
        self.paint_over_button.setVisible(False)

    def use_file(self, path):
        """Route a file the owner produced (a paint-over result, a downloaded
        URL) into whichever slot the current mode uses."""
        if self.mode() == MODE_REVISION:
            self.set_preview_path(path)
        else:
            self.attachments_box.add_value(path)

    # ---- payload ----

    def payload(self):
        return {
            "mode": self.mode(),
            "text": self.text_edit.toPlainText().strip(),
            "status": self.selected_status(),
            "checklist": self.checklist_editor.items(),
            "links": self.links_box.values(),
            "attachments": self.attachments_box.values(),
            "for_client": self.for_client_check.isChecked(),
            "preview_file_path": self._preview_path,
            "preview_file_url": self._preview_url,
            # 0 means "Auto" (see setSpecialValueText) — let Kitsu
            # auto-increment the revision instead of pinning a number.
            "revision": self.version_spin.value() or None,
            "set_thumbnail": self.set_thumbnail_check.isChecked(),
        }

    def apply_draft(self, draft):
        """Restores a locally-saved draft. Only the fields present are applied,
        so drafts written by an older build still load."""
        self.text_edit.setPlainText(draft.get("comment_text") or "")
        self.checklist_editor.set_items(draft.get("checklist"))
        self.links_box.set_values(draft.get("links"))
        self.attachments_box.set_values(draft.get("attachments"))
        self.for_client_check.setChecked(bool(draft.get("for_client")))
        self.set_thumbnail_check.setChecked(bool(draft.get("set_thumbnail")))
        self.version_spin.setValue(draft.get("version") or 0)
        self._set_mode(draft.get("mode") or MODE_COMMENT)

    def reset(self):
        """Back to empty, keeping the mode and the status selection — after
        posting, the next comment is usually on the same task."""
        self.text_edit.clear()
        self.checklist_editor.clear()
        self.links_box.clear()
        self.attachments_box.clear()
        self.clear_preview()
        self.for_client_check.setChecked(False)
        self.set_thumbnail_check.setChecked(False)
        self.version_spin.setValue(0)
        self.set_draft_state(False)

    def set_busy(self, busy):
        self.post_button.setEnabled(not busy)

    def set_draft_state(self, draft_present, message=""):
        self.draft_label.setText(message)
        self.draft_label.setVisible(draft_present and bool(message))
        self.discard_draft_button.setVisible(draft_present)

    def _on_post_clicked(self):
        self.post_requested.emit(self.payload())
