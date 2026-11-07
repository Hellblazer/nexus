# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-hbnml: ``retired_queue_table`` reads the engine's ``GET /v1/status`` fields about the retired
``aspect_extraction_queue`` table. Only ``gone`` and ``empty`` are clean; everything else, including a status
that could not be read, must never be rendered as a zero."""
from __future__ import annotations

import pytest

from nexus.db.t2.http_aspect_queue import RetiredQueueTable, retired_queue_table


@pytest.mark.parametrize(
    ("status", "state", "rows", "clean"),
    [
        (None, "unreported", None, False),
        ({}, "unreported", None, False),
        ({"aspect_queue_table_present": True}, "unreported", None, False),
        ({"aspect_queue_table_present": "yes", "aspect_queue_rows_retained": 0}, "unreported", None, False),
        ({"aspect_queue_table_present": True, "aspect_queue_rows_retained": True}, "unreported", None, False),
        ({"aspect_queue_table_present": False, "aspect_queue_rows_retained": 0}, "gone", 0, True),
        ({"aspect_queue_table_present": True, "aspect_queue_rows_retained": 0}, "empty", 0, True),
        ({"aspect_queue_table_present": True, "aspect_queue_rows_retained": -1}, "uncounted", None, False),
        ({"aspect_queue_table_present": True, "aspect_queue_rows_retained": 5}, "retained", 5, False),
    ],
)
def test_state_rows_and_cleanliness(status: object, state: str, rows: int | None, clean: bool) -> None:
    t = retired_queue_table(status)
    assert (t.state, t.rows, t.clean) == (state, rows, clean)


def test_reasons_are_sorted_and_only_read_when_rows_remain() -> None:
    t = retired_queue_table({
        "aspect_queue_table_present": True, "aspect_queue_rows_retained": 3,
        "aspect_queue_rows_retained_reasons": {"unknown_status": 1, "body_over_cap": 2},
    })
    assert t == RetiredQueueTable("retained", 3, (("body_over_cap", 2), ("unknown_status", 1)))
