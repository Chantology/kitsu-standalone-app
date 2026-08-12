"""Locally-saved draft comments.

Kitsu/Zou has no draft-comment concept server-side — a comment only exists
once it's actually posted. This covers the common "write feedback now, wait
for the render, attach it once it's ready" workflow: the text/status/
version are saved locally, keyed by task id, and survive an app restart
(same QSettings store as credentials.py) until the comment is actually
posted, at which point the caller clears the draft.
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
    """Returns {"comment_text", "status_id", "version", "saved_at"} or None."""
    return _load_all().get(task_id)


def save_draft(task_id, comment_text, status_id, version):
    drafts = _load_all()
    drafts[task_id] = {
        "comment_text": comment_text,
        "status_id": status_id,
        "version": version,
        "saved_at": datetime.now().isoformat(timespec="minutes"),
    }
    _save_all(drafts)


def clear_draft(task_id):
    drafts = _load_all()
    if task_id in drafts:
        del drafts[task_id]
        _save_all(drafts)
