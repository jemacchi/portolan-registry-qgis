"""Format values for display."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(size: int | None) -> str:
    """Return ``size`` in bytes as a short decimal string, or "" when unknown."""
    if size is None or size < 0:
        return ""
    value = float(size)
    for unit in _UNITS:
        if value < 1000 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return ""  # pragma: no cover - the loop always returns
