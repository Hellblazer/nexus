# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 1 (nexus-rfvyy.6.20): a sensitive ``store put`` whose title already names a live
note in a Voyage collection.

In local mode a sensitive put to a Voyage collection is re-routed to the bge sibling of the same subject. When
a live note with that title already sits in the Voyage collection, the re-route would write a NEW note in the
sibling and leave the original, unflagged and Voyage-embedded, beside it: an unflagged twin. The same rule as
``refuse_external_existing`` (residual S-B) applies: refuse without ``--ack-external-embedder`` before anything
is written; with the ack the put goes to the Voyage collection and flags the existing note in place. The Voyage
tokens are collection-name labels.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests.test_sensitive_external_rules import _CONTENT, _VOYAGE, BGE, Stop, _reader

_TITLE = "rfvyy-existing-voyage-note"


def _hold_the_row_under_voyage(name: str) -> None:
    """Make the registry row of the seeded Voyage collection say Voyage.

    The substrate's engine registers every collection under its bge profile, so the row a seed registers for
    this name holds ``bge-base-en-v15-768`` whatever the name says. P3.8's engine allows one live collection a
    tuple, and the bge sibling a sensitive put routes to is the tuple (knowledge, rfvyy-ext, bge, v1): the
    seeded row would hold it and refuse the sibling's registration (409). A Voyage collection holds the Voyage
    model, as in an install that embeds with Voyage."""
    from tests._catalog_fixture_ops import _run_psql
    from tests._chunk_seed import _lit, ambient_tenant

    _run_psql(
        "UPDATE nexus.catalog_collections SET embedding_model = 'voyage-context-3' "
        f"WHERE tenant_id = {_lit(ambient_tenant())} AND name = {_lit(name)};"
    )


def _plain_note_in_voyage(title: str = _TITLE):
    """A live note that an earlier non-sensitive put left in the Voyage collection (the catalog row is what a
    re-put resolves)."""
    from nexus.catalog.factory import make_catalog_writer
    from tests._catalog_fixture_ops import register_seed_collections

    # RDR-229 P3.1: a document names a registered collection (the engine refuses one that is not, 422).
    register_seed_collections(_VOYAGE["knowledge"])
    _hold_the_row_under_voyage(_VOYAGE["knowledge"])
    reader, writer = _reader(), make_catalog_writer()
    owner = reader.curator_owner_tumbler_by_name("rfvyy-ext") or writer.register_owner("rfvyy-ext", "curator")
    tumbler = writer.register(
        owner=owner, title=title, content_type="knowledge", physical_collection=_VOYAGE["knowledge"],
        source_uri=f"nx-note://{_VOYAGE['knowledge']}/{title}")
    return str(tumbler)


def _titled(title: str):
    return _reader().find_by_title_exact(title)


def test_cli_refuses_a_sensitive_reput_over_a_voyage_note_and_leaves_no_twin(t2_service_env) -> None:
    tumbler = _plain_note_in_voyage()

    out = CliRunner().invoke(
        main, ["store", "put", "-", "--title", _TITLE, "-c", _VOYAGE["knowledge"], "--sensitive"], input=_CONTENT)

    assert out.exit_code != 0, out.output
    assert tumbler in out.output and "--ack-external-embedder" in out.output, out.output
    (only,) = _titled(_TITLE)
    assert str(only.tumbler) == tumbler and only.physical_collection == _VOYAGE["knowledge"], "no twin in the sibling"
    assert only.sensitive is False


def test_cli_with_the_ack_puts_in_place_on_the_voyage_collection(t2_service_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _plain_note_in_voyage()
    seen: list[dict] = []

    def fake_put(**kw):
        seen.append(kw)
        raise Stop

    monkeypatch.setattr("nexus.catalog.note_write.put_note", fake_put)
    CliRunner().invoke(
        main, ["store", "put", "-", "--title", _TITLE, "-c", _VOYAGE["knowledge"], "--sensitive",
               "--ack-external-embedder"], input=_CONTENT)
    assert [s["collection"] for s in seen] == [_VOYAGE["knowledge"]] and seen[0]["sensitive"] is True


def test_cli_still_routes_a_new_title_to_the_sibling(t2_service_env) -> None:
    """Negative control: only a title with a live note in the Voyage collection refuses."""
    _plain_note_in_voyage()
    out = CliRunner().invoke(
        main, ["store", "put", "-", "--title", "rfvyy-a-new-title", "-c", _VOYAGE["knowledge"], "--sensitive"],
        input=_CONTENT)
    assert out.exit_code == 0, out.output
    (entry,) = _titled("rfvyy-a-new-title")
    assert entry.physical_collection == f"knowledge__rfvyy-ext__{BGE}__v1" and entry.sensitive is True


def test_mcp_refuses_the_same_reput(t2_service_env) -> None:
    from nexus.mcp import core

    tumbler = _plain_note_in_voyage("rfvyy-existing-voyage-note-mcp")
    out = str(core.store_put(
        content=_CONTENT, collection=_VOYAGE["knowledge"], title="rfvyy-existing-voyage-note-mcp", sensitive=True))
    assert out.startswith("Error: store_put:") and tumbler in out and "--ack-external-embedder" in out, out
    (only,) = _titled("rfvyy-existing-voyage-note-mcp")
    assert only.physical_collection == _VOYAGE["knowledge"] and only.sensitive is False


def test_an_unreadable_catalog_refuses_instead_of_guessing(t2_service_env, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.sensitive_write as sw

    def down():
        raise ConnectionError("catalog unreachable")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", down)
    with pytest.raises(sw.ExternalEmbedderRefusedError, match="could not check"):
        sw.refuse_external_existing_note(_VOYAGE["knowledge"], _TITLE, ack=False, what="store put --sensitive")
    # A local-embedder collection, the ack, and a blank title never ask the catalog.
    sw.refuse_external_existing_note(f"knowledge__rfvyy-ext__{BGE}__v1", _TITLE, ack=False, what="w")
    sw.refuse_external_existing_note(_VOYAGE["knowledge"], _TITLE, ack=True, what="w")
    sw.refuse_external_existing_note(_VOYAGE["knowledge"], "", ack=False, what="w")
