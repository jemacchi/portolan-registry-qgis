from __future__ import annotations

import pytest

from portolan_registry_qgis.core.format import human_size


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (None, ""),
        (-1, ""),
        (0, "0 B"),
        (999, "999 B"),
        (1000, "1.0 KB"),
        (1393694428, "1.4 GB"),
        (10**18, "1000.0 PB"),
    ],
)
def test_human_size(size, text):
    assert human_size(size) == text
