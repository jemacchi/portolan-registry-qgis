"""Fetch documents through QGIS's network stack.

Every request goes through ``QgsBlockingNetworkRequest``, so the user's proxy,
SSL, and authentication settings apply. A blocking request is safe off the
main thread, which is where ``run_task`` sends it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import QgsApplication, QgsBlockingNetworkRequest, QgsTask
from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

if TYPE_CHECKING:
    from collections.abc import Callable

_ACCEPT = b"application/geo+json, application/json"
# QgsTask.fromFunction tasks are owned by the task manager, but the Python
# wrapper is not. Without a reference the wrapper is collected mid-run and its
# on_finished callback never fires.
_RUNNING: set[Any] = set()


class NetworkError(OSError):
    """A request failed or returned an HTTP error."""


def fetch_bytes(url: str) -> bytes:
    """Return the body at ``url``.

    Raises:
        NetworkError: The request failed or the server answered with an error.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Accept", _ACCEPT)
    blocking = QgsBlockingNetworkRequest()
    code = blocking.get(request, forceRefresh=False)
    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"{url}: {blocking.errorMessage()}")
    return bytes(blocking.reply().content())


def fetch_range(url: str, offset: int, length: int) -> bytes:
    """Return ``length`` bytes of ``url`` from ``offset``.

    A server that ignores the Range header answers 200 with the whole body.
    The function slices that body, so the caller always gets the asked range.

    Raises:
        NetworkError: The request failed.
    """
    request = QNetworkRequest(QUrl(url))
    request.setRawHeader(b"Range", f"bytes={offset}-{offset + length - 1}".encode())
    # Qt's disk cache keys on the URL alone. A cached range would answer a
    # request for a different range, so ranges bypass the cache both ways.
    request.setAttribute(QNetworkRequest.Attribute.CacheSaveControlAttribute, False)
    blocking = QgsBlockingNetworkRequest()
    code = blocking.get(request, forceRefresh=True)
    if code != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise NetworkError(f"{url}: {blocking.errorMessage()}")
    reply = blocking.reply()
    body = bytes(reply.content())
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    if status == 200:
        return body[offset : offset + length]
    return body


def range_fetcher(location: str) -> Callable[[int, int], bytes]:
    """Return a ``fetch_range(offset, length)`` for a URL or a local path."""
    if location.startswith(("http://", "https://")):
        return lambda offset, length: fetch_range(location, offset, length)
    path = Path(QUrl(location).toLocalFile() if location.startswith("file:") else location)

    def read(offset: int, length: int) -> bytes:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    return read


def fetch_json(url: str) -> object:
    """Return the parsed JSON at ``url``.

    Raises:
        NetworkError: The request failed.
        ValueError: The body is not JSON.
    """
    body = fetch_bytes(url)
    try:
        return json.loads(body)
    except ValueError as error:
        raise ValueError(f"{url} did not return JSON: {error}") from error


def run_task(
    description: str,
    function: Callable[[QgsTask], object],
    on_done: Callable[[object, BaseException | None], None],
) -> QgsTask:
    """Run ``function`` in a background task and report back on the main thread.

    Args:
        description: Shown in the QGIS task manager.
        function: Called with the task, off the main thread.
        on_done: Called with ``(result, None)`` on success or
            ``(None, error)`` on failure or cancellation.

    Returns:
        The task, already queued.
    """

    def finished(exception: BaseException | None, result: object = None) -> None:
        _RUNNING.discard(task)
        if exception is None and result is None and task.isCanceled():
            exception = NetworkError("Cancelled")
        on_done(None if exception else result, exception)

    task = QgsTask.fromFunction(description, function, on_finished=finished)
    _RUNNING.add(task)
    QgsApplication.taskManager().addTask(task)
    return task
