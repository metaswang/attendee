"""Helpers for control-plane recording readiness races."""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

import requests

logger = logging.getLogger(__name__)

T = TypeVar("T")

RECORDING_NOT_READY_ERROR_TEXT = "No recording in progress"


class RecordingNotReadyError(Exception):
    """Control plane has no recording in progress yet; retry without failing the session."""


def is_recording_not_ready_http_error(exc: BaseException) -> bool:
    if not isinstance(exc, requests.HTTPError):
        return False
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 409:
        return False
    try:
        body = response.text or ""
    except Exception:
        body = ""
    return RECORDING_NOT_READY_ERROR_TEXT in body


def is_recording_not_ready_error(exc: BaseException) -> bool:
    if isinstance(exc, RecordingNotReadyError):
        return True
    return is_recording_not_ready_http_error(exc)


def post_with_recording_ready_retry(
    post_fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 5,
    base_sleep_seconds: float = 0.2,
) -> T:
    """Short-retry POSTs that can race ahead of JOINED_RECORDING."""
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return post_fn()
        except requests.HTTPError as exc:
            if not is_recording_not_ready_http_error(exc):
                raise
            last_exc = exc
            logger.info(
                "%s deferred; recording not ready yet attempt=%s/%s",
                what,
                attempt,
                attempts,
            )
            if attempt >= attempts:
                break
            time.sleep(base_sleep_seconds * attempt)
    assert last_exc is not None
    raise RecordingNotReadyError(str(last_exc)) from last_exc
