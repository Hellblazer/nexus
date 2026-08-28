# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ou4tb (b): degraded-service errors must not read as empty results.

``_ServiceCollectionStub.get`` / ``.delete`` and ``HttpVectorClient.existing_ids``
caught ``VectorServiceError`` and returned an empty result, so a degraded
service was indistinguishable from a genuinely-empty collection. That is the
no-silent-fallbacks-for-correctness directive, and the consequences are
concrete per caller:

  nx catalog verify   every expected document reported as a GHOST
  migration ETL       concludes nothing landed and rewrites it
  --skip-existing     silently skips nothing
  staleness check     re-embeds everything, charged to the re-embed budget
  stale-chunk prune   caller believes it pruned; search still sees the chunks

The fix adopts the contract ``count`` and ``get_all_metadata`` already
document in this same class — "the caller owns the boundary" — which both
docstrings explicitly named ``get``/``delete`` as the holdouts from.
"""
from __future__ import annotations

import ast

import pytest

from nexus.db import http_vector_client as hvc
from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    _ServiceCollectionStub,
)


@pytest.fixture
def failing_post(monkeypatch):
    """Every service call fails the way a degraded service fails."""
    def boom(*a, **kw):
        raise VectorServiceError("service unreachable", code=503)

    monkeypatch.setattr(hvc, "_post", boom)
    return boom


def _always_raises(stmts: "list[ast.stmt]") -> bool:
    """True iff *stmts* is GUARANTEED to exit via ``raise`` on every
    execution path — no fall-through, no ``return``/``continue``/``break``.

    nexus-uxg4u round 2 (code-review-expert Finding C / substantive-critic
    SIGNIFICANT): the prior version of
    ``TestCallerLoopsIsolatePerItem::test_doc_indexer_registers_
    unconditionally_at_small_doc_tail`` checked only "does an
    ``ast.Raise`` node appear ANYWHERE in the handler's subtree" — the
    reviewers' counterexample, ``except Exception as exc: if
    isinstance(exc, KeyboardInterrupt): raise\\n _log.warning(...);
    return 0``, contains a ``Raise`` node (so the old check passed it)
    while still swallowing every non-``KeyboardInterrupt`` exception —
    exactly the silent-strand shape this guard exists to catch.

    Handles the shapes this codebase's except-handlers actually use: a
    straight-line sequence ending in (or containing) an unconditional
    ``raise``, or an ``if``/``else`` whose BOTH branches always raise.
    Conservative elsewhere — a ``for``/``while``/``try``/``with`` in the
    middle is treated as "does not itself guarantee raise" (scanning
    continues past it), and a bare ``if`` with no ``else`` is never
    exhaustive on its own; both bias the check toward FALSE (flagging a
    handler as swallowing) rather than toward a false pass.
    """
    for stmt in stmts:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, (ast.Return, ast.Continue, ast.Break)):
            return False
        if isinstance(stmt, ast.If) and stmt.orelse:
            if _always_raises(stmt.body) and _always_raises(stmt.orelse):
                return True
    return False


class TestGetFailsLoud:
    def test_get_by_where_raises_instead_of_looking_empty(self, failing_post):
        stub = _ServiceCollectionStub("code__o__bge-768__v1")
        with pytest.raises(VectorServiceError):
            stub.get(where={"doc_id": "d1"}, include=["metadatas"])

    def test_get_by_ids_raises_instead_of_looking_empty(self, failing_post):
        stub = _ServiceCollectionStub("code__o__bge-768__v1")
        with pytest.raises(VectorServiceError):
            stub.get(ids=["a" * 64])


class TestDeleteFailsLoud:
    def test_failed_prune_raises(self, failing_post):
        """A caller that believes it pruned, over a service that did not, is a
        permanent divergence between the caller's model and what search sees."""
        stub = _ServiceCollectionStub("code__o__bge-768__v1")
        with pytest.raises(VectorServiceError):
            stub.delete(ids=["a" * 64])

    def test_empty_delete_is_still_a_no_op(self, failing_post):
        """No ids means no request — must not raise on a degraded service."""
        stub = _ServiceCollectionStub("code__o__bge-768__v1")
        stub.delete(ids=[])


