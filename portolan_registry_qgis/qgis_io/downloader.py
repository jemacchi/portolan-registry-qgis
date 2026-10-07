"""Download a planned set of files and verify their checksums.

``QgsFileDownloader`` streams each file to disk on the main thread's event
loop, so a 1 GB GeoParquet file never sits in memory and QGIS stays
responsive. A file that declares ``file:checksum`` is hashed in a background
task before the next download starts. A file already on disk with a matching
checksum is skipped, so a second run resumes an interrupted download.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qgis.core import QgsFileDownloader
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal

from portolan_registry_qgis.core.multihash import ChecksumError, file_matches
from portolan_registry_qgis.qgis_io.network import run_task

if TYPE_CHECKING:
    from pathlib import Path

    from portolan_registry_qgis.core.download import PlannedFile


@dataclass
class DownloadReport:
    """What a download run did."""

    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    verified: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


def target_path(folder: Path, planned: PlannedFile) -> Path:
    """Return where ``planned`` goes, refusing any path outside ``folder``."""
    path = (folder / planned.path).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"{planned.path} would land outside {folder}")
    return path


class DownloadJob(QObject):
    """Download files one after another into a folder.

    Signals:
        progress: ``(files_done, files_total, message)``.
        finished: The ``DownloadReport``.
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(object)

    def __init__(self, folder: Path, files: list[PlannedFile], parent: QObject | None = None):
        super().__init__(parent)
        self._folder = folder
        self._files = list(files)
        self._index = 0
        self._report = DownloadReport()
        self._downloader: QgsFileDownloader | None = None
        self._cancelled = False
        self._done = False

    def start(self) -> None:
        """Begin with the first file."""
        self._next()

    def cancel(self) -> None:
        """Stop after the current file's request is aborted."""
        self._cancelled = True
        self._report.cancelled = True
        if self._downloader is not None:
            self._downloader.cancelDownload()

    def _current(self) -> PlannedFile:
        return self._files[self._index]

    def _finish(self) -> None:
        # A cancelled QgsFileDownloader can emit downloadCanceled and
        # downloadError both, so the report goes out once.
        if not self._done:
            self._done = True
            self.finished.emit(self._report)

    def _next(self) -> None:
        if self._cancelled or self._index >= len(self._files):
            self._finish()
            return
        planned = self._current()
        self.progress.emit(self._index, len(self._files), planned.path)
        try:
            path = target_path(self._folder, planned)
        except ValueError as error:
            self._fail(str(error))
            return
        if path.exists() and planned.checksum:
            self._verify(path, existing=True)
            return
        if path.exists() and planned.size is not None and path.stat().st_size == planned.size:
            self._report.skipped.append(planned.path)
            self._advance()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fetch(path)

    def _fetch(self, path: Path) -> None:
        planned = self._current()
        downloader = QgsFileDownloader(QUrl(planned.url), str(path), "", True)
        # QgsFileDownloader deletes itself when it completes, fails, or is
        # cancelled. C++ must own it, or the Python wrapper deletes it again.
        sip.transferto(downloader, None)
        downloader.downloadCompleted.connect(lambda _url: self._fetched(path))
        downloader.downloadError.connect(lambda errors: self._fail("; ".join(errors)))
        downloader.downloadCanceled.connect(self._stopped)
        self._downloader = downloader
        downloader.startDownload()

    def _fetched(self, path: Path) -> None:
        self._downloader = None
        planned = self._current()
        self._report.downloaded.append(planned.path)
        if planned.checksum:
            self._verify(path, existing=False)
        else:
            self._advance()

    def _verify(self, path: Path, *, existing: bool) -> None:
        planned = self._current()
        checksum = planned.checksum or ""
        self.progress.emit(self._index, len(self._files), f"Verifying {planned.path}")

        def hash_file(task: object) -> bool:
            cancelled = getattr(task, "isCanceled", lambda: False)
            return file_matches(path, checksum, cancelled=cancelled)

        def done(result: object, error: BaseException | None) -> None:
            if isinstance(error, ChecksumError):
                # The catalog's checksum is unreadable, not the file. Keep it.
                if existing:
                    self._report.skipped.append(planned.path)
                self._advance()
            elif error is not None:
                self._fail(f"Checksum check failed: {error}")
            elif result:
                self._report.verified += 1
                if existing:
                    self._report.skipped.append(planned.path)
                self._advance()
            elif existing:
                # A stale or partial copy. Replace it.
                path.unlink(missing_ok=True)
                self._fetch(path)
            else:
                path.unlink(missing_ok=True)
                self._fail("Downloaded bytes do not match file:checksum")

        run_task(f"Verify {planned.path}", hash_file, done)

    def _fail(self, reason: str) -> None:
        self._downloader = None
        if self._cancelled:
            self._stopped()
            return
        self._report.failed.append((self._current().path, reason))
        self._advance()

    def _stopped(self) -> None:
        self._downloader = None
        self._cancelled = True
        self._report.cancelled = True
        self._finish()

    def _advance(self) -> None:
        self._index += 1
        self._next()
