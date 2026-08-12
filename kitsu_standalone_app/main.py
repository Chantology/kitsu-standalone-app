"""Entry point: shows the login window, then swaps to the main window (and
back again on Log Out)."""

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import theme
from .login_window import LoginWindow
from .main_window import MainWindow
from .resources import resource_path


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Kitsu")
    app.setOrganizationName("Kitsu")
    # Sets the default icon for every window/dialog in the app (title bar,
    # taskbar/Alt-Tab) — a window only needs its own setWindowIcon() if it
    # wants to override this.
    app.setWindowIcon(QIcon(resource_path("assets", "kitsu_icon.png")))
    theme.apply_theme(app, theme.load_theme())

    # Keeps strong references alive for the life of the app — otherwise
    # Python would garbage-collect the windows as soon as this function's
    # local scope would normally end.
    windows = {}

    def _show_login_window():
        login_window = LoginWindow()
        login_window.logged_in.connect(_on_logged_in)
        windows["login"] = login_window
        login_window.show()

    def _on_logged_in(session):
        main_window = MainWindow(session)
        main_window.logged_out.connect(_on_logged_out)
        windows["main"] = main_window
        main_window.show()
        windows["login"].close()
        del windows["login"]

    def _on_logged_out():
        old_main = windows.pop("main")
        old_main.close()
        old_main.deleteLater()
        _show_login_window()

    _show_login_window()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
