"""Paint over (mark up) an image and save a flattened copy of the result.

The DCC plugins this app was ported from could only draw over whatever their
host application had on screen (viewport capture / video-frame drawover), so
the port dropped it. Here the same idea works on any image the app already
has: the file picked for the next comment, or an existing revision/attachment
already downloaded from Kitsu — mark it up, and the flattened result becomes
the file that gets published.

Strokes go onto a separate transparent overlay kept at the image's own full
resolution rather than the (often downscaled) size shown on screen, so saving
neither degrades the original nor bakes in the on-screen zoom factor.
"""

import os

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .kitsu_core import media_cache

# The canvas is shown at most this big (bigger images are scaled down to fit
# on screen); the overlay strokes are still recorded at full resolution.
_MAX_DISPLAY_WIDTH = 1000
_MAX_DISPLAY_HEIGHT = 640
_PRESET_COLORS = [
    ("Red", "#ff3b30"),
    ("Yellow", "#ffcc00"),
    ("Green", "#34c759"),
    ("Blue", "#0a84ff"),
    ("White", "#ffffff"),
    ("Black", "#000000"),
]
_UNDO_LIMIT = 40
_SWATCH_SIZE = 22


class _PaintCanvas(QWidget):
    """The image plus a transparent stroke overlay drawn on top of it."""

    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        self._base = pixmap
        self._overlay = QPixmap(pixmap.size())
        self._overlay.fill(Qt.transparent)
        self._undo_stack = []
        self._last_point = None
        # Strokes are kept as geometry as well as pixels: the pixels make the
        # flattened image, the geometry becomes a real Kitsu annotation (see
        # kitsu_core.annotations). Both are in the image's own coordinates.
        self.strokes = []
        self._current_stroke = None

        self.pen_color = QColor(_PRESET_COLORS[0][1])
        self.pen_width = 5

        # One fixed display scale (and a fixed widget size to match) so
        # widget coordinates map to image coordinates by a single divide —
        # no letterboxing offsets to account for on every mouse event.
        self._scale = min(
            1.0,
            _MAX_DISPLAY_WIDTH / max(1, pixmap.width()),
            _MAX_DISPLAY_HEIGHT / max(1, pixmap.height()),
        )
        self.setFixedSize(
            max(1, round(pixmap.width() * self._scale)),
            max(1, round(pixmap.height() * self._scale)),
        )
        self.setCursor(Qt.CrossCursor)

    # ---- painting ----

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), self._base)
        painter.drawPixmap(self.rect(), self._overlay)

    def _to_image(self, position):
        return QPointF(position.x() / self._scale, position.y() / self._scale)

    def _image_pen(self):
        # Divided by the scale so a "5 px" brush looks like 5 px on screen
        # whatever the zoom — the stroke is stored at image resolution.
        return QPen(
            self.pen_color,
            max(1.0, self.pen_width / self._scale),
            Qt.SolidLine,
            Qt.RoundCap,
            Qt.RoundJoin,
        )

    def _draw(self, start, end):
        painter = QPainter(self._overlay)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(self._image_pen())
        if start == end:
            painter.drawPoint(start)  # a plain click should still leave a dot
        else:
            painter.drawLine(start, end)
        painter.end()
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._push_undo()
        self._last_point = self._to_image(event.position())
        self._current_stroke = {
            "points": [(self._last_point.x(), self._last_point.y())],
            "color": self.pen_color.name(),
            "width": self.pen_width,
        }
        self.strokes.append(self._current_stroke)
        self._draw(self._last_point, self._last_point)

    def mouseMoveEvent(self, event):
        if self._last_point is None:
            return
        point = self._to_image(event.position())
        self._draw(self._last_point, point)
        if self._current_stroke is not None:
            self._current_stroke["points"].append((point.x(), point.y()))
        self._last_point = point

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last_point = None
            self._current_stroke = None

    # ---- history ----

    def _push_undo(self):
        # Snapshot the stroke list, not just its length: Clear replaces the list
        # wholesale, and a length would then restore nothing.
        self._undo_stack.append((self._overlay.copy(), list(self.strokes)))
        if len(self._undo_stack) > _UNDO_LIMIT:
            self._undo_stack.pop(0)

    def undo(self):
        if not self._undo_stack:
            return
        overlay, strokes = self._undo_stack.pop()
        self._overlay = overlay
        self.strokes = strokes
        self.update()

    def clear(self):
        self._push_undo()
        self._overlay.fill(Qt.transparent)
        self.strokes = []
        self.update()

    def is_empty(self):
        return not self._undo_stack

    def image_size(self):
        """The coordinate space the strokes are recorded in."""
        return self._base.width(), self._base.height()

    def flattened(self):
        """The image with the strokes baked in, at full resolution."""
        result = self._base.copy()
        painter = QPainter(result)
        painter.drawPixmap(0, 0, self._overlay)
        painter.end()
        return result


