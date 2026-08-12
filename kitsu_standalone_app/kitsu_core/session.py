"""Thin wrapper around `gazu` (the Kitsu/Zou API client) for this app's needs.

All Kitsu/Zou protocol details (URL shape, auth headers, endpoint paths) are
handled by `gazu` itself. This module only adapts its calls to what the UI
needs and translates failures into specific, user-facing messages instead of
a generic "Login failed".

Unlike the DCC plugins this app's login/comment logic is ported from, `gazu`
is a normal pip dependency here (see pyproject.toml) — there is no bundled-
Python vendoring constraint to work around, so this module skips the
plugins' deferred-import/vendor-path/stale-module-cache handling entirely.
"""

import gazu
import gazu.exception


class KitsuConnectionError(Exception):
    """A specific, user-facing reason a Kitsu/Zou call failed."""


def normalize_host(url):
    """Turn a user-entered server URL into the `.../api` host gazu expects."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise KitsuConnectionError("Server URL is empty.")
    if not url.endswith("/api"):
        url = f"{url}/api"
    return url


class KitsuSession:
    """One authenticated connection to a Kitsu/Zou instance."""

    def __init__(self, host):
        self.host = normalize_host(host)
        # use_refresh_token=True so gazu silently mints a fresh access token
        # from the refresh token whenever a request hits a 401 instead of
        # failing the instant the short-lived access token expires mid
        # session. Without it, the refresh token below is only useful at
        # the *next* login, not within this one.
        self.client = gazu.client.create_client(self.host, use_refresh_token=True)

    def login(self, email, password):
        exceptions = gazu.exception
        try:
            gazu.log_in(email, password, client=self.client)
        except exceptions.AuthFailedException as exc:
            raise KitsuConnectionError(
                f"{self.host} rejected the login — check the email/password."
            ) from exc
        except exceptions.RouteNotFoundException as exc:
            raise KitsuConnectionError(
                f"{self.host} has no route at '{exc}' (404). The server URL "
                "is likely wrong for the API — the Zou API may live at a "
                "different host or path than the Kitsu web UI. Ask whoever "
                "manages the server for the correct API URL."
            ) from exc
        except exceptions.NotAllowedException as exc:
            raise KitsuConnectionError(
                f"{self.host} refused the request (403): {exc}"
            ) from exc
        except exceptions.ServerErrorException as exc:
            raise KitsuConnectionError(
                f"{self.host} returned a server error: {exc}"
            ) from exc
        except Exception as exc:
            raise KitsuConnectionError(f"Could not reach {self.host}: {exc}") from exc

    def login_with_token(self, refresh_token):
        """Authenticate using a refresh token saved from a previous
        email/password login (see get_refresh_token) instead of typing the
        password again."""
        gazu.client.set_tokens(
            {"access_token": None, "refresh_token": refresh_token},
            client=self.client,
        )
        try:
            self.client.refresh_access_token()
        except gazu.exception.NotAuthenticatedException as exc:
            raise KitsuConnectionError(
                "The saved login has expired or was revoked — log in with "
                "your email and password once to get a new one."
            ) from exc
        except Exception as exc:
            raise KitsuConnectionError(f"Could not use the saved login: {exc}") from exc

    def get_refresh_token(self):
        """The refresh token gazu stored on this session's client after a
        successful email/password login — save this to skip the password
        next time (see login_with_token). None if this session logged in
        with a token to begin with (there's nothing new to save)."""
        return self.client.refresh_token

    def current_user(self):
        return gazu.client.get_current_user(client=self.client)

    def get_person(self, person_id):
        return gazu.person.get_person(person_id, client=self.client)

    def download_person_avatar(self, person, file_path):
        return gazu.files.download_person_avatar(person, file_path, client=self.client)

    def project(self, project_id):
        return gazu.project.get_project(project_id, client=self.client)

    def download_project_thumbnail(self, project, file_path):
        """Download a project's avatar/thumbnail. Raises if the project has
        none set — callers should treat that as "no thumbnail" rather than
        an error worth surfacing (see the project's own `has_avatar` flag,
        which callers should check first to avoid this call entirely)."""
        return gazu.files.download_project_avatar(project, file_path, client=self.client)

    def get_preview_file(self, preview_file_id):
        return gazu.files.get_preview_file(preview_file_id, client=self.client)

    def get_comment(self, comment_id):
        return gazu.task.get_comment(comment_id, client=self.client)

    def project_news(self, project_id):
        """Recent activity across every task in the project (comments,
        status changes, previews) — Kitsu's own "News" feed. gazu doesn't
        wrap this endpoint, so this hits Zou's REST route directly (same
        approach as update_comment_checklist, for the same reason: no
        gazu function exists to call instead)."""
        url = gazu.client.get_full_url(f"data/projects/{project_id}/news", client=self.client)
        headers = self.client.make_auth_header()
        try:
            response = self.client.session.get(url, headers=headers)
        except Exception as exc:
            raise KitsuConnectionError(
                f"Could not reach {self.host} for the news feed: {exc}"
            ) from exc

        if not (200 <= response.status_code < 300):
            raise KitsuConnectionError(
                f"Kitsu rejected the news feed request — HTTP {response.status_code} on GET {url}"
            )
        return response.json().get("data", [])

    def all_tasks_to_do(self):
        """Every not-yet-complete task assigned to the logged-in user,
        across all of their projects — the source for the task picker."""
        return gazu.user.all_tasks_to_do(client=self.client)

    def all_task_statuses_for_project(self, project):
        return gazu.task.all_task_statuses_for_project(project, client=self.client)

    def all_task_types_for_project(self, project):
        return gazu.task.all_task_types_for_project(project, client=self.client)

    def all_task_statuses(self):
        """Every task status in the database, not just the ones a
        project's own settings list — used as a fallback when a project's
        own status list can't be fetched."""
        return gazu.task.all_task_statuses(client=self.client)

    def all_comments_for_task(self, task):
        return gazu.task.all_comments_for_task(task, client=self.client)

    def update_comment_checklist(self, comment_id, checklist):
        """Persist a comment's checklist (with updated "checked" flags).

        Sends a body with ONLY `checklist` (no "id"), bypassing
        `gazu.client.put()`/`gazu.task.update_comment()` directly so a
        failure surfaces Zou's actual JSON error body instead of just a
        bare route string (gazu's own exception classes discard it).
        """
        url = gazu.client.get_full_url(f"data/comments/{comment_id}", client=self.client)
        headers = self.client.make_auth_header()
        headers["Content-Type"] = "application/json"
        try:
            response = self.client.session.put(
                url, json={"checklist": checklist}, headers=headers
            )
        except Exception as exc:
            raise KitsuConnectionError(
                f"Could not reach {self.host} to update the checklist: {exc}"
            ) from exc

        if 200 <= response.status_code < 300:
            return response.json()

        try:
            body = response.json()
        except ValueError:
            body = response.text
        raise KitsuConnectionError(
            f"Kitsu rejected this checklist update — HTTP {response.status_code} "
            f"on PUT {url}. Server response: {body}"
        )

    def publish_preview(self, task, task_status, comment="", preview_file_path=None, revision=None):
        """Publish a comment with a proper Kitsu preview/revision attached
        (shows up in the task's revision history), instead of a plain
        comment attachment."""
        return gazu.task.publish_preview(
            task,
            task_status,
            comment=comment,
            preview_file_path=preview_file_path,
            revision=revision,
            client=self.client,
        )

    def add_comment(self, task, task_status, comment=""):
        """Post a status change + comment with no preview file attached."""
        return gazu.task.add_comment(task, task_status, comment=comment, client=self.client)

    def download_attachment(self, attachment_file, file_path):
        return gazu.files.download_attachment_file(attachment_file, file_path, client=self.client)

    def download_preview(self, preview_file, file_path):
        return gazu.files.download_preview_file(preview_file, file_path, client=self.client)

    def download_preview_thumbnail(self, preview_file, file_path):
        return gazu.files.download_preview_file_thumbnail(preview_file, file_path, client=self.client)
