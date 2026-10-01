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

import re

import gazu
import gazu.exception


class KitsuConnectionError(Exception):
    """A specific, user-facing reason a Kitsu/Zou call failed."""


# 2FA methods a code can be typed in for. Zou also supports "fido"
# (security keys), which needs a WebAuthn client this app doesn't have —
# deliberately left out until someone actually needs it.
TWO_FACTOR_METHODS = ("totp", "email_otp", "recovery_code")


class TwoFactorRequired(KitsuConnectionError):
    """The email/password were accepted but the account has 2FA enabled —
    log in again with a code for one of `methods` (a subset of
    TWO_FACTOR_METHODS, `preferred` first). For "email_otp" the code has
    already been emailed by the time this is raised."""

    def __init__(self, message, methods, preferred):
        super().__init__(message)
        self.methods = methods
        self.preferred = preferred


class WrongTwoFactorCode(KitsuConnectionError):
    """The 2FA code was rejected (wrong, expired or already used). The
    methods from the preceding TwoFactorRequired still apply."""


def two_factor_prompt(method):
    """The status line asking for a code of the given 2FA method."""
    return {
        "totp": "Enter the code from your authenticator app, then log in again.",
        "email_otp": "A code was sent to your email — enter it, then log in again.",
        "recovery_code": "Enter one of your Kitsu recovery codes, then log in again.",
    }.get(method, "Enter your two-factor code, then log in again.")


# How much of a server error body is worth putting in a user-facing message.
_ERROR_BODY_LIMIT = 300


