"""Plan a DuckDB read of a remote GeoParquet file.

The plan picks the geometry column, the CRS, the layer geometry type, and the
attribute types, and builds the SELECT statement. It uses the GeoParquet
``geo`` metadata when the file has it. A bbox filter goes through the
GeoParquet 1.1 ``covering`` column when the file declares one, so DuckDB skips
row groups through Parquet statistics instead of scanning the whole file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["bool", "int", "double", "string", "date", "datetime"]

_INT_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
)
_DOUBLE_TYPES = ("FLOAT", "DOUBLE", "REAL")
_GEOMETRY_NAMES = ("geometry", "geom", "wkb_geometry", "the_geom")
_MULTI = {
    "Point": "MultiPoint",
    "LineString": "MultiLineString",
    "Polygon": "MultiPolygon",
}
DEFAULT_CRS = "OGC:CRS84"


@dataclass(frozen=True)
class Column:
    """One attribute column and the QGIS field kind it maps to."""

    name: str
    duckdb_type: str
    kind: FieldKind


@dataclass(frozen=True)
class Covering:
    """The struct column and field names of a GeoParquet bbox covering."""

    xmin: tuple[str, ...]
    ymin: tuple[str, ...]
    xmax: tuple[str, ...]
    ymax: tuple[str, ...]


@dataclass(frozen=True)
class ReadPlan:
    """Everything needed to query a GeoParquet file and build a layer."""

    geometry: str
    geometry_is_wkb_blob: bool
    crs: str
    geometry_type: str
    columns: tuple[Column, ...]
    covering: Covering | None


class GeoParquetError(ValueError):
    """The file has no geometry column the plugin can read."""


def field_kind(duckdb_type: str) -> FieldKind:
    """Map a DuckDB column type to the QGIS field kind that holds it."""
    upper = duckdb_type.upper()
    if upper == "BOOLEAN":
        return "bool"
    if upper in _INT_TYPES:
        return "int"
    if upper in _DOUBLE_TYPES or upper.startswith("DECIMAL"):
        return "double"
    if upper == "DATE":
        return "date"
    if upper.startswith("TIMESTAMP"):
        return "datetime"
    return "string"


def quote(name: str) -> str:
    """Quote a SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _geo_metadata(raw: str | bytes | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _crs_input(column: dict[str, object]) -> str:
    """Return a string ``QgsCoordinateReferenceSystem.createFromUserInput`` reads.

    GeoParquet stores the CRS as PROJJSON. A missing ``crs`` key means
    OGC:CRS84. An ``id`` names the CRS directly. Anything else goes to PROJ as
    the PROJJSON text, which PROJ reads.
    """
    if "crs" not in column:
        return DEFAULT_CRS
    crs = column["crs"]
    if crs is None:
        return ""
    if isinstance(crs, str):
        return crs
    if isinstance(crs, dict):
        ident = crs.get("id")
        if isinstance(ident, dict) and "authority" in ident and "code" in ident:
            return f"{ident['authority']}:{ident['code']}"
        return json.dumps(crs)
    return DEFAULT_CRS


def layer_geometry_type(types: list[str]) -> str:
    """Return one QGIS memory-layer geometry type for the GeoParquet types.

    A layer holds one type, so single and multi parts of the same family
    promote to the multi type. Mixed families, or no declared types, return
    ``Unknown``. The caller then takes the type from the first feature.
    """
    has_z = any(name.endswith((" Z", " ZM")) for name in types)
    names = {name.split(" ")[0] for name in types}
    if len(names) == 1:
        family = names.pop()
    else:
        families = {_MULTI.get(name, name) for name in names}
        if len(families) != 1:
            return "Unknown"
        family = families.pop()
    return f"{family}Z" if has_z else family


def _covering(column: dict[str, object]) -> Covering | None:
    covering = column.get("covering")
    bbox = covering.get("bbox") if isinstance(covering, dict) else None
    if not isinstance(bbox, dict):
        return None
    parts = {}
    for key in ("xmin", "ymin", "xmax", "ymax"):
        path = bbox.get(key)
        if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
            return None
        parts[key] = tuple(path)
    return Covering(**parts)


def _implicit_covering(schema: list[tuple[str, str]]) -> Covering | None:
    """Find a ``bbox`` struct column that a file carries without covering metadata.

    GeoParquet 1.1 names this layout in its covering example, and writers
    such as DuckDB emit the column without the metadata that points to it.
    """
    for name, duckdb_type in schema:
        upper = duckdb_type.upper().replace(" ", "")
        if (
            name == "bbox"
            and upper.startswith("STRUCT(")
            and all(f"{key.upper()}" in upper for key in ("XMIN", "YMIN", "XMAX", "YMAX"))
        ):
            return Covering(("bbox", "xmin"), ("bbox", "ymin"), ("bbox", "xmax"), ("bbox", "ymax"))
    return None


def _pick_geometry(
    schema: list[tuple[str, str]], geo: dict[str, object]
) -> tuple[str, dict[str, object]]:
    columns = geo.get("columns")
    columns = columns if isinstance(columns, dict) else {}
    primary = geo.get("primary_column")
    names = [name for name, _ in schema]
    if isinstance(primary, str) and primary in names:
        info = columns.get(primary)
        return primary, info if isinstance(info, dict) else {}
    for name, duckdb_type in schema:
        if duckdb_type.upper().startswith("GEOMETRY"):
            info = columns.get(name)
            return name, info if isinstance(info, dict) else {}
    for name, duckdb_type in schema:
        if name.lower() in _GEOMETRY_NAMES and duckdb_type.upper() == "BLOB":
            info = columns.get(name)
            return name, info if isinstance(info, dict) else {}
    raise GeoParquetError("The file has no geometry column")


def plan_read(schema: list[tuple[str, str]], geo_json: str | bytes | None) -> ReadPlan:
    """Plan a read from the file's DuckDB schema and its ``geo`` metadata.

    Args:
        schema: ``(name, type)`` pairs from ``DESCRIBE``.
        geo_json: The value of the ``geo`` key in the Parquet metadata, or
            None when the file has none.

    Raises:
        GeoParquetError: No column holds geometry.
    """
    geo = _geo_metadata(geo_json)
    geometry, info = _pick_geometry(schema, geo)
    types = info.get("geometry_types")
    covering = _covering(info) or _implicit_covering(schema)
    skip = {geometry}
    if covering is not None:
        skip.add(covering.xmin[0])
    columns = tuple(
        Column(name, duckdb_type, field_kind(duckdb_type))
        for name, duckdb_type in schema
        if name not in skip
    )
    geometry_type = dict(schema)[geometry]
    return ReadPlan(
        geometry=geometry,
        geometry_is_wkb_blob=geometry_type.upper() == "BLOB",
        crs=_crs_input(info),
        geometry_type=layer_geometry_type(
            [t for t in types if isinstance(t, str)] if isinstance(types, list) else []
        ),
        columns=columns,
        covering=covering,
    )


def _path(parts: tuple[str, ...]) -> str:
    return ".".join(quote(part) for part in parts)


def build_select(
    plan: ReadPlan,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int | None = None,
) -> tuple[str, list[object]]:
    """Build the SELECT for ``plan``, with the URL as the first parameter.

    Args:
        plan: The read plan.
        bbox: Keep only features that intersect this box, in the file's CRS.
        limit: Return at most this many rows.

    Returns:
        The SQL and its parameters after the URL.
    """
    geometry = quote(plan.geometry)
    shape = f"ST_GeomFromWKB({geometry})" if plan.geometry_is_wkb_blob else geometry
    selected = [f"ST_AsWKB({shape}) AS __wkb"]
    for column in plan.columns:
        name = quote(column.name)
        selected.append(f"CAST({name} AS VARCHAR)" if column.kind == "string" else name)
    where = [f"{geometry} IS NOT NULL"]
    params: list[object] = []
    if bbox is not None:
        west, south, east, north = bbox
        if plan.covering is not None:
            c = plan.covering
            where.append(
                f"{_path(c.xmin)} <= ? AND {_path(c.xmax)} >= ? "
                f"AND {_path(c.ymin)} <= ? AND {_path(c.ymax)} >= ?"
            )
            params += [east, west, north, south]
        where.append(f"ST_Intersects({shape}, ST_MakeEnvelope(?, ?, ?, ?))")
        params += [west, south, east, north]
    sql = f"SELECT {', '.join(selected)} FROM read_parquet(?) WHERE {' AND '.join(where)}"  # noqa: S608  # nosec B608 - identifiers are quoted, values are parameters
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql, params
