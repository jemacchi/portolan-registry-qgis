"""Query GeoParquet over HTTP with DuckDB.

DuckDB is an external dependency. QGIS does not ship it, so every function
imports it lazily and the plugin loads without it. ``duckdb_status`` reports
whether a usable version is present, and the GUI tells the user how to install
it when it is not.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING, Any

from portolan_registry_qgis.core.geoparquet import ReadPlan, build_select, plan_read

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

MINIMUM_DUCKDB = (1, 5, 0)
MEMORY_LIMIT = "4GB"
_EXTENSIONS = ("httpfs", "spatial")
_lock = threading.Lock()
_connection: Any = None


class DuckDBMissingError(ImportError):
    """DuckDB is absent or older than ``MINIMUM_DUCKDB``."""


def _version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def duckdb_status() -> tuple[bool, str | None]:
    """Return whether DuckDB is usable, and its version when it is installed."""
    try:
        import duckdb
    except ImportError:
        return False, None
    version = str(duckdb.__version__)
    return _version(version) >= MINIMUM_DUCKDB, version


def is_flatpak() -> bool:
    """Return whether this Python runs inside a Flatpak sandbox."""
    return bool(os.environ.get("FLATPAK_ID")) or sys.prefix.startswith("/app")


def install_command(upgrade: bool) -> str:
    """Return the shell command that installs DuckDB for this QGIS."""
    minimum = ".".join(str(part) for part in MINIMUM_DUCKDB)
    flag = "--upgrade " if upgrade else ""
    if is_flatpak():
        return (
            "flatpak run --command=python3 org.qgis.qgis \\\n"
            f"  -m pip install --user {flag}'duckdb>={minimum}'"
        )
    return f"{sys.executable} -m pip install --user {flag}'duckdb>={minimum}'"


def connection(extension_dir: str | None = None) -> Any:
    """Return the shared DuckDB connection with httpfs and spatial loaded.

    Args:
        extension_dir: Where DuckDB installs extensions when its default
            directory is not writable, as in some sandboxes.

    Raises:
        DuckDBMissingError: DuckDB is absent or too old.
    """
    global _connection  # noqa: PLW0603 - one connection per QGIS session
    ok, version = duckdb_status()
    if not ok:
        raise DuckDBMissingError(f"DuckDB {version or 'is not installed'}")
    import duckdb

    with _lock:
        if _connection is None:
            con = duckdb.connect()
            try:
                _load_extensions(con)
            except duckdb.IOException:
                if extension_dir is None:
                    raise
                con.execute("SET extension_directory = ?", [extension_dir])
                _load_extensions(con)
            con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
            _connection = con
        return _connection


def _load_extensions(con: Any) -> None:
    for name in _EXTENSIONS:
        con.execute(f"INSTALL {name}")
        con.execute(f"LOAD {name}")


def close() -> None:
    """Close the shared connection. The plugin calls this on unload."""
    global _connection  # noqa: PLW0603 - see connection()
    with _lock:
        if _connection is not None:
            _connection.close()
            _connection = None


def read_plan(con: Any, url: str) -> ReadPlan:
    """Read a file's schema and GeoParquet metadata and plan the query."""
    cursor = con.cursor()
    try:
        schema = [
            (str(row[0]), str(row[1]))
            for row in cursor.execute("DESCRIBE SELECT * FROM read_parquet(?)", [url]).fetchall()
        ]
        geo = cursor.execute(
            "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'geo' LIMIT 1", [url]
        ).fetchall()
    finally:
        cursor.close()
    return plan_read(schema, geo[0][0] if geo else None)


def iter_rows(
    con: Any,
    url: str,
    plan: ReadPlan,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int | None = None,
    batch: int = 10_000,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[list[tuple[Any, ...]]]:
    """Yield batches of rows. Each row is ``(wkb, *attribute values)``.

    The rows hold each column of ``plan.columns`` in order. A cancelled read
    stops at the next batch and interrupts the query.
    """
    sql, params = build_select(plan, bbox, limit)
    cursor = con.cursor()
    try:
        cursor.execute(sql, [url, *params])
        while True:
            if cancelled is not None and cancelled():
                cursor.interrupt()
                return
            rows = cursor.fetchmany(batch)
            if not rows:
                return
            yield rows
    finally:
        cursor.close()
