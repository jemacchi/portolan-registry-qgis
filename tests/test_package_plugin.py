"""QGIS plugin archive tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

from scripts.package_plugin import build_archive


def test_package_has_one_qgis_plugin_root(tmp_path: Path) -> None:
    output = tmp_path / "portolan-registry-qgis.zip"

    build_archive(output)

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert "portolan_registry_qgis/__init__.py" in names
    assert "portolan_registry_qgis/metadata.txt" in names
    assert {name.split("/", 1)[0] for name in names} == {"portolan_registry_qgis"}
    assert not any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names)


def test_release_workflow_publishes_installable_archive_as_release_asset() -> None:
    workflow = (Path(__file__).parent.parent / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )

    assert "python scripts/package_plugin.py" in workflow
    assert 'gh release upload "$TAG"' in workflow
    assert '"dist/portolan-registry-qgis-${VERSION}.zip"' in workflow
    assert "actions/upload-artifact@" not in workflow
