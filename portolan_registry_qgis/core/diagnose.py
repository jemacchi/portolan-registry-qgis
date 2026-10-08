"""Explain why QGIS could not open an asset.

QGIS reports a failed GDAL open as ``Cannot open GDAL dataset <uri>:`` followed
by GDAL's own message. That message says what is wrong, so the panel shows it
instead of a bare failure.
"""

from __future__ import annotations

import re

# The URI holds colons of its own, so it ends at the first space.
_PREFIX = re.compile(r"^Cannot open GDAL dataset \S+:\s*")
_PROVIDER_INVALID = re.compile(r"^Provider is not valid\b")
# GDAL words a codec it was built without in either of these two ways.
_MISSING_CODEC = re.compile(
    r"missing codec (\w+)|(\w+) compression support is not configured", re.IGNORECASE
)


def gdal_reason(detail: str) -> str:
    """Return GDAL's message from a QGIS layer error, or "" when it has none."""
    lines = []
    for raw in detail.splitlines():
        line = _PREFIX.sub("", raw.strip())
        if line and not _PROVIDER_INVALID.match(line):
            lines.append(line)
    return " ".join(lines)


def open_failure(href: str, detail: str, flatpak: bool) -> str:
    """Return a message that says why ``href`` did not open and what to do.

    Args:
        href: The asset's URL.
        detail: The text of the layer's error, as QGIS reports it.
        flatpak: Whether QGIS runs as a Flatpak.
    """
    reason = gdal_reason(detail)
    codec = _MISSING_CODEC.search(reason)
    if codec is None:
        return f"QGIS could not open {href}: {reason}" if reason else f"QGIS could not open {href}"
    name = (codec.group(1) or codec.group(2)).upper()
    message = (
        f"QGIS could not open {href}. The file uses {name} compression, "
        f"and the GDAL in this QGIS cannot decode {name}."
    )
    if flatpak:
        return (
            f"{message} The QGIS Flatpak builds GDAL without {name}. Open the file in a "
            "QGIS from your Linux distribution, conda-forge, or the QGIS installers."
        )
    return f"{message} Open it in a QGIS whose GDAL includes {name}."
