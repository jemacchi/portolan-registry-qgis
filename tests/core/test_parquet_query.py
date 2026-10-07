from __future__ import annotations

import datetime
import decimal
import json

import pytest

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core.geoparquet import plan_read

pytest.importorskip("duckdb")


@pytest.fixture(scope="module")
def con():
    connection = parquet_query.connection()
    yield connection
    parquet_query.close()


@pytest.fixture(scope="module")
def points(tmp_path_factory, con):
    path = tmp_path_factory.mktemp("parquet") / "points.parquet"
    con.execute(
        f"""COPY (SELECT i AS id, 'n' || i AS name, i * 1.5 AS val,
            DATE '2026-01-01' + i::INT AS day,
            ST_Point(11 + i * 0.01, 44 + i * 0.005) AS geometry,
            {{'xmin': 11 + i * 0.01, 'ymin': 44 + i * 0.005,
              'xmax': 11 + i * 0.01, 'ymax': 44 + i * 0.005}} AS bbox
            FROM range(200) t(i))
        TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 20)"""
    )
    return str(path)


def test_status_and_install_command(monkeypatch):
    ok, version = parquet_query.duckdb_status()
    assert ok
    assert version
    monkeypatch.setenv("FLATPAK_ID", "org.qgis.qgis")
    assert parquet_query.install_command(upgrade=False).startswith("flatpak run")
    assert "--upgrade" in parquet_query.install_command(upgrade=True)
    monkeypatch.delenv("FLATPAK_ID")
    monkeypatch.setattr(parquet_query.sys, "prefix", "/usr")
    assert "pip install --user 'duckdb>=1.5.0'" in parquet_query.install_command(upgrade=False)


def test_missing_duckdb_is_reported(monkeypatch):
    monkeypatch.setattr(parquet_query, "duckdb_status", lambda: (False, None))
    monkeypatch.setattr(parquet_query, "_connection", None)
    with pytest.raises(parquet_query.DuckDBMissingError):
        parquet_query.connection()


def test_old_versions_are_rejected(monkeypatch):
    import duckdb

    monkeypatch.setattr(duckdb, "__version__", "1.4.9")
    assert parquet_query.duckdb_status() == (False, "1.4.9")
    monkeypatch.setattr(duckdb, "__version__", "1.5.0-dev123")
    assert parquet_query.duckdb_status() == (True, "1.5.0-dev123")


def test_read_all_rows(con, points):
    plan = parquet_query.read_plan(con, points)
    assert plan.geometry == "geometry"
    assert plan.geometry_type == "Point"
    rows = [row for batch in parquet_query.iter_rows(con, points, plan, batch=50) for row in batch]
    assert len(rows) == 200
    wkb, ident, name, val, day = rows[0][:5]
    assert isinstance(wkb, bytes)
    assert wkb[0] in (0, 1)
    assert (ident, name) == (0, "n0")
    assert isinstance(val, decimal.Decimal)
    assert day == datetime.date(2026, 1, 1)


def test_bbox_and_limit(con, points):
    plan = parquet_query.read_plan(con, points)
    box = (11.095, 44.0, 11.305, 45.0)
    rows = [r for b in parquet_query.iter_rows(con, points, plan, box) for r in b]
    assert sorted(r[1] for r in rows) == list(range(10, 31))
    limited = [r for b in parquet_query.iter_rows(con, points, plan, box, limit=5) for r in b]
    assert len(limited) == 5


def test_covering_filter_gives_the_same_rows(con, points):
    schema = [(r[0], r[1]) for r in con.execute(f"DESCRIBE SELECT * FROM '{points}'").fetchall()]
    covering = {
        "columns": {
            "geometry": {
                "covering": {"bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}}
            }
        }
    }
    plan = plan_read(schema, json.dumps(covering))
    assert plan.covering is not None
    assert "bbox" not in [c.name for c in plan.columns]
    box = (11.095, 44.0, 11.305, 45.0)
    rows = [r for b in parquet_query.iter_rows(con, points, plan, box) for r in b]
    assert sorted(r[1] for r in rows) == list(range(10, 31))


def test_cancel_stops_the_read(con, points):
    plan = parquet_query.read_plan(con, points)
    batches = list(parquet_query.iter_rows(con, points, plan, batch=10, cancelled=lambda: True))
    assert batches == []


def test_close_is_idempotent():
    parquet_query.close()
    parquet_query.close()
