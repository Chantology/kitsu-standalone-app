"""Project info tab: name, thumbnail, and the project's own settings.

The settings shown are Kitsu's project-edit "Parameters" tab, minus both
the handful of workflow toggles nobody browsing project info needs
(Homepage, Isolate Client Comments, Allow Artists to Download, Set New
Preview as [entity thumbnail], Allow Only One Preview File per Revision)
and the more technical/rarely-set ones (Man Days, File Tree, Frame In
Numbering, Revision Padding, HD/LD Bitrate Compression, Bot Collaboration
Enabled, Publish Default For Artists) — and any field that's simply empty
on this project is left out entirely rather than shown as "—" or "0".

The project's activity feed lives in MainWindow instead of here (see
NewsFeedWidget) — it takes over the main window's whole right-hand side
while this tab is active, rather than being squeezed into a column next to
this form.
"""

import os
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFormLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from .async_worker import run_async

_THUMBNAIL_WIDTH = 240

# (field on the project dict, display label) — shown only when the project
# actually has a value for it (see set_project).
_FIELD_LABELS = [
    ("code", "Code"),
    ("description", "Description"),
    ("project_status_name", "Status"),
    ("production_type", "Production Type"),
    ("production_style", "Production Style"),
    ("resolution", "Resolution"),
    ("fps", "Frame Rate"),
    ("ratio", "Ratio"),
    ("start_date", "Start Date"),
    ("end_date", "End Date"),
    ("nb_episodes", "Number of Episodes"),
    ("episode_span", "Episode Span"),
    ("max_retakes", "Max Retakes"),
]


class ProjectInfoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._async_workers = []

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        outer_layout.addWidget(scroll_area)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(10)
        scroll_area.setWidget(content)

        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)

        self.thumbnail_label = QLabel()
        self.thumbnail_label.setVisible(False)
        layout.addWidget(self.thumbnail_label)

        self.form_layout = QFormLayout()
        self.form_layout.setVerticalSpacing(8)
        layout.addLayout(self.form_layout)
        layout.addStretch(1)

    def set_session(self, session):
        self.session = session

    def set_project(self, project):
        self.name_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.name_label.setText(project.get("name") or "?")
        self.thumbnail_label.setVisible(False)
        self._load_thumbnail(project)

        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)

        for field, label in _FIELD_LABELS:
            value = project.get(field)
            if not value:  # None, "", or 0 — nothing entered, so skip the row entirely
                continue
            self.form_layout.addRow(f"{label}:", QLabel(str(value)))

    def show_no_projects_message(self):
        """Called when the logged-in user is on the team of no open project —
        without this, the tab would just be a blank, unexplained screen (see
        the "new test user, sees nothing" report this was added for) instead
        of saying why."""
        self.thumbnail_label.setVisible(False)
        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)
        self.name_label.setStyleSheet("font-size: 14px; font-weight: normal; color: #888888;")
        self.name_label.setText(
            "You aren't on the team of any open project in Kitsu.\n\n"
            "Ask whoever manages the production to add you to a project, then "
            "log out and back in (or restart the app)."
        )

    def _load_thumbnail(self, project):
        if not project.get("has_avatar"):
            return

        def work():
            cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_previews")
            os.makedirs(cache_dir, exist_ok=True)
            target_path = os.path.join(cache_dir, f"{project['id']}_project_thumb.png")
            self.session.download_project_thumbnail(project, target_path)
            return target_path

        def on_done(path):
            pixmap = QPixmap(path)
            if pixmap.isNull():
                return
            if pixmap.width() > _THUMBNAIL_WIDTH:
                pixmap = pixmap.scaledToWidth(_THUMBNAIL_WIDTH, Qt.SmoothTransformation)
            self.thumbnail_label.setPixmap(pixmap)
            self.thumbnail_label.setVisible(True)

        def on_error(_exc):
            pass  # not every project has a thumbnail set — silent, same as elsewhere

        run_async(self, work, on_done, on_error)
