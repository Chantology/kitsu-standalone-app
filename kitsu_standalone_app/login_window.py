"""Login screen: server URL, email/password, or a saved token.

Ported from the DCC plugins' inline login form (see e.g. the Nuke panel's
`_on_login_clicked`), minus the project-ID field — `all_tasks_to_do()` is
user-scoped across all of a person's projects, not project-scoped, so there
is nothing to ask for up front.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .async_worker import run_async
from .kitsu_core import credentials
from .kitsu_core.session import KitsuConnectionError, KitsuSession


class LoginWindow(QWidget):
    logged_in = Signal(object)  # emits the authenticated KitsuSession

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kitsu — Log In")
        self.setMinimumWidth(360)

        self.session = None
        self._async_workers = []  # kept alive for the life of a background call (see async_worker.run_async)

        layout = QVBoxLayout(self)

        title = QLabel("Log in to Kitsu")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        form = QFormLayout()
        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("https://kitsu.example.studio")
        self.email_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Server URL", self.server_edit)
        form.addRow("Email", self.email_edit)
        form.addRow("Password", self.password_edit)
        layout.addLayout(form)

        self.remember_label = QLabel(
            "Your login will be remembered securely (OS keyring) so you "
            "won't need to enter your password every time."
        )
        self.remember_label.setWordWrap(True)
        self.remember_label.setStyleSheet("color: #666666;")
        layout.addWidget(self.remember_label)

        self.login_button = QPushButton("Log In")
        self.login_button.clicked.connect(self._on_login_clicked)
        layout.addWidget(self.login_button)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addStretch(1)

        self._load_saved_connection()

    # ---- persistence ----

    def _load_saved_connection(self):
        connection = credentials.load_connection()
        self.server_edit.setText(connection.server_url)
        self.email_edit.setText(connection.email)
        if connection.server_url and connection.email:
            token_result = credentials.load_refresh_token(
                connection.server_url, connection.email
            )
            if token_result.token:
                self._set_status("Saved login found — signing in...")
                self._attempt_login(
                    connection.server_url, connection.email, None, token_result.token
                )

    # ---- status helper ----

    def _set_status(self, message, is_error=False):
        self.status_label.setText(message)
        color = "#b00020" if is_error else "#0b7a0b"
        self.status_label.setStyleSheet(f"color: {color};")

    # ---- actions ----

    def _on_login_clicked(self):
        server_url = self.server_edit.text().strip()
        email = self.email_edit.text().strip()
        password = self.password_edit.text()

        if not server_url or not email:
            QMessageBox.warning(self, "Kitsu", "Please fill in the server URL and email.")
            return
        if not password:
            QMessageBox.warning(self, "Kitsu", "Please enter your password.")
            return

        self._attempt_login(server_url, email, password, None)

    def _attempt_login(self, server_url, email, password, token):
        self._set_status("Connecting...")
        self.login_button.setEnabled(False)

        def work():
            # Runs on a background thread — only KitsuSession/gazu network
            # calls and plain Python here, no Qt widgets.
            session = KitsuSession(server_url)
            if token:
                session.login_with_token(token)
            else:
                session.login(email, password)
            user = session.current_user()
            return session, user

        def on_done(result):
            session, user = result
            self.login_button.setEnabled(True)
            self.session = session
            self.password_edit.clear()

            credentials.save_connection(server_url, email)
            # Re-save after EVERY successful login, not just a fresh
            # password one: gazu's refresh_access_token() (used by
            # login_with_token, i.e. auto-login) rotates the refresh token
            # whenever the server returns a new one, invalidating the old
            # one — only saving after a password login meant the saved
            # token would silently go stale after the very next auto-login
            # that happened to get rotated.
            refresh_token = session.get_refresh_token()
            if refresh_token:
                used_keyring = credentials.save_refresh_token(
                    server_url, email, refresh_token
                )
                if not used_keyring:
                    QMessageBox.warning(
                        self,
                        "Kitsu",
                        "Could not reach the OS keyring — your login "
                        "was saved in the app's settings file instead "
                        "of a secure credential store.",
                    )

            display_name = user.get("full_name") or user.get("email") or email
            self._set_status(f"Connected as {display_name} to {session.host}")
            self.logged_in.emit(session)

        def on_error(exc):
            self.login_button.setEnabled(True)
            if token:
                # A saved token failed (expired/revoked) — drop it so the
                # next launch doesn't just fail silently again and asks for
                # a password instead.
                credentials.clear_refresh_token(server_url, email)
            if isinstance(exc, KitsuConnectionError):
                self._set_status(str(exc), is_error=True)
            else:
                self._set_status(f"Unexpected error: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)
