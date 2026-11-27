# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-inventory loops skip retired collections, and say so (RDR-229 R7, nexus-p2m1x.25).

The engine answers 409 ``collection_superseded`` to a read that names a retired collection by label when no live
successor exists (``nexus.collection_errors.RetiredNoSuccessorError``). A retired row can still hold chunks (a
``re-embed --keep-source`` source whose target was deleted), so ``list_collections()`` still lists it, and a loop that
reads each listed collection by name would meet that 409 on it. Such a loop skips the retired row up front, through
:func:`skip_retired_rows`, which logs what it left out so a skip is never silent.

A leaf module: no imports from the vector client, so any inventory loop can use it without a cycle.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: How many skipped names one log line carries; the count is always complete.
_LOGGED_NAMES = 20

#: A ``lifecycle_state`` that marks a retired collection (the engine's vocabulary has grown past the closed set
#: :data:`nexus.db.http_vector_client.LIFECYCLE_STATES` once before; the superseded marker below is the one RDR-229 sets).
RETIRED_LIFECYCLE_STATE = "retired"


def is_retired_collection_row(row: Any) -> bool:
    """Whether a ``list_collections()`` row names a retired collection: ``superseded_by`` is set (a rename's old name,
    or a source whose successor was deleted) or ``lifecycle_state`` says retired. A non-dict entry (a bare name) carries
    no state and is not retired; an engine that predates the keys omits them and the row reads as not retired."""
    if not isinstance(row, dict):
        return False
    return bool(row.get("superseded_by")) or row.get("lifecycle_state") == RETIRED_LIFECYCLE_STATE


def skip_retired_rows(rows: Iterable[Any], *, site: str) -> tuple[list[Any], tuple[str, ...]]:
    """*rows* without the retired ones, and the names left out.

    Logs ``retired_collection_skipped`` (structlog, warning) with the *site* and the count whenever it left any out, so a
    caller that also reports to the person can use the returned names for its own line."""
    kept: list[Any] = []
    skipped: list[str] = []
    for row in rows:
        if is_retired_collection_row(row):
            skipped.append(str(row.get("name", "")))
        else:
            kept.append(row)
    if skipped:
        _log.warning(
            "retired_collection_skipped", site=site, skipped=len(skipped), collections=skipped[:_LOGGED_NAMES],
        )
    return kept, tuple(skipped)
