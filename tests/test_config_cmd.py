"""AC: nx config set/get/list/init — credential and config management."""
import json
from pathlib import Path

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
    # Remove any real API keys from env so config-file fallback is tested
    for key in ("CHROMA_API_KEY", "VOYAGE_API_KEY", "MXBAI_API_KEY",
                "CHROMA_TENANT", "CHROMA_DATABASE"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


# ── nx config set ─────────────────────────────────────────────────────────────

def test_config_set_writes_credential(runner: CliRunner, fake_home: Path) -> None:
    """nx config set chroma_api_key=abc writes to ~/.config/nexus/config.yml."""
    result = runner.invoke(main, ["config", "set", "chroma_api_key=abc123"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    assert config_path.exists()
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["chroma_api_key"] == "abc123"


def test_config_set_creates_config_dir(runner: CliRunner, fake_home: Path) -> None:
    """nx config set creates ~/.config/nexus/ directory if missing."""
    config_dir = fake_home / ".config" / "nexus"
    assert not config_dir.exists()

    runner.invoke(main, ["config", "set", "voyage_api_key=vk-test"])

    assert config_dir.exists()


def test_config_set_preserves_existing_settings(runner: CliRunner, fake_home: Path) -> None:
    """nx config set preserves pre-existing config keys."""
    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"server": {"port": 9999}}))

    runner.invoke(main, ["config", "set", "chroma_api_key=x"])

    data = yaml.safe_load(config_path.read_text())
    assert data["server"]["port"] == 9999
    assert data["credentials"]["chroma_api_key"] == "x"


def test_config_set_updates_existing_credential(runner: CliRunner, fake_home: Path) -> None:
    """nx config set overwrites an existing credential value."""
    runner.invoke(main, ["config", "set", "chroma_api_key=first"])
    runner.invoke(main, ["config", "set", "chroma_api_key=second"])

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["chroma_api_key"] == "second"


def test_config_set_accepts_space_separated_key_value(runner: CliRunner, fake_home: Path) -> None:
    """nx config set KEY VALUE (space-separated) also works."""
    result = runner.invoke(main, ["config", "set", "voyage_api_key", "vk-space"])
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    data = yaml.safe_load(config_path.read_text())
    assert data["credentials"]["voyage_api_key"] == "vk-space"


# ── nx config get ─────────────────────────────────────────────────────────────

def test_config_get_returns_value_from_file(runner: CliRunner, fake_home: Path) -> None:
    """nx config get --show prints the full credential value from config.yml."""
    runner.invoke(main, ["config", "set", "voyage_api_key=vk-123"])
    result = runner.invoke(main, ["config", "get", "--show", "voyage_api_key"])
    assert result.exit_code == 0, result.output
    assert "vk-123" in result.output


def test_config_get_masks_by_default(runner: CliRunner, fake_home: Path) -> None:
    """nx config get masks the credential value by default."""
    runner.invoke(main, ["config", "set", "voyage_api_key=secret-voyage-key-xyz"])
    result = runner.invoke(main, ["config", "get", "voyage_api_key"])
    assert result.exit_code == 0, result.output
    assert "secret-voyage-key-xyz" not in result.output
    assert "***" in result.output


def test_config_get_prefers_env_var(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx config get --show returns env var value when both env and file set."""
    runner.invoke(main, ["config", "set", "chroma_api_key=file-val"])
    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    result = runner.invoke(main, ["config", "get", "--show", "chroma_api_key"])
    assert "env-val" in result.output


def test_config_get_missing_key_reports_not_set(runner: CliRunner, fake_home: Path) -> None:
    """nx config get reports 'not set' for unset credential."""
    result = runner.invoke(main, ["config", "get", "chroma_api_key"])
    assert result.exit_code == 0
    assert "not set" in result.output.lower()


# ── nx config list ────────────────────────────────────────────────────────────

def test_config_list_masks_api_key_values(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows masked value (not plaintext) for API keys."""
    runner.invoke(main, ["config", "set", "chroma_api_key=super-secret"])
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "super-secret" not in result.output
    assert "chroma_api_key" in result.output


def test_config_list_shows_credential_names(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows all known credential names."""
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    assert "chroma_api_key" in result.output
    assert "voyage_api_key" in result.output
    assert "mxbai_api_key" in result.output


def test_config_list_shows_non_secret_settings(runner: CliRunner, fake_home: Path) -> None:
    """nx config list shows server and other non-secret settings."""
    result = runner.invoke(main, ["config", "list"])
    assert result.exit_code == 0, result.output
    # Server port should be visible
    assert "port" in result.output or "server" in result.output.lower()


# ── nx config init ────────────────────────────────────────────────────────────

def test_config_init_writes_provided_values(runner: CliRunner, fake_home: Path) -> None:
    """nx config init with all inputs writes all credentials to config.yml."""
    # Simulate interactive input: provide values for each prompt, then empty to skip mxbai
    inputs = "chroma-key\nmy-tenant\nmy-db\nvoyage-key\n\n"
    result = runner.invoke(main, ["config", "init"], input=inputs)
    assert result.exit_code == 0, result.output

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    assert config_path.exists()
    data = yaml.safe_load(config_path.read_text())
    creds = data.get("credentials", {})
    assert creds.get("chroma_api_key") == "chroma-key"
    assert creds.get("voyage_api_key") == "voyage-key"


def test_config_init_shows_signup_urls(runner: CliRunner, fake_home: Path) -> None:
    """nx config init output includes URLs to obtain the required keys."""
    result = runner.invoke(main, ["config", "init"], input="\n\n\n\n\n\n")
    output = result.output
    assert "trychroma.com" in output or "chromadb" in output.lower()
    assert "voyageai.com" in output or "voyage" in output.lower()


def test_config_init_skips_keys_already_in_env(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx config init does not prompt for keys already set in environment."""
    monkeypatch.setenv("CHROMA_API_KEY", "env-chroma")
    result = runner.invoke(main, ["config", "init"], input="\n\n\n\n\n\n")
    # Should mention the key is already set via env, not prompt to re-enter
    assert "env-chroma" in result.output or "already set" in result.output.lower() or "environment" in result.output.lower()


# ── get_credential() fallback ────────────────────────────────────────────────

def test_get_credential_env_var_takes_precedence(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_credential returns env var over config file value."""
    from nexus.config import get_credential

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"credentials": {"chroma_api_key": "file-val"}}))

    monkeypatch.setenv("CHROMA_API_KEY", "env-val")
    assert get_credential("chroma_api_key") == "env-val"


def test_get_credential_falls_back_to_config_file(fake_home: Path) -> None:
    """get_credential returns config file value when env var is absent."""
    from nexus.config import get_credential

    config_path = fake_home / ".config" / "nexus" / "config.yml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump({"credentials": {"voyage_api_key": "vk-file"}}))

    assert get_credential("voyage_api_key") == "vk-file"


def test_get_credential_returns_empty_when_unset(fake_home: Path) -> None:
    """get_credential returns empty string when neither env nor file set."""
    from nexus.config import get_credential

    assert get_credential("chroma_api_key") == ""
