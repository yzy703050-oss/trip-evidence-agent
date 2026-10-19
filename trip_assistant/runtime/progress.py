"""Request-local presentation observer; never controls execution."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_observer = ContextVar("travel_progress", default=None)


def report_progress(text):
    callback = _observer.get()
    if callback:
        try:
            callback(text)
        except Exception:
            logging.getLogger(__name__).warning("Progress display unavailable", exc_info=True)


@contextmanager
def progress_scope(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)
