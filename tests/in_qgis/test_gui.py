from __future__ import annotations

from pathlib import Path

import pytest
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsRectangle,
    QgsSettings,
    QgsVectorTileLayer,
)
from qgis.gui import QgsMapCanvas, QgsMessageBar
from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

import portolan_registry_qgis
from portolan_registry_qgis.gui import dock as dock_module
from portolan_registry_qgis.gui.dock import RegistryDock
from portolan_registry_qgis.plugin import REGISTRY_URL_SETTING
from portolan_registry_qgis.qgis_io.layers import PMTILES_PROPERTY
from portolan_registry_qgis.qgis_io.tileserver import TileServer

from .conftest import wait_for


class FakeIface:
    """The parts of QgisInterface the plugin calls."""

    def __init__(self):
        self.window = QMainWindow()
        self.canvas = QgsMapCanvas()
        self.canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        self.canvas.resize(400, 400)
        self.bar = QgsMessageBar()
        self.docks = []
        self.menu = []
        self.toolbar = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def messageBar(self):
        return self.bar

    def addDockWidget(self, _area, dock):
        self.docks.append(dock)

    def removeDockWidget(self, dock):
        self.docks.remove(dock)

    def addPluginToWebMenu(self, _menu, action):
        self.menu.append(action)

    def removePluginWebMenu(self, _menu, action):
        self.menu.remove(action)

    def addWebToolBarIcon(self, action):
        self.toolbar.append(action)

    def removeWebToolBarIcon(self, action):
        self.toolbar.remove(action)


@pytest.fixture
def iface():
    fake = FakeIface()
    yield fake
    QgsProject.instance().clear()
    for widget in (*fake.docks, fake.canvas, fake.bar, fake.window):
        widget.deleteLater()
    QgsApplication.processEvents()


@pytest.fixture
def server():
    tiles = TileServer()
    yield tiles
    tiles.stop()


def rows(tree):
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def open_dock(iface, server, catalog):
    dock = RegistryDock(iface, server, f"{catalog['base']}/registry.json")
    wait_for(lambda: rows(dock.catalogs) == ["Test catalog"])
    return dock


def open_collection(dock):
    dock.catalogs.setCurrentItem(dock.catalogs.topLevelItem(0))
    root = dock.tree.topLevelItem(0)
    wait_for(lambda: root.childCount() == 2 and root.child(0).text(0) == "Test points")
    dock.tree.setCurrentItem(root.child(0))
    wait_for(lambda: dock.assets.topLevelItemCount() > 0)


def test_registry_lists_and_filters(iface, server, catalog):
    dock = open_dock(iface, server, catalog)
    assert dock.status.itemText(1) == "Valid"
    dock.search.setText("nothing like this")
    assert rows(dock.catalogs) == ["No catalog matches these filters."]
    dock.search.setText("test")
    assert rows(dock.catalogs) == ["Test catalog"]
    # Far from the catalog's extent, the extent filter hides it.
    iface.canvas.setExtent(QgsRectangle(-80, 30, -70, 40))
    dock.in_extent.setChecked(True)
    assert rows(dock.catalogs) == ["No catalog matches these filters."]
    iface.canvas.setExtent(QgsRectangle(10, 43, 14, 46))
    assert rows(dock.catalogs) == ["Test catalog"]
    dock.disconnect_canvas()


def test_unreachable_registry_says_so(iface, server, catalog):
    dock = RegistryDock(iface, server, f"{catalog['base']}/gone.json")
    wait_for(lambda: rows(dock.catalogs) == ["The Portolan registry is unavailable."])
    dock.disconnect_canvas()


def test_tree_details_and_assets(iface, server, catalog):
    dock = open_dock(iface, server, catalog)
    open_collection(dock)
    names = rows(dock.assets)
    assert names[0] == "Point tiles"
    assert "geojson" in names
    assert "Test points" in dock.details.toPlainText()
    assert "CC-BY-4.0" in dock.details.toPlainText()
    assert dock.style.itemText(0) == "style-red"
    assert dock.zoom.isEnabled()
    assert dock.download_all.isEnabled()
    assert not dock.add.isEnabled()
    # The missing collection reports its failure in the details pane.
    root = dock.tree.topLevelItem(0)
    dock.tree.setCurrentItem(root.child(1))
    wait_for(lambda: "Could not read" in dock.details.toPlainText())
    dock.disconnect_canvas()


def asset_row(dock, name):
    return next(
        dock.assets.topLevelItem(i)
        for i in range(dock.assets.topLevelItemCount())
        if dock.assets.topLevelItem(i).text(0) == name
    )


