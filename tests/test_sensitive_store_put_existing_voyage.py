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


def _plain_note_in_voyage(title: str = _TITLE):
    """A live note that an earlier non-sensitive put left in the Voyage collection (the catalog row is what a
    re-put resolves)."""
    from nexus.catalog.factory import make_catalog_writer

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
