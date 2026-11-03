# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seam that seeds legacy aspect-queue TABLE rows (nexus-bpikw step A).

``NX_ASPECT_QUEUE=table`` is refused (``AspectQueueTableModeRetiredError``), so a
test can no longer ask for the table producer through the environment. The
worker's table drain, ``requeue-failed``'s table reader, the doctor's census and
the upgrade rung still read rows the old producer wrote, and a test of those needs
such rows. ``HttpAspectQueue.enqueue`` and ``enqueue_many`` still carry the table
branch (taken only when ``aspect_queue_mode()`` returns ``"table"``, which no
environment value yields any more); this module makes them take it by wrapping that
one function in the module that calls it.

The wrapper turns the UNSET variable into the table producer and leaves every
explicit value to the real rule: a test that then sets ``NX_ASPECT_QUEUE=tuples``
gets the tuple producer, and one that sets ``table`` gets the refusal.

Step B deletes the table, the branch and this module together.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import nexus.db.t2.http_aspect_queue as _mod

_ENV = "NX_ASPECT_QUEUE"
_REAL = _mod.aspect_queue_mode  # captured at import, before any test patches it


def _legacy_mode() -> str:
    if not os.environ.get(_ENV, "").strip():
        return "table"
    return _REAL()


def use_table_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ``NX_ASPECT_QUEUE`` means the table producer for this whole test."""
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(_mod, "aspect_queue_mode", _legacy_mode)


@contextmanager
def table_producer() -> Iterator[None]:
    """The same, inside the block only (a test that also needs the real rule, or the
    tuple producer, outside it)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv(_ENV, raising=False)
        mp.setattr(_mod, "aspect_queue_mode", _legacy_mode)
        yield
