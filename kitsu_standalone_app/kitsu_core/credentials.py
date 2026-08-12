"""Persisted login state for the standalone app.

Non-secret fields (server URL, email) go in plain `QSettings` — same as the
DCC plugins this app is ported from. The refresh token, unlike in those
plugins, goes to the OS keyring (Windows Credential Manager / Linux Secret
Service via the `keyring` package) instead of a plaintext file: this app has
no DCC-bundled-Python constraint forcing the simpler plaintext approach, and
a standalone app is the thing a user will treat as "the app that has my
Kitsu login" rather than throwaway per-session state.

If no keyring backend is available (e.g. a minimal Linux box with no Secret
Service running), `save_refresh_token`/`load_refresh_token` fall back to the
plaintext QSettings field instead of crashing — callers should surface
`used_keyring=False` as a one-time warning, not silently pretend the token
is stored securely.
"""

from collections import namedtuple

import keyring
from PySide6.QtCore import QSettings

_ORG = "Kitsu"
_APP = "StandaloneApp"
_KEYRING_SERVICE = "kitsu-standalone-app"

Connection = namedtuple("Connection", ["server_url", "email"])

TokenResult = namedtuple("TokenResult", ["token", "used_keyring"])


def _settings():
    return QSettings(_ORG, _APP)


def _token_key(server_url, email):
    return f"{server_url}|{email}"


def load_connection():
    settings = _settings()
    return Connection(
        server_url=settings.value("server_url", "") or "",
        email=settings.value("email", "") or "",
    )


def save_connection(server_url, email):
    settings = _settings()
    settings.setValue("server_url", server_url)
    settings.setValue("email", email)
    settings.sync()


def load_refresh_token(server_url, email):
    """Returns a TokenResult(token, used_keyring). `token` is "" if nothing
    is saved yet. `used_keyring` is False if the keyring backend itself is
    unavailable and the plaintext fallback field was read instead."""
    key = _token_key(server_url, email)
    try:
        token = keyring.get_password(_KEYRING_SERVICE, key)
        return TokenResult(token or "", True)
    except Exception:
        # Any backend failure (no Secret Service running, dbus missing,
        # ...) — fall back rather than making a broken keyring backend
        # block login entirely.
        token = _settings().value(f"refresh_token_fallback_{key}", "") or ""
        return TokenResult(token, False)


def save_refresh_token(server_url, email, token):
    """Returns True if the token was saved to the OS keyring, False if it
    had to fall back to the plaintext settings field."""
    key = _token_key(server_url, email)
    try:
        keyring.set_password(_KEYRING_SERVICE, key, token)
        return True
    except Exception:
        settings = _settings()
        settings.setValue(f"refresh_token_fallback_{key}", token)
        settings.sync()
        return False


def clear_refresh_token(server_url, email):
    key = _token_key(server_url, email)
    try:
        keyring.delete_password(_KEYRING_SERVICE, key)
    except Exception:
        pass
    settings = _settings()
    settings.remove(f"refresh_token_fallback_{key}")
    settings.sync()
