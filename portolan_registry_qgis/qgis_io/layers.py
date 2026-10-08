"""Turn STAC assets and PMTiles links into QGIS layers.

PMTiles become native vector tile layers through the loopback tile server and
take the collection's MapLibre style. GeoJSON and FlatGeobuf open through OGR,
COGs through GDAL, and COPC through the point cloud provider, all over HTTP
range requests without a download. GeoParquet goes through DuckDB instead, in
``parquet_layer``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from qgis.core import (
    QgsDataSourceUri,
    QgsMapBoxGlStyleConversionContext,
    QgsMapBoxGlStyleConverter,
    QgsMapLayer,
    QgsPointCloudLayer,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsVectorTileLayer,
)

from portolan_registry_qgis.core.diagnose import open_failure
from portolan_registry_qgis.core.parquet_query import is_flatpak
from portolan_registry_qgis.qgis_io.tileserver import Archive, TileServer, open_archive

if TYPE_CHECKING:
    from collections.abc import Callable

    from portolan_registry_qgis.core.stac import Asset, Document

PMTILES_PROPERTY = "portolan/pmtiles_url"
STYLE_PROPERTY = "portolan/style_url"
_PMTILES_SCHEME = "pmtiles://"


class LayerError(RuntimeError):
    """QGIS could not open the asset as a layer."""


@dataclass
class Styled:
    """A style split per PMTiles source, ready for ``QgsMapBoxGlStyleConverter``."""

    by_source: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def pmtiles_sources(style: dict[str, Any], style_url: str) -> dict[str, str]:
    """Map each PMTiles source id in a MapLibre style to an absolute archive URL.

    A Portolan style names its archive as ``pmtiles://<path>``, with the path
    relative to the style file.
    """
    sources = style.get("sources")
    found: dict[str, str] = {}
    if not isinstance(sources, dict):
        return found
    for source_id, source in sources.items():
        if not isinstance(source, dict) or source.get("type") != "vector":
            continue
        url = source.get("url")
        if isinstance(url, str) and url.startswith(_PMTILES_SCHEME):
            found[str(source_id)] = urljoin(style_url, url[len(_PMTILES_SCHEME) :])
    return found


def split_style(style: dict[str, Any], style_url: str) -> Styled:
    """Split a MapLibre style into one style per PMTiles source.

    QGIS draws one archive per vector tile layer, so each archive gets the
    style layers that read from it. Background layers go with the first
    archive. Layers on any other source are dropped with a warning.
    """
    sources = pmtiles_sources(style, style_url)
    result = Styled()
    raw_layers = style.get("layers")
    layers = raw_layers if isinstance(raw_layers, list) else []
    first = next(iter(sources.values()), None)
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        source = layer.get("source")
        if layer.get("type") == "background" and first is not None:
            url: str | None = first
        else:
            url = sources.get(str(source)) if source is not None else None
        if url is None:
            result.warnings.append(
                f"Style layer {layer.get('id')!r} reads source {source!r}, "
                "which is not a PMTiles archive; skipped."
            )
            continue
        target = result.by_source.setdefault(url, {**style, "layers": []})
        target["layers"].append(layer)
    return result


def convert_style(
    style: dict[str, Any],
    sprite: tuple[Any, dict[str, Any]] | None = None,
) -> tuple[Any, Any, list[str]]:
    """Convert a MapLibre style to a QGIS vector tile renderer and labeling.

    Args:
        style: The style, already limited to one archive's layers.
        sprite: The sprite image (a QImage) and its JSON index, when the style
            names a sprite.

    Returns:
        The renderer, the labeling (None when the style has no labels), and
        the converter's warnings.
    """
    converter = QgsMapBoxGlStyleConverter()
    context = QgsMapBoxGlStyleConversionContext()
    if sprite is not None:
        context.setSprites(sprite[0], sprite[1])
    converter.convert(style, context)
    warnings = [*list(context.warnings()), converter.errorMessage()]
    return converter.renderer(), converter.labeling(), [w for w in warnings if w]


def vector_tile_layer(
    server: TileServer,
    archive: Archive,
    name: str,
) -> QgsVectorTileLayer:
    """Return a vector tile layer that reads ``archive`` through ``server``."""
    uri = QgsDataSourceUri()
    uri.setParam("type", "xyz")
    uri.setParam("url", server.register(archive))
    uri.setParam("zmin", str(archive.min_zoom))
    uri.setParam("zmax", str(archive.max_zoom))
    layer = QgsVectorTileLayer(bytes(uri.encodedUri()).decode(), name)
    if not layer.isValid():
        raise LayerError(f"QGIS could not open the tiles of {archive.url}")
    layer.setCustomProperty(PMTILES_PROPERTY, archive.url)
    return layer


@dataclass
class PreparedTiles:
    """PMTiles archives read and styles split, ready to become layers.

    ``prepare_pmtiles`` builds this off the main thread, because reading an
    archive header is a network request. ``build_pmtiles`` turns it into layers
    on the main thread.
    """

    parts: list[tuple[Archive, dict[str, Any] | None, str]]
    sprite: tuple[bytes, dict[str, Any]] | None
    style_url: str | None
    warnings: list[str]


def prepare_pmtiles(
    document: Document,
    fetch_bytes: Callable[[str], bytes],
    style_asset: Asset | None,
) -> PreparedTiles:
    """Read a collection's style and PMTiles archives.

    Args:
        document: The collection, for its PMTiles links and title.
        fetch_bytes: Fetches the style and its sprite.
        style_asset: The style to apply, or None for QGIS's default style.

    Raises:
        PmtilesError: An archive cannot be read.
    """
    warnings: list[str] = []
    targets: list[tuple[str, dict[str, Any] | None]] = []
    style: dict[str, Any] | None = None
    if style_asset is not None:
        try:
            style = parse_style(fetch_bytes(style_asset.href))
        except (OSError, ValueError) as error:
            warnings.append(f"Could not read the style {style_asset.href}: {error}")
    if style is not None and style_asset is not None:
        styled = split_style(style, style_asset.href)
        warnings.extend(styled.warnings)
        targets = list(styled.by_source.items())
    if not targets:
        targets = [(link.href, None) for link in document.pmtiles]
    sprite = None
    if style is not None and style_asset is not None:
        sprite = _sprite(style, style_asset.href, fetch_bytes, warnings)
    parts = []
    for url, part in targets:
        title = (part or {}).get("name") or document.title
        parts.append((open_archive(url), part, str(title)))
    return PreparedTiles(
        parts=parts,
        sprite=sprite,
        style_url=style_asset.href if style_asset else None,
        warnings=warnings,
    )


def build_pmtiles(
    server: TileServer,
    prepared: PreparedTiles,
    load_image: Callable[[bytes], Any],
) -> tuple[list[QgsMapLayer], list[str]]:
    """Create the styled vector tile layers. Call on the main thread."""
    warnings = list(prepared.warnings)
    sprite = None
    if prepared.sprite is not None:
        sprite = (load_image(prepared.sprite[0]), prepared.sprite[1])
    layers: list[QgsMapLayer] = []
    for archive, part, title in prepared.parts:
        layer = vector_tile_layer(server, archive, title)
        if part is not None:
            renderer, labeling, notes = convert_style(part, sprite)
            warnings.extend(notes)
            if renderer is not None:
                layer.setRenderer(renderer)
            if labeling is not None:
                layer.setLabeling(labeling)
            layer.setCustomProperty(STYLE_PROPERTY, prepared.style_url or "")
        layers.append(layer)
    return layers, warnings


def _sprite(
    style: dict[str, Any],
    style_url: str,
    fetch_bytes: Callable[[str], bytes],
    warnings: list[str],
) -> tuple[bytes, dict[str, Any]] | None:
    base = style.get("sprite")
    if not isinstance(base, str):
        return None
    root = urljoin(style_url, base)
    try:
        index = json.loads(fetch_bytes(f"{root}.json"))
        image = fetch_bytes(f"{root}.png")
    except (OSError, ValueError) as error:
        warnings.append(f"Could not load the style's sprite at {root}: {error}")
        return None
    return (image, index) if isinstance(index, dict) else None


def asset_layer(asset: Asset, name: str | None = None) -> QgsMapLayer:
    """Open a data asset as a layer, read in place over HTTP.

    Raises:
        LayerError: The asset has no supported format, or QGIS cannot open it.
    """
    title = name or asset.label
    path = f"/vsicurl/{asset.href}"
    layer: QgsMapLayer
    if asset.format in {"geojson", "flatgeobuf"}:
        layer = QgsVectorLayer(path, title, "ogr")
    elif asset.format == "cog":
        layer = QgsRasterLayer(path, title, "gdal")
    elif asset.format == "copc":
        layer = QgsPointCloudLayer(asset.href, title, "copc")
    else:
        raise LayerError(f"{asset.label} has no format QGIS can open in place")
    if not layer.isValid():
        raise LayerError(open_failure(asset.href, layer.error().summary(), is_flatpak()))
    return layer


def parse_style(raw: bytes) -> dict[str, Any]:
    """Parse a style file, rejecting anything but a MapLibre v8 style object."""
    style = json.loads(raw)
    if not isinstance(style, dict) or style.get("version") != 8:
        raise ValueError("Not a MapLibre GL style (version 8)")
    return style
