# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "MXBAI_API_KEY",
                "CHROMA_TENANT", "CHROMA_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _config_path(fake_home: Path) -> Path:
    return fake_home / ".config" / "nexus" / "config.yml"


def _read_config(fake_home: Path) -> dict:
    return yaml.safe_load(_config_path(fake_home).read_text())


def _write_config(fake_home: Path, data: dict) -> None:
    p = _config_path(fake_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(data))


# ── nx config set ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("args,section,key,expected", [
    (["chroma_api_key=abc123"], "credentials", "chroma_api_key", "abc123"),
    (["voyage_api_key", "vk-space"], "credentials", "voyage_api_key", "vk-space"),
    (["pdf.extractor=mineru"], "pdf", "extractor", "mineru"),
    (["pdf.extractor", "mineru"], "pdf", "extractor", "mineru"),
])
def test_config_set_writes(runner, fake_home, args, section, key, expected) -> None:
    result = runner.invoke(main, ["config", "set", *args])
    assert result.exit_code == 0, result.output
    assert _read_config(fake_home)[section][key] == expected


def test_config_set_creates_dir(runner, fake_home) -> None:
    assert not _config_path(fake_home).parent.exists()
    runner.invoke(main, ["config", "set", "voyage_api_key=vk-test"])
    assert _config_path(fake_home).parent.exists()


def test_config_set_preserves_existing(runner, fake_home) -> None:
    _write_config(fake_home, {"server": {"port": 9999}})
    runner.invoke(main, ["config", "set", "chroma_api_key=x"])
    data = _read_config(fake_home)
    assert data["server"]["port"] == 9999
    assert data["credentials"]["chroma_api_key"] == "x"


def test_config_set_updates_existing(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "chroma_api_key=first"])
    runner.invoke(main, ["config", "set", "chroma_api_key=second"])
    assert _read_config(fake_home)["credentials"]["chroma_api_key"] == "second"


def test_config_set_dotted_preserves_existing(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "chroma_api_key=abc"])
    runner.invoke(main, ["config", "set", "pdf.extractor=docling"])
    data = _read_config(fake_home)
    assert data["credentials"]["chroma_api_key"] == "abc"
    assert data["pdf"]["extractor"] == "docling"


def test_config_set_dotted_overwrites(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "pdf.extractor=mineru"])
    runner.invoke(main, ["config", "set", "pdf.extractor=auto"])
    assert _read_config(fake_home)["pdf"]["extractor"] == "auto"


# ── get_pdf_extractor ───────────────────────────────────────────────────────


@pytest.mark.parametrize("config_data,repo_root,expected", [
    (None, None, "auto"),
    ({"pdf": {"extractor": "mineru"}}, None, "mineru"),
    ({"pdf": {"extractor": "bogus"}}, None, "auto"),
])
def test_get_pdf_extractor(fake_home, config_data, repo_root, expected) -> None:
    from nexus.config import get_pdf_extractor
    if config_data:
        _write_config(fake_home, config_data)
    assert get_pdf_extractor() == expected


def test_get_pdf_extractor_repo_overrides_global(fake_home, tmp_path) -> None:
    from nexus.config import get_pdf_extractor
    _write_config(fake_home, {"pdf": {"extractor": "auto"}})
    (tmp_path / ".nexus.yml").write_text(yaml.dump({"pdf": {"extractor": "mineru"}}))
    assert get_pdf_extractor(repo_root=tmp_path) == "mineru"


# ── CLI --extractor reads config default ────────────────────────────────────


@pytest.mark.parametrize("extra_args,expected_extractor", [
    ([], "docling"),
    (["--extractor", "mineru"], "mineru"),
])
def test_index_pdf_extractor(runner, fake_home, extra_args, expected_extractor) -> None:
    _write_config(fake_home, {"pdf": {"extractor": "docling"}})
    pdf = fake_home / "test.pdf"
    pdf.write_bytes(b"dummy")
    with patch("nexus.doc_indexer.index_pdf",
               return_value={"chunks": 1, "pages": [], "title": "", "author": ""}) as mock_index:
        result = runner.invoke(main, ["index", "pdf", str(pdf), *extra_args])
    assert result.exit_code == 0, result.output
    _, kwargs = mock_index.call_args
    assert kwargs["extractor"] == expected_extractor


# ── nx config get ───────────────────────────────────────────────────────────


def test_config_get_returns_value(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "voyage_api_key=vk-123"])
    result = runner.invoke(main, ["config", "get", "--show", "voyage_api_key"])
    assert result.exit_code == 0, result.output
    assert "vk-123" in result.output


def test_config_get_masks_by_default(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "voyage_api_key=secret-voyage-key-xyz"])
    result = runner.invoke(main, ["config", "get", "voyage_api_key"])
    assert result.exit_code == 0
    assert "secret-voyage-key-xyz" not in result.output
    assert "***" in result.output


