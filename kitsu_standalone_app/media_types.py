"""What kind of media a file or Kitsu preview is.

Kitsu hands out an `extension` field on previews and attachments while local
files only have a path, and several places need the same answer from either —
whether something can be shown inline and drawn on, or only downloaded. Kept in
one place so those places can't drift apart.
"""

import os

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
# Kitsu normalizes uploaded movies to mp4, but a production can publish others.
MOVIE_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}


def normalize_extension(extension):
    return (extension or "").strip().lstrip(".").lower()


def extension_of(path):
    return normalize_extension(os.path.splitext(path or "")[1])


def is_image_extension(extension):
    return normalize_extension(extension) in IMAGE_EXTENSIONS


def is_movie_extension(extension):
    return normalize_extension(extension) in MOVIE_EXTENSIONS


def is_image_path(path):
    return extension_of(path) in IMAGE_EXTENSIONS
