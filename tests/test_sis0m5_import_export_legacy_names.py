# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sis0m.5: ``nx store import``/``export`` resolve a legacy two-segment
``-c`` name the way every other store verb does.

After nexus-8o7ae only a bare subject went through ``t3_collection_name``;
anything containing ``__`` passed raw. A two-segment name then reached the
exporter's model gate unresolved, where the model is guessed from the prefix
(a Voyage model for ``knowledge``), and a bge install was refused; export of
a two-segment name looked for a collection by that literal name. ``put``,
``list``, ``get`` and ``delete`` all grandfather an existing legacy name or
promote a missing one to the conformant form.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_writer
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.exporter import export_collection

pytestmark = pytest.mark.integration


def _seed(client, src: str, title: str, body: str) -> str:
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")
    source_uri = uri_for(src, title)
    assert source_uri is not None
    tumbler = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=src, source_uri=source_uri,
    ))
    chash = hashlib.sha256(body.encode()).hexdigest()
    client.upsert_chunks_with_embeddings(
        src, ids=[chash], documents=[body], embeddings=[],
        metadatas=[{"title": title, "chunk_text_hash": chash,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=src)
    return chash


def test_import_into_a_two_segment_name_lands_where_other_verbs_read(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    legacy = "knowledge__sis0m5-imported"
    resolved = t3_collection_name(legacy, t3=client, for_write=True)
    assert resolved != legacy  # the condition: a name that must be promoted
    src = f"knowledge__sis0m5-src__{resolved.split('__')[2]}__v1"
    chash = _seed(client, src, "sis0m5 import note", "sis0m5 two-segment import body")
    out = tmp_path / "sis0m5.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    runner = CliRunner()
    with patch("nexus.commands.store._t3", return_value=client):
        imported = runner.invoke(main, ["store", "import", str(out), "-c", legacy])
        listed = runner.invoke(main, ["store", "list", "-c", legacy])

    assert imported.exit_code == 0, imported.output
    assert f"into {resolved}" in imported.output, imported.output
    assert listed.exit_code == 0 and "sis0m5 import note" in listed.output, listed.output
    assert chash in client.get_collection(resolved).get(ids=[chash], include=[])["ids"]


def test_export_of_a_two_segment_name_finds_the_conformant_collection(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    legacy = "knowledge__sis0m5-exported"
    resolved = t3_collection_name(legacy, t3=client)
    assert resolved != legacy
    _seed(client, resolved, "sis0m5 export note", "sis0m5 two-segment export body")
    out = tmp_path / "exported.nxexp"

    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "export", legacy, "-o", str(out)])

    assert result.exit_code == 0, result.output
    assert resolved in result.output, result.output
    assert out.exists() and out.stat().st_size > 0


class _FakeT3:
    def __init__(self, existing: set[str]) -> None:
        self._existing = existing

    def collection_exists(self, name: str) -> bool:
        return name in self._existing


@pytest.mark.parametrize("exists", [True, False], ids=["restore-existing", "new-mint"])
def test_placeholder_legacy_name_is_a_restore_only_when_it_exists(exists) -> None:
    """``knowledge__knowledge`` is a placeholder subject. Importing into it
    is refused as a new mint, but into an existing legacy collection of that
    exact name it is a restore, which passed raw before and must keep
    working."""
    from nexus.commands.store import _resolve_bare_subject

    t3 = _FakeT3({"knowledge__knowledge"} if exists else set())
    if exists:
        assert _resolve_bare_subject("knowledge__knowledge", t3=t3, for_write=True) == "knowledge__knowledge"
    else:
        with pytest.raises(Exception, match="(?i)placeholder"):
            _resolve_bare_subject("knowledge__knowledge", t3=t3, for_write=True)



def test_export_of_a_two_segment_name_prefers_the_conformant_collection_when_both_exist() -> None:
    """With both a legacy collection and its conformant counterpart present,
    a two-segment name reaches the conformant one, as for put/list/get/delete
    (nexus-hmxi). The export echoes the resolved name."""
    from nexus.commands.store import _resolve_bare_subject

    legacy = "knowledge__sis0m5-both"
    conformant = t3_collection_name(legacy)
    t3 = _FakeT3({legacy, conformant})
    assert _resolve_bare_subject(legacy, t3=t3) == conformant
