# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.8 (nexus-p2m1x.13): a WRITE whose T3 existence probe fails refuses.

``t3_collection_name`` probes ``t3.collection_exists`` to find a collection that already holds the
subject. When that probe fails, a read keeps falling back to the rendered name (looking at data
needs no probe), but a write must not guess the rendered name: the subject's data may sit under
another name, and writing to the guess would split it. The write raises
:class:`CollectionStoreUnreadableError`, a sibling of :class:`CollectionRegistryUnreadableError`
whose message names the store probe.
"""
from __future__ import annotations

import pytest

import nexus.corpus as corpus
from nexus.corpus import CollectionRegistryUnreadableError, t3_collection_name

pytestmark = pytest.mark.usefixtures("cloud_mode")

_RENDERED = "knowledge__field-notes__voyage-context-3__v1"


class _Probe:
    """A store whose ``collection_exists`` answers by name and raises for *failing* names."""

    def __init__(self, *, present: set[str] = frozenset(), failing: set[str] | None = None) -> None:
        self.present = set(present)
        self.failing = failing  # None: every probe fails

    def collection_exists(self, name: str) -> bool:
        if self.failing is None or name in self.failing:
            raise ConnectionError(f"the store is down while probing {name}")
        return name in self.present


def test_a_write_refuses_when_the_existence_probe_fails() -> None:
    with pytest.raises(corpus.CollectionStoreUnreadableError, match="vector store") as exc:
        t3_collection_name("field-notes", t3=_Probe(), for_write=True)
    assert "field-notes" in exc.value.format_message()
    assert isinstance(exc.value, CollectionRegistryUnreadableError)
    assert isinstance(exc.value.__cause__, ConnectionError)


def test_a_read_falls_back_to_the_rendered_name_when_the_probe_fails() -> None:
    assert t3_collection_name("field-notes", t3=_Probe()) == _RENDERED


def test_a_write_refuses_when_a_later_probe_fails() -> None:
    """The conformant name holds nothing; the probe of the legacy name then fails."""
    t3 = _Probe(failing={"knowledge__field-notes"})
    with pytest.raises(corpus.CollectionStoreUnreadableError):
        t3_collection_name("knowledge__field-notes", t3=t3, for_write=True)
    assert t3_collection_name("knowledge__field-notes", t3=_Probe(failing={"knowledge__field-notes"})) == _RENDERED


def test_a_write_that_probes_cleanly_is_unchanged() -> None:
    """No failure, no change: the conformant name that exists, and the one that does not."""
    assert t3_collection_name("field-notes", t3=_Probe(present={_RENDERED}, failing=set()), for_write=True) == _RENDERED
    assert t3_collection_name("field-notes", t3=_Probe(failing=set()), for_write=True) == _RENDERED