class TestExistingIdsFailsLoud:
    def test_raises_rather_than_reporting_everything_missing(self, failing_post):
        """The dangerous one: empty here means 'all of these are MISSING'."""
        client = HttpVectorClient()
        with pytest.raises(VectorServiceError):
            client.existing_ids("code__o__bge-768__v1", ["a" * 64, "b" * 64])

    def test_empty_input_short_circuits_without_a_request(self, failing_post):
        client = HttpVectorClient()
        assert client.existing_ids("code__o__bge-768__v1", []) == set()


class TestTheContractIsUniformAcrossTheClass:
    """count/get_all_metadata already raised; these three were the holdouts.

    Pinning uniformity is the point — the bug was that two methods degraded
    and two did not, in the same class, so a caller could not reason about
    any of them without reading each one.
    """

    def test_no_method_swallows_a_service_error_to_an_empty_result(self):
        from pathlib import Path

        src = Path("src/nexus/db/http_vector_client.py").read_text()
        for banned in (
            'return {"ids": [], "documents": [], "metadatas": []}',
            "http_vector_existing_ids_failed",
            "service_collection_delete_failed",
            "service_collection_get_failed",
        ):
            assert banned not in src, (
                f"{banned!r} is back — a degraded service can look empty again"
            )


# ── caller isolation (option 2): loud at the client, isolated at the loops ───
#
# The client contract change alone converts a degraded service from "silently
# wrong results" into "aborts the whole command", which trades one bad
# behaviour for another. These pin the second half: sweeps and per-item loops
# skip the item they could not read and keep going, and say so.


