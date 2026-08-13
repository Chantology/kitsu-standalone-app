"""Project info + the project's Assets/Shots browser.

Shows the active project's real content — every asset and every shot, each
with every task on it, whoever it is assigned to — assembled by
`kitsu_core.project_tasks.load_project_content` (see that module for why the
earlier "build the trees from the logged-in user's own task queues" approach
under-reported so badly). Two filters narrow it: one picks between all tasks,
the ones assigned to the logged-in user, and the ones waiting on their review
("My Checks"); the other picks a single task type. The task-type filter is
per-tab, since Modeling/Shading are asset task types while Animation/Comp are
shot ones — one shared selection would only ever empty the other tab.

The active project's name is shown in the main window's title bar instead of
repeated in every row, so there's no project-level grouping in the trees — a
project selector picks which project is displayed and drives the Project tab +
window title via `project_changed`. It lists every project the user is on
(`all_open_projects`), not only those they happen to have a task in.

Each tree is 3 levels: group header (Asset Type / [Episode /] Sequence) ->
entity (the asset/shot name, not itself selectable) -> one row per task on
that entity, sorted the same left-to-right order Kitsu's own UI uses (task
type `priority`), with that task's latest published version and status (short
form, e.g. "WIP") as the trailing columns. Clicking a task row emits
`entity_selected(entity_tasks, clicked_task)` — every task on the entity (so
CommentPanel's Task Type dropdown can switch between them, regardless of the
current filter) plus the one actually clicked, which is the one it preselects.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .async_worker import run_async
from .kitsu_core import project_tasks
from .project_info_widget import ProjectInfoWidget
from .status_colors import status_display_color

_NAME_COLUMN_WIDTH = 200
_TASK_TYPE_COLUMN_WIDTH = 150
_VERSION_COLUMN_WIDTH = 60
# Status trails each row and stays a small fixed-width badge rather than
# stretching into a colored bar as the window resizes.
_STATUS_COLUMN_WIDTH = 90

FILTER_ALL = "all"
FILTER_MINE = "mine"
FILTER_CHECKS = "checks"
ALL_TASK_TYPES = None  # the task-type filter's "no filter" value

# A project name can be long ("GTX Membrane Illustration"); left to itself the
# combo grows to fit the longest one and eats the whole row, so it is capped
# and lets Qt elide the current entry — the popup still shows full names.
_PROJECT_COMBO_MAX_WIDTH = 230

# A whole project expanded is a wall of rows (655 tasks over 83 entities on a
# real one), so entities start collapsed past this many task rows — small
# projects and filtered views still open up fully, which is what makes a task
# one click away rather than two.
_AUTO_EXPAND_TASK_LIMIT = 60


class TaskListWidget(QWidget):
    entity_selected = Signal(object, object)  # (all tasks on the entity, the one that was clicked)
    project_changed = Signal(object)  # emits the full project dict for the newly-active project
    load_failed = Signal(object)  # emits the exception

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._async_workers = []
        self._active_project_id = None
        self._person_id = None
        self._check_task_ids = set()
        self._project_cache = {}  # project_id -> full project dict
        self._content_cache = {}  # project_id -> ProjectContent
        self._revision_cache = {}  # project_id -> {task id: latest revision}
        # kind -> {task id: its row}, per kind so one tree can be repopulated
        # (a task-type change only affects the tab it was made on) without
        # losing the other's rows for _apply_revisions.
        self._task_items = {"Asset": {}, "Shot": {}}
        self._filter_mode = FILTER_ALL
        # Kept per kind: Modeling/Shading are asset task types, Animation/Comp
        # are shot ones, so one shared selection would just empty the other tab.
        self._task_type_filter = {"Asset": ALL_TASK_TYPES, "Shot": ALL_TASK_TYPES}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        project_row = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.project_combo.setMaximumWidth(_PROJECT_COMBO_MAX_WIDTH)
        self.project_combo.currentIndexChanged.connect(self._on_project_combo_changed)
        project_row.addWidget(self.project_combo)
        project_row.addStretch(1)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setToolTip("Re-fetch this project from Kitsu (statuses, versions, new entities)")
        self.refresh_button.clicked.connect(self.refresh_active_project)
        project_row.addWidget(self.refresh_button)
        layout.addLayout(project_row)

        # Second row so neither combo has to fight the project name for space.
        filter_row = QHBoxLayout()
        self.task_type_combo = QComboBox()
        self.task_type_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.task_type_combo.setToolTip(
            "Show only one task type — the list holds the task types actually "
            "used by the tasks on this tab."
        )
        self.task_type_combo.currentIndexChanged.connect(self._on_task_type_filter_changed)
        filter_row.addWidget(self.task_type_combo)

        self.filter_combo = QComboBox()
        self.filter_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.filter_combo.addItem("All tasks", FILTER_ALL)
        self.filter_combo.addItem("My tasks", FILTER_MINE)
        self.filter_combo.addItem("My checks", FILTER_CHECKS)
        self.filter_combo.setToolTip(
            "All tasks: everything in the project.\n"
            "My tasks: only tasks assigned to you.\n"
            "My checks: only tasks waiting on your review."
        )
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self.filter_combo)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        self.tabs = QTabWidget()
        self.project_info = ProjectInfoWidget()
        self.assets_tree = self._build_tree(["Asset", "Task Type", "Version", "Status"])
        self.shots_tree = self._build_tree(["Shot", "Task Type", "Version", "Status"])
        self.tabs.addTab(self.project_info, "Project")
        self.tabs.addTab(self.assets_tree, "Assets")
        self.tabs.addTab(self.shots_tree, "Shots")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

        self._sync_task_type_combo()

    def _build_tree(self, headers):
        tree = QTreeWidget()
        tree.setHeaderLabels(headers)
        tree.setColumnWidth(0, _NAME_COLUMN_WIDTH)
        tree.setColumnWidth(1, _TASK_TYPE_COLUMN_WIDTH)
        tree.setColumnWidth(2, _VERSION_COLUMN_WIDTH)
        # Status is a colored cell (see _add_task_item) — without this, Qt's
        # default "stretch the last column to fill the view" turns it into a
        # long colorful bar that grows with the window instead of staying a
        # small badge with empty space after it.
        tree.header().setStretchLastSection(False)
        tree.setColumnWidth(3, _STATUS_COLUMN_WIDTH)
        tree.currentItemChanged.connect(self._on_current_item_changed)
        return tree

    # ---- loading ----

    def load_tasks(self, session):
        self.session = session
        self.project_info.set_session(session)

        def work():
            projects = session.all_open_projects()
            person = session.current_user() or {}
            # Only used to mark/filter rows — a person with no review
            # responsibilities just gets an empty set here.
            try:
                check_task_ids = {task.get("id") for task in session.all_tasks_requiring_feedback()}
            except Exception:
                check_task_ids = set()
            return projects, person.get("id"), check_task_ids

        def on_done(result):
            projects, person_id, check_task_ids = result
            self._person_id = person_id
            self._check_task_ids = check_task_ids
            self._setup_projects(projects)

        def on_error(exc):
            self.load_failed.emit(exc)

        run_async(self, work, on_done, on_error)

    def refresh_active_project(self):
        """Drops what's cached for the active project and re-fetches it —
        nothing else re-reads Kitsu on its own, so a status changed here or in
        the web UI would otherwise sit stale until the app restarts."""
        if not (self.session and self._active_project_id):
            return
        self._content_cache.pop(self._active_project_id, None)
        self._revision_cache.pop(self._active_project_id, None)
        self._load_project_content(self._active_project_id)

    # ---- project selection ----

    def _setup_projects(self, projects):
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for project in projects:
            self.project_combo.addItem(project.get("name") or "?", project)
        self.project_combo.blockSignals(False)
        self.project_combo.setVisible(len(projects) > 1)

        if projects:
            self._activate_project(projects[0])
        else:
            self._active_project_id = None
            self._populate_tree(self.assets_tree, [], "Asset")
            self._populate_tree(self.shots_tree, [], "Shot")
            self.project_info.show_no_projects_message()

    def _on_project_combo_changed(self, index):
        if index < 0:
            return
        self._activate_project(self.project_combo.itemData(index))

    def _activate_project(self, project):
        project_id = project.get("id")
        self._active_project_id = project_id
        self._project_cache.setdefault(project_id, project)
        self.project_info.set_project(project)
        self.project_changed.emit(project)
        self._load_project_content(project_id)

    def _load_project_content(self, project_id):
        cached = self._content_cache.get(project_id)
        if cached is not None:
            self._apply_content(project_id, cached)
            return

        session = self.session
        project = self._project_cache.get(project_id) or {}
        project_name = project.get("name")
        person_id = self._person_id
        check_task_ids = self._check_task_ids
        self.refresh_button.setEnabled(False)
        self._show_loading()

        def work():
            return project_tasks.load_project_content(
                session,
                project_id,
                project_name=project_name,
                person_id=person_id,
                check_task_ids=check_task_ids,
            )

        def on_done(content):
            self.refresh_button.setEnabled(True)
            self._content_cache[project_id] = content
            if project_id != self._active_project_id:
                return  # the user picked a different project while this was in flight
            self._apply_content(project_id, content)

        def on_error(exc):
            self.refresh_button.setEnabled(True)
            self.load_failed.emit(exc)

        run_async(self, work, on_done, on_error)

    def _apply_content(self, project_id, content):
        self._populate_tree(self.assets_tree, content.assets, "Asset")
        self._populate_tree(self.shots_tree, content.shots, "Shot")
        self._sync_task_type_combo()
        self._apply_revisions(self._revision_cache.get(project_id) or {})
        if project_id not in self._revision_cache:
            self._load_revisions(project_id)

    def _show_loading(self):
        self._task_items = {"Asset": {}, "Shot": {}}
        for tree in (self.assets_tree, self.shots_tree):
            tree.clear()
            tree.addTopLevelItem(self._header_item("Loading..."))

    def _load_revisions(self, project_id):
        """Fetched separately from the rest of the project because it's slow
        (see project_tasks.latest_revision_by_task_id) — the trees are already
        on screen by the time this fills in the Version column."""
        session = self.session

        def work():
            return project_tasks.latest_revision_by_task_id(session, project_id)

        def on_done(revisions):
            self._revision_cache[project_id] = revisions
            if project_id == self._active_project_id:
                self._apply_revisions(revisions)

        def on_error(_exc):
            # Versions are a nice-to-have next to each row, not worth taking
            # over the window's status bar for.
            self._revision_cache[project_id] = {}

        run_async(self, work, on_done, on_error)

    def _apply_revisions(self, revisions):
        for items in self._task_items.values():
            for task_id, item in items.items():
                revision = revisions.get(task_id)
                if revision is None:
                    continue
                try:
                    item.setText(2, f"v{revision}")
                except RuntimeError:
                    pass  # the tree was repopulated (this row deleted) mid-update

    def _on_filter_changed(self, index):
        if index < 0:
            return
        self._filter_mode = self.filter_combo.itemData(index)
        content = self._content_cache.get(self._active_project_id)
        if content is not None:
            self._apply_content(self._active_project_id, content)

    # ---- task-type filter ----

    def _active_kind(self):
        """"Asset"/"Shot" for those tabs, None for the Project tab."""
        widget = self.tabs.currentWidget()
        if widget is self.assets_tree:
            return "Asset"
        if widget is self.shots_tree:
            return "Shot"
        return None

    def _tree_for_kind(self, kind):
        return self.assets_tree if kind == "Asset" else self.shots_tree

    def _records_for_kind(self, kind):
        content = self._content_cache.get(self._active_project_id)
        if content is None:
            return []
        return content.assets if kind == "Asset" else content.shots

    def _task_types_in_use(self, kind):
        """The task types actually on this tab's entities, in the same
        priority order the rows use — offering the project's full configured
        list would mean offering task types that filter to nothing."""
        content = self._content_cache.get(self._active_project_id)
        if content is None:
            return []
        used_ids = {
            task.get("task_type_id")
            for record in self._records_for_kind(kind)
            for task in record["tasks"]
        }
        ordered = sorted(
            (t for t in content.task_types if t.get("id") in used_ids),
            key=lambda t: (
                t.get("priority") if t.get("priority") is not None else project_tasks.UNKNOWN_PRIORITY,
                t.get("name") or "",
            ),
        )
        return [(t.get("name") or "?", t.get("id")) for t in ordered]

    def _sync_task_type_combo(self):
        """Rebuilds the task-type filter for the active tab, restoring that
        tab's own selection."""
        kind = self._active_kind()
        self.task_type_combo.setEnabled(kind is not None)

        self.task_type_combo.blockSignals(True)
        self.task_type_combo.clear()
        self.task_type_combo.addItem("All task types", ALL_TASK_TYPES)
        if kind is not None:
            selected = self._task_type_filter[kind]
            for name, task_type_id in self._task_types_in_use(kind):
                self.task_type_combo.addItem(name, task_type_id)
                if task_type_id == selected:
                    self.task_type_combo.setCurrentIndex(self.task_type_combo.count() - 1)
            # The stored selection can be gone after a project switch — fall
            # back to showing everything rather than filtering by a task type
            # this project doesn't have.
            if self.task_type_combo.currentIndex() == 0:
                self._task_type_filter[kind] = ALL_TASK_TYPES
        self.task_type_combo.blockSignals(False)

    def _on_tab_changed(self, _index):
        self._sync_task_type_combo()

    def _on_task_type_filter_changed(self, index):
        kind = self._active_kind()
        if index < 0 or kind is None:
            return
        self._task_type_filter[kind] = self.task_type_combo.itemData(index)
        self._populate_tree(self._tree_for_kind(kind), self._records_for_kind(kind), kind)
        self._apply_revisions(self._revision_cache.get(self._active_project_id) or {})

    # ---- tree building ----

    def _filtered_tasks(self, tasks, kind):
        task_type_id = self._task_type_filter.get(kind)
        if task_type_id is not ALL_TASK_TYPES:
            tasks = [task for task in tasks if task.get("task_type_id") == task_type_id]
        if self._filter_mode == FILTER_MINE:
            return [task for task in tasks if task.get("_is_mine")]
        if self._filter_mode == FILTER_CHECKS:
            return [task for task in tasks if task.get("_is_check")]
        return list(tasks)

    def _populate_tree(self, tree, entity_records, kind):
        tree.clear()
        self._task_items[kind] = {}
        filtering = self._filter_mode != FILTER_ALL or self._task_type_filter.get(kind) is not ALL_TASK_TYPES

        # entity_records arrive sorted by (group, name), so plain insertion
        # order keeps both the groups and the entities inside them sorted.
        groups = {}
        total_task_rows = 0
        for record in entity_records:
            tasks = self._filtered_tasks(record["tasks"], kind)
            if filtering and not tasks:
                continue  # a filtered view shouldn't list entities with nothing left in them
            groups.setdefault(record["group"], []).append((record, tasks))
            total_task_rows += len(tasks)

        expand_entities = total_task_rows <= _AUTO_EXPAND_TASK_LIMIT

        for group_name, entries in groups.items():
            entity_count = len(entries)
            group_item = self._header_item(f"{group_name}  ({entity_count})")
            tree.addTopLevelItem(group_item)
            for record, tasks in entries:
                self._add_entity_item(group_item, record, tasks, expand_entities, kind)
            group_item.setExpanded(True)

    def _add_entity_item(self, parent_item, record, tasks, expanded, kind):
        name = record["name"]
        if record.get("canceled"):
            name += "  (canceled)"
        entity_item = QTreeWidgetItem([name, "", "", ""])
        entity_item.setFlags(Qt.ItemIsEnabled)  # a container for its task rows below, not itself selectable
        if record.get("canceled"):
            entity_item.setForeground(0, QColor("#888888"))
        parent_item.addChild(entity_item)

        for task in tasks:
            # The full task list, not the filtered one: CommentPanel's Task
            # Type dropdown should still be able to reach an entity's other
            # tasks after a row was reached through a filter.
            self._add_task_item(entity_item, record["tasks"], task, kind)

        entity_item.setExpanded(expanded)

    def _add_task_item(self, parent_item, entity_tasks, task, kind):
        status_text = (task.get("task_status_short_name") or task.get("task_status_name") or "?").upper()
        type_text = task.get("task_type_name") or "?"
        if task.get("_is_check"):
            # Waiting on this person's review (Kitsu's "My Checks") — worth
            # surfacing rather than looking like any other task.
            type_text += " (Review)"

        # Version is filled in by _apply_revisions once the project's preview
        # files land — see _load_revisions.
        task_item = QTreeWidgetItem(["", type_text, "", status_text])
        task_item.setData(0, Qt.UserRole, (entity_tasks, task))
        task_item.setTextAlignment(2, Qt.AlignCenter)
        self._task_items[kind][task.get("id")] = task_item

        if task.get("_is_mine"):
            # Assigned to the logged-in user — the one distinction worth
            # keeping now that the tree shows everybody's tasks.
            font = task_item.font(1)
            font.setBold(True)
            task_item.setFont(1, font)

        color = task.get("task_status_color")
        if color:
            task_item.setBackground(3, status_display_color(color))
            task_item.setForeground(3, QColor("white"))

        parent_item.addChild(task_item)

    def _header_item(self, group_name):
        item = QTreeWidgetItem([group_name, "", "", ""])
        item.setFlags(Qt.ItemIsEnabled)  # header row, not selectable
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def _on_current_item_changed(self, current, _previous):
        if current is None:
            return
        data = current.data(0, Qt.UserRole)
        if data is not None:  # skip group-header and entity-container rows
            entity_tasks, clicked_task = data
            self.entity_selected.emit(entity_tasks, clicked_task)
