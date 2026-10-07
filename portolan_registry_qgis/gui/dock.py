"""The Portolan Registry dock panel.

Top to bottom: the registry's catalogs with filters, the chosen catalog's
tree, the chosen node's details, and its assets with the actions that add
them to the map or download them.
"""

from __future__ import annotations

import contextlib
import html
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMessageLog,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QImage, QPixmap
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from portolan_registry_qgis.core import download, parquet_query, registry
from portolan_registry_qgis.core.format import human_size
from portolan_registry_qgis.core.stac import Asset, Document, Node, PmtilesLink, read_document
from portolan_registry_qgis.qgis_io import layers as layer_io
from portolan_registry_qgis.qgis_io import parquet_layer
from portolan_registry_qgis.qgis_io.downloader import DownloadJob, DownloadReport
from portolan_registry_qgis.qgis_io.network import fetch_bytes, fetch_json, run_task

if TYPE_CHECKING:
    from portolan_registry_qgis.core.registry import CatalogEntry
    from portolan_registry_qgis.qgis_io.tileserver import TileServer

LOG_TAG = "Portolan Registry"
_ROLE = Qt.ItemDataRole.UserRole
_WGS84 = "EPSG:4326"
_PMTILES_KEY = "\x00pmtiles:"


def _log(message: str, level: Any = None) -> None:
    QgsMessageLog.logMessage(message, LOG_TAG, level or Qgis.MessageLevel.Info)