class TestCallerLoopsIsolatePerItem:
    """Grep-level pins on the sites audited for nexus-ou4tb (b).

    Originally seven; now four live entries — nexus-tbkk1 retired the
    doc_indexer.py entry when its guarded prune was deleted as dead code
    (see the SITES list comment below), the same shape as RDR-155 P4b's
    earlier retirement of the migration/collision_audit.py entry.
    nexus-afudo (2026-08-05) retired the indexer.py legacy-prune entry
    the same way — see below. RDR-191 Phase 6 (nexus-o8dil.33, 2026-08-15)
    retired the indexer.py GC-sweep-read entry the same way: the guarded
    region (``_prune_deleted_files``'s client-side fetch-diff-copy-delete
    fallback) is deleted outright — the manifest-chunk FK makes the
    completeness apparatus it existed to prove correct unreachable by
    construction — so there is no longer a degraded read here to isolate.

    Deliberately structural rather than behavioural: driving a real degraded
    service through each of these paths needs a different fixture per site,
    and the property that actually regresses is "somebody removed the guard".
    """

    SITES = [
        # (file, marker that must be inside a guarded region)
        ("src/nexus/indexer.py", "frecency_staleness_lookup_failed_skipping_file"),
        # (nexus-afudo: indexer.py's "legacy_prune_failed_skipping_
        # source_path" guarded region — _prune_misclassified_in_
        # collection's per-path source_path fallback loop — was DELETED
        # as dead code, completing RDR-102 D2's "Phase 5b" for the
        # indexer.py/indexer_utils.py sibling sites nexus-tbkk1 left
        # live. RDR-102 D2 removed source_path from make_chunk_metadata
        # for every writer (not just doc_indexer.py's), so the prune's
        # where-clause never matched a real chunk row regardless of
        # legacy_paths cardinality; a live-store probe (field>=!
        # existence test) confirmed zero source_path rows across 13
        # representative collections, including the code__/docs__
        # collections this prune specifically targets. There is no
        # longer a prune here to isolate a failure around. See
        # nexus.indexer_utils.StalenessCache's docstring and
        # test_indexer.py::test_prune_misclassified_source_path_
        # fallback_deleted_as_dead_code.)
        # (RDR-191 Phase 6, nexus-o8dil.33: indexer.py's
        # "gc_sweep_read_failed_skipping_collection" guarded region —
        # _prune_deleted_files' client-side fetch-diff-copy-delete
        # fallback sweep — was DELETED outright; the manifest-chunk FK
        # makes the completeness apparatus it existed to prove correct
        # unreachable by construction. There is no longer a degraded read
        # here to isolate. See indexer.py's _prune_deleted_files docstring.)
        ("src/nexus/exporter.py", "skip_existing_probe_failed_importing_batch"),
        # (nexus-tbkk1: doc_indexer.py's "stale_chunk_prune_failed_
        # registration_still_running" guarded region — index_pdf's
        # small-doc-branch stale-chunk prune — was DELETED as dead code.
        # RDR-102 D2 removed source_path from make_chunk_metadata, so the
        # prune's where-clause never matched a real chunk row; there is
        # no longer a prune here to isolate a failure around. See
        # nexus.doc_indexer._identity_where's docstring and
        # test_doc_indexer.py::test_index_pdf_small_doc_prune_deleted_as_
        # dead_code.)
        ("src/nexus/commands/catalog_cmds/integrity.py", "catalog_verify_collection_unreadable"),
        # (RDR-155 P4b: migration/collision_audit.py's guarded page loop
        # died with the file.)
        ("src/nexus/db/reconcile.py", "vector_etl_verify_fill_page_unreachable"),
    ]

    @pytest.mark.parametrize(("path", "marker"), SITES)
    def test_site_still_isolates_and_reports(self, path: str, marker: str) -> None:
        from pathlib import Path

        src = Path(path).read_text()
        assert marker in src, (
            f"{path} lost its per-item isolation for {marker!r} — a degraded "
            f"service now aborts the whole sweep instead of skipping one item"
        )

    def test_catalog_verify_never_reports_all_good_over_unread_collections(self) -> None:
        """The worst possible regression for an integrity checker: a confident
        clean verdict over collections it never managed to probe.

        Three channels, because each covers a reader the others miss: a
        stderr warning (human), a non-zero exit (script), and the summary
        wording (anyone reading the transcript). The JSON stdout shape is
        deliberately NOT one of them — it is a documented machine-parseable
        collection->ghosts map and changing it would break every parser.
        """
        from pathlib import Path

        src = Path("src/nexus/commands/catalog_cmds/integrity.py").read_text()
        assert "could not be read and " in src
        assert "err=True" in src, "the warning must not corrupt --json stdout"
        assert "_exit_incomplete_if_unreadable" in src
        assert "the skipped ones above are unverified" in src

    def test_verify_json_stdout_shape_pins_the_ci_contract(self) -> None:
        """nexus-sj4a3: the old top-level {collection: [{tumbler, title,
        doc_id}]} shape encoded the retired pre-RDR-108 doc_id identity and
        was DELIBERATELY broken. The new CI contract is a "summary" block
        plus the finding lists — pin its presence in the emitter so a
        future edit cannot silently drop or rename it.

        ``never_chunked_count`` (a bare int) -> ``never_chunked`` (a
        structured per-collection breakdown) is a SECOND deliberate break
        (nexus-sj4a3 substantive critique CRIT-1): the bare int blanket-
        labeled every zero-chunk doc RDR-145-exempt, including the
        code__*/docs__*/rdr__* population RDR-145 never covered."""
        from pathlib import Path

        src = Path("src/nexus/commands/catalog_cmds/integrity.py").read_text()
        for must_have in (
            '"summary": summary', '"vanished_collections": vanished_collections',
            '"damaged": damaged', '"lost": lost',
            '"never_chunked": never_chunked', '"unreadable": unreadable',
        ):
            assert must_have in src, (
                f"{must_have!r} missing — the --json CI contract shape changed "
                "without updating this pin"
            )
        assert "ghosts_by_collection" not in src, (
            "the retired doc_id-keyed ghosts_by_collection shape is back"
        )

    def test_doc_indexer_registers_unconditionally_at_small_doc_tail(self) -> None:
        """nexus-tbkk1: supersedes test_doc_indexer_registers_even_when_
        the_prune_fails. That test pinned "a prune failure must not
        strand catalog registration" by asserting no ``raise`` sat
        between the (isolated, try/except-wrapped) prune's warning
        marker and ``_register_in_catalog``. The prune it isolated
        against is DELETED dead code (RDR-102 D2 made its where-clause
        permanently unable to match a real chunk row — see
        ``nexus.doc_indexer._identity_where``'s docstring), so there is
        no longer a prune failure mode to isolate registration from.

        This test pins the STRONGER surviving invariant directly:
        registration must never be STRANDED — silently skipped while
        the function carries on — by an intervening try/except between
        the small-doc branch's dead-prune-deletion marker and the
        completion stamp.

        nexus-uxg4u (task 2, fresh-mint rollback on
        ``IndexRunVerifyRefused``) added exactly one intervening
        ``try``/``except`` in this span, around the ``_fence_complete``
        completion-stamp call — but it re-raises unconditionally after
        its rollback compensation, so it PROPAGATES rather than
        SWALLOWS: registration is never reached on that path either way
        (the bare, unwrapped call already aborted the function on this
        exact exception before this fix), so the original invariant
        still holds. A bare textual scan for ``try:``/``raise`` tokens
        cannot tell a propagating guard from a swallowing one, so this
        now parses the AST and checks every ``except`` handler in the
        span for an UNCONDITIONAL ``raise`` on every execution path
        through the handler (nexus-uxg4u round 2: a mere ``ast.Raise``
        node ANYWHERE in the subtree is not enough — see
        ``_always_raises``'s docstring for the counterexample this
        closes) — silence, not the mere presence of a try/except, is
        what would actually strand registration.
        """
        from pathlib import Path

        src = Path("src/nexus/doc_indexer.py").read_text()
        # nexus-tbkk1's "stale-chunk prune ... DELETED" comment appears at
        # all three deleted prune sites verbatim; rindex the LAST one
        # (index_pdf's small-doc branch, the only one that precedes
        # _register_in_catalog) rather than the first (_index_document's).
        marker = src.rindex(
            "nexus-tbkk1: stale-chunk prune via _identity_where's source_path"
        )
        register = src.index("_register_in_catalog(metadatas_list", marker)
        marker_line = src.count("\n", 0, marker) + 1
        register_line = src.count("\n", 0, register) + 1

        tree = ast.parse(src, filename="src/nexus/doc_indexer.py")
        offending: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not (marker_line <= node.lineno <= register_line):
                continue
            for handler in node.handlers:
                if not _always_raises(handler.body):
                    offending.append(ast.dump(handler.type) if handler.type else "bare except")
        assert not offending, (
            "a SWALLOWING except (no raise in its body) reappeared between "
            "the deleted prune site and catalog registration in index_pdf's "
            "small-doc branch — registration must run unconditionally, "
            f"never isolated behind a swallowable failure: {offending}"
        )

    def test_always_raises_helper_rejects_the_reviewers_counterexample(self) -> None:
        """Self-falsification (nexus-uxg4u round 2, code-review-expert
        Finding C): proves ``_always_raises`` actually has teeth against
        the exact counterexample the review named — a handler that
        contains an ``ast.Raise`` node somewhere, yet still swallows the
        exception on the common path. Guards against this test file's
        OWN guard regressing back to a bare "raise anywhere" check."""
        counterexample = ast.parse(
            "try:\n"
            "    risky()\n"
            "except Exception as exc:\n"
            "    if isinstance(exc, KeyboardInterrupt):\n"
            "        raise\n"
            "    _log.warning('swallowed')\n"
            "    return 0\n",
            mode="exec",
        )
        handler = counterexample.body[0].handlers[0]  # type: ignore[attr-defined]
        assert not _always_raises(handler.body), (
            "_always_raises must reject a handler that only raises on "
            "SOME paths (here: KeyboardInterrupt only) and swallows + "
            "returns on every other exception — a bare 'is there a raise "
            "node anywhere' check passes this counterexample; this "
            "guard must not"
        )

        # Sibling positive case: an unconditional raise (this bead's own
        # rollback-then-re-raise shape) must still pass.
        propagating = ast.parse(
            "try:\n"
            "    risky()\n"
            "except Exception as exc:\n"
            "    compensate(exc)\n"
            "    raise\n",
            mode="exec",
        )
        prop_handler = propagating.body[0].handlers[0]  # type: ignore[attr-defined]
        assert _always_raises(prop_handler.body), (
            "_always_raises must accept a handler that unconditionally "
            "re-raises after a best-effort compensation call — the exact "
            "shape index_pdf's own rollback wiring uses"
        )
