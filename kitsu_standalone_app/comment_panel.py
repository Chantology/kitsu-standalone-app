"""Entity detail: task-type picker + comment history + compose/post.

Ported from the DCC plugins' `KitsuPanel` (see e.g. the Nuke port's
panel.py, `comments_list`/`comment_detail_area`/`comment_edit` and
`_load_comments`/`_render_comments`/`_build_comment_item`/
`_on_comment_selected`/`_on_comment_poll_tick`), with everything tied to a
DCC viewport/scene dropped: no viewport capture, no "variations" queue.

Writing a comment or publishing a revision is `CommentComposer`'s job (see
comment_composer.py, which mirrors Kitsu's own two-sided form); this panel owns
the task selection, the comment history, and the network calls the composer's
payload turns into.

The comment list stays a scannable one-line index and the selected comment is
shown in full underneath it — markdown rendered, with its checklist, links,
revisions and attachments.

`TaskListWidget`'s leaves are entities, not tasks (the same asset/shot can have
several tasks — Modeling, Shading, ... — on it), so this panel's own Task Type
dropdown is what actually picks which of the selected entity's tasks a
comment/preview is posted to; everything below it (statuses, comments, post)
reloads whenever that selection changes.
"""

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import frame_picker, media_types, movie_frames, paint_over_dialog, url_attachment
from .async_worker import run_async
from .comment_composer import MODE_REVISION, CommentComposer
from .kitsu_core import annotations, drafts, media_cache, project_tasks

# Minimum heights for the splitter panes (see _build_*_pane) — just enough to
# show each section's own label plus its most essential control, so the
# combined minimum is large enough that a small window scrolls the whole right
# side (MainWindow's comment_scroll_area) instead of the splitter simply
# squeezing every pane down to nothing to fit.
_TASK_INFO_MIN_HEIGHT = 130
_COMMENTS_MIN_HEIGHT = 120
_COMMENT_DETAIL_MIN_HEIGHT = 110
_PUBLISH_MIN_HEIGHT = 300
_ENTITY_THUMBNAIL_WIDTH = 160
_PREVIEW_IMAGE_WIDTH = 320
_COMMENT_TEXT_MAX_HEIGHT = 220


