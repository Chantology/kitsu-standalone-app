"""Assemble a project's real Assets/Shots content into the task shape the UI
already speaks.

The Assets/Shots trees used to be built from the logged-in user's own two task
queues: `user/tasks` (tasks assigned to them that aren't complete) merged with
`user/tasks-to-check` (tasks awaiting their review). Neither is the project, so
the trees silently dropped every asset/shot the user has no task on and every
task already marked done — and for anyone whose queue is only review work,
every row that did show up was by definition "Waiting For Approval", which
reads as "the statuses are all wrong". Measured against one real project: 21
tasks over 20 entities were listed, out of 655 tasks over 83 entities.

So the trees are built from the project itself now
(`projects/{id}/assets|shots|tasks`). Those endpoints return plain models
carrying ids where the user-queue endpoints returned denormalized names, so
this module resolves the ids once — ~8 requests, ~0.3s against a 655-task
project — and hands back task dicts with the same `entity_name` /
`task_type_name` / `task_status_short_name` / ... fields the rest of the app
already reads, so nothing downstream has to care where they came from.

Runs on a worker thread (see async_worker.run_async): plain blocking calls, no
Qt in here.
"""

from collections import namedtuple

UNKNOWN_PRIORITY = 999
NO_ASSET_TYPE = "(no asset type)"
NO_SEQUENCE = "(no sequence)"

# assets/shots: entity records (see _entity_record), each with its own tasks.
ProjectContent = namedtuple("ProjectContent", ["assets", "shots", "task_types", "statuses"])


def load_project_content(session, project_id, project_name=None, person_id=None, check_task_ids=()):
    """Every asset and shot in the project, each with every task on it —
    including entities nobody has a task on, and tasks belonging to other
    people, which is the whole point."""
    assets = session.all_assets_for_project(project_id)
    shots = session.all_shots_for_project(project_id)
    sequences = session.all_sequences_for_project(project_id)
    episodes = session.all_episodes_for_project(project_id)
    asset_types = session.all_asset_types_for_project(project_id)
    task_types = session.all_task_types_for_project(project_id)
    statuses = merged_task_statuses(session, project_id)
    tasks = session.all_tasks_for_project(project_id)

    asset_type_names = {t.get("id"): t.get("name") for t in asset_types}
    sequence_labels = _sequence_labels(sequences, episodes)

    entities_by_id = {}
    asset_records = []
    shot_records = []
    # Canceled entities are kept, marked rather than dropped (see the
    # `canceled` flag on each record): on one real project 14 of 60 shots
    # were canceled, and quietly hiding a quarter of the shot list is exactly
    # the "it isn't fetching all the shots" problem this module exists to fix.
    for asset in assets:
        group = asset_type_names.get(asset.get("entity_type_id")) or NO_ASSET_TYPE
        record = _entity_record(asset, "Asset", group)
        entities_by_id[record["id"]] = record
        asset_records.append(record)
    for shot in shots:
        group = sequence_labels.get(shot.get("parent_id")) or NO_SEQUENCE
        record = _entity_record(shot, "Shot", group)
        entities_by_id[record["id"]] = record
        shot_records.append(record)

    task_types_by_id = {t.get("id"): t for t in task_types}
    statuses_by_id = {s.get("id"): s for s in statuses}
    check_task_ids = set(check_task_ids)

    for task in tasks:
        entity = entities_by_id.get(task.get("entity_id"))
        if entity is None:
            continue  # a concept/edit/episode task, or an entity skipped above
        entity["tasks"].append(
            _denormalized_task(
                task,
                entity,
                task_types_by_id,
                statuses_by_id,
                project_id,
                project_name,
                person_id,
                check_task_ids,
            )
        )

    priority_by_type_id = {
        t.get("id"): t.get("priority") if t.get("priority") is not None else UNKNOWN_PRIORITY
        for t in task_types
    }
    for record in asset_records + shot_records:
        # Same left-to-right order Kitsu's own UI uses for task types.
        record["tasks"].sort(
            key=lambda task: (
                priority_by_type_id.get(task.get("task_type_id"), UNKNOWN_PRIORITY),
                task.get("task_type_name") or "",
            )
        )

    asset_records.sort(key=_entity_sort_key)
    shot_records.sort(key=_entity_sort_key)
    return ProjectContent(asset_records, shot_records, task_types, statuses)


