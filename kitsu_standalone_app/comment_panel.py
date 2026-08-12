"""Entity detail: task-type picker + comment history + compose/post.

Ported from the DCC plugins' `KitsuPanel` (see e.g. the Nuke port's
panel.py, `comments_list`/`comment_detail_area`/`comment_edit` and
`_load_comments`/`_render_comments`/`_build_comment_item`/
`_on_comment_selected`/`_on_comment_poll_tick`), with everything tied to a
DCC viewport/scene dropped: no viewport capture, no video-frame annotation,
no "variations" queue. The file published with a comment comes from a plain
`QFileDialog`, a URL (see url_attachment), or a paint-over of an image the
app already has (see paint_over_dialog) instead.

`TaskListWidget`'s leaves are entities, not tasks (the same asset/shot can
have several tasks — Modeling, Shading, ... — assigned to the same user),
so this panel's own Task Type dropdown is what actually picks which of the
selected entity's tasks a comment/preview is posted to; everything below it
(statuses, comments, post) reloads whenever that selection changes.
"""

import os
import tempfile

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import paint_over_dialog, url_attachment
from .async_worker import run_async
from .kitsu_core import drafts

_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
# Minimum heights for the splitter panes (see _build_*_pane) — just enough to
# show each section's own label plus its most essential control, so the
# combined minimum is large enough that a small window scrolls the whole right
# side (MainWindow's comment_scroll_area) instead of the splitter simply
# squeezing every pane down to nothing to fit.
_TASK_INFO_MIN_HEIGHT = 130
_COMMENTS_MIN_HEIGHT = 120
_COMMENT_DETAIL_MIN_HEIGHT = 110
_PUBLISH_MIN_HEIGHT = 220
_ENTITY_THUMBNAIL_WIDTH = 160
_PREVIEW_IMAGE_WIDTH = 320


def _is_image_path(path):
    return os.path.splitext(path or "")[1].lstrip(".").lower() in _IMAGE_EXTENSIONS


