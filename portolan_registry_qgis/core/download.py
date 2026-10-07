"""Plan the download of a catalog, collection, or item to disk.

The plan keeps the catalog's own layout. Portolan catalogs link with relative
hrefs, so saving each STAC document next to its assets at the same relative
path gives a local copy that STAC tools can open.
"""

from __future__ import annotations

import posixpath
import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from portolan_registry_qgis.core.stac import Document, StacError, read_document

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    FetchJson = Callable[[str], object]

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+=-]")
EXTERNAL_DIR = "_external"


@dataclass(frozen=True)
class PlannedFile:
    """One file to download, and where it goes under the target folder."""

    url: str
    path: str
    size: int | None = None
    checksum: str | None = None


@dataclass
class Plan:
    """The files to download, and the documents the walk could not read."""

    files: list[PlannedFile]
    failures: list[tuple[str, str]]
    truncated: bool = False

    @property
    def known_bytes(self) -> int:
        """The sum of the sizes the catalog declares."""
        return sum(file.size or 0 for file in self.files)

    @property
    def unknown_sizes(self) -> int:
        """How many files have no declared size."""
        return sum(1 for file in self.files if file.size is None)


def _safe_segments(path: str) -> list[str]:
    segments = []
    for segment in unquote(path).split("/"):
        if segment in {"", ".", ".."}:
            continue
        segments.append(_UNSAFE.sub("_", segment))
    return segments


def local_path(url: str, root: str) -> str:
    """Return the POSIX path, relative to the target folder, for ``url``.

    A URL under the directory of ``root`` keeps its relative path. Anything
    else goes under ``_external/<host>/``. No result can climb out of the
    target folder, because ``..`` segments are dropped and each segment is
    reduced to a safe character set.
    """
    target, base = urlsplit(url), urlsplit(root)
    base_dir = posixpath.dirname(base.path).rstrip("/") + "/"
    if (target.scheme, target.netloc) == (base.scheme, base.netloc) and target.path.startswith(
        base_dir
    ):
        segments = _safe_segments(target.path[len(base_dir) :])
    else:
        segments = [EXTERNAL_DIR, *_safe_segments(target.netloc), *_safe_segments(target.path)]
    if not segments or segments == [EXTERNAL_DIR]:
        segments.append("index")
    return "/".join(segments)


def files_of(document: Document, root: str) -> list[PlannedFile]:
    """Return the document itself, its assets, and its PMTiles links."""
    files = [PlannedFile(document.href, local_path(document.href, root))]
    files.extend(
        PlannedFile(asset.href, local_path(asset.href, root), asset.size, asset.checksum)
        for asset in document.assets
        if asset.href.startswith(("http://", "https://"))
    )
    asset_urls = {asset.href for asset in document.assets}
    files.extend(
        PlannedFile(link.href, local_path(link.href, root))
        for link in document.pmtiles
        if link.href not in asset_urls
    )
    return files


def walk(
    fetch_json: FetchJson,
    start: str,
    failures: list[tuple[str, str]],
    max_documents: int = 5000,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[Document]:
    """Read ``start`` and every catalog, collection, and item below it.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON. It raises on
            failure.
        start: The document to begin with.
        failures: Receives ``(url, reason)`` for each document that could
            not be read. The walk continues past it.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.

    Yields:
        Each document read, breadth first.
    """
    queue: deque[str] = deque([start])
    seen = {start}
    reads = 0
    while queue and reads < max_documents:
        if cancelled is not None and cancelled():
            return
        href = queue.popleft()
        reads += 1
        try:
            document = read_document(fetch_json(href), href)
        except (OSError, ValueError, StacError) as error:
            failures.append((href, str(error)))
            continue
        yield document
        for child in document.children:
            if child.href not in seen:
                seen.add(child.href)
                queue.append(child.href)


def plan(
    fetch_json: FetchJson,
    start: str,
    root: str | None = None,
    max_documents: int = 5000,
    cancelled: Callable[[], bool] | None = None,
) -> Plan:
    """Plan the download of ``start`` and everything below it.

    Args:
        fetch_json: Fetches a URL and returns the parsed JSON.
        start: The catalog, collection, or item to download.
        root: The URL that local paths are relative to. Defaults to
            ``start``, so the chosen document lands at the top of the folder.
        max_documents: Stops the walk after this many reads.
        cancelled: Polled before each read.
    """
    base = root or start
    failures: list[tuple[str, str]] = []
    files: dict[str, PlannedFile] = {}
    reads = 0
    for document in walk(fetch_json, start, failures, max_documents, cancelled):
        reads += 1
        for planned in files_of(document, base):
            files.setdefault(planned.url, planned)
    truncated = reads + len(failures) >= max_documents
    return Plan(list(files.values()), failures, truncated=truncated)
