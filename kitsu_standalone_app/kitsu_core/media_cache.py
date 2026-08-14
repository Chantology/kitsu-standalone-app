"""Where downloaded Kitsu media is cached locally, and when it goes away.

Every preview, attachment, frame strip, movie and paint-over the app fetches is
written under the OS temp directory so it doesn't have to be fetched twice. That
is production material sitting on disk, and it used to sit there forever — six
different directories, each created ad hoc by whichever widget needed it, none
of them ever cleaned. On a roaming profile or a shared workstation that's a pile
of unreviewed studio media nobody remembers is there, and it grows without
limit.

So the paths live here instead of being spelled out at each call site, and there
are two ways they get emptied: `prune_old()` at startup drops anything older than
a week, and `clear_all()` runs on log out, on the reasoning that logging out is
the point at which someone is done with a production's media.

The temp directory is per-user on Windows and mode 0700-ish on Linux, so this is
about retention rather than about other people on the machine reading it.
"""

import os
import shutil
import tempfile
import time

# Kept as bare names so every directory this app creates is discoverable from
# one place — see the module docstring.
_DIRECTORY_NAMES = (
    "kitsu_standalone_previews",
    "kitsu_standalone_attachments",
    "kitsu_standalone_url_attachments",
    "kitsu_standalone_paintovers",
    "kitsu_standalone_tiles",
    "kitsu_standalone_movies",
    "kitsu_standalone_news",
)

PREVIEWS = "kitsu_standalone_previews"
ATTACHMENTS = "kitsu_standalone_attachments"
URL_ATTACHMENTS = "kitsu_standalone_url_attachments"
PAINT_OVERS = "kitsu_standalone_paintovers"
TILES = "kitsu_standalone_tiles"
MOVIES = "kitsu_standalone_movies"
NEWS = "kitsu_standalone_news"

MAX_AGE_DAYS = 7


def directory(name):
    """The path of one cache directory. Does not create it."""
    return os.path.join(tempfile.gettempdir(), name)


def ensure_directory(name):
    """The path of one cache directory, created if needed."""
    path = directory(name)
    os.makedirs(path, exist_ok=True)
    return path


def all_directories():
    return [directory(name) for name in _DIRECTORY_NAMES]


def total_bytes():
    total = 0
    for path in all_directories():
        for root, _dirs, files in os.walk(path):
            for file_name in files:
                try:
                    total += os.path.getsize(os.path.join(root, file_name))
                except OSError:
                    continue
    return total


def prune_old(max_age_days=MAX_AGE_DAYS):
    """Deletes cached files older than `max_age_days`. Returns how many went.

    Safe to call at startup: anything still wanted gets re-fetched on demand,
    and nothing is open yet."""
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    removed = 0
    for path in all_directories():
        if not os.path.isdir(path):
            continue
        for root, _dirs, files in os.walk(path):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                try:
                    if os.path.getmtime(file_path) < cutoff:
                        os.remove(file_path)
                        removed += 1
                except OSError:
                    continue  # in use, or already gone — either way, skip it
    return removed


def clear_all():
    """Removes every cache directory. Returns how many were removed.

    Failures are ignored on purpose: a file held open by a viewer shouldn't turn
    logging out into an error."""
    removed = 0
    for path in all_directories():
        if not os.path.isdir(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.isdir(path):
            removed += 1
    return removed
