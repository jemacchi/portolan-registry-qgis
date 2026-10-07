"""Load GeoParquet into a QGIS memory layer through DuckDB.

``prepare`` runs off the main thread. It plans the query, reads the rows, and
builds the features. ``build`` runs on the main thread and puts them in a
memory layer. A read keeps at most ``limit`` features, and the caller can
narrow it to a box, because a remote file can hold tens of millions of rows.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QMetaType, QVariant

from portolan_registry_qgis.core import parquet_query

if TYPE_CHECKING:
    from collections.abc import Callable

    from qgis.core import QgsCoordinateTransformContext

    from portolan_registry_qgis.core.geoparquet import FieldKind, ReadPlan

DEFAULT_LIMIT = 1_000_000
SOURCE_PROPERTY = "portolan/parquet_url"
_QMETATYPE_FIELDS = Qgis.QGIS_VERSION_INT >= 33800


@dataclass
class Prepared:
    """Features read from a GeoParquet file, ready to become a layer."""

    url: str
    crs: QgsCoordinateReferenceSystem
    geometry_type: str
    fields: QgsFields
    features: list[QgsFeature]
    truncated: bool


def _field_type(kind: FieldKind) -> Any:
    if _QMETATYPE_FIELDS:
        meta = QMetaType.Type
        return {
            "bool": meta.Bool,
            "int": meta.LongLong,
            "double": meta.Double,
            "date": meta.QDate,
            "datetime": meta.QDateTime,
        }.get(kind, meta.QString)
    return {
        "bool": QVariant.Bool,
        "int": QVariant.LongLong,
        "double": QVariant.Double,
        "date": QVariant.Date,
        "datetime": QVariant.DateTime,
    }.get(kind, QVariant.String)


def fields_for(plan: ReadPlan) -> QgsFields:
    """Return the QGIS fields for a plan's attribute columns."""
    fields = QgsFields()
    for column in plan.columns:
        fields.append(QgsField(column.name, _field_type(column.kind)))
    return fields


def crs_for(plan: ReadPlan) -> QgsCoordinateReferenceSystem:
    """Return the plan's CRS, or WGS84 when the file names none QGIS can read."""
    crs = QgsCoordinateReferenceSystem()
    if plan.crs and crs.createFromUserInput(plan.crs) and crs.isValid():
        return crs
    return QgsCoordinateReferenceSystem("EPSG:4326")


def _value(value: Any) -> Any:
    # A Decimal has no QVariant form. Dates and datetimes convert on their own.
    return float(value) if isinstance(value, decimal.Decimal) else value


def extension_directory() -> str:
    """Return a writable folder for DuckDB extensions in the QGIS profile."""
    folder = Path(QgsApplication.qgisSettingsDirPath()) / "portolan_registry" / "duckdb"
    folder.mkdir(parents=True, exist_ok=True)
    return str(folder)


def file_bbox(
    extent: QgsRectangle | None,
    extent_crs: QgsCoordinateReferenceSystem | None,
    file_crs: QgsCoordinateReferenceSystem,
    context: QgsCoordinateTransformContext,
) -> tuple[float, float, float, float] | None:
    """Transform the map extent into the file's CRS."""
    if extent is None or extent_crs is None:
        return None
    rect = QgsCoordinateTransform(extent_crs, file_crs, context).transformBoundingBox(extent)
    return (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())


def prepare(
    url: str,
    context: QgsCoordinateTransformContext,
    extent: QgsRectangle | None = None,
    extent_crs: QgsCoordinateReferenceSystem | None = None,
    limit: int = DEFAULT_LIMIT,
    extension_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
) -> Prepared:
    """Read a GeoParquet file into features. Safe off the main thread.

    Args:
        url: The file's URL or local path.
        context: The project's transform context, for the extent.
        extent: Keep only features that intersect this rectangle.
        extent_crs: The CRS of ``extent``.
        limit: Keep at most this many features.
        extension_dir: Where DuckDB installs extensions if its default
            folder is read-only.
        cancelled: Polled between batches.
        progress: Called with the number of features read so far.

    Raises:
        parquet_query.DuckDBMissingError: DuckDB is absent or too old.
        GeoParquetError: The file has no geometry column.
    """
    con = parquet_query.connection(extension_dir)
    plan = parquet_query.read_plan(con, url)
    crs = crs_for(plan)
    bbox = file_bbox(extent, extent_crs, crs, context)
    fields = fields_for(plan)
    features: list[QgsFeature] = []
    for rows in parquet_query.iter_rows(con, url, plan, bbox, limit + 1, cancelled=cancelled):
        for row in rows:
            feature = QgsFeature(fields)
            if row[0] is not None:
                geometry = QgsGeometry()
                geometry.fromWkb(bytes(row[0]))
                feature.setGeometry(geometry)
            feature.setAttributes([_value(v) for v in row[1:]])
            features.append(feature)
        if progress is not None:
            progress(len(features))
    truncated = len(features) > limit
    return Prepared(url, crs, plan.geometry_type, fields, features[:limit], truncated)


def _layer_type(prepared: Prepared) -> str:
    if prepared.geometry_type != "Unknown":
        return prepared.geometry_type
    for feature in prepared.features:
        geometry = feature.geometry()
        if not geometry.isNull():
            return QgsWkbTypes.displayString(QgsWkbTypes.multiType(geometry.wkbType()))
    return "Point"


def build(prepared: Prepared, name: str) -> tuple[QgsVectorLayer, int]:
    """Create the memory layer. Call on the main thread.

    Returns:
        The layer, and how many features it refused because their geometry
        type did not fit the layer.
    """
    layer = QgsVectorLayer(f"{_layer_type(prepared)}?index=yes", name, "memory")
    layer.setCrs(prepared.crs)
    provider = layer.dataProvider()
    provider.addAttributes(prepared.fields.toList())
    layer.updateFields()
    expected = layer.wkbType()
    family = QgsWkbTypes.geometryType(expected)
    multi = QgsWkbTypes.isMultiType(expected)
    accepted = []
    refused = 0
    for feature in prepared.features:
        geometry = feature.geometry()
        if not geometry.isNull():
            if QgsWkbTypes.geometryType(geometry.wkbType()) != family:
                # A layer holds one geometry family. A line in a point layer
                # has nowhere to go.
                refused += 1
                continue
            if multi and not geometry.isMultipart():
                geometry.convertToMultiType()
                feature.setGeometry(geometry)
        accepted.append(feature)
    provider.addFeatures(accepted)
    layer.updateExtents()
    layer.setCustomProperty(SOURCE_PROPERTY, prepared.url)
    return layer, refused