class PaintOverDialog(QDialog):
    """`allow_annotation` adds the second accept button: markup can either go
    back to Kitsu as a real annotation on the revision it was drawn on (see
    kitsu_core.annotations), or be flattened into a file to attach/publish.
    Which one was pressed is in `result_action`."""

    ACTION_ANNOTATE = "annotate"
    ACTION_FILE = "file"

    def __init__(self, pixmap, source_name, parent=None, allow_annotation=False):
        super().__init__(parent)
        self.setWindowTitle(f"Paint Over — {source_name}")
        self._source_name = source_name
        self.saved_path = None
        self.result_action = None
        self._allow_annotation = allow_annotation

        layout = QVBoxLayout(self)

        self.canvas = _PaintCanvas(pixmap)

        tools_row = QHBoxLayout()
        self._swatches = []
        for name, hex_color in _PRESET_COLORS:
            tools_row.addWidget(self._build_swatch(name, hex_color))
        more_colors_button = QPushButton("More...")
        more_colors_button.setToolTip("Pick any other brush color")
        more_colors_button.clicked.connect(self._on_more_colors_clicked)
        tools_row.addWidget(more_colors_button)

        tools_row.addSpacing(12)
        tools_row.addWidget(QLabel("Brush"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 60)
        self.width_spin.setValue(self.canvas.pen_width)
        self.width_spin.setSuffix(" px")
        self.width_spin.valueChanged.connect(self._on_width_changed)
        tools_row.addWidget(self.width_spin)

        tools_row.addStretch(1)
        undo_button = QPushButton("Undo")
        undo_button.setShortcut("Ctrl+Z")
        undo_button.clicked.connect(self.canvas.undo)
        tools_row.addWidget(undo_button)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.canvas.clear)
        tools_row.addWidget(clear_button)
        layout.addLayout(tools_row)

        # Scrolled rather than shrunk-to-fit: the canvas is already capped at
        # a screen-friendly size, and a large image on a small screen should
        # still be markable at that size instead of being scaled down again.
        scroll_area = QScrollArea()
        scroll_area.setWidget(self.canvas)
        scroll_area.setAlignment(Qt.AlignCenter)
        layout.addWidget(scroll_area, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        if self._allow_annotation:
            annotate_button = buttons.addButton(
                "Save Annotation", QDialogButtonBox.ButtonRole.AcceptRole
            )
            annotate_button.setToolTip(
                "Store the markup on this revision in Kitsu, where its own "
                "review player shows it — no new version is created."
            )
            annotate_button.clicked.connect(lambda: self._accept_with(self.ACTION_ANNOTATE))
        file_button = buttons.addButton(
            "Attach as File" if self._allow_annotation else "Save",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        if self._allow_annotation:
            file_button.setToolTip(
                "Flatten the markup into an image instead, ready to attach to a "
                "comment or publish as a new revision."
            )
        file_button.clicked.connect(lambda: self._accept_with(self.ACTION_FILE))
        layout.addWidget(buttons)

        self._select_color(0)

    def _build_swatch(self, name, hex_color):
        button = QPushButton()
        button.setToolTip(name)
        button.setCheckable(True)
        button.setFixedSize(_SWATCH_SIZE, _SWATCH_SIZE)
        button.setStyleSheet(f"background-color: {hex_color}; border: 1px solid #555555;")
        index = len(self._swatches)
        button.clicked.connect(lambda _checked=False, i=index: self._select_color(i))
        self._swatches.append((button, QColor(hex_color)))
        return button

    def _select_color(self, index):
        for position, (button, color) in enumerate(self._swatches):
            is_current = position == index
            button.setChecked(is_current)
            # Checkable QPushButtons with a background-color stylesheet don't
            # show their checked state at all, so the border carries it.
            button.setStyleSheet(
                f"background-color: {color.name()}; "
                f"border: {'3px solid #ffffff' if is_current else '1px solid #555555'};"
            )
            if is_current:
                self.canvas.pen_color = color

    def _on_more_colors_clicked(self):
        color = QColorDialog.getColor(self.canvas.pen_color, self, "Brush Color")
        if color.isValid():
            self.canvas.pen_color = color
            for button, _color in self._swatches:
                button.setChecked(False)

    def _on_width_changed(self, value):
        self.canvas.pen_width = value

    def _accept_with(self, action):
        if self.canvas.is_empty():
            QMessageBox.information(
                self, "Paint Over", "Nothing drawn yet — draw on the image, or Cancel."
            )
            return
        if action == self.ACTION_FILE:
            try:
                self.saved_path = self._save_flattened()
            except Exception as exc:
                QMessageBox.warning(self, "Paint Over", f"Could not save the markup: {exc}")
                return
        self.result_action = action
        self.accept()

    def strokes(self):
        """The markup as geometry in the image's own pixel coordinates."""
        return self.canvas.strokes

    def image_size(self):
        return self.canvas.image_size()

    def _save_flattened(self):
        cache_dir = media_cache.ensure_directory(media_cache.PAINT_OVERS)

        stem = os.path.splitext(os.path.basename(self._source_name))[0] or "markup"
        # Never overwrite an earlier markup of the same source — someone may
        # well paint two different notes over the same revision.
        target_path = os.path.join(cache_dir, f"{stem}_paintover.png")
        counter = 2
        while os.path.exists(target_path):
            target_path = os.path.join(cache_dir, f"{stem}_paintover_{counter}.png")
            counter += 1

        if not self.canvas.flattened().save(target_path, "PNG"):
            raise RuntimeError(f"Qt refused to write {target_path}")
        return target_path


def paint_over(image_path, parent=None):
    """Opens the paint-over dialog on `image_path`. Returns the path of the
    saved flattened markup, or None if the user cancelled or the file isn't a
    loadable image."""
    pixmap = QPixmap(image_path)
    if pixmap.isNull():
        QMessageBox.warning(
            parent,
            "Paint Over",
            f"Could not open {os.path.basename(image_path)} as an image.",
        )
        return None
    dialog = PaintOverDialog(pixmap, os.path.basename(image_path), parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.saved_path


def paint_over_media(pixmap, source_name, parent=None):
    """Paint over media fetched from Kitsu, where the markup can become a real
    annotation on the revision. Returns the accepted dialog (read its
    `result_action`, `strokes()`, `image_size()` and `saved_path`), or None if
    the user cancelled."""
    dialog = PaintOverDialog(pixmap, source_name, parent, allow_annotation=True)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog
