# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2t63u round 2 (substantive-critic Significant #2: zero test
coverage on the refusal-message branches). Unit-level coverage for
``nexus.commands.index._index_run_refused_message``'s collection
diagnostic, pinning exact hint presence/absence for all cases.

nexus-nb3yg (2026-08-08): every ``--corpus``-only invocation (no explicit
``--collection``) used to call this function with ``target_collection=""``
— the live lookup was SKIPPED ENTIRELY and the generic hint printed
UNCONDITIONALLY, asserting an unchecked cause on the CLI's most common
invocation shape. The fix threads a ``corpus`` fallback through the
function so the lookup runs whenever *doc_id* resolves, regardless of
whether the collection was named explicitly (``--collection``) or left to
the ``--corpus`` default. This file now pins:

1. AGREE       — the live lookup confirms the document's stamped collection
                 already matches this run's target (explicit OR
                 corpus-derived). Collection mismatch is positively ruled
                 out; the message states the check PLAINLY (stamped
                 collection + "genuinely absent"), never a bare, silent
                 counts line and never the nexus-2t63u hint (reviewer #3,
                 round 2: the pre-round-2 code printed the "may be stale"
                 hint here too — misdirection the fix exists to kill).
2. DISAGREE    — the live lookup finds a genuine mismatch; the NAMED-cause
                 nexus-2t63u message fires, quoting both collections.
3. UNKNOWN     — the live lookup raises, or resolves to no document; the
                 message states the check as UNKNOWN — never asserts a
                 "likely cause" the lookup never confirmed.
4. CORPUS-ONLY — no explicit --collection, only --corpus: the lookup runs
                 (this is the nb3yg bug fix) against the corpus-derived
                 default collection name.
5. NEITHER     — no target_collection AND no corpus: nothing to compare
                 against, lookup skipped, bare counts line.

