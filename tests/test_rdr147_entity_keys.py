# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-147 Step 1 (nexus-fai28): per-collection entity-key patterns, ``entity_key`` on ``store_put``,
and the ``classify_entity`` / ``find_key_tokens`` helpers.

The config and helper tests are pure (a ``.nexus.yml`` in a tmp repo root, read through the real
``load_config``). The ``store_put`` tests run on the engine substrate: a real tenant, the real
catalog and the real vector service, then read the catalog entry's ``meta`` and the stored chunks'
metadata back. An entity key lives in the catalog document's ``meta`` and in no chunk's metadata
(RDR-236).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.aspect_readers import uri_for
from nexus.catalog.factory import make_catalog_reader
from nexus.cli import main
from nexus.config import load_config
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.entity_keys import (
    EntityConfigError,
    classify_entity,
    find_key_tokens,
    pattern_for_collection,
    resolve_put_key,
)
from nexus.mcp.core import store_put

_PATTERN = r"CUST-\d{4}|eid_[0-9a-f]{8}"
_GLOB = "knowledge__herb-*"
_HERB = "knowledge__herb-core__bge-base-en-v15-768__v1"
_CODE = "code__herb-code__bge-base-en-v15-768__v1"


@pytest.fixture()
def repo_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a per-repo ``.nexus.yml`` and point ``load_config`` at it. Returns the writer."""
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("NEXUS_REPO_ROOT", str(root))

    def write(collections: object) -> Path:
        path = root / ".nexus.yml"
        path.write_text(yaml.safe_dump({"entity": {"collections": collections}}))
        return path

    return write


# ── config ──────────────────────────────────────────────────────────────────


def test_the_default_gives_no_collection_a_pattern(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_REPO_ROOT", str(tmp_path))
    assert load_config()["entity"]["collections"] == {}
    assert pattern_for_collection(_HERB) is None


def test_a_glob_returns_its_collections_pattern_and_an_unmatched_collection_none(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    pattern = pattern_for_collection(_HERB)
    assert pattern is not None and pattern.pattern == _PATTERN
    assert pattern_for_collection("knowledge__other-core__bge-base-en-v15-768__v1") is None
    assert pattern_for_collection("docs__herb-core__bge-base-en-v15-768__v1") is None


def test_an_invalid_regex_fails_at_load_naming_the_key(repo_cfg):
    repo_cfg({_GLOB: "CUST-(\\d{4"})
    with pytest.raises(EntityConfigError) as exc:
        load_config()
    assert f'entity.collections["{_GLOB}"]' in str(exc.value)


def test_a_pattern_wide_inline_flag_fails_at_load_naming_the_key(repo_cfg):
    """The token form wraps the pattern in a group, where ``(?i)`` is an error: it fails here, not at
    the first search."""
    repo_cfg({_GLOB: "(?i)cust-\\d{4}"})
    with pytest.raises(EntityConfigError) as exc:
        load_config()
    assert f'entity.collections["{_GLOB}"]' in str(exc.value)


def test_a_collections_value_that_is_no_map_fails_at_load(repo_cfg):
    repo_cfg(["CUST-\\d{4}"])
    with pytest.raises(EntityConfigError, match="entity.collections"):
        load_config()


def test_an_entity_section_that_is_no_map_fails_at_load(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_REPO_ROOT", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text("entity: 5\n")
    with pytest.raises(EntityConfigError, match="entity must be a map"):
        load_config()


@pytest.mark.parametrize("value", [[], "", 0, False], ids=["empty-list", "empty-string", "zero", "false"])
def test_a_falsy_collections_value_that_is_no_map_fails_at_load_naming_key_and_file(repo_cfg, value):
    """``collections: []`` used to load clean (only a truthy value was checked) and then every
    non-code put raised. Anything present and not None must be a map."""
    path = repo_cfg(value)
    with pytest.raises(EntityConfigError, match=r"entity\.collections") as exc:
        load_config()
    assert str(path) in str(exc.value)


def test_a_null_collections_value_is_nothing_configured(repo_cfg):
    repo_cfg(None)
    assert load_config()["entity"]["collections"] == {}


def test_a_bad_regex_names_the_repo_file_it_came_from(repo_cfg):
    path = repo_cfg({_GLOB: "CUST-(\\d{4"})
    with pytest.raises(EntityConfigError) as exc:
        load_config()
    assert str(path) in str(exc.value)


def test_a_bad_regex_in_the_global_file_names_the_global_file_not_the_repo_file(repo_cfg, tmp_path, monkeypatch):
    repo_path = repo_cfg({"knowledge__ok-*": _PATTERN})
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    global_path = cfg_dir / "config.yml"
    global_path.write_text(yaml.safe_dump({"entity": {"collections": {_GLOB: "CUST-(\\d{4"}}}))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    with pytest.raises(EntityConfigError) as exc:
        load_config()
    assert str(global_path) in str(exc.value)
    assert str(repo_path) not in str(exc.value)


def test_the_inline_flag_error_says_how_to_scope_it(repo_cfg):
    repo_cfg({_GLOB: "(?i)cust-\\d{4}"})
    with pytest.raises(EntityConfigError, match=r"\(\?i:\.\.\.\)"):
        load_config()


def test_a_scoped_inline_flag_loads_and_matches_case_insensitively(repo_cfg):
    repo_cfg({_GLOB: "(?i:cust-\\d{4})"})
    assert find_key_tokens("see Cust-0018 and CUST-0042", _HERB) == ["Cust-0018", "CUST-0042"]


def test_an_earlier_layers_matching_glob_wins_over_a_later_layers(repo_cfg, tmp_path, monkeypatch):
    """The merge keeps the global file's globs ahead of the repo file's, and the first match wins: a
    repo glob that differs from the global one does not override it."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(yaml.safe_dump({"entity": {"collections": {_GLOB: "GLOBAL-\\d+"}}}))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
    repo_cfg({"knowledge__herb-core*": "REPO-\\d+"})
    pattern = pattern_for_collection(_HERB)
    assert pattern is not None and pattern.pattern == "GLOBAL-\\d+"
    repo_cfg({_GLOB: "REPO-\\d+"})  # the SAME glob text replaces the pattern in place
    pattern = pattern_for_collection(_HERB)
    assert pattern is not None and pattern.pattern == "REPO-\\d+"


