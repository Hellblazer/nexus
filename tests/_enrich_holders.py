# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stand-ins for the catalog's chunk ownership, for tests of ``nx enrich bib`` that fake T3.

nexus-6pfgb.21: ``nx enrich bib`` selects chunks through the catalog Document that holds them
(``commands.enrich._catalog_collection_documents`` and ``_catalog_documents_for_chunks``). A test
whose T3 is a mock patches those two functions with :func:`held`; the real lookup is covered against the engine in
``tests/integration/test_6pfgb21_bib_enrich_by_manifest.py``.
"""
from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch


def ch(name: str) -> str:
    """A chunk's natural id is its chash: 64 hex of the text hash."""
    return hashlib.sha256(name.encode()).hexdigest()


@contextmanager
def held(
    by_chunk: dict[str, str], *, bib_ids: dict[str, str] | None = None,
    file_paths: dict[str, str] | None = None,
    content_hashes: dict[str, str] | None = None,
):
    """Stand-in for the catalog reads of ``nx enrich bib``: each chunk id in
    *by_chunk* is held by ONE catalog document titled ``by_chunk[id]`` (equal
    titles are one document). ``bib_ids`` maps a title to the catalog id column
    it already holds, ``file_paths`` a title to its ``file_path``,
    ``content_hashes`` a title to its ``index_content_hash``. The catalog
    itself is covered by tests/integration/test_6pfgb21_bib_enrich_by_manifest.py.

    Two seams are patched: ``_catalog_collection_documents`` (the collection's
    documents and the ones still to look up) and ``_catalog_documents_for_chunks``
    (who holds a chunk). The catalog is also reported uninitialised
    (``make_catalog_reader`` returns None) for the block, so the post-run
    citation-link pass and an unpatched write hook do nothing and the test
    reaches no storage (and needs no tenant).
    """
    from nexus.commands.enrich import _CollectionDocuments  # noqa: PLC0415 — deferred: keeps the helper importable without the command module

    titles = sorted(set(by_chunk.values()))
    tumbler = {t: f"1.1.{i + 1}" for i, t in enumerate(titles)}

    def _entries(collection):
        return {
            tumbler[t]: SimpleNamespace(
                tumbler=tumbler[t], title=t, file_path=(file_paths or {}).get(t, ""),
                bib_semantic_scholar_id=(bib_ids or {}).get(t, ""),
                bib_openalex_id="", physical_collection=collection,
                index_content_hash=(content_hashes or {}).get(t, ""), meta={},
            )
            for t in titles
        }

    def _fake_owners(collection, chunk_ids):
        wanted = set(chunk_ids)
        owners = {c: [tumbler[t]] for c, t in by_chunk.items() if c in wanted}
        return owners, _entries(collection)

    def _fake_documents(collection, id_field, served):
        entries = _entries(collection)
        to_look_up = {}
        for t in titles:
            entry = entries[tumbler[t]]
            if getattr(entry, id_field, ""):
                continue
            cids = [c for c, title in by_chunk.items() if title == t and c in served]
            if cids:
                to_look_up[tumbler[t]] = (entry, cids)
        enriched = sum(1 for e in entries.values() if getattr(e, id_field, ""))
        return _CollectionDocuments(
            to_look_up=to_look_up, total=len(entries), enriched=enriched, untitled=0,
        )

    with ExitStack() as stack:
        stack.enter_context(
            patch("nexus.commands.enrich._catalog_documents_for_chunks", side_effect=_fake_owners),
        )
        stack.enter_context(
            patch("nexus.commands.enrich._catalog_collection_documents", side_effect=_fake_documents),
        )
        stack.enter_context(
            patch("nexus.catalog.factory.make_catalog_reader", return_value=None),
        )
        yield
