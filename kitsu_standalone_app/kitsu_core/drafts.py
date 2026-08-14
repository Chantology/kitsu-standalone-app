"""Locally-saved draft comments.

Kitsu/Zou has no draft-comment concept server-side — a comment only exists
once it's actually posted. This covers the common "write feedback now, wait
for the render, attach it once it's ready" workflow: everything the composer
holds — text, status, checklist, links, attachments, client visibility, version
— is saved locally, keyed by task id, and survives an app restart (same
QSettings store as credentials.py) until the comment is actually posted, at
which point the caller clears the draft.
"""

import json
from datetime import datetime

from PySide6.QtCore import QSettings

_ORG = "Kitsu"
_APP = "StandaloneApp"
_SETTINGS_KEY = "draft_comments"


def _settings():
    return QSettings(_ORG, _APP)


def _load_all():
    raw = _settings().value(_SETTINGS_KEY, "") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _save_all(drafts):
    settings = _settings()
    settings.setValue(_SETTINGS_KEY, json.dumps(drafts))
    settings.sync()


def load_draft(task_id):
    """Returns the saved draft dict or None. Callers must treat every field as
    optional — a draft written by an older build has only comment_text/
    status_id/version."""
    return _load_all().get(task_id)


def save_draft(
    task_id,
    comment_text,
    status_id,
    version,
    checklist=None,
    links=None,
    attachments=None,
    for_client=False,
    set_thumbnail=False,
    mode=None,
):
    """Saves everything the composer holds, so reopening a task restores the
    comment as it was being written — not just its text. `attachments` are local
    file paths, which may of course be gone by the time the draft is reopened;
    that's the user's problem to notice, same as with any saved path."""
    drafts = _load_all()
    drafts[task_id] = {
        "comment_text": comment_text,
        "status_id": status_id,
        "version": version,
        "checklist": checklist or [],
        "links": links or [],
        "attachments": attachments or [],
        "for_client": bool(for_client),
        "set_thumbnail": bool(set_thumbnail),
        "mode": mode,
        "saved_at": datetime.now().isoformat(timespec="minutes"),
    }
    _save_all(drafts)


def clear_draft(task_id):
    drafts = _load_all()
    if task_id in drafts:
        del drafts[task_id]
        _save_all(drafts)
