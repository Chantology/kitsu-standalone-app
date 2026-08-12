"""Kitsu status colors, adjusted for legibility in this app.

Kitsu's own "Todo" (and any similarly light) status color is a near-white
grey — legible on Kitsu's own web UI (dark text, white page background)
but effectively invisible against the white text this app's colored
badges use. Anything that light is swapped for a darker grey instead of
being shown as Kitsu sends it.
"""

from PySide6.QtGui import QColor

_MIN_READABLE_LUMINANCE = 200
_FALLBACK_DARK_GREY = "#757575"


def status_display_color(hex_color):
    if not hex_color:
        return QColor(_FALLBACK_DARK_GREY)
    color = QColor(hex_color)
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    if luminance > _MIN_READABLE_LUMINANCE:
        return QColor(_FALLBACK_DARK_GREY)
    return color
