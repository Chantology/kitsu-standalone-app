"""Kitsu-native annotations: overpaints stored on a preview file.

Kitsu's own review player draws annotations on top of a revision rather than
turning them into a new version, and stores them on the preview file. This
module builds that payload from plain strokes so the desktop app's paint-over
lands in Kitsu's player, not just as a flattened attachment.

The shape was taken from 3216 real annotation entries on the user's server
(every single one had exactly these three top-level keys):

    {"time": 1.637, "frame": 50,
     "drawing": {"objects": [ <fabric.js object>, ... ]}}

and the objects are fabric.js `PSStroke`s — Kitsu's own pressure-stroke
subclass — of which the load-bearing parts are:

    {"type": "PSStroke", "version": "5.1.0",
     "stroke": "#039BE5", "strokeWidth": 2,
     "left": .., "top": .., "width": .., "height": ..,     # bounding box
     "canvasWidth": 1971.53, "canvasHeight": 1108.99,      # see below
     "strokePoints": [{"x": .., "y": .., "type": "PSPoint", "pressure": 0.5}, ...]}

`canvasWidth`/`canvasHeight` are the crux. Coordinates are *not* in the
preview's pixel space — they are in whatever fabric canvas the annotation was
drawn on, and the observed sizes vary per annotation (1564x880, 1813x1020,
786x442, 2204x1240, ... all matching a browser window rather than the media).
Kitsu rescales by the ratio between the stored canvas size and the one it is
currently rendering at, so an annotation is portable as long as every stroke
says which canvas its numbers belong to. This module therefore writes the
preview's own pixel size as the canvas size and coordinates in preview pixels,
which is the one space the desktop app actually knows.

Frame numbering, checked against ten real entries at 29.97 fps:
`frame = floor(time * fps) + 1` (1-based). Stills use `time 0, frame 1`.
"""

import time as time_module
import uuid

FABRIC_VERSION = "5.1.0"
STROKE_TYPE = "PSStroke"
POINT_TYPE = "PSPoint"
# What Kitsu records for a mouse (as opposed to a pressure-sensitive pen).
DEFAULT_PRESSURE = 0.5


def frame_for_time(seconds, fps):
    """1-based frame number for a position in a movie, the way Kitsu numbers
    them. Falls back to frame 1 without a usable fps."""
    if not fps or fps <= 0:
        return 1
    return int((seconds or 0) * fps) + 1


def time_for_frame(frame, fps):
    """Inverse of frame_for_time — the start of that frame."""
    if not fps or fps <= 0:
        return 0
    return max(0, (int(frame) - 1) / float(fps))


def stroke_object(points, color, stroke_width, canvas_width, canvas_height, pressure=DEFAULT_PRESSURE):
    """One fabric.js PSStroke from a list of (x, y) points in canvas
    coordinates. Every field a real Kitsu stroke carries is set, so the player
    has nothing to guess."""
    if not points:
        raise ValueError("a stroke needs at least one point")

    xs = [float(x) for x, _y in points]
    ys = [float(y) for _x, y in points]
    half = stroke_width / 2.0
    # The bounding box observed on real strokes is the points' extent inset by
    # half the stroke width on each side. It only drives selection bounds — the
    # rendered geometry comes from strokePoints, which are absolute canvas
    # coordinates — but it is set the same way here to stay consistent.
    left = min(xs) + half
    top = min(ys) + half
    width = max(0.0, (max(xs) - min(xs)) - stroke_width)
    height = max(0.0, (max(ys) - min(ys)) - stroke_width)

    now_ms = int(time_module.time() * 1000)
    return {
        "id": str(uuid.uuid4()),
        "type": STROKE_TYPE,
        "version": FABRIC_VERSION,
        "stroke": color,
        "strokeWidth": stroke_width,
        "strokePoints": [
            {"x": float(x), "y": float(y), "type": POINT_TYPE, "pressure": pressure}
            for x, y in points
        ],
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "canvasWidth": float(canvas_width),
        "canvasHeight": float(canvas_height),
        "fill": "rgb(0,0,0)",
        "fillRule": "nonzero",
        "paintFirst": "fill",
        "globalCompositeOperation": "source-over",
        "backgroundColor": "",
        "opacity": 1,
        "visible": True,
        "angle": 0,
        "originX": "left",
        "originY": "top",
        "scaleX": 1,
        "scaleY": 1,
        "skewX": 0,
        "skewY": 0,
        "flipX": False,
        "flipY": False,
        "shadow": None,
        "strokeDashArray": None,
        "strokeDashOffset": 0,
        "strokeLineCap": "butt",
        "strokeLineJoin": "miter",
        "strokeMiterLimit": 4,
        "strokeUniform": False,
        "startTime": now_ms,
        "endTime": now_ms,
    }


def annotation(objects, seconds=0, fps=None, frame=None):
    """One annotation entry: the drawing plus where in the media it belongs."""
    return {
        "time": round(float(seconds or 0), 3),
        "frame": frame if frame is not None else frame_for_time(seconds, fps),
        "drawing": {"objects": list(objects)},
    }


def annotation_from_strokes(strokes, canvas_width, canvas_height, seconds=0, fps=None, frame=None):
    """Turns the paint-over canvas's strokes into one annotation entry.

    `strokes` is what _PaintCanvas records: dicts of
    {"points": [(x, y), ...], "color": "#rrggbb", "width": int} already in the
    image's own pixel coordinates — which is why the image size is passed
    straight through as the canvas size."""
    objects = [
        stroke_object(
            stroke["points"],
            stroke["color"],
            stroke["width"],
            canvas_width,
            canvas_height,
        )
        for stroke in strokes
        if stroke.get("points")
    ]
    if not objects:
        return None
    return annotation(objects, seconds=seconds, fps=fps, frame=frame)


def objects_at_frame(preview_file, frame):
    """Existing annotation objects on that frame, so earlier notes can be shown
    under a new pass instead of being invisible. `frame` may be stored as a
    string on a handful of old entries, hence the loose comparison."""
    result = []
    for entry in preview_file.get("annotations") or []:
        if str(entry.get("frame")) != str(frame):
            continue
        result.extend((entry.get("drawing") or {}).get("objects") or [])
    return result


def frames_with_annotations(preview_file):
    """Which frames already carry notes — sorted, ints where possible."""
    frames = set()
    for entry in preview_file.get("annotations") or []:
        try:
            frames.add(int(entry.get("frame")))
        except (TypeError, ValueError):
            continue
    return sorted(frames)