def normalize_host(url):
    """Turn a user-entered server URL into the `.../api` host gazu expects.

    A missing scheme becomes `https://` rather than being passed through: typing
    "kitsu.example.studio" used to produce a URL requests can't parse and a
    confusing failure, and defaulting to plain http would send the password
    unencrypted. `http://` is still accepted for an internal server that really
    is served that way — see is_insecure_host, which the login screen uses to
    make that a deliberate choice rather than a silent one."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise KitsuConnectionError("Server URL is empty.")

    # Matched on "scheme:" rather than "scheme://" so schemes that don't use the
    # double slash are caught too — "javascript:alert(1)" has no "//" and would
    # otherwise be treated as a bare hostname and quietly prefixed with https://.
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", url)
    if not scheme_match:
        url = f"https://{url}"
    elif scheme_match.group(1).lower() not in ("http", "https"):
        raise KitsuConnectionError(
            f"{scheme_match.group(1)}: is not a supported server URL — use "
            "https:// (or http:// for an internal server)."
        )

    if not url.endswith("/api"):
        url = f"{url}/api"
    return url


def is_insecure_host(url):
    """True when this URL would send the login over unencrypted http."""
    return (url or "").strip().lower().startswith("http://")


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

    def login(self, email, password, two_factor_method=None, two_factor_code=None):
        """Log in with email/password, plus a 2FA code once the server has
        asked for one (see TwoFactorRequired).

        Posts to auth/login directly instead of via `gazu.log_in`: gazu
        turns Zou's 400 into an exception that keeps only the message text,
        dropping the `missing_OTP` / `wrong_OTP` flags and the list of the
        account's 2FA methods that decide whether a code field is needed at
        all. Same reasoning as update_comment_checklist below.
        """
        payload = {"email": email, "password": password}
        if two_factor_method and two_factor_code:
            payload[two_factor_method] = two_factor_code.strip()

        path = "auth/login"
        url = gazu.client.get_full_url(path, client=self.client)
        try:
            response = self.client.session.post(url, json=payload)
        except Exception as exc:
            raise KitsuConnectionError(f"Could not reach {self.host}: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        status = response.status_code

        if 200 <= status < 300 and body.get("login") is not False and body.get("access_token"):
            gazu.client.set_tokens(body, client=self.client)
            return

        if body.get("missing_OTP"):
            enabled = body.get("two_factor_authentication_enabled") or []
            methods = [m for m in TWO_FACTOR_METHODS if m in enabled]
            if not methods:
                raise KitsuConnectionError(
                    "This account's two-factor method isn't supported here. "
                    "Enable an authenticator app or email codes in Kitsu."
                )
            preferred = body.get("preferred_two_factor_authentication")
            if preferred not in methods:
                # e.g. a security-key-only account: only a recovery code is left.
                preferred = methods[0]
            if preferred == "email_otp":
                self.send_email_code(email)
            raise TwoFactorRequired(two_factor_prompt(preferred), methods, preferred)

        if body.get("wrong_OTP"):
            raise WrongTwoFactorCode("That code was wrong or has expired — try again.")

        if status == 404:
            raise KitsuConnectionError(
                f"{self.host} has no route at '{path}' (404). The server URL "
                "is likely wrong for the API — the Zou API may live at a "
                "different host or path than the Kitsu web UI. Ask whoever "
                "manages the server for the correct API URL."
            )
        if status == 403:
            raise KitsuConnectionError(
                f"{self.host} refused the request (403): {body.get('message') or path}"
            )
        if status >= 500:
            raise KitsuConnectionError(
                f"{self.host} returned a server error (HTTP {status}): "
                f"{body.get('message') or response.text[:200]}"
            )
        raise KitsuConnectionError(
            f"{self.host} rejected the login — check the email/password."
        )

    def send_email_code(self, email):
        """Have Kitsu email a fresh 2FA code (for "Send Code")."""
        try:
            gazu.send_email_otp(email, client=self.client)
        except Exception as exc:
            raise KitsuConnectionError(
                f"Could not ask {self.host} to email a code: {exc}"
            ) from exc

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

    def all_tasks_requiring_feedback(self):
        """Kitsu's "My Checks" — tasks pending this person's review/approval
        (e.g. a supervisor or production manager's queue), regardless of
        whether they're the assigned artist. Same denormalized task shape
        as all_tasks_to_do (both are Zou's own task listing endpoints,
        just filtered differently), and empty for anyone with no review
        responsibilities — no role check needed on this end."""
        return gazu.user.all_tasks_requiring_feedback(client=self.client)

    def all_open_projects(self):
        """Every project the logged-in user is on the team of (an admin gets
        all of them). Deliberately not derived from their own task list: a
        supervisor or production manager can have no task assigned in a
        project they still need to open."""
        return gazu.project.all_open_projects(client=self.client)

    def all_assets_for_project(self, project):
        return gazu.asset.all_assets_for_project(project, client=self.client)

    def all_shots_for_project(self, project):
        return gazu.shot.all_shots_for_project(project, client=self.client)

    def all_sequences_for_project(self, project):
        return gazu.shot.all_sequences_for_project(project, client=self.client)

    def all_episodes_for_project(self, project):
        """Empty for a project that has no episodes (a film or commercial
        rather than a series) — Zou answers that with an error rather than an
        empty list, which is nothing a caller needs to hear about."""
        try:
            return gazu.shot.all_episodes_for_project(project, client=self.client)
        except Exception:
            return []

    def all_asset_types_for_project(self, project):
        return gazu.asset.all_asset_types_for_project(project, client=self.client)

    def all_tasks_for_project(self, project):
        """Every task in the project, whoever it belongs to and whatever its
        status — unlike all_tasks_to_do/all_tasks_requiring_feedback, which
        only ever return the logged-in user's own slice of that."""
        return gazu.task.all_tasks_for_project(project, client=self.client)

    def all_preview_files_for_project(self, project):
        """Every published preview in the project in one request — the cheap
        way to find each task's latest revision number, which the task
        payload itself doesn't carry (it only has last_preview_file_id)."""
        return gazu.task.all_preview_files_for_project(project, client=self.client)

    def all_task_statuses_for_project(self, project):
        """A project's *configured* status list — not necessarily every status
        its tasks actually use (see kitsu_core.project_tasks)."""
        return gazu.task.all_task_statuses_for_project(project, client=self.client)

    def all_task_types_for_project(self, project):
        return gazu.task.all_task_types_for_project(project, client=self.client)

    def all_task_statuses(self):
        """Every task status in the database, not just the ones a project's
        own settings list."""
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
        # Truncated: the whole body ends up in a message shown in the UI, and a
        # server error page can carry a lot of incidental detail. Enough to
        # diagnose, not a page of it.
        body = str(body)
        if len(body) > _ERROR_BODY_LIMIT:
            body = f"{body[:_ERROR_BODY_LIMIT]}... (truncated)"
        raise KitsuConnectionError(
            f"Kitsu rejected this checklist update — HTTP {response.status_code} "
            f"on PUT {url}. Server response: {body}"
        )

    def publish_preview(
        self,
        task,
        task_status,
        comment="",
        preview_file_path=None,
        preview_file_url=None,
        revision=None,
        checklist=None,
        attachments=None,
        links=None,
        for_client=False,
        set_thumbnail=False,
    ):
        """Publish a comment with a proper Kitsu preview/revision attached
        (shows up in the task's revision history), instead of a plain comment
        attachment.

        `preview_file_url` is a convenience only — gazu downloads that URL to a
        temp file on this machine and uploads it like any other file; the Kitsu
        server never sees the URL. Callers wanting their own error handling or
        size limits should download it themselves and pass a path instead (see
        CommentPanel, which does).

        Deliberately not gazu's own `publish_preview`: that one calls
        `add_comment` without `for_client`, so a revision could never be posted
        as an internal-only comment. This does the same two steps itself
        (comment, then preview on that comment) with the full comment options."""
        new_comment = self.add_comment(
            task,
            task_status,
            comment=comment,
            checklist=checklist,
            attachments=attachments,
            links=links,
            for_client=for_client,
        )
        preview_file = gazu.task.add_preview(
            task,
            new_comment,
            preview_file_path=preview_file_path,
            preview_file_url=preview_file_url,
            revision=revision,
            client=self.client,
        )
        if set_thumbnail:
            gazu.task.set_main_preview(preview_file, client=self.client)
        return new_comment, preview_file

    def add_comment(
        self,
        task,
        task_status,
        comment="",
        checklist=None,
        attachments=None,
        links=None,
        for_client=False,
    ):
        """Post a status change + comment with no preview file attached.

        `checklist` is [{"text": ..., "checked": bool}], `attachments` is a list
        of local file paths, `links` a list of URLs, and `for_client=True` makes
        the comment visible to clients (Zou treats the default, False, as
        internal-only)."""
        return gazu.task.add_comment(
            task,
            task_status,
            comment=comment,
            checklist=checklist or [],
            attachments=attachments or [],
            links=links or [],
            for_client=for_client,
            client=self.client,
        )

    def all_persons(self):
        """Everyone in the database — for offering @mentions while writing a
        comment."""
        return gazu.person.all_persons(client=self.client)

    def update_preview_annotations(self, preview_file, additions=None, updates=None, deletions=None):
        """Kitsu's own overpaint data, stored on the preview file itself rather
        than as a new revision — what the web player draws on top of a
        revision. See kitsu_core.annotations for the payload shape."""
        return gazu.files.update_preview_annotations(
            preview_file,
            additions=additions,
            updates=updates,
            deletions=deletions,
            client=self.client,
        )

    def download_preview_tile(self, preview_file, file_path):
        """A movie preview's frame strip: one PNG holding evenly-spaced frames
        stacked vertically (1424x48000 for a 1920x1080 clip — 60 frames), which
        is the cheap way to get something to draw on without pulling the whole
        movie down."""
        return gazu.files.extract_tile_from_preview(preview_file, file_path, client=self.client)

    def download_attachment(self, attachment_file, file_path):
        return gazu.files.download_attachment_file(attachment_file, file_path, client=self.client)

    def download_preview(self, preview_file, file_path):
        return gazu.files.download_preview_file(preview_file, file_path, client=self.client)

    def download_preview_thumbnail(self, preview_file, file_path):
        return gazu.files.download_preview_file_thumbnail(preview_file, file_path, client=self.client)