class CommentPanel(QWidget):
    # Publishing changes the task's status server-side, so the task list has
    # to re-read it or it keeps showing the status this panel just replaced.
    published = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.task = None
        self._entity_tasks = []
        self._async_workers = []
        self._preview_cache = {}  # preview (revision) id -> local file path
        self._attachment_cache = {}  # attachment id -> local file path
        self._project_fps_cache = {}  # project id -> fps, for annotation frame numbers
        self._last_comment_ids = None
        self._comment_poll_in_flight = False
        self._current_thumbnail_preview_id = None  # guards against a slow download landing after the entity changed again
        self._pending_draft_status_id = None  # applied once _load_statuses' async call populates the composer's status picker

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

        self.composer = CommentComposer()
        self.composer.post_requested.connect(self._on_post_requested)
        self.composer.save_draft_requested.connect(self._on_save_draft_requested)
        self.composer.discard_draft_requested.connect(self._on_discard_draft_clicked)
        self.composer.file_chosen.connect(self._on_composer_file_chosen)
        self.composer.attach_url_requested.connect(self._on_attach_url_requested)
        layout.addWidget(self.composer, 1)

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
        self.composer.reset()
        self._last_comment_ids = None
        self._pending_draft_status_id = None
        self.latest_version_label.setVisible(False)  # recomputed once _load_comments' async call finishes
        self._update_due_date_label(self.task)

        draft = drafts.load_draft(self.task["id"])
        if draft:
            self.composer.apply_draft(draft)
            self._pending_draft_status_id = draft.get("status_id")
            self.composer.set_draft_state(
                True,
                f"Draft saved {draft.get('saved_at', '')} — finish it and post, "
                "or Discard Draft to drop it.",
            )

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
            cache_dir = media_cache.ensure_directory(media_cache.PREVIEWS)
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
        self.composer.clear_statuses()
        task = self.task
        project_id = task.get("project_id")

        def work():
            # Not just the project's configured status list: it can be missing
            # the task's own current status, in which case this combo used to
            # sit silently on its first entry — so publishing a comment on a
            # "Todo" task would have set it to "Approved". See
            # project_tasks.statuses_for_task.
            return project_tasks.statuses_for_task(self.session, project_id, task)

        def on_done(statuses):
            if task is not self.task:
                return  # a different task was selected while this was in flight
            # A saved draft's status (the one the user picked when writing it)
            # takes priority over the task's current status — it's what they
            # intended to post, which may differ from whatever the task's
            # status has since become.
            current_status_id = self._pending_draft_status_id or task.get("task_status_id")
            self.composer.set_statuses(statuses, current_status_id)

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
        attachments = comment.get("attachment_files") or []
        # One line per comment — this list is the index, the full (markdown)
        # text is shown in the detail pane below on selection. So the summary
        # collapses the line breaks a multi-line comment would otherwise show
        # as boxes.
        text = " ".join((comment.get("text") or "").split())

        prefix_parts = [part for part in (revision_label, status_label) if part]
        prefix = f"{' - '.join(prefix_parts)} - " if prefix_parts else ""
        label = f"{prefix}{person}: {text}"
        markers = []
        if attachments:
            markers.append(f"{len(attachments)} attachment(s)")
        if comment.get("checklist"):
            checklist = comment["checklist"]
            done = sum(1 for item in checklist if item.get("checked"))
            markers.append(f"checklist {done}/{len(checklist)}")
        if comment.get("for_client"):
            markers.append("client")
        if markers:
            label += f"  [{', '.join(markers)}]"
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

        text = comment.get("text") or ""
        if text:
            self.comment_detail_layout.addWidget(self._build_comment_text(comment, text))

        links = comment.get("links") or []
        if links:
            self.comment_detail_layout.addWidget(self._section_label("Links"))
            self.comment_detail_layout.addWidget(self._build_links_view(links))

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

        has_content = bool(text or links or checklist or previews or attachments)
        self._detail_pane.setVisible(has_content)
        if has_content:
            self.comment_detail_layout.addStretch(1)

    def _build_comment_text(self, comment, text):
        """The selected comment in full, with its markdown rendered — Kitsu
        treats comment text as markdown, so `**bold**` should read as bold here
        rather than as literal asterisks. Qt 6's own setMarkdown does this, so
        no markdown library is needed."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header_parts = [self._resolve_comment_author(comment)]
        date = (comment.get("created_at") or "").replace("T", " ")
        if date:
            header_parts.append(date)
        if comment.get("for_client"):
            header_parts.append("visible to clients")
        header = QLabel(" · ".join(header_parts))
        header.setStyleSheet("color: #888888;")
        header.setWordWrap(True)
        layout.addWidget(header)

        browser = self._rich_text_view()
        browser.setMarkdown(text)
        browser.setMaximumHeight(_COMMENT_TEXT_MAX_HEIGHT)
        layout.addWidget(browser)
        return container

    def _build_links_view(self, links):
        browser = self._rich_text_view()
        # Shown as plain text, not markdown: a "link" is a string somebody else
        # typed, and running it through the markdown parser lets it inject
        # formatting into this panel.
        browser.setPlainText("\n".join(links))
        browser.setMaximumHeight(90)
        return browser

    def _rich_text_view(self):
        """A read-only view for text other people wrote.

        Links are handled explicitly instead of letting Qt hand any clicked URL
        straight to the OS: a comment comes from whoever wrote it, and a link's
        visible label need not match where it points. Qt runs no scripts and
        fetches no remote images in a QTextBrowser, so this is about not opening
        arbitrary schemes on a click rather than about script injection."""
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setOpenLinks(False)
        browser.anchorClicked.connect(self._on_anchor_clicked)
        return browser

    def _on_anchor_clicked(self, url):
        scheme = (url.scheme() or "").lower()
        if scheme not in ("http", "https"):
            self._set_status(
                f"That link points at {scheme + ':' if scheme else 'an unknown scheme'} "
                "— not opening it.",
                is_error=True,
            )
            return
        QDesktopServices.openUrl(url)

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

    def _build_image_preview(self, local_path, file_name, caption=None, preview_file=None):
        """An image from the comment history with its Paint Over button. Given a
        `preview_file` (i.e. it's a revision, not a plain attachment) the markup
        can also be saved as a Kitsu annotation on it."""
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
        if preview_file is None:
            paint_over_button.setToolTip(
                "Draw notes on top of this image — the markup becomes the file "
                "attached under Publish, ready to post as feedback."
            )
            paint_over_button.clicked.connect(
                lambda _checked=False, path=local_path: self._on_paint_over_existing_clicked(path)
            )
        else:
            paint_over_button.setToolTip(
                "Draw notes on this revision — save them as a Kitsu annotation "
                "(shown in Kitsu's own player) or attach the flattened markup."
            )
            paint_over_button.clicked.connect(
                lambda _checked=False, path=local_path, pf=preview_file: self._on_paint_over_revision(
                    path, pf
                )
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

        if media_types.is_image_extension(extension):
            preview = self._build_image_preview(local_path, file_name)
            if preview is not None:
                return preview

        return self._wrapped_label(f"{file_name} (no preview available for this file type)")

    def _build_revision_preview(self, preview_file):
        revision = preview_file.get("revision", "?")
        file_name = preview_file.get("original_name") or f"revision_{revision}"
        extension = (preview_file.get("extension") or "").lower()

        if media_types.is_image_extension(extension):
            try:
                local_path = self._ensure_preview_downloaded(preview_file)
            except Exception as exc:
                return self._wrapped_label(f"Revision {revision}: could not download ({exc})")
            widget = self._build_image_preview(
                local_path,
                file_name,
                caption=f"Revision {revision}",
                preview_file=preview_file,
            )
            if widget is not None:
                return widget
            return self._wrapped_label(f"Revision {revision} (could not be shown)")

        if media_types.is_movie_extension(extension):
            return self._build_movie_revision(preview_file, revision, file_name)

        return self._wrapped_label(f"Revision {revision}: {file_name} (open in Kitsu's web UI to view)")

    def _build_movie_revision(self, preview_file, revision, file_name):
        """A movie revision: its own thumbnail plus Fetch Media, which pulls the
        frame strip (see frame_picker) so a frame can be picked and drawn on.
        The strip is only fetched on demand — it's the one expensive request in
        the app."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        annotated_frames = annotations.frames_with_annotations(preview_file)
        caption = f"Revision {revision} · {file_name}"
        if annotated_frames:
            caption += f" · {len(annotated_frames)} annotated frame(s)"
        layout.addWidget(self._wrapped_label(caption))

        thumbnail = self._movie_thumbnail_label(preview_file)
        if thumbnail is not None:
            layout.addWidget(thumbnail)

        button_row = QHBoxLayout()
        fetch_button = QPushButton("Fetch Media & Paint Over...")
        fetch_button.setToolTip(
            "Pull this revision's frames from Kitsu, pick one, and draw on it — "
            "the markup can go back as a Kitsu annotation on this revision."
        )
        fetch_button.clicked.connect(
            lambda _checked=False, pf=preview_file, b=fetch_button: self._on_fetch_media_clicked(pf, b)
        )
        button_row.addWidget(fetch_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        return container

    def _movie_thumbnail_label(self, preview_file):
        """Kitsu's own small thumbnail for the movie — enough to recognise the
        shot without fetching anything heavy. Absent is fine."""
        preview_id = preview_file.get("id")
        cache_key = f"{preview_id}_movie_thumb"
        path = self._preview_cache.get(cache_key)
        if not (path and os.path.exists(path)):
            cache_dir = media_cache.ensure_directory(media_cache.PREVIEWS)
            try:
                os.makedirs(cache_dir, exist_ok=True)
                path = os.path.join(cache_dir, f"{cache_key}.png")
                self.session.download_preview_thumbnail(preview_file, path)
            except Exception:
                return None
            self._preview_cache[cache_key] = path

        pixmap = QPixmap(path)
        if pixmap.isNull():
            return None
        label = QLabel()
        label.setPixmap(pixmap)
        return label

    def _ensure_preview_downloaded(self, preview):
        preview_id = preview.get("id")
        if preview_id in self._preview_cache:
            cached_path = self._preview_cache[preview_id]
            if os.path.exists(cached_path):
                return cached_path

        cache_dir = media_cache.ensure_directory(media_cache.PREVIEWS)

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

        cache_dir = media_cache.ensure_directory(media_cache.ATTACHMENTS)

        file_name = attachment.get("name") or str(attachment_id)
        target_path = os.path.join(cache_dir, f"{attachment_id}_{file_name}")

        self.session.download_attachment(attachment, target_path)
        self._attachment_cache[attachment_id] = target_path
        return target_path

    # ---- compose + post ----

    def _on_composer_file_chosen(self, file_path):
        """The composer picked a file and it's an image — offer to mark it up
        before it goes anywhere. Declining just leaves the file as chosen."""
        if not media_types.is_image_path(file_path):
            return
        result_path = paint_over_dialog.paint_over(file_path, self)
        if not result_path:
            return
        self.composer.use_file(result_path)
        self._set_status("Markup attached.")

    # ---- paint over a revision (Kitsu annotations) ----

    def _on_paint_over_revision(self, image_path, preview_file):
        """An image revision: draw on it, then either store the markup as a real
        Kitsu annotation on that revision or flatten it into a file."""
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            self._set_status(f"Could not open {os.path.basename(image_path)}.", is_error=True)
            return
        self._run_paint_over_media(
            pixmap,
            f"Revision {preview_file.get('revision', '?')}",
            preview_file,
            seconds=0,
            frame=1,  # a still: Kitsu stores these at time 0, frame 1
        )

    def _on_fetch_media_clicked(self, preview_file, button):
        """Fetch a movie revision's frame strip, let the user scrub to a frame,
        and paint over that. The strip is the whole reason video revisions can be
        annotated at all — see frame_picker."""
        button.setEnabled(False)
        self._set_status("Fetching frames from Kitsu...")
        session = self.session
        preview_id = preview_file.get("id")

        def work():
            cache_dir = media_cache.ensure_directory(media_cache.TILES)
            target_path = os.path.join(cache_dir, f"{preview_id}_tile.png")
            if not os.path.exists(target_path):
                session.download_preview_tile(preview_file, target_path)
            return target_path

        def on_done(tile_path):
            button.setEnabled(True)
            frames, tile_size = frame_picker.load_tile(
                tile_path, preview_file.get("width"), preview_file.get("height")
            )
            if not frames:
                self._set_status(
                    f"Kitsu returned a frame strip ({tile_size[0]}x{tile_size[1]}) "
                    "that couldn't be read as frames.",
                    is_error=True,
                )
                return
            self._set_status(f"{len(frames)} frames fetched.")
            self._open_frame_picker(frames, preview_file)

        def on_error(exc):
            button.setEnabled(True)
            if not (preview_file.get("width") and preview_file.get("duration")):
                # Kitsu processes an uploaded movie in the background and its
                # frame strip doesn't exist until that finishes — which reads as
                # a plain server error otherwise.
                self._set_status(
                    "Kitsu is still processing this revision — its frames aren't "
                    "available yet. Try again in a moment.",
                    is_error=True,
                )
                return
            self._set_status(f"Could not fetch frames: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _open_frame_picker(self, frames, preview_file):
        dialog = frame_picker.FramePickerDialog(
            frames,
            preview_file.get("duration"),
            f"Revision {preview_file.get('revision', '?')}",
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.selected_pixmap() is None:
            return
        self._grab_full_frame(preview_file, dialog.selected_time())

    def _grab_full_frame(self, preview_file, seconds):
        """The tile's cells are 178x100 thumbnails — good enough to pick a frame,
        far too small to draw on. So the movie itself is fetched (once, cached)
        and the chosen frame decoded at full resolution."""
        session = self.session
        self._set_status(
            "Reading the frame..."
            if movie_frames.is_cached(preview_file)
            else "Downloading the movie (first time only)..."
        )

        def work():
            movie_path = movie_frames.download_movie(session, preview_file)
            return movie_frames.grab_frame(movie_path, seconds)

        def on_done(result):
            image, actual_seconds = result
            self._set_status(f"Frame at {actual_seconds:.2f}s ready.")
            self._run_paint_over_media(
                QPixmap.fromImage(image),
                f"Revision {preview_file.get('revision', '?')} @ {actual_seconds:.2f}s",
                preview_file,
                # The position actually decoded, not the one asked for — the
                # annotation should sit on the frame that was drawn on.
                seconds=actual_seconds,
            )

        def on_error(exc):
            self._set_status(f"Could not read the movie: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _run_paint_over_media(self, pixmap, source_name, preview_file, seconds=0, frame=None):
        dialog = paint_over_dialog.paint_over_media(pixmap, source_name, self)
        if dialog is None:
            return

        if dialog.result_action == dialog.ACTION_FILE:
            if dialog.saved_path:
                self.composer.use_file(dialog.saved_path)
                self._set_status("Markup attached below — add a comment and post it as feedback.")
            return

        self._save_annotation(dialog, preview_file, seconds, frame)

    def _save_annotation(self, dialog, preview_file, seconds, frame):
        """Writes the markup onto the revision itself, the way Kitsu's own
        player does. The strokes are in the drawn image's pixel space, which is
        what gets recorded as the annotation's canvas size."""
        width, height = dialog.image_size()
        annotation = annotations.annotation_from_strokes(
            dialog.strokes(),
            width,
            height,
            seconds=seconds,
            fps=self._project_fps(),
            frame=frame,
        )
        if annotation is None:
            self._set_status("Nothing was drawn, so no annotation was saved.", is_error=True)
            return

        session = self.session
        task = self.task
        self._set_status("Saving annotation...")

        def work():
            return session.update_preview_annotations(preview_file, additions=[annotation])

        def on_done(_result):
            self._set_status(
                f"Annotation saved on revision {preview_file.get('revision', '?')}, "
                f"frame {annotation['frame']} — visible in Kitsu's player."
            )
            if task is self.task:
                self._load_comments()

        def on_error(exc):
            self._set_status(f"Could not save the annotation: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _project_fps(self):
        """The active project's fps, needed to turn a time into Kitsu's frame
        number. Fetched once and remembered; a project without one just gets
        frame 1, same as a still."""
        project_id = (self.task or {}).get("project_id")
        if not project_id:
            return None
        if project_id not in self._project_fps_cache:
            try:
                project = self.session.project(project_id)
                self._project_fps_cache[project_id] = float(project.get("fps") or 0) or None
            except Exception:
                self._project_fps_cache[project_id] = None
        return self._project_fps_cache[project_id]

    def _on_paint_over_existing_clicked(self, image_path):
        """Paint over a revision/attachment from the comment history and queue
        the markup on the composer — the supervisor's round trip (look at
        revision N, draw on it, send it back) without leaving the app."""
        result_path = paint_over_dialog.paint_over(image_path, self)
        if not result_path:
            return
        self.composer.use_file(result_path)
        self._set_status("Markup attached below — add a comment and post it as feedback.")

    def _on_attach_url_requested(self, url):
        """Kitsu only ever stores uploaded files, so a URL has to be downloaded
        before it can be attached. Done through url_attachment rather than
        gazu's own URL handling so the scheme check, size ceiling and error
        messages are the same wherever a URL is used here."""
        self._set_status(f"Downloading {url}...")

        def work():
            return url_attachment.download_to_temp(url)

        def on_done(file_path):
            self.composer.use_file(file_path)
            self._set_status(f"Attached {os.path.basename(file_path)} from URL.")

        def on_error(exc):
            self._set_status(f"Could not download {url}: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _on_post_requested(self, payload):
        # Validation goes to the status label rather than a modal box: these are
        # "you're not done yet" messages, and a dialog to dismiss for each one
        # is more friction than the mistake deserves.
        if not (self.session and self.task):
            self._set_status("Select a task first.", is_error=True)
            return

        status_data = payload.get("status")
        if status_data is None:
            self._set_status("No task status selected.", is_error=True)
            return

        is_revision = payload["mode"] == MODE_REVISION
        preview_path = payload.get("preview_file_path")
        preview_url = payload.get("preview_file_url")
        if is_revision and not (preview_path or preview_url):
            self._set_status(
                "Publishing a revision needs a file — choose one, or give a URL "
                "for Kitsu to fetch.",
                is_error=True,
            )
            return

        task = self.task
        self.composer.set_busy(True)
        self._set_status("Posting...")

        def work():
            if is_revision:
                # A URL is resolved to a real file here rather than handed to
                # gazu: it downloads URLs client-side anyway, and going through
                # url_attachment keeps one set of rules and error messages.
                path = preview_path or url_attachment.download_to_temp(preview_url)
                return self.session.publish_preview(
                    task,
                    status_data,
                    comment=payload["text"],
                    preview_file_path=path,
                    revision=payload.get("revision"),
                    checklist=payload.get("checklist"),
                    links=payload.get("links"),
                    for_client=payload.get("for_client", False),
                    set_thumbnail=payload.get("set_thumbnail", False),
                )
            return self.session.add_comment(
                task,
                status_data,
                comment=payload["text"],
                checklist=payload.get("checklist"),
                attachments=payload.get("attachments"),
                links=payload.get("links"),
                for_client=payload.get("for_client", False),
            )

        def on_done(_result):
            self.composer.set_busy(False)
            drafts.clear_draft(task["id"])
            self._set_status("Revision published." if is_revision else "Comment posted.")
            if task is self.task:
                self.composer.reset()
                self._load_comments()
            self.published.emit()

        def on_error(exc):
            self.composer.set_busy(False)
            self._set_status(f"Could not post: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)

    def _on_save_draft_requested(self, payload):
        if not self.task:
            self._set_status("Select a task first.", is_error=True)
            return

        status_data = payload.get("status")
        drafts.save_draft(
            self.task["id"],
            comment_text=payload["text"],
            status_id=status_data.get("id") if status_data else None,
            version=payload.get("revision"),
            checklist=payload.get("checklist"),
            links=payload.get("links"),
            attachments=payload.get("attachments"),
            for_client=payload.get("for_client", False),
            set_thumbnail=payload.get("set_thumbnail", False),
            mode=payload.get("mode"),
        )
        self.composer.set_draft_state(True, "Draft saved just now — post it whenever it's ready.")
        self._set_status("Draft saved locally (not posted to Kitsu).")

    def _on_discard_draft_clicked(self):
        if self.task:
            drafts.clear_draft(self.task["id"])
        self.composer.set_draft_state(False)
        self._set_status("Draft discarded.")