class RegistryDock(QDockWidget):
    """Browse the Portolan registry, add its data to the map, and download it."""

    def __init__(self, iface: Any, server: TileServer, registry_url: str = registry.REGISTRY_URL):
        super().__init__("Portolan Registry")
        self.setObjectName("PortolanRegistryDock")
        self._iface = iface
        self._server = server
        self._registry_url = registry_url
        self._entries: list[CatalogEntry] = []
        self._catalog: CatalogEntry | None = None
        self._documents: dict[str, Document] = {}
        self._document: Document | None = None
        self._job: DownloadJob | None = None
        self._build()
        self.reload()

    # ---------- layout ----------

    def _build(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(4, 4, 4, 4)

        filters = QHBoxLayout()
        self.search = QLineEdit(placeholderText="Filter catalogs")
        self.search.setClearButtonEnabled(True)
        self.status = QComboBox()
        self.in_extent = QCheckBox("In map extent")
        self.refresh = QToolButton(text="Reload")
        self.refresh.setToolTip("Read the registry again")
        for widget in (self.search, self.status, self.in_extent, self.refresh):
            filters.addWidget(widget)
        filters.setStretch(0, 1)
        layout.addLayout(filters)

        self.catalogs = self._tree(["Catalog", "Status", "Collections", "Size"])
        self.tree = self._tree(["Contents"])
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)
        self.thumbnail = QLabel()
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.hide()
        self.assets = self._tree(["Asset", "Format", "Size", "Roles"])
        self.assets.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        info = QWidget()
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.addWidget(self.thumbnail)
        info_layout.addWidget(self.details)

        splitter = QSplitter(Qt.Orientation.Vertical)
        for widget in (self.catalogs, self.tree, info, self.assets):
            splitter.addWidget(widget)
        layout.addWidget(splitter, 1)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style"))
        self.style = QComboBox()
        self.style.setToolTip("MapLibre style applied to vector tiles")
        style_row.addWidget(self.style, 1)
        layout.addLayout(style_row)

        self.parquet_extent = QCheckBox("Load GeoParquet in the map extent only")
        self.parquet_extent.setChecked(True)
        self.parquet_extent.setToolTip(
            "DuckDB reads only the features that intersect the current map extent. "
            f"Every read stops at {parquet_layer.DEFAULT_LIMIT:,} features."
        )
        layout.addWidget(self.parquet_extent)

        actions = QHBoxLayout()
        self.add = QPushButton("Add to map")
        self.zoom = QPushButton("Zoom to extent")
        self.download_selected = QPushButton("Download selected…")
        self.download_all = QPushButton("Download all…")
        self.download_all.setToolTip("Download this node and everything below it")
        for button in (self.add, self.zoom, self.download_selected, self.download_all):
            actions.addWidget(button)
        layout.addLayout(actions)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        self.cancel = QPushButton("Cancel")
        self.cancel.hide()
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.cancel)
        layout.addLayout(progress_row)

        self.setWidget(root)
        self._connect()
        self._sync_buttons()

    @staticmethod
    def _tree(headers: list[str]) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels(headers)
        tree.setRootIsDecorated(len(headers) == 1)
        tree.setUniformRowHeights(True)
        header = tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        return tree

    def _connect(self) -> None:
        self.search.textChanged.connect(self._show_catalogs)
        self.status.currentIndexChanged.connect(self._show_catalogs)
        self.in_extent.toggled.connect(self._show_catalogs)
        self.refresh.clicked.connect(self.reload)
        self._iface.mapCanvas().extentsChanged.connect(self._extent_changed)
        self.catalogs.currentItemChanged.connect(self._catalog_chosen)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(self._node_chosen)
        self.assets.itemSelectionChanged.connect(self._sync_buttons)
        self.assets.itemDoubleClicked.connect(lambda *_: self._add_selected())
        self.add.clicked.connect(self._add_selected)
        self.zoom.clicked.connect(self._zoom)
        self.download_selected.clicked.connect(self._download_selected)
        self.download_all.clicked.connect(self._download_all)
        self.cancel.clicked.connect(self._cancel_download)

    def disconnect_canvas(self) -> None:
        """Stop following the map canvas. Call before the dock is deleted."""
        with contextlib.suppress(TypeError):
            self._iface.mapCanvas().extentsChanged.disconnect(self._extent_changed)

    # ---------- registry ----------

    def reload(self) -> None:
        """Read the registry and list its catalogs."""
        self.catalogs.clear()
        self._placeholder(self.catalogs, "Reading the Portolan registry…")
        url = self._registry_url

        def read(_task: object) -> list[CatalogEntry]:
            return registry.parse_registry(fetch_json(url), url)

        run_task("Read the Portolan registry", read, self._registry_read)

    def _registry_read(self, result: object, error: BaseException | None) -> None:
        if error is not None or not isinstance(result, list):
            self.catalogs.clear()
            self._placeholder(self.catalogs, "The Portolan registry is unavailable.")
            self._warn(f"Could not read the Portolan registry: {error}")
            return
        self._entries = result
        current = self.status.currentData()
        self.status.blockSignals(True)
        self.status.clear()
        self.status.addItem("All statuses", None)
        for status in registry.statuses(result):
            self.status.addItem(status.capitalize(), status)
        index = self.status.findData(current)
        self.status.setCurrentIndex(max(index, 0))
        self.status.blockSignals(False)
        self._show_catalogs()

    def _extent_changed(self) -> None:
        if self.in_extent.isChecked():
            self._show_catalogs()

    def canvas_bbox(self) -> tuple[float, float, float, float] | None:
        """Return the map extent in WGS84, or None when it cannot be transformed."""
        canvas = self._iface.mapCanvas()
        transform = QgsCoordinateTransform(
            canvas.mapSettings().destinationCrs(),
            QgsCoordinateReferenceSystem(_WGS84),
            QgsProject.instance(),
        )
        try:
            rect = transform.transformBoundingBox(canvas.extent())
        except Exception:  # noqa: BLE001 - QgsCsException is not importable by name in every QGIS
            return None
        return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())

    def _show_catalogs(self) -> None:
        bbox = self.canvas_bbox() if self.in_extent.isChecked() else None
        shown = registry.filter_entries(
            self._entries, self.search.text(), self.status.currentData(), bbox
        )
        self.catalogs.clear()
        for entry in shown:
            item = QTreeWidgetItem(
                [
                    entry.title,
                    entry.status,
                    "" if entry.collection_count is None else str(entry.collection_count),
                    human_size(entry.total_size_bytes),
                ]
            )
            item.setData(0, _ROLE, entry)
            item.setToolTip(0, entry.url)
            if entry.failure_reason:
                item.setToolTip(1, entry.failure_reason)
            self.catalogs.addTopLevelItem(item)
        if not shown and self._entries:
            self._placeholder(self.catalogs, "No catalog matches these filters.")

    # ---------- catalog tree ----------

    def _catalog_chosen(self, item: QTreeWidgetItem | None, _previous: object = None) -> None:
        entry = item.data(0, _ROLE) if item is not None else None
        if entry is None or entry is self._catalog:
            return
        self._catalog = entry
        self.tree.clear()
        root = self._node_item(Node(entry.url, entry.title, "catalog"))
        self.tree.addTopLevelItem(root)
        self.tree.setCurrentItem(root)
        root.setExpanded(True)

    def _node_item(self, node: Node) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.title])
        item.setData(0, _ROLE, node)
        item.setToolTip(0, node.href)
        if node.kind != "item":
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        return item

    def _open(self, href: str, then: Any) -> None:
        cached = self._documents.get(href)
        if cached is not None:
            then(cached, None)
            return

        def read(_task: object) -> Document:
            return read_document(fetch_json(href), href)

        def done(result: object, error: BaseException | None) -> None:
            if isinstance(result, Document):
                self._documents[href] = result
            then(result, error)

        run_task(f"Read {href}", read, done)

    def _expand(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, _ROLE)
        if not isinstance(node, Node) or item.data(1, _ROLE):
            return
        item.setData(1, _ROLE, True)
        loading = QTreeWidgetItem(["Loading…"])
        item.addChild(loading)

        def opened(document: object, error: BaseException | None) -> None:
            item.takeChildren()
            if not isinstance(document, Document):
                item.setData(1, _ROLE, False)
                item.addChild(QTreeWidgetItem([f"Could not read: {error}"]))
                return
            for child in document.children:
                item.addChild(self._node_item(child))
            if not document.children:
                item.setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                )

        self._open(node.href, opened)

    def _node_chosen(self, item: QTreeWidgetItem | None, _previous: object = None) -> None:
        node = item.data(0, _ROLE) if item is not None else None
        if not isinstance(node, Node):
            return
        self._document = None
        self.details.setPlainText(f"Reading {node.href}…")
        self.assets.clear()
        self.thumbnail.hide()
        self._sync_buttons()

        def opened(document: object, error: BaseException | None) -> None:
            if self.tree.currentItem() is not item:
                return
            if not isinstance(document, Document):
                self.details.setPlainText(f"Could not read {node.href}:\n{error}")
                return
            self._show_document(document)

        self._open(node.href, opened)

    # ---------- details and assets ----------

    def _show_document(self, document: Document) -> None:
        self._document = document
        self.details.setHtml(_details_html(document))
        self.assets.clear()
        for link in document.pmtiles:
            row = QTreeWidgetItem(
                [link.title or "Vector tiles", "pmtiles", "", ", ".join(link.layers)]
            )
            row.setData(0, _ROLE, _PMTILES_KEY + link.href)
            self.assets.addTopLevelItem(row)
        for asset in document.assets:
            row = QTreeWidgetItem(
                [asset.label, asset.format or "", human_size(asset.size), ", ".join(asset.roles)]
            )
            row.setData(0, _ROLE, asset)
            row.setToolTip(0, asset.href)
            self.assets.addTopLevelItem(row)
        self.style.clear()
        for style in document.styles:
            self.style.addItem(style.label, style)
        self.style.addItem("QGIS default style", None)
        self._load_thumbnail(document)
        self._sync_buttons()

    def _load_thumbnail(self, document: Document) -> None:
        thumb = next((a for a in document.assets if "thumbnail" in a.roles), None)
        if thumb is None:
            return

        def done(result: object, _error: BaseException | None) -> None:
            if self._document is not document or not isinstance(result, bytes):
                return
            image = QImage.fromData(result)
            if image.isNull():
                return
            pixmap = QPixmap.fromImage(image).scaledToWidth(
                min(320, image.width()), Qt.TransformationMode.SmoothTransformation
            )
            self.thumbnail.setPixmap(pixmap)
            self.thumbnail.show()

        run_task(f"Read {thumb.href}", lambda _task: fetch_bytes(thumb.href), done)

    def _selected(self) -> tuple[list[str], list[Asset]]:
        tiles: list[str] = []
        assets: list[Asset] = []
        for row in self.assets.selectedItems():
            value = row.data(0, _ROLE)
            if isinstance(value, str) and value.startswith(_PMTILES_KEY):
                tiles.append(value[len(_PMTILES_KEY) :])
            elif isinstance(value, Asset):
                if value.format == "pmtiles":
                    tiles.append(value.href)
                else:
                    assets.append(value)
        return tiles, assets

    def _sync_buttons(self) -> None:
        tiles, assets = self._selected()
        loadable = [a for a in assets if a.format is not None]
        self.add.setEnabled(bool(tiles or loadable))
        self.style.setEnabled(bool(tiles) and self.style.count() > 1)
        has_document = self._document is not None
        self.zoom.setEnabled(has_document and self._document.bbox is not None)  # type: ignore[union-attr]
        self.download_selected.setEnabled(bool(tiles or assets) and self._job is None)
        self.download_all.setEnabled(has_document and self._job is None)

    def _add_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        if tiles:
            self._add_tiles(document, tiles)
        for asset in assets:
            if asset.format == "parquet":
                self._add_parquet(asset)
            elif asset.format is not None:
                self._add_asset(asset)

    def _add_tiles(self, document: Document, urls: list[str]) -> None:
        style = self.style.currentData()
        known = {link.href for link in document.pmtiles}
        # The links the user picked, plus PMTiles that a catalog publishes as
        # an asset without a matching link.
        picked = [link for link in document.pmtiles if link.href in urls]
        picked += [PmtilesLink(url, None, ()) for url in urls if url not in known]
        scoped = replace(document, pmtiles=tuple(picked))

        def prepare(_task: object) -> layer_io.PreparedTiles:
            return layer_io.prepare_pmtiles(scoped, fetch_bytes, style)

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, layer_io.PreparedTiles):
                self._warn(f"Could not add vector tiles: {error}")
                return
            try:
                layers, warnings = layer_io.build_pmtiles(
                    self._server, result, lambda data: QImage.fromData(data)
                )
            except layer_io.LayerError as failure:
                self._warn(str(failure))
                return
            for note in warnings:
                _log(note, Qgis.MessageLevel.Warning)
            for layer in layers:
                QgsProject.instance().addMapLayer(layer)
            self._info(f"Added {len(layers)} vector tile layer(s) from {document.title}.")

        run_task(f"Open vector tiles of {document.title}", prepare, done)

    def _add_asset(self, asset: Asset) -> None:
        try:
            layer = layer_io.asset_layer(asset)
        except layer_io.LayerError as failure:
            self._warn(str(failure))
            return
        QgsProject.instance().addMapLayer(layer)
        self._info(f"Added {asset.label}.")

    def _add_parquet(self, asset: Asset) -> None:
        ok, version = parquet_query.duckdb_status()
        if not ok:
            self._duckdb_help(version)
            return
        canvas = self._iface.mapCanvas()
        extent = canvas.extent() if self.parquet_extent.isChecked() else None
        extent_crs = canvas.mapSettings().destinationCrs()
        context = QgsProject.instance().transformContext()
        extension_dir = parquet_layer.extension_directory()
        name = asset.title or asset.href.rsplit("/", 1)[-1].removesuffix(".parquet")

        def read(task: Any) -> parquet_layer.Prepared:
            return parquet_layer.prepare(
                asset.href,
                context,
                extent,
                extent_crs,
                extension_dir=extension_dir,
                cancelled=task.isCanceled,
            )

        def done(result: object, error: BaseException | None) -> None:
            if not isinstance(result, parquet_layer.Prepared):
                self._warn(f"DuckDB could not read {asset.href}: {error}")
                return
            layer, refused = parquet_layer.build(result, name)
            QgsProject.instance().addMapLayer(layer)
            notes = [f"Added {layer.featureCount():,} features from {name}."]
            if result.truncated:
                notes.append(f"The read stopped at {parquet_layer.DEFAULT_LIMIT:,} features.")
            if refused:
                notes.append(f"{refused} features had a geometry type the layer cannot hold.")
            if not result.features and extent is not None:
                notes.append("No feature intersects the map extent.")
            (self._warn if result.truncated or refused else self._info)(" ".join(notes))

        self._info(f"Reading {name} with DuckDB…")
        run_task(f"Read {asset.href} with DuckDB", read, done)

    def _duckdb_help(self, version: str | None) -> None:
        minimum = ".".join(str(part) for part in parquet_query.MINIMUM_DUCKDB)
        if version:
            heading = (
                f"DuckDB {html.escape(version)} is too old. GeoParquet needs {minimum} or newer."
            )
        else:
            heading = f"GeoParquet needs DuckDB {minimum} or newer, and it is not installed."
        command = html.escape(parquet_query.install_command(upgrade=version is not None))
        where = (
            "QGIS runs in a Flatpak, so install DuckDB into your user folder from a terminal:"
            if parquet_query.is_flatpak()
            else "Install it from a terminal with the Python that QGIS uses:"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Portolan Registry: DuckDB needed")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<p><b>{heading}</b></p><p>{where}</p><pre>{command}</pre><p>Then restart QGIS.</p>"
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()

    def _zoom(self) -> None:
        document = self._document
        if document is None or document.bbox is None:
            return
        canvas = self._iface.mapCanvas()
        transform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem(_WGS84),
            canvas.mapSettings().destinationCrs(),
            QgsProject.instance(),
        )
        try:
            extent = transform.transformBoundingBox(QgsRectangle(*document.bbox))
        except Exception as error:  # noqa: BLE001 - see canvas_bbox
            self._warn(f"Could not transform the extent: {error}")
            return
        canvas.setExtent(extent)
        canvas.refresh()

    # ---------- downloads ----------

    def _choose_folder(self) -> Path | None:
        folder = QFileDialog.getExistingDirectory(self, "Download into folder")
        return Path(folder) if folder else None

    def _root_url(self) -> str | None:
        return self._catalog.url if self._catalog is not None else None

    def _download_selected(self) -> None:
        document = self._document
        if document is None:
            return
        tiles, assets = self._selected()
        root = self._root_url() or document.href
        files = [
            download.PlannedFile(a.href, download.local_path(a.href, root), a.size, a.checksum)
            for a in assets
        ]
        files += [download.PlannedFile(url, download.local_path(url, root)) for url in tiles]
        folder = self._choose_folder()
        if folder is not None:
            self._start_download(folder, files)

    def _download_all(self) -> None:
        document = self._document
        if document is None:
            return
        folder = self._choose_folder()
        if folder is None:
            return
        root = self._root_url() or document.href
        self._show_progress(0, 0, f"Listing everything below {document.title}…")

        def build(task: Any) -> download.Plan:
            return download.plan(fetch_json, document.href, root, cancelled=task.isCanceled)

        def planned(result: object, error: BaseException | None) -> None:
            self._hide_progress()
            if not isinstance(result, download.Plan):
                self._warn(f"Could not list {document.title}: {error}")
                return
            if self._confirm(result):
                self._start_download(folder, result.files)

        run_task(f"List {document.title}", build, planned)

    def _confirm(self, plan: download.Plan) -> bool:
        lines = [f"{len(plan.files)} files, at least {human_size(plan.known_bytes)}."]
        if plan.unknown_sizes:
            lines.append(f"{plan.unknown_sizes} files do not declare a size.")
        if plan.failures:
            lines.append(f"{len(plan.failures)} documents could not be read and are left out.")
        if plan.truncated:
            lines.append("The listing stopped at its document limit.")
        answer = QMessageBox.question(self, "Download", "\n".join([*lines, "", "Download now?"]))
        return answer == QMessageBox.StandardButton.Yes

    def _start_download(self, folder: Path, files: list[download.PlannedFile]) -> None:
        job = DownloadJob(folder, files, self)
        job.progress.connect(self._show_progress)
        job.finished.connect(lambda report: self._downloaded(folder, report))
        self._job = job
        self.cancel.show()
        self._sync_buttons()
        job.start()

    def _cancel_download(self) -> None:
        if self._job is not None:
            self._job.cancel()

    def _downloaded(self, folder: Path, report: DownloadReport) -> None:
        self._job = None
        self._hide_progress()
        self._sync_buttons()
        for path, reason in report.failed:
            _log(f"{path}: {reason}", Qgis.MessageLevel.Warning)
        summary = (
            f"{len(report.downloaded)} downloaded, {len(report.skipped)} already present, "
            f"{report.verified} checksums verified, {len(report.failed)} failed"
        )
        if report.cancelled:
            self._warn(f"Download cancelled: {summary}.")
        elif report.failed:
            self._warn(f"Download into {folder} finished with errors: {summary}.")
        else:
            self._info(f"Download into {folder} finished: {summary}.")

    def _show_progress(self, done: int, total: int, message: str) -> None:
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%v/%m)" if total else message)
        self.progress.show()

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.cancel.hide()

    # ---------- messages ----------

    @staticmethod
    def _placeholder(tree: QTreeWidget, text: str) -> None:
        item = QTreeWidgetItem([text])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        tree.addTopLevelItem(item)

    def _info(self, text: str) -> None:
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Info, 5)

    def _warn(self, text: str) -> None:
        _log(text, Qgis.MessageLevel.Warning)
        self._iface.messageBar().pushMessage(LOG_TAG, text, Qgis.MessageLevel.Warning, 10)


def _details_html(document: Document) -> str:
    parts = [f"<h3>{html.escape(document.title)}</h3>"]
    facts = [("Type", document.kind.capitalize()), ("ID", document.id)]
    if document.license:
        facts.append(("License", document.license))
    if document.bbox is not None:
        facts.append(("Extent", ", ".join(f"{value:.4f}" for value in document.bbox)))
    parts.append(
        "<table>"
        + "".join(
            f"<tr><td><b>{html.escape(k)}</b></td><td>{html.escape(v)}</td></tr>" for k, v in facts
        )
        + "</table>"
    )
    if document.description:
        parts.append(f"<p>{html.escape(document.description).replace(chr(10), '<br>')}</p>")
    docs = [link for link in document.links if link.rel in {"describedby", "license", "via"}]
    if docs:
        parts.append(
            "<p>"
            + " · ".join(
                f'<a href="{html.escape(link.href)}">{html.escape(link.title or link.rel)}</a>'
                for link in docs
            )
            + "</p>"
        )
    parts.append(f'<p><a href="{html.escape(document.href)}">{html.escape(document.href)}</a></p>')
    return "".join(parts)
