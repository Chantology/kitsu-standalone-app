"""Get one frame of a movie revision at full resolution.

Kitsu's tile (see frame_picker) hands over every frame of a clip in one cheap
request, but as 178x100 thumbnails — fine for choosing a frame, useless for
drawing notes on it. The only source of the real thing is the movie itself, so
this downloads it once (cached per preview file) and decodes the wanted frame.

**Why PyAV and not Qt's own multimedia stack.** QtMultimedia was tried first, to
avoid a dependency. Measured on two real Kitsu revisions, four grabs each: about
half timed out after 20s, the successes took 2-17s, stale frames from the
previous grab leaked into the next one, and one arrangement crashed the media
backend outright (0xC0000005). The failures were structural, not tuning — a seek
does not make the backend deliver a frame, `QMediaPlayer.position()` is not the
position of the frame in hand, and a paused player stops delivering frames after
the next seek. PyAV, on the same files: every grab succeeded, ~0.15s each,
frame-accurate to within one frame, and repeatable. The cost is one dependency
whose wheel bundles ffmpeg (~28 MB).

Frame-accurate seeking still lands on the nearest frame the codec allows, so
`grab_frame` reports the position it actually decoded and callers annotate
*that* time — the note belongs on the frame the user was really looking at.

Blocking; call from a worker thread (see async_worker). Nothing here touches Qt
widgets, only QImage.
"""

import os

from PySide6.QtGui import QImage

from .kitsu_core import media_cache

# A ceiling on what will be pulled down for one frame. Big enough for a long
# real revision (their longest is a 1250-second cut) and small enough that
# clicking through to something enormous by accident stops rather than filling
# the disk. A preview file's own `file_size` is checked first, so this normally
# refuses *before* downloading anything.
MAX_MOVIE_BYTES = 2 * 1024 * 1024 * 1024


class MovieFrameError(Exception):
    """Something specific went wrong fetching a frame, worded for the user."""


def cache_path(preview_file):
    extension = (preview_file.get("extension") or "mp4").lstrip(".")
    cache_dir = media_cache.directory(media_cache.MOVIES)
    return os.path.join(cache_dir, f"{preview_file.get('id')}.{extension}")


def is_cached(preview_file):
    path = cache_path(preview_file)
    return os.path.exists(path) and os.path.getsize(path) > 0


def download_movie(session, preview_file):
    """Downloads the movie unless it's already cached. Returns the local path."""
    target_path = cache_path(preview_file)
    if is_cached(preview_file):
        return target_path

    # Kitsu reports the size on the preview file itself, so an oversized
    # revision is refused before a single byte moves.
    declared_size = preview_file.get("file_size") or 0
    if declared_size and declared_size > MAX_MOVIE_BYTES:
        raise MovieFrameError(
            f"That revision is {declared_size / (1024 ** 3):.1f} GB, over this "
            f"app's {MAX_MOVIE_BYTES // (1024 ** 3)} GB limit for fetching a "
            "frame. Review it in Kitsu's web player instead."
        )

    media_cache.ensure_directory(media_cache.MOVIES)
    # Downloaded to a .part file first so an interrupted download can't leave
    # something truncated behind that later looks cached.
    partial_path = f"{target_path}.part"
    try:
        session.download_preview(preview_file, partial_path)
        # Checked again after the fact for the case where the preview file
        # carried no size at all.
        if os.path.getsize(partial_path) > MAX_MOVIE_BYTES:
            raise MovieFrameError(
                f"That revision turned out to be over this app's "
                f"{MAX_MOVIE_BYTES // (1024 ** 3)} GB limit for fetching a frame."
            )
        os.replace(partial_path, target_path)
    except Exception:
        if os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass
        raise
    return target_path


def grab_frame(movie_path, seconds):
    """Returns (QImage, actual_seconds) for the frame at or just after
    `seconds`."""
    try:
        import av
    except ImportError as exc:
        raise MovieFrameError(
            f"This build can't decode movies — the av package is missing ({exc})."
        ) from exc

    wanted = max(0.0, float(seconds or 0))
    try:
        with av.open(movie_path) as container:
            if not container.streams.video:
                raise MovieFrameError("That movie has no video stream.")
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            time_base = float(stream.time_base)

            # Seeking goes to the keyframe at or before the target, then frames
            # are decoded forward from there until the target is reached.
            if wanted > 0 and time_base > 0:
                container.seek(int(wanted / time_base), stream=stream)

            chosen = None
            for frame in container.decode(stream):
                position = float(frame.pts * time_base) if frame.pts is not None else 0.0
                chosen = (position, frame)
                if position + 0.001 >= wanted:
                    break
            if chosen is None:
                raise MovieFrameError("No frame could be decoded from that movie.")

            position, frame = chosen
            return _to_qimage(frame), position
    except MovieFrameError:
        raise
    except Exception as exc:
        raise MovieFrameError(
            f"Could not read a frame from {os.path.basename(movie_path)}: {exc}"
        ) from exc


def _to_qimage(frame):
    """PyAV frame -> QImage without going through PIL or numpy: reformat to
    packed RGB and hand Qt the plane. `line_size` matters — the plane is padded,
    so passing the width as the stride would shear the image."""
    rgb = frame.reformat(format="rgb24")
    plane = rgb.planes[0]
    image = QImage(
        bytes(plane),
        rgb.width,
        rgb.height,
        plane.line_size,
        QImage.Format.Format_RGB888,
    )
    # Copied because the QImage above only borrows the plane's buffer.
    return image.copy()
