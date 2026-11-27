# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-9biyo: ``nx collection verify --deep`` and the MCP ``collection_verify`` against the real engine.

``verify_collection_deep`` sampled its probe documents through ``db._client_for(...)`` and ``col.peek(...)``, both
Chroma-era surfaces that ``HttpVectorClient`` (the production T3 handle in every mode) does not have. The deep
probe raised ``AttributeError`` and the CLI turned it into "embedding probe failed", exit 1, on every service-backed
install. Every existing verify test passes a ``T3Database`` over an in-memory client or mocks the function, so none of
them could see it.

These tests drive the real verbs against the session's engine substrate (server-side embedding, pgvector search), with
no mock of the vector client, and observe the result through the verb that did not write the data.
"""
from __future__ import annotations

from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.store import _t3
from nexus.db.http_vector_client import HttpVectorClient, is_service_backed
from nexus.mcp.core import collection_verify

_NOTES = {
    # Twelve short, distinct documents: more than the probe's n_results=10, so a 100% hit rate means each sampled
    # document was found among others rather than because the collection fits inside one result page.
    "9biyo-note-hnsw": "HNSW graphs find approximate nearest neighbors through layered proximity links.",
    "9biyo-note-wal": "Write ahead logging records every change durably before data pages are touched.",
    "9biyo-note-vacuum": "Autovacuum reclaims dead tuples so that updated tables do not bloat.",
    "9biyo-note-bread": "Sourdough bread rises slowly because wild yeast ferments the flour overnight.",
    "9biyo-note-orbit": "A geostationary satellite orbits above the equator once every sidereal day.",
    "9biyo-note-chess": "In chess the knight moves in an L shape and may jump over other pieces.",
    "9biyo-note-coral": "Coral reefs shelter a quarter of marine species despite covering little seafloor.",
    "9biyo-note-latin": "Latin nouns change their endings to show case, number and grammatical gender.",
    "9biyo-note-solder": "Solder joins electronic components by melting a tin alloy around the leads.",
    "9biyo-note-monsoon": "The summer monsoon brings heavy rain to South Asia as warm air rises inland.",
    "9biyo-note-fugue": "A fugue weaves one melodic subject through several independent voices.",
    "9biyo-note-glacier": "Glaciers carve broad valleys as slowly flowing ice drags rock across the bed.",
}


def _put_notes(runner: CliRunner) -> str:
    """Store the notes with ``nx store put`` and return the collection they landed in."""
    collection = ""
    for title, text in _NOTES.items():
        put = runner.invoke(main, ["store", "put", "-", "--title", title, "-c", "fixture-subject"], input=text + "\n")
        assert put.exit_code == 0, put.output
        collection = put.output.split("→")[-1].strip().split()[0]
    assert collection, "store put did not name a collection"
    return collection


def test_verify_deep_on_the_service_client(t2_service_env) -> None:
    """One tenant, both surfaces: the CLI verb and the MCP tool read the same collection."""
    runner = CliRunner()
    collection = _put_notes(runner)

    # Non-vacuity: the handle the verb gets is the service client, the one without the Chroma-era surface.
    db = _t3()
    assert isinstance(db, HttpVectorClient) and is_service_backed(db)
    assert db.collection_info(collection)["count"] == len(_NOTES)

    result = runner.invoke(main, ["collection", "verify", collection, "--deep"])
    assert result.exit_code == 0, result.output
    assert "embedding probe failed" not in result.output
    assert "embedding health OK" in result.output
    assert "100% probe hit rate" in result.output
    assert "cosine" in result.output

    out = collection_verify(collection)
    assert "Status:     healthy" in out, out
    assert f"Documents:  {len(_NOTES)}" in out, out
    assert "Probe hit rate: 100%" in out, out
    assert "(cosine)" in out, out
