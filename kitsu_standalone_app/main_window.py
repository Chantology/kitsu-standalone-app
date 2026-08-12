"""Main window: task picker on the left, tab-dependent content on the
right — the project's news feed while the Project tab is active, the
selected entity's comments while Assets/Shots is active. A hamburger menu
in the top-right corner (see menu bar corner widget below) holds Log Out
and the light/dark theme toggle."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QToolButton,
    QWidget,
    QVBoxLayout,
)

from . import __version__, theme
from .comment_panel import CommentPanel
from .kitsu_core import credentials
from .news_feed_widget import NewsFeedWidget
from .task_list_widget import TaskListWidget


class MainWindow(QMainWindow):
    logged_out = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._base_title = f"Kitsu Desktop {__version__}"
        self.setWindowTitle(self._base_title)
        self.resize(1100, 700)
        self._entity_selected_once = False
        self._right_widget = None

        self._build_hamburger_menu()

        self.task_list = TaskListWidget()
        self.task_list.entity_selected.connect(self._on_entity_selected)
        self.task_list.project_changed.connect(self._on_project_changed)
        self.task_list.load_failed.connect(self._on_task_load_failed)
        self.task_list.tabs.currentChanged.connect(self._on_tab_changed)

        self.news_feed = NewsFeedWidget()
        self.news_feed.set_session(session)

        self.comment_panel = CommentPanel(session)
        # The right side can hold a lot (comments, checklist/preview
        # thumbnails, the compose form) — scrollable as a unit so none of
        # it gets clipped on a shorter window.
        self.comment_scroll_area = QScrollArea()
        self.comment_scroll_area.setWidgetResizable(True)
        self.comment_scroll_area.setWidget(self.comment_panel)

        self.placeholder = QWidget()
        placeholder_layout = QVBoxLayout(self.placeholder)
        placeholder_label = QLabel("Select an asset or shot on the left to see its comments.")
        placeholder_label.setStyleSheet("color: #666666;")
        placeholder_layout.addWidget(placeholder_label)
        placeholder_layout.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(self.task_list)
        splitter.addWidget(self.placeholder)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)
        self._splitter = splitter
        self._right_widget = self.placeholder

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

        # Qt only emits currentChanged on an actual change, and "Project"
        # (index 0) is already current at construction — sync the right
        # side to it explicitly rather than starting on the placeholder.
        self._on_tab_changed(self.task_list.tabs.currentIndex())

        self.task_list.load_tasks(session)

    def _build_hamburger_menu(self):
        menu = QMenu(self)

        self.light_theme_action = menu.addAction("Light Theme")
        self.light_theme_action.setCheckable(True)
        self.light_theme_action.setChecked(theme.load_theme() == theme.LIGHT)
        self.light_theme_action.toggled.connect(self._on_light_theme_toggled)

        menu.addSeparator()
        menu.addAction("Log Out", self._on_logout_clicked)

        self.hamburger_button = QToolButton()
        self.hamburger_button.setText("☰")
        self.hamburger_button.setToolTip("Menu")
        self.hamburger_button.setAutoRaise(True)
        self.hamburger_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.hamburger_button.setMenu(menu)

        # The menu bar's corner widget is the standard way to put something
        # in a QMainWindow's top-right — no toolbar/layout wrangling needed.
        self.menuBar().setCornerWidget(self.hamburger_button, Qt.Corner.TopRightCorner)

    def _on_light_theme_toggled(self, checked):
        theme.apply_theme(QApplication.instance(), theme.LIGHT if checked else theme.DARK)

    def _on_logout_clicked(self):
        connection = credentials.load_connection()
        if connection.server_url and connection.email:
            credentials.clear_refresh_token(connection.server_url, connection.email)
        self.shutdown()
        self.logged_out.emit()

    def shutdown(self):
        self.comment_panel.shutdown()

    def _set_right_widget(self, widget):
        if self._right_widget is widget:
            return
        index = self._splitter.indexOf(self._right_widget)
        if index != -1:
            self._splitter.replaceWidget(index, widget)
            self._right_widget.setParent(None)
        else:
            self._splitter.addWidget(widget)
        self._right_widget = widget

    def _on_tab_changed(self, index):
        if self.task_list.tabs.widget(index) is self.task_list.project_info:
            self._set_right_widget(self.news_feed)
        else:
            self._set_right_widget(self.comment_scroll_area if self._entity_selected_once else self.placeholder)

    def _on_entity_selected(self, tasks, clicked_task):
        self._entity_selected_once = True
        self._set_right_widget(self.comment_scroll_area)
        self.comment_panel.set_entity_tasks(tasks, clicked_task)

    def _on_project_changed(self, project):
        name = project.get("name") or "?"
        self.setWindowTitle(f"{self._base_title} — {name}")
        self.news_feed.load(project["id"])

    def _on_task_load_failed(self, exc):
        self._status_bar.showMessage(f"Could not load your tasks: {exc}")
