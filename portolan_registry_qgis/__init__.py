"""Portolan Registry: browse, load, and download Portolan catalogs in QGIS."""

from __future__ import annotations

from typing import Any


def classFactory(iface: Any) -> Any:  # noqa: N802 - QGIS calls this name
    """Return the plugin instance. QGIS calls this when it loads the plugin."""
    from portolan_registry_qgis.plugin import PortolanRegistryPlugin

    return PortolanRegistryPlugin(iface)