def merged_task_statuses(session, project_id):
    """The project's configured status list, plus any status it's missing.

    A project's configured set is not the set its tasks actually use: on one
    real project, 415 of 655 tasks sat on "Todo" — a status the project's own
    list doesn't contain at all. Resolving a task's status from the project
    list alone therefore leaves those tasks with no status to display."""
    project_statuses = []
    if project_id:
        try:
            project_statuses = list(session.all_task_statuses_for_project(project_id))
        except Exception:
            project_statuses = []

    try:
        global_statuses = session.all_task_statuses()
    except Exception:
        return project_statuses

    merged = list(project_statuses)
    known_ids = {status.get("id") for status in merged}
    for status in global_statuses:
        if status.get("id") not in known_ids:
            merged.append(status)
    return merged


def statuses_for_task(session, project_id, task):
    """The statuses a status picker should offer for one task: the project's
    configured list, plus the task's current status if that list is missing it.

    Not the full merged list — the project's set is what Kitsu itself offers,
    and widening it invites setting a status the production doesn't use. But a
    picker that can't represent the task's *current* status is worse than
    either: it silently sits on the first entry, so posting a comment on a
    "Todo" task would have set it to whatever came first ("Approved")."""
    project_statuses = []
    if project_id:
        try:
            project_statuses = list(session.all_task_statuses_for_project(project_id))
        except Exception:
            project_statuses = []
    if not project_statuses:
        return session.all_task_statuses()

    current_id = task.get("task_status_id")
    if current_id and not any(status.get("id") == current_id for status in project_statuses):
        for status in session.all_task_statuses():
            if status.get("id") == current_id:
                project_statuses.insert(0, status)
                break
    return project_statuses


def _sequence_labels(sequences, episodes):
    """sequence id -> label to group its shots under. On an episodic project
    two episodes can hold same-named sequences, so the episode is part of the
    label there ("E01 / SQ A Intro") and left out entirely when there are no
    episodes."""
    episode_names = {episode.get("id"): episode.get("name") for episode in episodes}
    labels = {}
    for sequence in sequences:
        name = sequence.get("name") or NO_SEQUENCE
        episode_name = episode_names.get(sequence.get("parent_id"))
        labels[sequence.get("id")] = f"{episode_name} / {name}" if episode_name else name
    return labels


def _entity_record(entity, kind, group_name):
    return {
        "id": entity.get("id"),
        "name": entity.get("name") or "?",
        "kind": kind,  # "Asset" or "Shot"
        "group": group_name,  # asset type, or (episode /) sequence
        "preview_file_id": entity.get("preview_file_id"),
        "canceled": bool(entity.get("canceled")),
        "tasks": [],
    }


def _entity_sort_key(record):
    return (record["group"].lower(), record["name"].lower())


def latest_revision_by_task_id(session, project_id):
    """task id -> highest published revision number, for the whole project.

    Split out of load_project_content and fetched separately because it is by
    far the most expensive request of the set — 7195 preview files and ~13s on
    one real project, against ~0.5s for everything else combined. The trees are
    worth showing before that lands, with the Version column filling in after.

    (A task carries `last_preview_file_id` but not the revision number, and
    resolving that per row would be one request per task row.)"""
    latest = {}
    for preview in session.all_preview_files_for_project(project_id):
        task_id = preview.get("task_id")
        revision = preview.get("revision")
        if task_id is None or revision is None:
            continue
        if revision > latest.get(task_id, -1):
            latest[task_id] = revision
    return latest


def _denormalized_task(
    task,
    entity,
    task_types_by_id,
    statuses_by_id,
    project_id,
    project_name,
    person_id,
    check_task_ids,
):
    task_type = task_types_by_id.get(task.get("task_type_id")) or {}
    status = statuses_by_id.get(task.get("task_status_id")) or {}

    enriched = dict(task)
    enriched.update(
        {
            "project_id": project_id,
            "project_name": project_name,
            "entity_name": entity["name"],
            "entity_preview_file_id": entity["preview_file_id"],
            # Zou's user-task payload puts the asset type in entity_type_name
            # for an asset task and the sequence in sequence_name for a shot
            # task; mirrored here so anything already reading those keeps
            # working against project-sourced tasks.
            "entity_type_name": entity["group"] if entity["kind"] == "Asset" else None,
            "sequence_name": entity["group"] if entity["kind"] == "Shot" else None,
            "task_type_name": task_type.get("name") or "?",
            "task_type_for_entity": task_type.get("for_entity") or entity["kind"],
            "task_status_name": status.get("name"),
            "task_status_short_name": status.get("short_name"),
            "task_status_color": status.get("color"),
            "_is_mine": bool(person_id) and person_id in (task.get("assignees") or []),
            "_is_check": task.get("id") in check_task_ids,
        }
    )
    return enriched