def test_config_get_prefers_env_var(runner, fake_home, monkeypatch) -> None:
    runner.invoke(main, ["config", "set", "chroma_api_key=file-val"])
    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    result = runner.invoke(main, ["config", "get", "--show", "chroma_api_key"])
    assert "env-val" in result.output


@pytest.mark.parametrize("key", ["chroma_api_key", "pdf.nonexistent_key", "ghost.setting"])
def test_config_get_missing_reports_not_set(runner, fake_home, key) -> None:
    result = runner.invoke(main, ["config", "get", key])
    assert result.exit_code == 0
    assert "not set" in result.output.lower()


def test_config_get_dotted_key_returns_value(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "pdf.mineru_server_url=http://10.0.0.1:9000"])
    result = runner.invoke(main, ["config", "get", "pdf.mineru_server_url"])
    assert result.exit_code == 0, result.output
    assert "http://10.0.0.1:9000" in result.output


def test_config_get_dotted_key_returns_default(runner, fake_home) -> None:
    result = runner.invoke(main, ["config", "get", "pdf.mineru_server_url"])
    assert result.exit_code == 0
    assert "127.0.0.1" in result.output


def test_config_get_dotted_show_not_masked(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "pdf.extractor=docling"])
    result = runner.invoke(main, ["config", "get", "--show", "pdf.extractor"])
    assert result.exit_code == 0
    assert "docling" in result.output


# ── nx config list ──────────────────────────────────────────────────────────


def test_config_list_masks_values(runner, fake_home) -> None:
    runner.invoke(main, ["config", "set", "chroma_api_key=super-secret"])
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0
    assert "super-secret" not in result.output
    assert "chroma_api_key" in result.output


def test_config_list_shows_credential_names(runner, fake_home) -> None:
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0
    assert "chroma_api_key" in result.output
    assert "voyage_api_key" in result.output


def test_config_list_shows_non_secret_settings(runner, fake_home) -> None:
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0
    assert "indexing" in result.output.lower()


# ── nx config init ──────────────────────────────────────────────────────────


def test_config_init_writes_provided_values(runner, fake_home) -> None:
    result = runner.invoke(main, ["config", "init"], input="chroma-key\nmy-db\nvoyage-key\n")
    assert result.exit_code == 0, result.output
    creds = _read_config(fake_home).get("credentials", {})
    assert creds.get("chroma_api_key") == "chroma-key"
    assert creds.get("chroma_database") == "my-db"
    assert creds.get("voyage_api_key") == "voyage-key"


def test_config_init_provisions_databases_success(runner, fake_home) -> None:
    mock_admin = MagicMock()
    with patch("nexus.commands._provision._cloud_admin_client", return_value=mock_admin), \
         patch("nexus.commands._provision.ensure_databases", return_value={"mydb": True}):
        result = runner.invoke(main, ["config", "init"], input="ck-key\nmydb\nvoyage-key\n")
    assert result.exit_code == 0, result.output
    assert "mydb" in result.output
    assert "created" in result.output


def test_config_init_provisioning_failure_is_warning(runner, fake_home) -> None:
    with patch("nexus.commands._provision._cloud_admin_client",
               side_effect=Exception("network timeout")):
        result = runner.invoke(main, ["config", "init"], input="ck-key\nmydb\nvoyage-key\n")
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower() or "could not" in result.output.lower()


def test_config_init_shows_signup_urls(runner, fake_home) -> None:
    result = runner.invoke(main, ["config", "init"], input="\n\n\n")
    output = result.output
    assert "trychroma.com" in output or "chromadb" in output.lower()
    assert "voyageai.com" in output or "voyage" in output.lower()


def test_config_init_skips_keys_already_in_env(runner, fake_home, monkeypatch) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "env-chroma")
    result = runner.invoke(main, ["config", "init"], input="\n\n\n")
    assert "env-chroma" in result.output or "already set" in result.output.lower() or "environment" in result.output.lower()


# ── get_credential() fallback ───────────────────────────────────────────────


def test_get_credential_env_takes_precedence(fake_home, monkeypatch) -> None:
    from nexus.config import get_credential
    _write_config(fake_home, {"credentials": {"chroma_api_key": "file-val"}})
    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    assert get_credential("chroma_api_key") == "env-val"


def test_get_credential_falls_back_to_file(fake_home) -> None:
    from nexus.config import get_credential
    _write_config(fake_home, {"credentials": {"voyage_api_key": "vk-file"}})
    assert get_credential("voyage_api_key") == "vk-file"


def test_get_credential_returns_empty_when_unset(fake_home) -> None:
    from nexus.config import get_credential
    assert get_credential("chroma_api_key") == ""
