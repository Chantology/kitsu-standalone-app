"""Run a blocking call off the UI thread, deliver the result back on it.

Ported from the Nuke/Maya plugins' `_AsyncWorker`/`_run_async` pair: a slow
or unresponsive Kitsu server must never freeze the Qt event loop. Qt
automatically marshals a signal emitted from a background thread onto the
receiving QObject's own (here: main/UI) thread, so this is thread-safe by
construction — unlike touching Qt widgets directly from the background
thread, which callers must never do inside `work_fn`.
"""

import threading

from PySide6.QtCore import QObject, Signal


class _AsyncWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def start(self, work_fn):
        thread = threading.Thread(target=self._run, args=(work_fn,), daemon=True)
        thread.start()

    def _run(self, work_fn):
        try:
            result = work_fn()
        except Exception as exc:  # forwarded to on_error, whatever it is
            self.failed.emit(exc)
        else:
            self.finished.emit(result)


def run_async(owner, work_fn, on_done, on_error=None):
    """Run `work_fn` (a plain callable, no arguments) on a background
    thread. `on_done(result)` or `on_error(exc)` runs back on the UI thread
    once it finishes.

    `work_fn` must never touch Qt widgets — only plain Python and
    session/gazu network calls. Resolve any UI state needed by `work_fn`
    on the calling (UI) thread first and pass it in as a plain value.

    `owner` is any QObject that outlives the call (typically the widget
    that triggered it) — it keeps the worker instance and its background
    thread reference alive for the call's duration, and is used to detect
    if the owning widget was destroyed before the call finished, in which
    case both callbacks are silently dropped instead of touching a deleted
    C++ object.
    """
    worker = _AsyncWorker(owner)
    if not hasattr(owner, "_async_workers"):
        owner._async_workers = []
    owner._async_workers.append(worker)

    def _cleanup():
        if worker in owner._async_workers:
            owner._async_workers.remove(worker)

    def _is_alive():
        try:
            owner.objectName()  # cheap call that raises once the
                                 # underlying C++ widget has been deleted
        except RuntimeError:
            return False
        return True

    def _handle_done(result):
        _cleanup()
        if _is_alive():
            on_done(result)

    def _handle_error(exc):
        _cleanup()
        if _is_alive() and on_error is not None:
            on_error(exc)

    worker.finished.connect(_handle_done)
    worker.failed.connect(_handle_error)
    worker.start(work_fn)
