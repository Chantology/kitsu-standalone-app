"""Login screen: server URL, email/password, or a saved token.

Ported from the DCC plugins' inline login form (see e.g. the Nuke panel's
`_on_login_clicked`), minus the project-ID field — `all_tasks_to_do()` is
user-scoped across all of a person's projects, not project-scoped, so there
is nothing to ask for up front.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .async_worker import run_async
from .kitsu_core import credentials
from .kitsu_core import session as session_module
from .kitsu_core.session import (
    KitsuConnectionError,
    KitsuSession,
    TwoFactorRequired,
    WrongTwoFactorCode,
    two_factor_prompt,
)


# Combo box labels for the 2FA methods KitsuSession.login can take a code for.
_TWO_FACTOR_LABELS = {
    "totp": "Authenticator App",
    "email_otp": "Email Code",
    "recovery_code": "Recovery Code",
}


class LoginWindow(QWidget):
    logged_in = Signal(object)  # emits the authenticated KitsuSession

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kitsu — Log In")
        self.setMinimumWidth(360)

        self.session = None
        # The account's 2FA methods once the server has asked for a code
        # (see TwoFactorRequired) — empty means no code is being asked for.
        self._two_factor_methods = []
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

        # Only shown once the server has said this account needs a 2FA code.
        self.two_factor_box = QGroupBox("Two-Factor Authentication")
        two_factor_form = QFormLayout(self.two_factor_box)
        self.two_factor_method_label = QLabel("Method")
        self.two_factor_method_combo = QComboBox()
        self.two_factor_method_combo.currentIndexChanged.connect(
            self._on_two_factor_method_changed
        )
        self.two_factor_code_edit = QLineEdit()
        self.two_factor_code_edit.setPlaceholderText("Two-factor code")
        self.send_code_button = QPushButton("Send Code")
        self.send_code_button.setToolTip("Email a new two-factor code to this account")
        self.send_code_button.clicked.connect(self._on_send_code_clicked)
        two_factor_form.addRow(self.two_factor_method_label, self.two_factor_method_combo)
        two_factor_form.addRow("Code", self.two_factor_code_edit)
        two_factor_form.addRow(self.send_code_button)
        self.two_factor_box.setVisible(False)
        layout.addWidget(self.two_factor_box)

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
                if not token_result.used_keyring:
                    # Warned every time it's *used*, not just once when it was
                    # saved: this token is long-lived and sitting in plain text
                    # where anything running as this user can read it, so the
                    # one-off warning at save time was too easy to miss.
                    QMessageBox.warning(
                        self,
                        "Kitsu",
                        "Your saved login is stored in plain text, because this "
                        "machine has no OS keyring available. Anything running "
                        "as you can read it. Use Log Out to remove it.",
                    )
                self._set_status("Saved login found — signing in...")
                self._attempt_login(
                    connection.server_url, connection.email, None, token_result.token
                )

    # ---- status helper ----

    def _set_status(self, message, is_error=False):
        self.status_label.setText(message)
        color = "#b00020" if is_error else "#0b7a0b"
        self.status_label.setStyleSheet(f"color: {color};")

    # ---- two-factor ----

    def _current_two_factor_method(self):
        return self.two_factor_method_combo.currentData()

    def _show_two_factor(self, methods, preferred):
        self._two_factor_methods = list(methods)
        # Signals blocked while repopulating: the status line should keep the
        # server's own prompt (str(TwoFactorRequired)), not the switch message.
        self.two_factor_method_combo.blockSignals(True)
        self.two_factor_method_combo.clear()
        for method in self._two_factor_methods:
            self.two_factor_method_combo.addItem(_TWO_FACTOR_LABELS.get(method, method), method)
        index = self.two_factor_method_combo.findData(preferred)
        self.two_factor_method_combo.setCurrentIndex(max(index, 0))
        self.two_factor_method_combo.blockSignals(False)

        multiple = len(self._two_factor_methods) > 1
        self.two_factor_method_label.setVisible(multiple)
        self.two_factor_method_combo.setVisible(multiple)
        self.two_factor_code_edit.clear()
        self._update_two_factor_widgets()
        self.two_factor_box.setVisible(True)
        self.two_factor_code_edit.setFocus()

    def _hide_two_factor(self):
        self._two_factor_methods = []
        self.two_factor_code_edit.clear()
        self.two_factor_box.setVisible(False)
        self._update_two_factor_widgets()

    def _update_two_factor_widgets(self):
        self.send_code_button.setVisible(self._current_two_factor_method() == "email_otp")
        self.login_button.setText(
            "Verify & Log In" if self._two_factor_methods else "Log In"
        )

    def _on_two_factor_method_changed(self, _index):
        self._update_two_factor_widgets()
        method = self._current_two_factor_method()
        if not method:
            return
        # Switching to email doesn't send anything by itself — there's a
        # Send Code button for that, so a stray click can't spam the inbox.
        if method == "email_otp":
            self._set_status("Click Send Code, then enter the emailed code and log in again.")
        else:
            self._set_status(two_factor_prompt(method))

    def _on_send_code_clicked(self):
        server_url = self.server_edit.text().strip()
        email = self.email_edit.text().strip()
        if not server_url or not email:
            self._set_status("Enter the server URL and email first.", is_error=True)
            return

        self._set_status("Sending code...")
        self.send_code_button.setEnabled(False)

        def work():
            # Background thread — no Qt widgets here (see async_worker).
            KitsuSession(server_url).send_email_code(email)

        def on_done(_result):
            self.send_code_button.setEnabled(True)
            self._set_status(two_factor_prompt("email_otp"))

        def on_error(exc):
            self.send_code_button.setEnabled(True)
            self._set_status(str(exc), is_error=True)

        run_async(self, work, on_done, on_error)

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

        # Only sent once the server has asked for it (TwoFactorRequired below).
        two_factor_method = None
        two_factor_code = None
        if self._two_factor_methods:
            two_factor_method = self._current_two_factor_method()
            two_factor_code = self.two_factor_code_edit.text().strip()
            if not two_factor_code:
                self._set_status("Enter your two-factor code.", is_error=True)
                return
        if session_module.is_insecure_host(server_url):
            # Allowed, because an internal Kitsu may genuinely be served over
            # http — but not silently, since the password goes over the wire in
            # the clear.
            answer = QMessageBox.warning(
                self,
                "Kitsu",
                f"{server_url} is not encrypted (http://), so your password and "
                "saved login would be sent in plain text over the network.\n\n"
                "Continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._attempt_login(
            server_url, email, password, None, two_factor_method, two_factor_code
        )

    def _attempt_login(
        self, server_url, email, password, token, two_factor_method=None, two_factor_code=None
    ):
        self._set_status("Connecting...")
        self.login_button.setEnabled(False)

        def work():
            # Runs on a background thread — only KitsuSession/gazu network
            # calls and plain Python here, no Qt widgets.
            session = KitsuSession(server_url)
            if token:
                session.login_with_token(token)
            else:
                # A saved token never needs a 2FA code — only this path does.
                session.login(email, password, two_factor_method, two_factor_code)
            user = session.current_user()
            return session, user

        def on_done(result):
            session, user = result
            self.login_button.setEnabled(True)
            self.session = session
            self.password_edit.clear()
            self._hide_two_factor()

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
            if isinstance(exc, TwoFactorRequired):
                # Password was right — reveal the code field and ask again.
                self._show_two_factor(exc.methods, exc.preferred)
                self._set_status(str(exc))
            elif isinstance(exc, WrongTwoFactorCode):
                self.two_factor_code_edit.clear()
                self.two_factor_code_edit.setFocus()
                self._set_status(str(exc), is_error=True)
            elif isinstance(exc, KitsuConnectionError):
                self._hide_two_factor()
                self._set_status(str(exc), is_error=True)
            else:
                self._hide_two_factor()
                self._set_status(f"Unexpected error: {exc}", is_error=True)

        run_async(self, work, on_done, on_error)