class CommentPanel(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.task = None
        self._entity_tasks = []
        self._async_workers = []
        self._preview_cache = {}  # preview (revision) id -> local file path
        self._attachment_cache = {}  # attachment id -> local file path
        self._last_comment_ids = None
        self._comment_poll_in_flight = False
        self._pending_attachment_path = None
        self._current_thumbnail_preview_id = None  # guards against a slow download landing after the entity changed again
        self._pending_draft_status_id = None  # applied once _load_statuses' async call actually populates status_combo

        self._comment_poll_timer = QTimer(self)
        self._comment_poll_timer.setInterval(60_000)
        self._comment_poll_timer.timeout.connect(self._on_comment_poll_tick)
        self._comment_poll_timer.start()

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # Independently resizable sections (drag the handles between them)
        # instead of one fixed stack — how much room Task Info vs Comments vs
        # the selected comment's detail vs Publish gets is a matter of what
        # the user wants to focus on right now, not something to hardcode.
        # The comment list and the selected comment's detail (checklist /
        # revisions / attachments) are deliberately two separate panes rather
        # than one "Comments" pane, so there's a handle between them: showing
        # more comments at once and showing more of a big paint-over pull in
        # opposite directions.
        splitter = QSplitter(Qt.Orientation.Vertical)
        outer_layout.addWidget(splitter)
        self._splitter = splitter

        splitter.addWidget(self._build_task_info_pane())
        splitter.addWidget(self._build_comments_pane())
        splitter.addWidget(self._build_comment_detail_pane())
        splitter.addWidget(self._build_publish_pane())
        splitter.setSizes([160, 260, 200, 320])

        self.setEnabled(False)

    def _build_task_info_pane(self):
        pane = QWidget()
        # A real floor for this pane, not just "shrink to whatever the
        # splitter feels like" — without it, a QSplitter happily squeezes
        # every pane down near zero to fit whatever space exists, so the
        # single scrollbar around the whole panel (see MainWindow's
        # comment_scroll_area) never actually engages. This is what makes
        # that outer scrollbar cover Task Info too, not just Comments/Publish.
        pane.setMinimumHeight(_TASK_INFO_MIN_HEIGHT)
        layout = QVBoxLayout(pane)
        layout.setSpacing(8)

        layout.addWidget(self._section_label("Task Info"))

        self.entity_header_label = QLabel("")
        self.entity_header_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.entity_header_label.setWordWrap(True)
        layout.addWidget(self.entity_header_label)

        self.entity_thumbnail_label = QLabel()
        self.entity_thumbnail_label.setVisible(False)
        layout.addWidget(self.entity_thumbnail_label)

        task_type_row = QHBoxLayout()
        task_type_row.addWidget(QLabel("Task Type"))
        self.task_type_combo = QComboBox()
        # AdjustToContents + no stretch factor (see the trailing addStretch
        # below) so it sizes to its longest item's text instead of
        # stretching to fill the row — otherwise it turns into a very long,
        # strange-looking dropdown whenever this panel gets wide.
        self.task_type_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.task_type_combo.currentIndexChanged.connect(self._on_task_type_changed)
        task_type_row.addWidget(self.task_type_combo)
        task_type_row.addStretch(1)
        layout.addLayout(task_type_row)

        self.latest_version_label = QLabel("")
        self.latest_version_label.setVisible(False)
        layout.addWidget(self.latest_version_label)

        self.due_date_label = QLabel("")
        self.due_date_label.setVisible(False)
        layout.addWidget(self.due_date_label)

        layout.addStretch(1)
        return pane

    def _build_comments_pane(self):
        pane = QWidget()
        pane.setMinimumHeight(_COMMENTS_MIN_HEIGHT)  # see _build_task_info_pane
        layout = QVBoxLayout(pane)
        layout.setSpacing(8)

        layout.addWidget(self._section_label("Comments"))

        self.comments_list = QListWidget()
        self.comments_list.currentItemChanged.connect(self._on_comment_selected)
        # Fills whatever space this pane's handle gives it, rather than a
        # fixed/capped height — dragging the Comments section bigger should
        # actually show more comments at once.
        self.comments_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.comments_list, 1)

        return pane

    def _build_comment_detail_pane(self):
        """The selected comment's checklist / revisions / attachments — its own
        splitter pane (hidden until a comment with any of those is selected,
        which hides its handle with it)."""
        pane = QWidget()
        pane.setMinimumHeight(_COMMENT_DETAIL_MIN_HEIGHT)  # see _build_task_info_pane
        pane.setVisible(False)
        layout = QVBoxLayout(pane)
        layout.setSpacing(8)
        self._detail_pane = pane

        self.comment_detail_area = QScrollArea()
        self.comment_detail_area.setWidgetResizable(True)
        detail_content = QWidget()
        self.comment_detail_layout = QVBoxLayout(detail_content)
        self.comment_detail_area.setWidget(detail_content)
        layout.addWidget(self.comment_detail_area, 1)

        return pane

    def _build_publish_pane(self):
        pane = QWidget()
        pane.setMinimumHeight(_PUBLISH_MIN_HEIGHT)  # see _build_task_info_pane
        layout = QVBoxLayout(pane)
        layout.setSpacing(8)

        layout.addWidget(self._section_label("Publish"))

        self.draft_label = QLabel("")
        self.draft_label.setStyleSheet("color: #8a6d00;")
        self.draft_label.setWordWrap(True)
        self.draft_label.setVisible(False)
        layout.addWidget(self.draft_label)

        self.status_combo = QComboBox()
        # Same reasoning as task_type_combo: size to content, and the
        # AlignLeft here is what actually stops a QVBoxLayout from
        # stretching it to the pane's full width.
        self.status_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        layout.addWidget(self.status_combo, 0, Qt.AlignmentFlag.AlignLeft)

        self.comment_edit = QTextEdit()
        self.comment_edit.setPlaceholderText("Write a comment...")
        self.comment_edit.setMinimumHeight(60)
        # Grows with the Publish section's own handle, same reasoning as
        # comments_list above — more room to focus on writing, on request.
        layout.addWidget(self.comment_edit, 1)

        attachment_row = QHBoxLayout()
        self.attach_button = QPushButton("Attach File...")
        self.attach_button.clicked.connect(self._on_attach_file_clicked)
        attachment_row.addWidget(self.attach_button)
        self.attach_url_button = QPushButton("Attach URL...")
        self.attach_url_button.setToolTip(
            "Download a file from a web address and attach that — Kitsu "
            "comments can only carry uploaded files, not links."
        )
        self.attach_url_button.clicked.connect(self._on_attach_url_clicked)
        attachment_row.addWidget(self.attach_url_button)
        attachment_row.addStretch(1)
        layout.addLayout(attachment_row)

        # Second row so the attached file's name has room to be readable and
        # the two buttons that only apply to it sit next to it, not next to
        # the two that pick a file in the first place.
        attachment_status_row = QHBoxLayout()
        self.attachment_label = QLabel("No file attached")
        self.attachment_label.setStyleSheet("color: #666666;")
        attachment_status_row.addWidget(self.attachment_label, 1)
        self.paint_over_button = QPushButton("Paint Over...")
        self.paint_over_button.setToolTip("Draw on top of the attached image before publishing it")
        self.paint_over_button.clicked.connect(self._on_paint_over_attachment_clicked)
        self.paint_over_button.setVisible(False)
        attachment_status_row.addWidget(self.paint_over_button)
        self.clear_attachment_button = QPushButton("Clear")
        self.clear_attachment_button.clicked.connect(self._on_clear_attachment_clicked)
        self.clear_attachment_button.setVisible(False)
        attachment_status_row.addWidget(self.clear_attachment_button)
        layout.addLayout(attachment_status_row)

        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("Version"))
        self.version_spin = QSpinBox()
        self.version_spin.setRange(0, 9999)
        self.version_spin.setSpecialValueText("Auto")
        self.version_spin.setToolTip(
            "Revision number for the attached file — pins the preview to a "
            "specific version instead of letting Kitsu auto-increment it. "
            "Only used when a file is attached."
        )
        version_row.addWidget(self.version_spin)
        version_row.addStretch(1)
        layout.addLayout(version_row)

        post_row = QHBoxLayout()
        self.post_button = QPushButton("Publish")
        self.post_button.clicked.connect(self._on_post_clicked)
        post_row.addWidget(self.post_button, 1)
        self.save_draft_button = QPushButton("Save as Draft")
        self.save_draft_button.setToolTip(
            "Save this comment's text/status/version locally without posting it "
            "to Kitsu — e.g. write feedback now, attach the render once it's "
            "done, and post later."
        )
        self.save_draft_button.clicked.connect(self._on_save_draft_clicked)
        post_row.addWidget(self.save_draft_button)
        self.discard_draft_button = QPushButton("Discard Draft")
        self.discard_draft_button.clicked.connect(self._on_discard_draft_clicked)
        self.discard_draft_button.setVisible(False)
        post_row.addWidget(self.discard_draft_button)
        layout.addLayout(post_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        return pane

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        """Stops background polling. This panel is a child widget embedded
        in MainWindow, not a top-level window, so closeEvent above never
        actually fires for it — callers that tear down the panel without
        closing it as a real window (e.g. logging out) must call this
        directly instead."""
        self._comment_poll_timer.stop()

    # ---- entity / task-type selection ----

    def set_entity_tasks(self, tasks, selected_task=None):
        """`tasks` is every task the logged-in user has on one entity
        (asset/shot) — usually one, but more if they're assigned more than
        one task type on it (see TaskListWidget). The Task Type dropdown
        picks which one is "the task" for everything below it; `selected_task`
        (the specific task row that was actually clicked) preselects it
        instead of always defaulting to the first task type."""
        self._entity_tasks = tasks
        self.setEnabled(True)

        first_task = tasks[0]
        entity_name = first_task.get("entity_name") or "?"
        # No project prefix — the active project is already shown in the
        # main window's title bar (see MainWindow._on_project_changed).
        self.entity_header_label.setText(entity_name)
        self._load_entity_thumbnail(first_task)

        initial_index = 0
        self.task_type_combo.blockSignals(True)
        self.task_type_combo.clear()
        for index, task in enumerate(tasks):
            self.task_type_combo.addItem(task.get("task_type_name") or "?", task)
            if selected_task is not None and task.get("id") == selected_task.get("id"):
                initial_index = index
        self.task_type_combo.setCurrentIndex(initial_index)
        self.task_type_combo.blockSignals(False)

        self._on_task_type_changed(initial_index)

    def _on_task_type_changed(self, index):
        if index < 0 or index >= len(self._entity_tasks):
            return
        self.task = self.task_type_combo.itemData(index)
        self._pending_attachment_path = None
        self._attachment_label_reset()
        self.version_spin.setValue(0)
        self.comment_edit.clear()
        self._last_comment_ids = None
        self._pending_draft_status_id = None
        self.latest_version_label.setVisible(False)  # recomputed once _load_comments' async call finishes
        self._update_due_date_label(self.task)

        draft = drafts.load_draft(self.task["id"])
        if draft:
            self.comment_edit.setPlainText(draft.get("comment_text") or "")
            self.version_spin.setValue(draft.get("version") or 0)
            self._pending_draft_status_id = draft.get("status_id")
            self.draft_label.setText(
                f"Draft saved {draft.get('saved_at', '')} — attach your file "
                "and Post to publish, or Discard Draft to drop it."
            )
            self.draft_label.setVisible(True)
            self.discard_draft_button.setVisible(True)
        else:
            self.draft_label.setVisible(False)
            self.discard_draft_button.setVisible(False)

        self._load_statuses()
        self._load_comments()

    def _update_due_date_label(self, task):
        # Ported from the DCC plugins' own due-date display (e.g. the Nuke
        # panel's _format_due_date): Zou returns an ISO date or datetime
        # string ("2026-08-06" or "2026-08-06T00:00:00") — only the date
        # part is relevant here.
        value = task.get("due_date")
        if not value:
            self.due_date_label.setVisible(False)
            return
        self.due_date_label.setText(f"Due Date: {str(value).split('T')[0]}")
        self.due_date_label.setVisible(True)

    def _load_entity_thumbnail(self, task):
        # `entity_preview_file_id` is the entity's own thumbnail (its "main"
        # preview, set on the asset/shot itself) — the same for every task
        # on this entity, unlike a comment's own per-task preview, so this
        # only needs to run once per entity (see set_entity_tasks), not on
        # every task-type change.
        preview_file_id = task.get("entity_preview_file_id")
        self._current_thumbnail_preview_id = preview_file_id
        if not preview_file_id:
            self.entity_thumbnail_label.setVisible(False)
            return

        cached_path = self._preview_cache.get(preview_file_id)
        if cached_path and os.path.exists(cached_path):
            self._show_entity_thumbnail(cached_path)
            return

        self.entity_thumbnail_label.setVisible(False)

        def work():
            cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_previews")
            os.makedirs(cache_dir, exist_ok=True)
            target_path = os.path.join(cache_dir, f"{preview_file_id}_entity_thumb.png")
            self.session.download_preview_thumbnail(preview_file_id, target_path)
            return target_path

        def on_done(path):
            self._preview_cache[preview_file_id] = path
            if preview_file_id == self._current_thumbnail_preview_id:
                self._show_entity_thumbnail(path)

        def on_error(_exc):
            pass  # not every entity has a thumbnail set — silent, same as the project/user avatar

        run_async(self, work, on_done, on_error)

    def _show_entity_thumbnail(self, path):
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.entity_thumbnail_label.setVisible(False)
            return
        if pixmap.width() > _ENTITY_THUMBNAIL_WIDTH:
            pixmap = pixmap.scaledToWidth(_ENTITY_THUMBNAIL_WIDTH, Qt.SmoothTransformation)
        self.entity_thumbnail_label.setPixmap(pixmap)
        self.entity_thumbnail_label.setVisible(True)

    # ---- status helper ----

    def _set_status(self, message, is_error=False):
        self.status_label.setText(message)
        color = "#b00020" if is_error else "#0b7a0b"
        self.status_label.setStyleSheet(f"color: {color};")

    def _section_label(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold; margin-top: 6px;")
        return label

    # ---- statuses ----

    def _load_statuses(self):
        self.status_combo.clear()
        task = self.task
        project_id = task.get("project_id")

        def work():
            if project_id:
                try:
                    return self.session.all_task_statuses_for_project(project_id)
                except Exception:
                    pass  # fall through to the global list below
            return self.session.all_task_statuses()

        def on_done(statuses):
            if task is not self.task:
                return  # a different task was selected while this was in flight
            self.status_combo.clear()
            for status in statuses:
                self.status_combo.addItem(status.get("name", status.get("short_name", "?")), status)
            # A saved draft's status (the one the user picked when writing
            # it) takes priority over the task's current status — it's
            # what they intended to post, which may differ from whatever
            # the task's status has since become.
            current_status_id = self._pending_draft_status_id or task.get("task_status_id")
            if current_status_id:
                for index in range(self.status_combo.count()):
                    if self.status_combo.itemData(index).get("id") == current_status_id:
                        self.status_combo.setCurrentIndex(index)
                        break

        def on_error(exc):
            self._set_status(f"Could not load task statuses: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    # ---- comments: load/render ----

    def _load_comments(self):
        self._clear_attachment_preview()
        self._detail_pane.setVisible(False)
        task = self.task
        if task is None:
            self.comments_list.clear()
            self._last_comment_ids = None
            return

        def work():
            return self.session.all_comments_for_task(task)

        def on_done(comments):
            if task is not self.task:
                return
            self._render_comments(comments)

        def on_error(exc):
            self._set_status(f"Could not load comments: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _build_comment_item(self, comment):
        revision_label = self._resolve_comment_revision(comment)
        status_label = self._resolve_comment_status(comment)
        person = self._resolve_comment_author(comment)
        text = comment.get("text", "")
        attachments = comment.get("attachment_files") or []

        prefix_parts = [part for part in (revision_label, status_label) if part]
        prefix = f"{' - '.join(prefix_parts)} - " if prefix_parts else ""
        label = f"{prefix}{person}: {text}"
        if attachments:
            label += f"  [{len(attachments)} attachment(s)]"
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, comment)
        return item

    def _render_comments(self, comments, preserve_selection=False):
        previously_selected_id = None
        if preserve_selection:
            current_item = self.comments_list.currentItem()
            if current_item is not None:
                previously_selected_id = current_item.data(Qt.UserRole).get("id")

        self.comments_list.clear()
        for comment in comments:
            item = self._build_comment_item(comment)
            self.comments_list.addItem(item)
            if previously_selected_id is not None and comment.get("id") == previously_selected_id:
                self.comments_list.setCurrentItem(item)

        self._last_comment_ids = [comment.get("id") for comment in comments]
        self._update_latest_version_label(comments)

    def _update_latest_version_label(self, comments):
        revisions = [
            preview.get("revision")
            for comment in comments
            for preview in (comment.get("previews") or [])
            if preview.get("revision") is not None
        ]
        if not revisions:
            self.latest_version_label.setVisible(False)
            return
        self.latest_version_label.setText(f"Latest Version: {max(revisions)}")
        self.latest_version_label.setVisible(True)

    def _on_comment_poll_tick(self):
        # Silent on failure and a no-op when nothing changed, so it never
        # disrupts a comment the user has open. Runs on a background
        # thread (see async_worker.run_async).
        if not (self.session and self.task):
            return
        if self._comment_poll_in_flight:
            return

        session = self.session
        task = self.task
        self._comment_poll_in_flight = True

        def work():
            return session.all_comments_for_task(task)

        def on_done(comments):
            self._comment_poll_in_flight = False
            if task is not self.task:
                return
            new_ids = [comment.get("id") for comment in comments]
            if new_ids == self._last_comment_ids:
                return
            self._render_comments(comments, preserve_selection=True)

        def on_error(_exc):
            self._comment_poll_in_flight = False

        run_async(self, work, on_done, on_error)

    def _resolve_comment_revision(self, comment):
        previews = comment.get("previews") or []
        if not previews:
            return None
        revision = previews[0].get("revision")
        return f"Revision {revision}" if revision is not None else None

    def _resolve_comment_status(self, comment):
        status = comment.get("task_status") or {}
        label = status.get("short_name") or status.get("name")
        return label.upper() if label else None

    def _resolve_comment_author(self, comment):
        person = comment.get("person") or {}
        full_name = person.get("full_name")
        if full_name:
            return full_name

        person_id = comment.get("person_id")
        if person_id and self.session:
            try:
                person = self.session.get_person(person_id)
            except Exception:
                person = None
            if person and person.get("full_name"):
                return person["full_name"]

        return "Unknown"

    # ---- comment detail (checklist + attachment/revision preview) ----

    def _clear_attachment_preview(self):
        while self.comment_detail_layout.count():
            child = self.comment_detail_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def _on_comment_selected(self, current, _previous):
        self._clear_attachment_preview()
        if current is None:
            self._detail_pane.setVisible(False)
            return

        comment = current.data(Qt.UserRole)

        checklist = comment.get("checklist") or []
        if checklist:
            self.comment_detail_layout.addWidget(self._section_label("Checklist"))
            for index, checklist_item in enumerate(checklist):
                checkbox = QCheckBox(checklist_item.get("text", ""))
                checkbox.setChecked(bool(checklist_item.get("checked")))
                checkbox.stateChanged.connect(
                    lambda _state, c=comment, i=index, cb=checkbox: self._on_checklist_item_toggled(c, i, cb)
                )
                self.comment_detail_layout.addWidget(checkbox)

        previews = comment.get("previews") or []
        if previews:
            self.comment_detail_layout.addWidget(self._section_label("Revisions"))
            for preview in previews:
                self.comment_detail_layout.addWidget(self._build_revision_preview(preview))

        attachments = comment.get("attachment_files") or []
        if attachments:
            self.comment_detail_layout.addWidget(self._section_label("Attachments"))
            for attachment in attachments:
                self.comment_detail_layout.addWidget(self._build_attachment_preview(attachment))

        has_content = bool(checklist or previews or attachments)
        self._detail_pane.setVisible(has_content)
        if has_content:
            self.comment_detail_layout.addStretch(1)

    def _on_checklist_item_toggled(self, comment, index, checkbox):
        checklist = comment.get("checklist") or []
        previous_value = checklist[index].get("checked", False)
        new_value = checkbox.isChecked()
        checklist[index]["checked"] = new_value

        try:
            self.session.update_comment_checklist(comment["id"], checklist)
        except Exception as exc:
            checklist[index]["checked"] = previous_value
            checkbox.blockSignals(True)
            checkbox.setChecked(previous_value)
            checkbox.blockSignals(False)
            QMessageBox.warning(self, "Kitsu", f"Could not sync checklist item to Kitsu: {exc}")

    def _wrapped_label(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _build_image_preview(self, local_path, file_name, caption=None):
        """An image from the comment history, with the Paint Over button that
        turns it into the next comment's attachment."""
        pixmap = QPixmap(local_path)
        if pixmap.isNull():
            return None

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        if caption:
            container_layout.addWidget(QLabel(caption))

        image_label = QLabel()
        image_label.setPixmap(
            pixmap.scaledToWidth(_PREVIEW_IMAGE_WIDTH, Qt.SmoothTransformation)
            if pixmap.width() > _PREVIEW_IMAGE_WIDTH
            else pixmap
        )
        image_label.setToolTip(file_name)
        container_layout.addWidget(image_label)

        button_row = QHBoxLayout()
        paint_over_button = QPushButton("Paint Over...")
        paint_over_button.setToolTip(
            "Draw notes on top of this image — the markup becomes the file "
            "attached under Publish, ready to post as feedback."
        )
        paint_over_button.clicked.connect(
            lambda _checked=False, path=local_path: self._on_paint_over_existing_clicked(path)
        )
        button_row.addWidget(paint_over_button)
        button_row.addStretch(1)
        container_layout.addLayout(button_row)

        return container

    def _build_attachment_preview(self, attachment):
        file_name = attachment.get("name") or str(attachment.get("id"))
        extension = (attachment.get("extension") or os.path.splitext(file_name)[1].lstrip(".")).lower()

        try:
            local_path = self._ensure_attachment_downloaded(attachment)
        except Exception as exc:
            return self._wrapped_label(f"{file_name}: could not download ({exc})")

        if extension in _IMAGE_EXTENSIONS:
            preview = self._build_image_preview(local_path, file_name)
            if preview is not None:
                return preview

        return self._wrapped_label(f"{file_name} (no preview available for this file type)")

    def _build_revision_preview(self, preview):
        revision = preview.get("revision", "?")
        file_name = preview.get("original_name") or f"revision_{revision}"
        extension = (preview.get("extension") or "").lower()

        if extension not in _IMAGE_EXTENSIONS:
            return self._wrapped_label(f"Revision {revision}: {file_name} (open in Kitsu's web UI to view)")

        try:
            local_path = self._ensure_preview_downloaded(preview)
        except Exception as exc:
            return self._wrapped_label(f"Revision {revision}: could not download ({exc})")

        preview = self._build_image_preview(local_path, file_name, caption=f"Revision {revision}")
        if preview is None:
            return self._wrapped_label(f"Revision {revision} (no preview available for this file type)")
        return preview

    def _ensure_preview_downloaded(self, preview):
        preview_id = preview.get("id")
        if preview_id in self._preview_cache:
            cached_path = self._preview_cache[preview_id]
            if os.path.exists(cached_path):
                return cached_path

        cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_previews")
        os.makedirs(cache_dir, exist_ok=True)

        name = preview.get("original_name")
        if not (name and "." in name):
            name = f"{preview_id}.{preview.get('extension') or 'png'}"
        target_path = os.path.join(cache_dir, f"{preview_id}_{name}")

        self.session.download_preview(preview, target_path)
        self._preview_cache[preview_id] = target_path
        return target_path

    def _ensure_attachment_downloaded(self, attachment):
        attachment_id = attachment.get("id")
        if attachment_id in self._attachment_cache:
            cached_path = self._attachment_cache[attachment_id]
            if os.path.exists(cached_path):
                return cached_path

        cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_attachments")
        os.makedirs(cache_dir, exist_ok=True)

        file_name = attachment.get("name") or str(attachment_id)
        target_path = os.path.join(cache_dir, f"{attachment_id}_{file_name}")

        self.session.download_attachment(attachment, target_path)
        self._attachment_cache[attachment_id] = target_path
        return target_path

    # ---- compose + post ----

    def _attachment_label_reset(self):
        self.attachment_label.setText("No file attached")
        self.attachment_label.setStyleSheet("color: #666666;")
        self.clear_attachment_button.setVisible(False)
        self.paint_over_button.setVisible(False)

    def _set_pending_attachment(self, file_path):
        """The single place the file-to-publish is set, whoever picked it —
        file dialog, URL download, or a paint-over."""
        self._pending_attachment_path = file_path
        self.attachment_label.setText(os.path.basename(file_path))
        self.attachment_label.setStyleSheet("")  # back to the theme's normal text color, not a hardcoded one
        self.clear_attachment_button.setVisible(True)
        self.paint_over_button.setVisible(_is_image_path(file_path))

    def _on_attach_file_clicked(self):
        file_path, _filter = QFileDialog.getOpenFileName(self, "Attach File")
        if not file_path:
            return
        self._set_pending_attachment(file_path)

    def _on_attach_url_clicked(self):
        url, accepted = QInputDialog.getText(
            self, "Attach from URL", "Image or file URL (http:// or https://):"
        )
        url = (url or "").strip()
        if not accepted or not url:
            return

        self.attach_url_button.setEnabled(False)
        self._set_status("Downloading...")

        def work():
            return url_attachment.download_to_temp(url)

        def on_done(file_path):
            self.attach_url_button.setEnabled(True)
            self._set_pending_attachment(file_path)
            self._set_status(f"Attached {os.path.basename(file_path)} from URL.")

        def on_error(exc):
            self.attach_url_button.setEnabled(True)
            self._set_status(f"Could not download {url}: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _on_paint_over_attachment_clicked(self):
        if not self._pending_attachment_path:
            return
        result_path = paint_over_dialog.paint_over(self._pending_attachment_path, self)
        if not result_path:
            return
        self._set_pending_attachment(result_path)
        self._set_status("Markup attached — Publish to post it.")

    def _on_paint_over_existing_clicked(self, image_path):
        """Paint over a revision/attachment from the comment history and queue
        the markup as the file to publish — the supervisor's round trip
        (look at revision N, draw on it, send it back) without leaving the app."""
        result_path = paint_over_dialog.paint_over(image_path, self)
        if not result_path:
            return
        self._set_pending_attachment(result_path)
        self._set_status("Markup attached below — add a comment and Publish it as feedback.")

    def _on_clear_attachment_clicked(self):
        self._pending_attachment_path = None
        self._attachment_label_reset()

    def _on_post_clicked(self):
        if not (self.session and self.task):
            QMessageBox.warning(self, "Kitsu", "Select a task first.")
            return

        status_data = self.status_combo.currentData()
        if status_data is None:
            QMessageBox.warning(self, "Kitsu", "No task status selected.")
            return

        comment_text = self.comment_edit.toPlainText().strip()
        attachment_path = self._pending_attachment_path
        # 0 means "Auto" (see setSpecialValueText) — let Kitsu auto-increment
        # the revision instead of pinning a specific number.
        version = self.version_spin.value() or None
        task = self.task

        self.post_button.setEnabled(False)
        self._set_status("Posting...")

        def work():
            if attachment_path:
                return self.session.publish_preview(
                    task,
                    status_data,
                    comment=comment_text,
                    preview_file_path=attachment_path,
                    revision=version,
                )
            return self.session.add_comment(task, status_data, comment=comment_text)

        def on_done(_result):
            self.post_button.setEnabled(True)
            drafts.clear_draft(task["id"])
            self.comment_edit.clear()
            self._pending_attachment_path = None
            self._attachment_label_reset()
            self.version_spin.setValue(0)
            self._set_status("Posted.")
            if task is self.task:
                self.draft_label.setVisible(False)
                self.discard_draft_button.setVisible(False)
                self._load_comments()

        def on_error(exc):
            self.post_button.setEnabled(True)
            self._set_status(f"Could not post comment: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _on_save_draft_clicked(self):
        if not self.task:
            QMessageBox.warning(self, "Kitsu", "Select a task first.")
            return

        status_data = self.status_combo.currentData()
        drafts.save_draft(
            self.task["id"],
            comment_text=self.comment_edit.toPlainText().strip(),
            status_id=status_data.get("id") if status_data else None,
            version=self.version_spin.value() or None,
        )
        self.draft_label.setText("Draft saved just now — attach your file and Post to publish.")
        self.draft_label.setVisible(True)
        self.discard_draft_button.setVisible(True)
        self._set_status("Draft saved locally (not posted to Kitsu).")

    def _on_discard_draft_clicked(self):
        if self.task:
            drafts.clear_draft(self.task["id"])
        self.draft_label.setVisible(False)
        self.discard_draft_button.setVisible(False)
        self._set_status("Draft discarded.")
