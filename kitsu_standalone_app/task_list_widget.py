"""Project info + 'my tasks' picker, scoped to one active project at a time.

Calls `gazu.user.all_tasks_to_do` via `KitsuSession.all_tasks_to_do`, which
returns every not-yet-complete task assigned to the logged-in user, one
entry per (entity, task type) pair — so the same asset/shot can appear more
than once if the user is assigned more than one task type on it (e.g.
Modeling and Shading on the same asset). `task_type_for_entity` ("Asset" or
"Shot") is the split between the Assets/Shots tabs; each then groups by the
asset type (`entity_type_name` — for asset tasks this IS the asset type
name, e.g. "Characters"/"Props", not a separate field) or by sequence
(`sequence_name`, shot tasks only).

The active project's name is shown in the main window's title bar instead
of repeated in every row here, so unlike an earlier version of this widget,
there's no project-level grouping in the trees — a project selector (shown
only when the assigned tasks actually span more than one project) picks
which project's tasks are displayed, and drives the Project tab + window
title via `project_changed`.

Each tree is 3 levels: group header (Sequence/Asset Type) -> entity (the
asset/shot name, not itself selectable) -> one row per task type on that
entity, sorted the same left-to-right order Kitsu's own UI uses (task
type `priority`), with that task's latest published version and status
(short form, e.g. "WIP") as the trailing columns. The version number isn't
on the task dict itself — it's resolved lazily per row from the task's
`last_preview_file_id` (see _load_task_version) rather than blocking the
whole tree's population on it. Clicking a task row emits
`entity_selected(entity_tasks, clicked_task)` — the full sibling list (so
CommentPanel's Task Type dropdown can still switch between them) plus
which one was actually clicked (so that's the one preselected).

Beyond a person's own assigned tasks (`all_tasks_to_do`), this also pulls
in Kitsu's "My Checks" (`all_tasks_requiring_feedback`) — tasks pending
this person's review (a supervisor/production manager role) even when
they're not the assigned artist. The two lists are merged and deduped by
task id; a task that's only there because it needs this person's review
(not also assigned to them) gets a "(Review)" marker in the Task Type
column so it's clear why it showed up.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .async_worker import run_async
from .project_info_widget import ProjectInfoWidget
from .status_colors import status_display_color

_NAME_COLUMN_WIDTH = 180
_TASK_TYPE_COLUMN_WIDTH = 150
_VERSION_COLUMN_WIDTH = 60
# Status trails each row and stays a small fixed-width badge rather than
# stretching into a colored bar as the window resizes.
_STATUS_COLUMN_WIDTH = 90
_UNKNOWN_PRIORITY = 999


def _merge_task_lists(assigned_tasks, check_tasks):
    """Combines "my tasks" and "my checks", deduped by task id — a task
    already present because it's assigned to this person takes priority
    over the same task also appearing in their checks queue (rare, but
    possible if they're both the artist and the reviewer)."""
    merged = []
    seen_ids = set()
    for task in assigned_tasks:
        task["_is_check_only"] = False
        merged.append(task)
        seen_ids.add(task.get("id"))
    for task in check_tasks:
        if task.get("id") in seen_ids:
            continue
        task["_is_check_only"] = True
        merged.append(task)
        seen_ids.add(task.get("id"))
    return merged


class TaskListWidget(QWidget):
    entity_selected = Signal(object, object)  # (all tasks on the entity, the one that was clicked)
    project_changed = Signal(object)  # emits the full project dict for the newly-active project
    load_failed = Signal(object)  # emits the exception

    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self._async_workers = []
        self._all_tasks = []
        self._active_project_id = None
        self._project_cache = {}  # project_id -> full project dict (fetched once, reused on reselect)
        self._task_type_cache = {}  # project_id -> that project's task types (for priority-based sorting)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Only shown once there's more than one project to choose between —
        # see _setup_projects.
        self.project_combo = QComboBox()
        self.project_combo.setVisible(False)
        self.project_combo.currentIndexChanged.connect(self._on_project_combo_changed)
        layout.addWidget(self.project_combo)

        self.tabs = QTabWidget()
        self.project_info = ProjectInfoWidget()
        self.assets_tree = self._build_tree(["Asset", "Task Type", "Version", "Status"])
        self.shots_tree = self._build_tree(["Shot", "Task Type", "Version", "Status"])
        self.tabs.addTab(self.project_info, "Project")
        self.tabs.addTab(self.assets_tree, "Assets")
        self.tabs.addTab(self.shots_tree, "Shots")
        layout.addWidget(self.tabs)

    def _build_tree(self, headers):
        tree = QTreeWidget()
        tree.setHeaderLabels(headers)
        tree.setColumnWidth(0, _NAME_COLUMN_WIDTH)
        tree.setColumnWidth(1, _TASK_TYPE_COLUMN_WIDTH)
        tree.setColumnWidth(2, _VERSION_COLUMN_WIDTH)
        # Status is a colored cell (see _add_task_item) — without this,
        # Qt's default "stretch the last column to fill the view" turns it
        # into a long colorful bar that grows with the window instead of
        # staying a small badge with empty space after it.
        tree.header().setStretchLastSection(False)
        tree.currentItemChanged.connect(self._on_current_item_changed)
        return tree

    def load_tasks(self, session):
        self.session = session
        self.project_info.set_session(session)

        def work():
            assigned_tasks = session.all_tasks_to_do()
            check_tasks = session.all_tasks_requiring_feedback()
            return _merge_task_lists(assigned_tasks, check_tasks)

        def on_done(tasks):
            self._all_tasks = tasks
            self._setup_projects(tasks)

        def on_error(exc):
            self.load_failed.emit(exc)

        run_async(self, work, on_done, on_error)

    # ---- project selection ----

    def _setup_projects(self, tasks):
        names_by_id = {}
        for task in tasks:
            names_by_id.setdefault(task.get("project_id"), task.get("project_name") or "?")
        project_ids = sorted(names_by_id, key=lambda pid: names_by_id[pid])

        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for project_id in project_ids:
            self.project_combo.addItem(names_by_id[project_id], project_id)
        self.project_combo.blockSignals(False)
        self.project_combo.setVisible(len(project_ids) > 1)

        if project_ids:
            self._activate_project(project_ids[0])
        else:
            self._active_project_id = None
            self._populate_tree(self.assets_tree, [], {}, group_field="entity_type_name", group_fallback="(no asset type)")
            self._populate_tree(self.shots_tree, [], {}, group_field="sequence_name", group_fallback="(no sequence)")
            self.project_info.show_no_tasks_message()

    def _on_project_combo_changed(self, index):
        if index < 0:
            return
        self._activate_project(self.project_combo.itemData(index))

    def _activate_project(self, project_id):
        self._active_project_id = project_id
        self._load_project_details(project_id)

    def _load_project_details(self, project_id):
        cached_project = self._project_cache.get(project_id)
        cached_types = self._task_type_cache.get(project_id)
        if cached_project and cached_types is not None:
            self._apply_active_project(project_id, cached_project, cached_types)
            return

        def work():
            project = cached_project or self.session.project(project_id)
            task_types = cached_types if cached_types is not None else self.session.all_task_types_for_project(project_id)
            return project, task_types

        def on_done(result):
            project, task_types = result
            self._project_cache[project_id] = project
            self._task_type_cache[project_id] = task_types
            if project_id != self._active_project_id:
                return  # the user picked a different project while this was in flight
            self._apply_active_project(project_id, project, task_types)

        def on_error(exc):
            self.load_failed.emit(exc)

        run_async(self, work, on_done, on_error)

    def _apply_active_project(self, project_id, project, task_types):
        self.project_info.set_project(project)
        self.project_changed.emit(project)

        priority_by_type_id = {t["id"]: t.get("priority", _UNKNOWN_PRIORITY) for t in task_types}
        project_tasks = [t for t in self._all_tasks if t.get("project_id") == project_id]
        asset_tasks = [t for t in project_tasks if t.get("task_type_for_entity") == "Asset"]
        shot_tasks = [t for t in project_tasks if t.get("task_type_for_entity") == "Shot"]
        self._populate_tree(
            self.assets_tree, asset_tasks, priority_by_type_id,
            group_field="entity_type_name", group_fallback="(no asset type)",
        )
        self._populate_tree(
            self.shots_tree, shot_tasks, priority_by_type_id,
            group_field="sequence_name", group_fallback="(no sequence)",
        )

    # ---- grouping ----

    def _populate_tree(self, tree, tasks, priority_by_type_id, group_field, group_fallback):
        tree.clear()

        # group (asset type / sequence) -> entity_id -> {entity_name, tasks: [...]}
        groups = {}
        for task in tasks:
            group_name = task.get(group_field) or group_fallback
            entity_id = task.get("entity_id")

            group = groups.setdefault(group_name, {})
            entity = group.setdefault(entity_id, {"entity_name": task.get("entity_name") or "?", "tasks": []})
            entity["tasks"].append(task)

        for group_name in sorted(groups):
            group_item = self._header_item(group_name)
            tree.addTopLevelItem(group_item)

            for entity in groups[group_name].values():
                self._add_entity_item(group_item, entity, priority_by_type_id)

            group_item.setExpanded(True)

    def _add_entity_item(self, parent_item, entity, priority_by_type_id):
        entity_item = QTreeWidgetItem([entity["entity_name"], "", "", ""])
        entity_item.setFlags(Qt.ItemIsEnabled)  # a container for its task rows below, not itself selectable
        parent_item.addChild(entity_item)

        sorted_tasks = sorted(
            entity["tasks"],
            key=lambda t: (priority_by_type_id.get(t.get("task_type_id"), _UNKNOWN_PRIORITY), t.get("task_type_name") or ""),
        )
        for task in sorted_tasks:
            self._add_task_item(entity_item, entity["tasks"], task)

        entity_item.setExpanded(True)

    def _add_task_item(self, parent_item, entity_tasks, task):
        status_text = (task.get("task_status_short_name") or task.get("task_status_name") or "?").upper()
        type_text = task.get("task_type_name") or "?"
        if task.get("_is_check_only"):
            # Not assigned to this person — it's here because it's pending
            # their review (Kitsu's "My Checks"), which is worth surfacing
            # rather than looking identical to an assigned task.
            type_text += " (Review)"
        task_item = QTreeWidgetItem(["", type_text, "", status_text])
        task_item.setData(0, Qt.UserRole, (entity_tasks, task))
        task_item.setTextAlignment(2, Qt.AlignCenter)

        color = task.get("task_status_color")
        if color:
            task_item.setBackground(3, status_display_color(color))
            task_item.setForeground(3, QColor("white"))

        parent_item.addChild(task_item)
        self._load_task_version(task_item, task)

    def _load_task_version(self, task_item, task):
        preview_file_id = task.get("last_preview_file_id")
        if not preview_file_id:
            return

        def work():
            return self.session.get_preview_file(preview_file_id)

        def on_done(preview_file):
            revision = preview_file.get("revision")
            if revision is None:
                return
            try:
                task_item.setText(2, f"v{revision}")
            except RuntimeError:
                pass  # the tree was repopulated (this item deleted) before this finished

        def on_error(_exc):
            pass  # not every task has had a preview published yet — silent

        run_async(self, work, on_done, on_error)

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