def test_add_styled_tiles_and_assets(iface, server, catalog):
    dock = open_dock(iface, server, catalog)
    open_collection(dock)
    # Zoom out first, so the map extent holds every point.
    iface.canvas.setExtent(QgsRectangle(0, 30, 30, 60))
    dock.parquet_extent.setChecked(True)
    for name in ("Point tiles", "geojson", "data"):
        asset_row(dock, name).setSelected(True)
    assert dock.add.isEnabled()
    dock.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 3, timeout=60)
    added = {layer.name(): layer for layer in QgsProject.instance().mapLayers().values()}
    tiles = added["Points in red"]
    assert isinstance(tiles, QgsVectorTileLayer)
    assert tiles.customProperty(PMTILES_PROPERTY).endswith("points.pmtiles")
    assert added["geojson"].isValid()
    assert added["points"].featureCount() == 200
    dock.zoom.click()
    extent = iface.canvas.extent()
    assert extent.contains(QgsRectangle(11.5, 44.2, 12.5, 44.8))
    dock.disconnect_canvas()


def test_parquet_in_a_small_extent(iface, server, catalog):
    dock = open_dock(iface, server, catalog)
    open_collection(dock)
    iface.canvas.setExtent(QgsRectangle(11.095, 44.0, 11.305, 45.0))
    asset_row(dock, "data").setSelected(True)
    dock.add.click()
    wait_for(lambda: len(QgsProject.instance().mapLayers()) == 1, timeout=60)
    (layer,) = QgsProject.instance().mapLayers().values()
    assert 0 < layer.featureCount() < 200
    dock.disconnect_canvas()


def test_missing_duckdb_shows_install_help(iface, server, catalog, monkeypatch):
    shown = []
    monkeypatch.setattr(dock_module.parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setenv("FLATPAK_ID", "org.qgis.qgis")
    monkeypatch.setattr(dock_module.QMessageBox, "exec", lambda box: shown.append(box.text()))
    dock = open_dock(iface, server, catalog)
    open_collection(dock)
    asset_row(dock, "data").setSelected(True)
    dock.add.click()
    assert len(shown) == 1
    assert "flatpak run --command=python3 org.qgis.qgis" in shown[0]
    assert "not installed" in shown[0]
    assert QgsProject.instance().mapLayers() == {}
    dock.disconnect_canvas()


def test_download_all(iface, server, catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(dock_module.QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    asked = []

    def answer(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(dock_module.QMessageBox, "question", answer)
    dock = open_dock(iface, server, catalog)
    open_collection(dock)
    dock.tree.setCurrentItem(dock.tree.topLevelItem(0))
    wait_for(lambda: dock._document is not None and dock._document.kind == "catalog")
    dock.download_all.click()
    wait_for(lambda: dock._job is None and asked and not dock.cancel.isVisible(), timeout=60)
    assert "could not be read" in asked[0]
    assert (tmp_path / "catalog.json").is_file()
    assert (tmp_path / "points" / "collection.json").is_file()
    assert (tmp_path / "points.pmtiles").is_file()
    assert (tmp_path / "points" / "styles" / "red.json").is_file()
    dock.disconnect_canvas()


def test_plugin_lifecycle(iface, catalog):
    settings = QgsSettings()
    settings.setValue(REGISTRY_URL_SETTING, f"{catalog['base']}/registry.json")
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    assert len(iface.menu) == 1
    assert Path(portolan_registry_qgis.__file__).with_name("icon.svg").is_file()
    plugin.action.setChecked(True)
    assert len(iface.docks) == 1
    wait_for(lambda: rows(plugin.dock.catalogs) == ["Test catalog"])
    settings.remove(REGISTRY_URL_SETTING)
    plugin.unload()
    assert iface.menu == []
    assert iface.toolbar == []
    assert iface.docks == []


def test_plugin_reconnects_saved_tiles(iface, catalog):
    plugin = portolan_registry_qgis.classFactory(iface)
    plugin.initGui()
    # A layer as a saved project restores it: the old port no longer answers.
    stale = QgsVectorTileLayer("type=xyz&url=http://127.0.0.1:9/old/{z}/{x}/{y}.pbf", "saved")
    stale.setCustomProperty(PMTILES_PROPERTY, f"{catalog['base']}/points.pmtiles")
    QgsProject.instance().addMapLayer(stale)
    wait_for(lambda: plugin.server.serves(stale.source()))
    assert stale.isValid()
    plugin.unload()