@pytest.mark.parametrize("command", [["config", "get", "entity.collections"], ["config", "list"]])
def test_the_cli_prints_a_bad_entity_config_as_one_line_naming_the_file(repo_cfg, command):
    path = repo_cfg({_GLOB: "CUST-(\\d{4"})
    result = CliRunner().invoke(main, command)
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.output
    assert str(path) in result.output and f'entity.collections["{_GLOB}"]' in result.output


# ── find_key_tokens ─────────────────────────────────────────────────────────


def test_find_key_tokens_is_distinct_first_seen_and_whole_tokens_only(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    text = "Ticket for CUST-0018 and CUST-0018, then CUST-0042; also eid_0a1b2c3d."
    assert find_key_tokens(text, _HERB) == ["CUST-0018", "CUST-0042", "eid_0a1b2c3d"]


def test_a_key_inside_a_longer_token_is_not_a_match(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert find_key_tokens("see CUST-00180 and xCUST-0018 and CUST-0018x", _HERB) == []
    assert find_key_tokens("(CUST-0018)", _HERB) == ["CUST-0018"]


def test_find_key_tokens_is_empty_without_a_pattern(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert find_key_tokens("CUST-0018", "knowledge__other-core__bge-base-en-v15-768__v1") == []
    assert find_key_tokens("", _HERB) == []


def test_a_hyphen_or_dot_neighbour_does_not_hide_a_key(repo_cfg):
    """Token boundaries are \\w-based: ``-`` and ``.`` are not word characters, so a suffix after a
    hyphen leaves the key whole."""
    repo_cfg({_GLOB: _PATTERN})
    assert find_key_tokens("CUST-0018-ARCHIVE", _HERB) == ["CUST-0018"]
    assert find_key_tokens("done: CUST-0042. next", _HERB) == ["CUST-0042"]
    assert find_key_tokens("CUST-0018_ARCHIVE", _HERB) == []


# ── classify_entity ─────────────────────────────────────────────────────────


def _doc(title: str, collection: str = _HERB, **meta):
    return {"title": title, "physical_collection": collection, "meta": meta}


def test_classify_entity_explicit_key_wins_over_the_title(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert classify_entity(_doc("CUST-0018", entity_key="CUST-0099")) == "CUST-0099"
    assert classify_entity(_doc("Customer record", entity_key="CUST-0099")) == "CUST-0099"


def test_classify_entity_stamps_a_full_title_match_and_not_a_partial_one(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert classify_entity(_doc("CUST-0018")) == "CUST-0018"
    assert classify_entity(_doc("eid_0a1b2c3d")) == "eid_0a1b2c3d"
    assert classify_entity(_doc("Customer CUST-0018")) is None
    assert classify_entity(_doc("CUST-00180")) is None
    assert classify_entity(_doc("")) is None


def test_classify_entity_without_a_pattern_or_explicit_key_is_none(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert classify_entity(_doc("CUST-0018", "knowledge__other-core__bge-base-en-v15-768__v1")) is None


def test_classify_entity_reads_a_catalog_entry_shape(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    entry = SimpleNamespace(title="CUST-0018", physical_collection=_HERB, meta={})
    assert classify_entity(entry) == "CUST-0018"


@pytest.mark.parametrize("glob", ["code__*", "*", "*herb*"])
def test_code_collections_have_no_keys_even_when_a_glob_and_the_text_match(repo_cfg, glob):
    repo_cfg({glob: _PATTERN})
    assert pattern_for_collection(_CODE) is None
    assert classify_entity(_doc("CUST-0018", _CODE)) is None
    assert classify_entity(_doc("CUST-0018", _CODE, entity_key="CUST-0018")) is None
    assert find_key_tokens("CUST-0018 CUST-0042", _CODE) == []


def test_resolve_put_key_is_classify_entity_for_a_put(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    assert resolve_put_key(collection=_HERB, title="CUST-0018") == "CUST-0018"
    assert resolve_put_key(collection=_HERB, title="Customer CUST-0018") == ""
    assert resolve_put_key(collection=_HERB, title="x", explicit=" CUST-0042 ") == "CUST-0042"
    assert resolve_put_key(collection=_CODE, title="CUST-0018") == "", "a title never stamps on code__"


def test_resolve_put_key_refuses_an_explicit_key_on_a_code_collection(repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    with pytest.raises(ValueError, match="code"):
        resolve_put_key(collection=_CODE, title="x", explicit="CUST-0018")
    assert resolve_put_key(collection=_CODE, title="CUST-0018") == "", "a title-derived key stays silent"
    assert resolve_put_key(collection=_CODE, title="x", explicit="  ") == "", "a blank key is none given"


@pytest.mark.parametrize("key", ["CUST\n0018", "CUST-0018\n", "\tCUST-0018", "CUST\x00", "CUST\x7f1"])
def test_resolve_put_key_refuses_control_characters(key):
    with pytest.raises(ValueError, match="control"):
        resolve_put_key(collection=_HERB, title="x", explicit=key)


def test_resolve_put_key_refuses_a_key_over_256_characters_and_takes_256():
    with pytest.raises(ValueError, match="256"):
        resolve_put_key(collection=_HERB, title="x", explicit="K" * 257)
    assert resolve_put_key(collection=_HERB, title="x", explicit="K" * 256) == "K" * 256


def test_the_mcp_description_and_the_cli_help_state_the_reput_and_code_rules():
    import typing  # noqa: PLC0415 — only this pin reads annotations

    from nexus.commands.store import put_cmd  # noqa: PLC0415 — the CLI half of the same pin

    described = typing.get_type_hints(store_put, include_extras=True)["entity_key"].__metadata__[0].description
    helped = next(o.help for o in put_cmd.params if o.name == "entity_key")
    for text in (described, helped):
        assert "re-put without it removes" in text.lower(), text
        assert "code__" in text and "256" in text, text


# ── store_put on the engine substrate ───────────────────────────────────────


def _entry_meta(collection: str, title: str) -> dict:
    reader = make_catalog_reader()
    assert reader is not None
    doc = reader.by_source_uri(uri_for(collection, title))
    assert doc is not None, f"{title!r} was never registered"
    return dict(doc.meta or {})


def _chunk_metadatas(client: HttpVectorClient, collection: str, content: str) -> list[dict]:
    chash = hashlib.sha256(content.encode()).hexdigest()
    got = client.get_collection(collection).get(ids=[chash], include=["metadatas"], sensitivity="include")
    return list(got["metadatas"])


@pytest.mark.t2_tenant
def test_store_put_entity_key_lands_in_catalog_meta_and_in_no_chunk(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "herb-explicit"
    body = "rdr147 explicit entity key body: the customer record"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        out = store_put(content=body, collection=subject, title="Customer record",
                        entity_key="CUST-0018")
    assert out.startswith("Stored:"), out

    collection = t3_collection_name(subject, t3=client)
    assert _entry_meta(collection, "Customer record")["entity_key"] == "CUST-0018"
    chunks = _chunk_metadatas(client, collection, body)
    assert chunks, "the chunk was never stored: the assertion below would be vacuous"
    assert all("entity_key" not in m for m in chunks)


@pytest.mark.t2_tenant
def test_a_configured_collection_stamps_a_full_title_match_and_not_a_partial_one(t2_service_env, repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "herb-auto"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content="rdr147 auto body one", collection=subject, title="CUST-0018")
        store_put(content="rdr147 auto body two", collection=subject, title="Customer CUST-0018")

    collection = t3_collection_name(subject, t3=client)
    assert collection.startswith("knowledge__herb-"), collection
    assert _entry_meta(collection, "CUST-0018")["entity_key"] == "CUST-0018"
    assert "entity_key" not in _entry_meta(collection, "Customer CUST-0018")
    assert all("entity_key" not in m for m in _chunk_metadatas(client, collection, "rdr147 auto body one"))


@pytest.mark.t2_tenant
def test_an_unconfigured_collection_stamps_nothing(t2_service_env, repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "other-auto"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content="rdr147 unconfigured body", collection=subject, title="CUST-0018")
    collection = t3_collection_name(subject, t3=client)
    assert "entity_key" not in _entry_meta(collection, "CUST-0018")


@pytest.mark.t2_tenant
def test_a_reput_replaces_the_key_as_it_does_the_other_note_keys(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "herb-reput"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content="rdr147 reput body one", collection=subject, title="doc", entity_key="CUST-0018")
        collection = t3_collection_name(subject, t3=client)
        assert _entry_meta(collection, "doc")["entity_key"] == "CUST-0018"
        store_put(content="rdr147 reput body two", collection=subject, title="doc", entity_key="CUST-0042")
        assert _entry_meta(collection, "doc")["entity_key"] == "CUST-0042"
        store_put(content="rdr147 reput body three", collection=subject, title="doc")
        assert not _entry_meta(collection, "doc").get("entity_key"), "a re-put with no key retracts it"


@pytest.mark.t2_tenant
def test_the_cli_and_mcp_puts_write_the_same_catalog_meta(t2_service_env, tmp_path, repo_cfg):
    repo_cfg({_GLOB: _PATTERN})
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "herb-parity"
    f = tmp_path / "doc.md"
    f.write_text("rdr147 parity body via the cli")
    with patch("nexus.mcp.core._get_t3", return_value=client), patch("nexus.commands.store._t3", return_value=client):
        cli = CliRunner().invoke(main, [
            "store", "put", str(f), "--collection", subject, "--title", "Cli record",
            "--entity-key", "CUST-0018"])
        assert cli.exit_code == 0, cli.output
        store_put(content="rdr147 parity body via mcp", collection=subject, title="Mcp record",
                  entity_key="CUST-0018")
        auto = CliRunner().invoke(main, [
            "store", "put", str(f), "--collection", subject, "--title", "CUST-0042"])
        assert auto.exit_code == 0, auto.output
        store_put(content="rdr147 parity body via mcp two", collection=subject, title="CUST-0043")

    collection = t3_collection_name(subject, t3=client)
    assert _entry_meta(collection, "Cli record")["entity_key"] == "CUST-0018"
    assert _entry_meta(collection, "Mcp record")["entity_key"] == "CUST-0018"
    assert _entry_meta(collection, "CUST-0042")["entity_key"] == "CUST-0042"
    assert _entry_meta(collection, "CUST-0043")["entity_key"] == "CUST-0043"
