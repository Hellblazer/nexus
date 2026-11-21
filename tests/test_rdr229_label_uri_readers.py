# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 review I2 (nexus-p2m1x.26): a label is the collection segment of an ``nx-note://<label>/<rest>`` URI
(and of the legacy ``chroma://`` spelling), and ``urlparse`` cannot be trusted with one. ``urlparse`` raises on a
``[`` in the netloc and on a fullwidth solidus (its NFKC check), and silently cuts the netloc at ``?`` and ``#``. The
readers take the collection by ``partition("/")``, as the engine's ``NoteUri.collectionOf`` does, so a label is read
whole and a URI that is still bad is handled, never a crash.
"""
from __future__ import annotations

import pytest

from nexus.aspect_readers import StatOk, read_source, stat_source

_LABELS = ["notes [draft]", "x[y", "what? now", "a#b", "a／b"]


class _RecordingT3:
    """A T3 whose only job is to say which collection the reader asked for."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_collection(self, name: str):
        self.asked.append(name)
        raise KeyError(name)


@pytest.mark.parametrize("label", _LABELS, ids=repr)
def test_read_source_asks_t3_for_the_whole_label(label: str) -> None:
    t3 = _RecordingT3()

    result = read_source(f"nx-note://{label}/some-note", t3=t3)

    assert t3.asked == [label]
    assert getattr(result, "reason", None) in {"unreachable", "transport"}  # a ReadFail, not an exception


@pytest.mark.parametrize("label", _LABELS, ids=repr)
def test_stat_source_of_a_note_uri_is_fresh_and_never_raises(label: str) -> None:
    result = stat_source(f"nx-note://{label}/some-note")

    assert isinstance(result, StatOk)
    assert result.current_mtime is None


def test_a_source_path_with_a_leading_slash_still_round_trips() -> None:
    """The reason ``removeprefix("/")`` was there: ``nx-note://collection//Users/...`` carries an absolute path."""
    seen: dict[str, str] = {}

    def doc_id_lookup(collection: str, source_id: str) -> str:
        seen.update(collection=collection, source_id=source_id)
        return ""

    class _T3:
        def get_collection(self, name: str):
            return object()

    read_source("nx-note://notes [draft]//Users/x/paper.pdf", t3=_T3(), doc_id_lookup=doc_id_lookup)

    assert seen == {"collection": "notes [draft]", "source_id": "/Users/x/paper.pdf"}


@pytest.mark.parametrize("uri", ["nx-note://", "nx-note:///x", "nx-note://only-a-label"])
def test_a_malformed_note_uri_is_a_read_failure_not_a_crash(uri: str) -> None:
    result = read_source(uri, t3=_RecordingT3())

    assert getattr(result, "reason", None) == "unreachable"
