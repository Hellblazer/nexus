# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8o7ae F4: ``nx store import -c <bare subject>`` resolves the subject
the way every other store verb does.

Shakeout 7.64.1 Surface F F4 (T2 nexus/shakeout-7.64.1-local-driver-2026-09-28):
the import handed the raw ``-c`` value to the exporter, which derived the
expected model from the unresolved name and refused with "target collection
'shakeout-imported' requires 'voyage-code-3'" on a bge install. The
consequence tested here: after importing with a bare subject, ``nx store
list -c <subject>`` (a different verb, same bare subject) sees the note.
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
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.integration


def test_import_into_bare_subject_lands_where_other_verbs_read(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    writer = make_catalog_writer(priority="interactive")
    owner = writer.register_owner("knowledge", "curator")

    # The source uses the same model token a bare subject resolves to on
    # this install, so the model gate passes only if -c is resolved.
    subject = "o7ae-imported"
    resolved = t3_collection_name(subject, t3=client, for_write=True)
    model_token = resolved.split("__")[2]
    src = f"knowledge__o7ae-src__{model_token}__v1"

    title, body = "o7ae note", "o7ae note body for the bare subject import"
    source_uri = uri_for(src, title)
    assert source_uri is not None
    tumbler = str(writer.register(
        owner=owner, title=title, content_type="knowledge",
        physical_collection=src, source_uri=source_uri,
    ))
    chash = hashlib.sha256(body.encode()).hexdigest()
    seed_chunks_direct(
        src, ids=[chash], documents=[body], embed=True,
        metadatas=[{"title": title, "chunk_text_hash": chash,
                    "indexed_at": datetime.now(UTC).isoformat()}],
    )
    writer.write_manifest(tumbler, [{"chash": chash, "position": 0}], collection=src)

    out = tmp_path / "o7ae.nxexp"
    export_collection(db=client, collection_name=src, output_path=out)

    runner = CliRunner()
    with patch("nexus.commands.store._t3", return_value=client):
        imported = runner.invoke(main, ["store", "import", str(out), "-c", subject])
        listed = runner.invoke(main, ["store", "list", "-c", subject])

    assert imported.exit_code == 0, imported.output
    assert f"into {resolved}" in imported.output, imported.output
    assert listed.exit_code == 0, listed.output
    assert title in listed.output, listed.output
    assert chash in client.get_collection(resolved).get(ids=[chash], include=[])["ids"]


def test_keyless_voyage_install_gets_the_remedy_not_a_traceback(t2_service_env, tmp_path, monkeypatch):
    """The resolve runs before the import's own error handling. On a local
    install whose embed model is voyage-shaped with no key, a new bare
    subject raises LocalVoyageCredentialMissingError; the operator must see
    its message and a clean exit, as ``nx store put`` gives (7.38.0)."""
    import nexus.config as _config_mod

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-context-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    real_get_credential = _config_mod.get_credential
    monkeypatch.setattr(
        "nexus.config.get_credential",
        lambda name: "" if name == "voyage_api_key" else real_get_credential(name),
    )
    f = tmp_path / "unused.nxexp"
    f.write_bytes(b"{}\n")

    client = HttpVectorClient(tenant=t2_service_env)
    with patch("nexus.commands.store._t3", return_value=client):
        result = CliRunner().invoke(main, ["store", "import", str(f), "-c", "o7ae-keyless-new"])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "voyage" in result.output.lower(), result.output
