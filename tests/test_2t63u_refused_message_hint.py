# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2t63u round 2 (substantive-critic Significant #2: zero test
coverage on the refusal-message branches). Unit-level coverage for
``nexus.commands.index._index_run_refused_message``'s ``target_collection``
diagnostic hint, pinning exact hint presence/absence for all four cases:

1. AGREE       — the live lookup confirms the document's stamped collection
                 already matches this run's target. Collection mismatch is
                 positively ruled out; NO hint (reviewer #3, round 2: the
                 pre-round-2 code printed the "may be stale" hint here too
                 — misdirection the fix exists to kill).
2. DISAGREE    — the live lookup finds a genuine mismatch; the NAMED-cause
                 message fires.
3. LOOKUP FAILS — the live lookup raises (catalog unreachable, transient
                 error); falls back to the generic hint (cannot positively
                 rule anything in or out).
4. RESOLVE NONE — the live lookup succeeds but returns no document (e.g.
                 the doc_id does not resolve); same generic-hint fallback
                 as case 3.

Pure unit-level: ``make_catalog_reader`` is patched with a fake reader
exposing only ``.resolve(doc_id)`` — no engine substrate needed, this file
carries no ``@pytest.mark.integration``.
"""
from __future__ import annotations

from unittest.mock import patch

_DOC_ID = "1.2.3"


class _FakeRefused:
    """Duck-typed stand-in for IndexRunVerifyRefused — the function under
    test never imports nexus.errors, it just reads attributes."""

    def __init__(self, *, doc_id: str = _DOC_ID, referenced: int = 4, present: int = 1, missing: int = 3) -> None:
        self.doc_id = doc_id
        self.referenced = referenced
        self.present = present
        self.missing = missing


class _FakeEntry:
    def __init__(self, physical_collection: str) -> None:
        self.physical_collection = physical_collection


class _FakeReaderResolves:
    def __init__(self, entry: _FakeEntry | None) -> None:
        self._entry = entry

    def resolve(self, doc_id: str):
        return self._entry


class _FakeReaderRaises:
    def resolve(self, doc_id: str):
        raise RuntimeError("simulated catalog lookup failure")


def _base_text(exc: _FakeRefused) -> str:
    return (
        f"completion REFUSED — document is NOT fully indexed "
        f"(referenced={exc.referenced} present={exc.present} "
        f"missing={exc.missing})"
    )


class TestIndexRunRefusedMessageCollectionHint:
    def test_agree_suppresses_the_hint_entirely(self) -> None:
        """Case 1 (reviewer #3, round 2): the live lookup CONFIRMS the
        stamped collection already matches this run's target — collection
        mismatch is positively ruled out. The message must be EXACTLY the
        base counts summary, no hint of any kind appended."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        collection = "docs__agree__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderResolves(_FakeEntry(collection)),
        ):
            msg = _index_run_refused_message(exc, target_collection=collection)

        assert msg == _base_text(exc), (
            f"confirmed agreement must suppress every hint — got {msg!r}"
        )
        assert "may still be stale" not in msg
        assert "likely cause" not in msg

    def test_disagree_names_the_mismatch(self) -> None:
        """Case 2: a genuine, confirmed mismatch — the NAMED-cause message
        fires, quoting both the stamped and target collections."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        stamped = "docs__old-stale__bge-base-en-v15-768__v1"
        target = "docs__new-target__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderResolves(_FakeEntry(stamped)),
        ):
            msg = _index_run_refused_message(exc, target_collection=target)

        assert msg.startswith(_base_text(exc))
        assert "likely cause" in msg
        assert stamped in msg
        assert target in msg
        assert _DOC_ID in msg

    def test_lookup_failure_falls_back_to_the_generic_hint(self) -> None:
        """Case 3: the live lookup raises — cannot positively confirm OR
        rule out a mismatch, so the generic (non-committal) hint prints,
        and the lookup failure never propagates as a raw traceback."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        target = "docs__whatever__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderRaises(),
        ):
            msg = _index_run_refused_message(exc, target_collection=target)

        assert msg.startswith(_base_text(exc))
        assert "may still be stale" in msg
        assert "likely cause" not in msg, (
            "an unconfirmed lookup must never render the NAMED-mismatch "
            "wording — that implies a confirmed collection value"
        )

    def test_resolve_none_falls_back_to_the_generic_hint(self) -> None:
        """Case 4: the lookup succeeds but the document does not resolve
        (``reader.resolve(doc_id)`` returns None) — same non-committal
        fallback as the lookup-failure case, not a crash and not a
        confident (dis)agreement claim."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        target = "docs__whatever-none__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderResolves(None),
        ):
            msg = _index_run_refused_message(exc, target_collection=target)

        assert msg.startswith(_base_text(exc))
        assert "may still be stale" in msg
        assert "likely cause" not in msg

    def test_no_target_collection_supplied_skips_the_lookup_and_hints_generically(self) -> None:
        """No target_collection at all (a caller that doesn't have one to
        pass) — the lookup is never attempted (nothing to compare against),
        straight to the generic hint. Distinguishes "opted out of the
        lookup" from "the lookup ran and confirmed something.\""""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
        ) as mock_reader_factory:
            msg = _index_run_refused_message(exc)

        mock_reader_factory.assert_not_called()
        assert msg.startswith(_base_text(exc))
        assert "may still be stale" in msg

    def test_no_doc_id_returns_bare_base_text(self) -> None:
        """An exception with no doc_id (duck-typed, defensive floor) must
        return exactly the base counts summary — no lookup possible, no
        hint of any kind."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused(doc_id="")
        msg = _index_run_refused_message(exc, target_collection="docs__x__bge-base-en-v15-768__v1")

        assert msg == _base_text(exc)
