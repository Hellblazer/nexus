# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-inventory loops skip a retired collection the engine refuses, and say so (RDR-229 R7, nexus-p2m1x.25).

The engine answers 409 ``collection_superseded`` (reason ``retired_no_successor``) to a read that names a retired
collection by label when no live successor exists (``nexus.collection_errors.RetiredNoSuccessorError``). Such a row can
still hold chunks, so ``list_collections()`` still lists it and a loop that reads each listed collection would meet the
refusal on it.

The listing cannot tell the loop in advance. Its ``superseded_by`` is label TEXT: for a tombstone whose successor was
deleted it keeps the deleted successor's label, which another live collection may since have taken, and the engine decides
whether a successor is live by walking ``superseded_by_id``, which the listing does not carry. A tombstone WITH a live
successor is still served. So a loop never pre-filters on the row; it reads, and a collection whose read raises
:class:`RetiredNoSuccessorError` is recorded here (:meth:`RetiredSkips.add`), logged, left out of its results, and named
in its output.

A leaf module: it imports nothing from the vector client, so any inventory loop can use it without a cycle.
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger(__name__)


class RetiredSkips:
    """The collections one loop left out because the engine refused to read them as retired with no live successor.

    ``add`` logs each (structlog warning ``retired_collection_skipped`` with the *site*) so a skip is never silent;
    ``names`` is what the loop reports to the person."""

    def __init__(self, site: str) -> None:
        self.site = site
        self._names: list[str] = []

    def add(self, name: str) -> None:
        if name not in self._names:
            self._names.append(name)
            _log.warning("retired_collection_skipped", site=self.site, skipped=1, collections=[name])

    def refused(self, name: str, exc: BaseException) -> bool:
        """Whether *exc* is the engine's refusal of a retired collection with no live successor; if so, *name* is
        recorded (and logged) as skipped."""
        from nexus.collection_errors import RetiredNoSuccessorError  # noqa: PLC0415 — deferred: collection_errors imports db.vector_errors

        if isinstance(exc, RetiredNoSuccessorError):
            self.add(name)
            return True
        return False

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def __bool__(self) -> bool:
        return bool(self._names)
