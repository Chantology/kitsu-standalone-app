"""Light/dark theme switching.

Applied via QPalette rather than a hand-written stylesheet, so native
widgets (menus, scrollbars, combo boxes, ...) stay correctly styled — but
that only actually takes effect under the "Fusion" style; Qt's native
Windows style mostly ignores a custom QPalette for its own chrome, so
`apply_theme` sets it once regardless of which theme is chosen.

Dark is the default, matching Kitsu's own web UI (see e.g. the reference
screenshot the News Feed pills were styled against) rather than whatever
the OS happens to prefer.
"""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtCore import QSettings

_ORG = "Kitsu"
_APP = "StandaloneApp"
_SETTINGS_KEY = "theme"

DARK = "dark"
LIGHT = "light"


def load_theme():
    settings = QSettings(_ORG, _APP)
    return settings.value(_SETTINGS_KEY, DARK) or DARK


def save_theme(theme):
    settings = QSettings(_ORG, _APP)
    settings.setValue(_SETTINGS_KEY, theme)
    settings.sync()


def apply_theme(app, theme):
    app.setStyle("Fusion")
    app.setPalette(_light_palette() if theme == LIGHT else _dark_palette())
    save_theme(theme)


def _light_palette():
    # Fusion's own built-in light palette — not hand-tuned, but a safe,
    # well-tested baseline to contrast the dark one against.
    from PySide6.QtWidgets import QStyleFactory

    return QStyleFactory.create("Fusion").standardPalette()


def _dark_palette():
    palette = QPalette()
    window = QColor(37, 37, 38)
    base = QColor(30, 30, 30)
    alt_base = QColor(45, 45, 48)
    text = QColor(220, 220, 220)
    disabled_text = QColor(120, 120, 120)
    button = QColor(51, 51, 55)
    highlight = QColor(60, 110, 180)

    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, base)
    palette.setColor(QPalette.ColorRole.AlternateBase, alt_base)
    palette.setColor(QPalette.ColorRole.ToolTipBase, alt_base)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, button)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 90, 90))
    palette.setColor(QPalette.ColorRole.Link, QColor(110, 170, 255))
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    return palette
