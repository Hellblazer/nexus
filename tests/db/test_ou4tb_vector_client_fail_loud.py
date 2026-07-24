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
    """Grep-level pins on the seven sites audited for nexus-ou4tb (b).

    Deliberately structural rather than behavioural: driving a real degraded
    service through each of these paths needs seven different fixtures, and
    the property that actually regresses is "somebody removed the guard".
    """

    SITES = [
        # (file, marker that must be inside a guarded region)
        ("src/nexus/indexer.py", "frecency_staleness_lookup_failed_skipping_file"),
        ("src/nexus/indexer.py", "legacy_prune_failed_skipping_source_path"),
        ("src/nexus/indexer.py", "gc_sweep_read_failed_skipping_collection"),
        ("src/nexus/exporter.py", "skip_existing_probe_failed_importing_batch"),
        ("src/nexus/doc_indexer.py", "stale_chunk_prune_failed_registration_still_running"),
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

    def test_verify_json_stdout_shape_is_unchanged(self) -> None:
        """Backward compatibility: the ghosts map stays top-level."""
        from pathlib import Path

        src = Path("src/nexus/commands/catalog_cmds/integrity.py").read_text()
        assert "_json.dumps(ghosts_by_collection, indent=2)" in src, (
            "the --json contract is a top-level collection->ghosts map; "
            "nesting it under a new key breaks existing parsers"
        )

    def test_doc_indexer_registers_even_when_the_prune_fails(self) -> None:
        """Registration must not be stranded behind a prune failure — the
        chunks and hooks are already committed by that point."""
        from pathlib import Path

        src = Path("src/nexus/doc_indexer.py").read_text()
        guard = src.index("stale_chunk_prune_failed_registration_still_running")
        register = src.index("_register_in_catalog(metadatas_list", guard)
        between = src[guard:register]
        assert "raise" not in between, (
            "the prune failure path must fall through to registration"
        )
