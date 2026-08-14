"""Emoji picker for the comment composer, matching Kitsu's own emoji button.

A fixed set rather than a full Unicode browser: review comments use the same
dozen or so marks over and over ("👍 approved", "⚠️ watch the timing"), and a
grid of those is faster than searching. Rendering needs no extra work — the
system emoji font (Segoe UI Emoji on Windows, Noto on Linux) covers all of
these.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QMenu, QToolButton, QWidget, QWidgetAction

# Grouped roughly by how they get used in review: verdicts, attention, craft
# notes, then the friendly ones.
EMOJI = [
    "👍", "👎", "✅", "❌", "🎉", "🔥", "⭐", "💯",
    "⚠️", "❓", "❗", "👀", "🔍", "⏱️", "🔁", "📌",
    "🎨", "💡", "🎬", "🎥", "🖼️", "✏️", "📝", "🧹",
    "🙏", "😀", "😅", "🤔", "😍", "🚀", "🧊", "🌟",
]
_COLUMNS = 8
_BUTTON_SIZE = 30


class EmojiPicker(QWidget):
    """The grid itself. Usually reached through `attach_to_button` rather than
    used directly."""

    emoji_chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        for index, emoji in enumerate(EMOJI):
            button = QToolButton()
            button.setText(emoji)
            button.setAutoRaise(True)
            button.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
            button.clicked.connect(lambda _checked=False, value=emoji: self.emoji_chosen.emit(value))
            layout.addWidget(button, index // _COLUMNS, index % _COLUMNS)


def attach_to_button(button, on_chosen):
    """Hangs an emoji grid off a QToolButton as a popup, calling `on_chosen`
    with the picked emoji. Returns the menu so the caller can keep it alive."""
    menu = QMenu(button)
    picker = EmojiPicker(menu)

    def handle(emoji):
        on_chosen(emoji)
        menu.close()

    picker.emoji_chosen.connect(handle)

    # A QMenu holding one QWidgetAction is the standard way to get a popup with
    # click-outside-to-dismiss and sane positioning without hand-managing a
    # frameless window.
    action = QWidgetAction(menu)
    action.setDefaultWidget(picker)
    menu.addAction(action)

    button.setMenu(menu)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    return menu