Pure unit-level: ``make_catalog_reader`` is patched with a fake reader
exposing only ``.resolve(doc_id)`` — no engine substrate needed, this file
carries no ``@pytest.mark.integration``.
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.corpus import docs_leaf_fallback_collection_name

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
    def test_agree_states_the_match_plainly_no_hint(self) -> None:
        """Case 1 (reviewer #3, round 2, extended by nb3yg): the live
        lookup CONFIRMS the stamped collection already matches this run's
        target — collection mismatch is positively ruled out. The message
        must plainly state the check matched (not stay silent) and must
        never render the nexus-2t63u hint."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        collection = "docs__agree__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderResolves(_FakeEntry(collection)),
        ):
            msg = _index_run_refused_message(exc, target_collection=collection)

        assert msg.startswith(_base_text(exc))
        assert "collection check: confirmed" in msg
        assert collection in msg
        assert "genuinely absent" in msg
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

    def test_lookup_failure_states_the_check_as_unknown(self) -> None:
        """Case 3: the live lookup raises — cannot positively confirm OR
        rule out a mismatch, so the message states the check as UNKNOWN
        (never asserting a "likely cause" it never confirmed), and the
        lookup failure never propagates as a raw traceback."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        target = "docs__whatever__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderRaises(),
        ):
            msg = _index_run_refused_message(exc, target_collection=target)

        assert msg.startswith(_base_text(exc))
        assert "collection check: unknown" in msg
        assert target in msg
        assert "likely cause" not in msg, (
            "an unconfirmed lookup must never render the NAMED-mismatch "
            "wording — that implies a confirmed collection value"
        )

    def test_resolve_none_states_the_check_as_unknown(self) -> None:
        """Case 3 variant: the lookup succeeds but the document does not
        resolve (``reader.resolve(doc_id)`` returns None) — same UNKNOWN
        statement as the lookup-failure case, not a crash and not a
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
        assert "collection check: unknown" in msg
        assert "likely cause" not in msg

    def test_resolved_doc_empty_stamped_collection_states_the_check_as_unknown(self) -> None:
        """reviewer M1 (2026-08-08): HttpCatalogClient._to_entry coerces a
        missing/None ``physical_collection`` to ``""`` for a ghost/
        unstamped document (http_catalog_client.py's ``d.get(...) or ""``
        default). A resolved entry with an EMPTY stamped collection is not
        a confirmed value to compare — routing it into the CONFIRMED-
        MISMATCH branch would render the confident 'likely cause: ...
        is ''' wording on evidence that doesn't support it. This must
        render the same UNKNOWN statement as an unresolved/failed lookup,
        and must never emit the nexus-2t63u named-cause hint."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        target = "docs__whatever-ghost__bge-base-en-v15-768__v1"
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=_FakeReaderResolves(_FakeEntry("")),
        ):
            msg = _index_run_refused_message(exc, target_collection=target)

        assert msg.startswith(_base_text(exc))
        assert "collection check: unknown" in msg
        assert target in msg
        assert "likely cause" not in msg, (
            "an empty stamped_collection is not a confirmed value — it must "
            "never render the NAMED-mismatch wording (nexus-2t63u)"
        )

    def test_corpus_only_still_performs_the_lookup_nb3yg(self) -> None:
        """nexus-nb3yg: the actual bug. A ``--corpus``-only invocation (no
        explicit ``--collection``, i.e. what every plain ``nx index md
        --corpus X`` call passes) must NOT skip the lookup — it derives
        the same conformant ``docs__<corpus>__<model>__v1`` name
        ``index_markdown``/``index_pdf`` compute internally and compares
        against it, exactly as if ``--collection`` had been passed
        explicitly."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        corpus = "mycorpus"
        with patch(
            "nexus.corpus.effective_embedding_model_for_writes",
            return_value="bge-base-en-v15-768",
        ):
            derived = docs_leaf_fallback_collection_name(corpus)
            with patch(
                "nexus.catalog.factory.make_catalog_reader",
                return_value=_FakeReaderResolves(_FakeEntry(derived)),
            ) as mock_reader_factory:
                msg = _index_run_refused_message(exc, target_collection="", corpus=corpus)

        mock_reader_factory.assert_called_once()
        assert msg.startswith(_base_text(exc))
        assert "collection check: confirmed" in msg
        assert derived in msg

    def test_corpus_only_mismatch_names_the_derived_target(self) -> None:
        """nexus-nb3yg companion: the corpus-derived fallback must also
        drive the DISAGREE branch (not just the AGREE branch above) —
        a stale physical_collection is diagnosable on a plain --corpus
        invocation, not only when --collection was passed explicitly."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        corpus = "mycorpus"
        stamped = "docs__old-corpus__bge-base-en-v15-768__v1"
        with patch(
            "nexus.corpus.effective_embedding_model_for_writes",
            return_value="bge-base-en-v15-768",
        ):
            derived = docs_leaf_fallback_collection_name(corpus)
            with patch(
                "nexus.catalog.factory.make_catalog_reader",
                return_value=_FakeReaderResolves(_FakeEntry(stamped)),
            ):
                msg = _index_run_refused_message(exc, target_collection="", corpus=corpus)

        assert "likely cause" in msg
        assert stamped in msg
        assert derived in msg

    def test_neither_target_nor_corpus_supplied_skips_the_lookup(self) -> None:
        """Case 5: a caller with genuinely nothing to compare against (no
        target_collection AND no corpus) — the lookup is never attempted
        (nothing to compare), straight to the bare counts line."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused()
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
        ) as mock_reader_factory:
            msg = _index_run_refused_message(exc)

        mock_reader_factory.assert_not_called()
        assert msg == _base_text(exc)

    def test_no_doc_id_returns_bare_base_text(self) -> None:
        """An exception with no doc_id (duck-typed, defensive floor) must
        return exactly the base counts summary — no lookup possible, no
        hint of any kind."""
        from nexus.commands.index import _index_run_refused_message

        exc = _FakeRefused(doc_id="")
        msg = _index_run_refused_message(exc, target_collection="docs__x__bge-base-en-v15-768__v1")

        assert msg == _base_text(exc)
