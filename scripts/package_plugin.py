#!/usr/bin/env python3
"""Build a QGIS-installable plugin ZIP."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_NAME = "portolan_registry_qgis"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def build_archive(output: Path) -> Path:
    """Create a plugin archive with the directory layout that QGIS requires."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="portolan-registry-qgis-") as temporary:
        plugin_root = Path(temporary) / PLUGIN_NAME
        shutil.copytree(
            PROJECT_ROOT / PLUGIN_NAME,
            plugin_root,
            ignore=_ignore_generated,
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(plugin_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(plugin_root.parent))
    return output


def main() -> int:
    """Run the plugin packager command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "dist" / "portolan-registry-qgis.zip",
        help="Write the installable QGIS ZIP to this path.",
    )
    args = parser.parse_args()
    print(build_archive(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
