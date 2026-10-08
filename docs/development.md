# Development

The plugin has two test suites.

- `tests/core` covers the STAC, registry, PMTiles, GeoParquet, and download logic in `portolan_registry_qgis/core`. That package imports neither QGIS nor GDAL, so the suite runs in the uv environment.
- `tests/in_qgis` needs PyQGIS. It builds a small catalog with GDAL and DuckDB and serves it on loopback. The tests then exercise the network code, the layers, the downloads, and the dock.

```bash
uv sync
uv run pytest
```

Build an installable ZIP with the directory layout that QGIS requires:

```bash
uv run python scripts/package_plugin.py \
  --output dist/portolan-registry-qgis.zip
unzip -t dist/portolan-registry-qgis.zip
```

The archive has one root directory named `portolan_registry_qgis`.

For the QGIS suite, make a virtual environment on the Python that has PyQGIS:

```bash
uv venv --python /usr/bin/python3 --system-site-packages .venv-qgis
VIRTUAL_ENV=.venv-qgis uv pip install pytest pytest-timeout duckdb
QT_QPA_PLATFORM=offscreen .venv-qgis/bin/python -m pytest tests/in_qgis
```

Add `-m network` to run the live checks against the registry and two published catalogs.

CI runs `tests/in_qgis` in the official QGIS 3.44 and 4.2 Docker images, which cover Qt5 and Qt6.

## Release

1. Run `uv run cz bump`. It updates `pyproject.toml`, `metadata.txt`, and `CHANGELOG.md`, then tags the commit.
2. Push the commit and the tag, then publish a GitHub release from the tag.
3. `.github/workflows/release.yml` packages the plugin with `qgis-plugin-ci`, attaches the zip to the release, and uploads it to the official QGIS plugin repository.
