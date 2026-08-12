"""Fetch a file from a URL so it can be attached to a comment.

Zou has no "link" field on a comment — a comment carries uploaded files and
nothing else — so attaching "a URL" means downloading what it points at and
publishing that file. Doing it here rather than asking the user to save the
file themselves first is the whole point: a render, plate or reference sitting
on some web address goes straight into the comment.

Runs on a background thread (see async_worker.run_async), so everything here
is plain blocking code with no Qt involvement.
"""

import os
import re
import tempfile
from urllib.parse import unquote, urlparse

import requests

_CHUNK_BYTES = 64 * 1024
# A sanity ceiling, not a Kitsu limit: mistyping a URL shouldn't quietly pull
# a multi-gigabyte file into the temp directory.
_MAX_BYTES = 500 * 1024 * 1024
_CONTENT_TYPE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tif",
    "image/bmp": "bmp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "application/pdf": "pdf",
}
_UNSAFE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def download_to_temp(url, timeout=30):
    """Downloads `url` into this app's temp cache and returns the local path.

    Raises ValueError for anything that isn't a plain http(s) URL and lets
    `requests` exceptions through for the caller to show."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs can be attached.")
    if not parsed.netloc:
        raise ValueError(f"{url} is not a complete URL.")

    cache_dir = os.path.join(tempfile.gettempdir(), "kitsu_standalone_url_attachments")
    os.makedirs(cache_dir, exist_ok=True)

    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        target_path = _unique_path(cache_dir, _file_name_for(parsed, response))
        written = 0
        try:
            with open(target_path, "wb") as target_file:
                for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > _MAX_BYTES:
                        raise ValueError(
                            f"{url} is larger than {_MAX_BYTES // (1024 * 1024)} MB — "
                            "download it yourself and use Attach File instead."
                        )
                    target_file.write(chunk)
        except Exception:
            # Don't leave a truncated file behind for the UI to attach.
            if os.path.exists(target_path):
                try:
                    os.remove(target_path)
                except OSError:
                    pass
            raise

    if written == 0:
        raise ValueError(f"{url} returned an empty file.")
    return target_path


def _file_name_for(parsed_url, response):
    from_header = _file_name_from_content_disposition(
        response.headers.get("Content-Disposition") or ""
    )
    if from_header:
        return from_header

    name = _safe_name(os.path.basename(unquote(parsed_url.path)))
    if name and "." in name:
        return name

    # No usable name in the URL (e.g. ".../download?id=123") — name it after
    # what the server says it actually sent.
    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
    stem = name or "download"
    return f"{stem}.{extension}" if extension else stem


def _file_name_from_content_disposition(header):
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.IGNORECASE)
    if match:
        return _safe_name(unquote(match.group(1).strip()))
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    if match:
        return _safe_name(match.group(1).strip())
    return ""


def _safe_name(name):
    """Strips path separators and characters Windows refuses in file names."""
    return _UNSAFE_NAME_CHARACTERS.sub("_", (name or "").strip()).strip(". ")


def _unique_path(directory, file_name):
    stem, extension = os.path.splitext(file_name or "download")
    target_path = os.path.join(directory, f"{stem}{extension}")
    counter = 2
    while os.path.exists(target_path):
        target_path = os.path.join(directory, f"{stem}_{counter}{extension}")
        counter += 1
    return target_path
