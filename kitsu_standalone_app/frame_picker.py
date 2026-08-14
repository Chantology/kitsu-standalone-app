"""Pick a frame out of a movie revision without downloading the movie.

Zou renders a movie preview's frames as one big PNG "tile", which is Kitsu's own
scrub filmstrip. Measured against real tiles on the user's server, the layout is
an **8-column grid of cells, one cell per source frame**, each cell
`tile_width / 8` wide and **100 px tall**:

    1920x1080, 6.08s @ 25fps -> tile 1424x1900 = 8 x 19 cells of 178x100 = 152
                                cells, exactly 152 source frames
    1920x1080, 84.8s         -> tile 1424x26500 = 8 x 265 = 2120 cells
    1080x1080                -> tile 800x...     = 8 cells of 100x100

Verified by finding the row and column boundaries in the pixel data: they fall
on exact multiples of 100 and of `tile_width / 8`.

Two consequences worth being blunt about:

- One request gets *every* frame of the clip, which makes this an excellent way
  to choose a frame. It is a terrible way to get something to draw on: a cell is
  178x100. Anything that needs the frame at real resolution has to get it from
  the movie itself.
- Qt silently refuses to load a large tile until the allocation limit is lifted:
  1424x26500 is 37 megapixels, and past the 256 MB default QImage just comes
  back null with no error.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
)

TILE_COLUMNS = 8
TILE_CELL_HEIGHT = 100
_PREVIEW_MAX_WIDTH = 900
_PREVIEW_MAX_HEIGHT = 520
# A cell that is essentially one flat colour is a padding cell at the end of the
# grid, not a frame — the grid is rounded up to whole rows of 8.
_BLANK_CELL_SPREAD = 6


def load_tile(tile_path, media_width=None, media_height=None):
    """Slices Kitsu's tile into per-frame thumbnails, in order.

    Returns (frames, tile_size). `frames` is a list of QPixmap of
    `tile_width / 8` x 100 — thumbnails, not full-resolution frames (see the
    module docstring). Trailing padding cells are dropped."""
    # Must come before reading any tile — see the module docstring.
    QImageReader.setAllocationLimit(0)

    image = QImage(tile_path)
    if image.isNull() or not image.width() or not image.height():
        return [], (0, 0)

    cell_width = image.width() // TILE_COLUMNS
    rows = image.height() // TILE_CELL_HEIGHT
    if cell_width < 1 or rows < 1:
        return [], (image.width(), image.height())

    frames = []
    for row in range(rows):
        for column in range(TILE_COLUMNS):
            cell = image.copy(
                column * cell_width, row * TILE_CELL_HEIGHT, cell_width, TILE_CELL_HEIGHT
            )
            frames.append(cell)

    while frames and _is_padding(frames[-1]):
        frames.pop()
    return [QPixmap.fromImage(frame) for frame in frames], (image.width(), image.height())


def _is_padding(cell):
    """True for a cell of essentially one colour. Sampled rather than exhaustive
    — a 178x100 cell is small, but this runs once per cell over a few thousand
    of them."""
    values = [
        cell.pixelColor(x, y).value()
        for y in range(0, cell.height(), max(1, cell.height() // 6))
        for x in range(0, cell.width(), max(1, cell.width() // 6))
    ]
    if not values:
        return True
    return max(values) - min(values) <= _BLANK_CELL_SPREAD


class FramePickerDialog(QDialog):
    """Scrub the sampled frames and pick one."""

    def __init__(self, frames, duration, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Choose a frame — {title}")
        self._frames = frames
        self._duration = duration or 0

        layout = QVBoxLayout(self)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(320, 180)
        layout.addWidget(self.image_label, 1)

        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, max(0, len(frames) - 1))
        self.slider.valueChanged.connect(self._on_slider_changed)
        slider_row.addWidget(self.slider, 1)
        self.position_label = QLabel("")
        slider_row.addWidget(self.position_label)
        layout.addLayout(slider_row)

        hint = QLabel(
            f"{len(frames)} frames — every frame of the clip, as Kitsu's own "
            "scrub thumbnails (178x100), so this is for choosing, not for judging "
            "detail."
        )
        hint.setStyleSheet("color: #888888;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Paint Over This Frame")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._on_slider_changed(0)

    def _on_slider_changed(self, index):
        if not self._frames:
            return
        pixmap = self._frames[index]
        shown = pixmap
        if pixmap.width() > _PREVIEW_MAX_WIDTH or pixmap.height() > _PREVIEW_MAX_HEIGHT:
            shown = pixmap.scaled(
                _PREVIEW_MAX_WIDTH,
                _PREVIEW_MAX_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.image_label.setPixmap(shown)
        self.position_label.setText(f"{self.selected_time():.2f}s  ({index + 1}/{len(self._frames)})")

    def selected_index(self):
        return self.slider.value()

    def selected_pixmap(self):
        return self._frames[self.slider.value()] if self._frames else None

    def selected_time(self):
        """Where the chosen frame sits in the clip. The tile holds one cell per
        source frame, so cell i of n is i/n of the way through."""
        if len(self._frames) < 2 or not self._duration:
            return 0.0
        return self._duration * self.slider.value() / len(self._frames)
