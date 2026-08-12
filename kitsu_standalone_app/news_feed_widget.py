"""Project-wide activity feed — a lightweight version of Kitsu's own News
page: every comment/status change/preview posted on ANY task in the
project, grouped by day (newest first, matching Zou's own ordering), with
the same task-type/status color pills and a small preview thumbnail. No
filtering yet — a later addition, by request.

The news endpoint itself only carries bare `task_type_id`/`task_status_id`
(not their name/color), so those are resolved via one lookup per project
(all_task_types_for_project/all_task_statuses_for_project) rather than a
per-item fetch.
"""

import os
import tempfile
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .async_worker import run_async
from .status_colors import status_display_color

_THUMBNAIL_SIZE = 40
_AVATAR_SIZE = 28


def _pill(text, hex_color):
    label = QLabel((text or "?").upper())
    color = status_display_color(hex_color)
    label.setStyleSheet(
        f"background-color: {color.name()}; color: white; border-radius: 8px; "
        "padding: 2px 8px; font-size: 10px; font-weight: bold;"
    )
    return label


def _format_date_header(date_str):
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}".upper()


class _NewsRow(QWidget):
    def __init__(self, item, task_type, task_status, parent=None):
        super().__init__(parent)
        self.item = item
        self._async_workers = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        when = item.get("created_at") or ""
        time_label = QLabel(when.split("T")[1][:5] if "T" in when else "")
        time_label.setFixedWidth(36)
        time_label.setStyleSheet("color: #888888; font-size: 10px;")
        layout.addWidget(time_label)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        layout.addWidget(self.avatar_label)

        if task_type:
            layout.addWidget(_pill(task_type.get("name"), task_type.get("color")))
        if task_status:
            layout.addWidget(_pill(task_status.get("short_name") or task_status.get("name"), task_status.get("color")))

        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(_THUMBNAIL_SIZE, _THUMBNAIL_SIZE)
        layout.addWidget(self.thumbnail_label)

        entity_label = QLabel(item.get("full_entity_name") or "")
        entity_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(entity_label, 1)

    def load_images(self, session):
        person = self.item.get("person") or {}
        if person.get("has_avatar") and person.get("id"):
            self._load_image(
                lambda path: session.download_person_avatar(person, path),
                f"{person['id']}_avatar.png",
                self.avatar_label,
                _AVATAR_SIZE,
            )

        preview_id = self.item.get("preview_file_id") or self.item.get("entity_preview_file_id")
        if preview_id:
            self._load_image(
                lambda path: session.download_preview_thumbnail(preview_id, path),
                f"{preview_id}_news_thumb.png",
                self.thumbnail_label,
                _THUMBNAIL_SIZE,
            )

    def _load_image(self, download_fn, cache_name, target_label, size):
        cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_news")
        os.makedirs(cache_dir, exist_ok=True)
        target_path = os.path.join(cache_dir, cache_name)

        if os.path.exists(target_path):
            self._apply_pixmap(target_path, target_label, size)
            return

        def work():
            download_fn(target_path)
            return target_path

        def on_done(path):
            self._apply_pixmap(path, target_label, size)

        def on_error(_exc):
            pass  # not everything has a thumbnail/avatar — silent, same as elsewhere

        run_async(self, work, on_done, on_error)

    def _apply_pixmap(self, path, label, size):
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        label.setPixmap(
            pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        )


class NewsFeedWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._async_workers = []
        self._rows = []  # keeps _NewsRow instances alive for their background image loads

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("News Feed")
        title.setStyleSheet("font-size: 14px; font-weight: bold;")
        outer_layout.addWidget(title)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        outer_layout.addWidget(scroll_area)

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setSpacing(4)
        scroll_area.setWidget(self._content)

    def set_session(self, session):
        self.session = session

    def load(self, project_id):
        self._clear()
        self._content_layout.addWidget(QLabel("Loading..."))

        def work():
            items = self.session.project_news(project_id)
            task_types = {t["id"]: t for t in self.session.all_task_types_for_project(project_id)}
            task_statuses = {s["id"]: s for s in self.session.all_task_statuses_for_project(project_id)}
            return items, task_types, task_statuses

        def on_done(result):
            items, task_types, task_statuses = result
            self._render(items, task_types, task_statuses)

        def on_error(exc):
            self._clear()
            self._content_layout.addWidget(QLabel(f"Could not load the news feed: {exc}"))

        run_async(self, work, on_done, on_error)

    def _clear(self):
        self._rows = []
        while self._content_layout.count():
            child = self._content_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def _render(self, items, task_types, task_statuses):
        self._clear()
        if not items:
            self._content_layout.addWidget(QLabel("No activity yet."))
            return

        current_date = None
        for item in items:
            date_str = (item.get("created_at") or "").split("T")[0]
            if date_str != current_date:
                current_date = date_str
                header = QLabel(_format_date_header(date_str))
                header.setStyleSheet("font-weight: bold; color: #444444; margin-top: 8px;")
                self._content_layout.addWidget(header)

            row = _NewsRow(item, task_types.get(item.get("task_type_id")), task_statuses.get(item.get("task_status_id")))
            self._content_layout.addWidget(row)
            self._rows.append(row)
            row.load_images(self.session)

        self._content_layout.addStretch(1)
