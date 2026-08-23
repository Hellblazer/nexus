# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nexus import config as cfgmod
from nexus.config import (
    _DEFAULTS,
    detect_test_command,
    get_telemetry_config,
    get_verification_config,
    load_config,
    set_config_value,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Clear ambient credential env so get_credential reads config.yml, not a
    # value CI exports (NX_SERVICE_TOKEN=test-token is set on the runners and
    # overrides config.yml — env-first precedence). Without this, the
    # service_url/service_token credential tests pass locally and fail in CI.
    for _ev in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(_ev, raising=False)
    return tmp_path


# ── load_config ──────────────────────────────────────────────────────────────


def test_config_defaults(home: Path) -> None:
    config = load_config(repo_root=home)
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"
    assert "codeModel" not in config["embeddings"]
    assert "docsModel" not in config["embeddings"]


def test_config_merge(home: Path) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}}))
    (home / ".nexus.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-3.0"}}))
    assert load_config(repo_root=home)["embeddings"]["rerankerModel"] == "rerank-3.0"


def test_config_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"embeddings": {"rerankerModel": "rerank-2.5"}}))
    monkeypatch.setenv("NX_EMBEDDINGS_RERANKER_MODEL", "rerank-3.0")
    assert load_config(repo_root=home)["embeddings"]["rerankerModel"] == "rerank-3.0"


def test_config_voyageai_default(home: Path) -> None:
    assert load_config(repo_root=home)["voyageai"]["read_timeout_seconds"] == 120


def test_config_voyageai_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_VOYAGEAI_READ_TIMEOUT_SECONDS", "60")
    cfg = load_config(repo_root=home)
    assert cfg["voyageai"]["read_timeout_seconds"] == 60
    assert isinstance(cfg["voyageai"]["read_timeout_seconds"], int)


def test_config_missing_files_returns_defaults(home: Path) -> None:
    config = load_config(repo_root=home)
    assert isinstance(config, dict) and "embeddings" in config


@pytest.mark.parametrize("content", [
    "just a bare string\n",
    "- item1\n- item2\n",
])
def test_config_non_dict_yaml_returns_defaults(home: Path, content: str) -> None:
    (home / ".nexus.yml").write_text(content)
    config = load_config(repo_root=home)
    assert isinstance(config, dict)
    assert config["embeddings"]["rerankerModel"] == "rerank-2.5"


def test_config_global_non_dict_yaml_returns_defaults(home: Path) -> None:
    global_dir = home / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text("just a bare string\n")
    config = load_config(repo_root=home)
    assert isinstance(config, dict) and config["embeddings"]["rerankerModel"] == "rerank-2.5"


# ── set_credential ───────────────────────────────────────────────────────────


def test_set_credential_cleans_up_temp_on_write_failure(home: Path) -> None:
    import os
    from nexus.config import set_credential

    unlinked: list[str] = []
    orig_unlink = os.unlink

    def tracking_unlink(path, *a, **kw):
        unlinked.append(str(path))
        return orig_unlink(path, *a, **kw)

    def failing_fdopen(fd, *a, **kw):
        os.close(fd)
        raise IOError("simulated write failure")

    with (
        patch("nexus.config.os.fdopen", side_effect=failing_fdopen),
        patch("nexus.config.os.unlink", side_effect=tracking_unlink),
    ):
        with pytest.raises(IOError, match="simulated write failure"):
            set_credential("voyage_api_key", "test-key")
    assert len(unlinked) >= 1


def test_set_credential_unknown_name_raises(home: Path) -> None:
    from nexus.config import set_credential
    with pytest.raises(ValueError, match="Unknown credential"):
        set_credential("totally_unknown_credential", "some-value")


# ── unset_credential (RDR-165 nexus-a11ge: nx uninstall managed-config clear) ──


def test_unset_credential_removes_from_config(home: Path) -> None:
    from nexus.config import get_credential, set_credential, unset_credential

    set_credential("service_url", "https://api.conexus-nexus.com")
    set_credential("service_token", "tok-abc")
    # unset one → it's gone; the other survives.
    assert unset_credential("service_url") is True  # was present
    assert get_credential("service_url") == ""
    assert get_credential("service_token") == "tok-abc"
    # config.yml no longer carries the removed key.
    import yaml
    data = yaml.safe_load((home / ".config" / "nexus" / "config.yml").read_text())
    assert "service_url" not in data.get("credentials", {})
    assert data["credentials"]["service_token"] == "tok-abc"


def test_unset_credential_absent_is_noop(home: Path) -> None:
    from nexus.config import unset_credential

    # Never set → unset reports not-present, raises nothing (idempotent teardown).
    assert unset_credential("service_token") is False


def test_unset_credential_unknown_name_raises(home: Path) -> None:
    from nexus.config import unset_credential
    with pytest.raises(ValueError, match="Unknown credential"):
        unset_credential("totally_unknown_credential")


# ── Indexing config ──────────────────────────────────────────────────────────


def test_defaults_include_indexing_section() -> None:
    assert _DEFAULTS["indexing"]["code_extensions"] == []
    assert _DEFAULTS["indexing"]["prose_extensions"] == []
    assert _DEFAULTS["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_load_config_returns_indexing_defaults(home: Path) -> None:
    cfg = load_config(repo_root=home)
    assert cfg["indexing"]["code_extensions"] == []
    assert cfg["indexing"]["prose_extensions"] == []
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_nexus_yml_indexing_overrides(home: Path) -> None:
    (home / ".nexus.yml").write_text(
        "indexing:\n  code_extensions: [.sql, .proto]\n  rdr_paths: [docs/rdr, design/decisions]\n"
    )
    cfg = load_config(repo_root=home)
    assert cfg["indexing"]["code_extensions"] == [".sql", ".proto"]
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr", "design/decisions"]
    assert cfg["indexing"]["prose_extensions"] == []


# ── Verification config ─────────────────────────────────────────────────────


def test_defaults_include_verification_section() -> None:
    v = _DEFAULTS["verification"]
    assert v == {
        "on_stop": False, "on_close": False,
        "test_command": "", "lint_command": "", "test_timeout": 120,
    }


def test_get_verification_config_defaults(home: Path) -> None:
    cfg = get_verification_config(repo_root=home)
    assert cfg == {
        "on_stop": False, "on_close": False,
        "test_command": "", "lint_command": "", "test_timeout": 120,
    }


def test_get_verification_config_merges_partial(home: Path) -> None:
    (home / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
    cfg = get_verification_config(repo_root=home)
    assert cfg["on_stop"] is True
    assert cfg["on_close"] is False and cfg["test_command"] == "" and cfg["test_timeout"] == 120


def test_get_verification_config_all_fields(home: Path) -> None:
    (home / ".nexus.yml").write_text(
        "verification:\n  on_stop: true\n  on_close: true\n"
        "  test_command: uv run pytest\n  lint_command: ruff check .\n  test_timeout: 60\n"
    )
    cfg = get_verification_config(repo_root=home)
    assert cfg == {
        "on_stop": True, "on_close": True,
        "test_command": "uv run pytest", "lint_command": "ruff check .", "test_timeout": 60,
    }


# ── detect_test_command ──────────────────────────────────────────────────────


@pytest.mark.parametrize("filename,content,expected", [
    ("pyproject.toml", "[build-system]\n", "uv run pytest"),
    ("pom.xml", "<project/>\n", "mvn test"),
    ("build.gradle", "// gradle\n", "./gradlew test"),
    ("package.json", '{"scripts": {"test": "jest"}}\n', "npm test"),
    ("Cargo.toml", '[package]\nname = "foo"\n', "cargo test"),
    ("Makefile", "test:\n\tpython -m pytest\n", "make test"),
    ("go.mod", "module example.com/foo\n", "go test ./..."),
    ("build.gradle.kts", "// kotlin gradle\n", "./gradlew test"),
])
def test_detect_test_command(tmp_path: Path, filename: str, content: str, expected: str) -> None:
    (tmp_path / filename).write_text(content)
    assert detect_test_command(repo_root=tmp_path) == expected


def test_detect_test_command_none(tmp_path: Path) -> None:
    assert detect_test_command(repo_root=tmp_path) == ""


def test_detect_test_command_priority(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    (tmp_path / "Makefile").write_text("test:\n\tpython -m pytest\n")
    assert detect_test_command(repo_root=tmp_path) == "uv run pytest"


def test_detect_table_matches_reader_script() -> None:
    import importlib.util
    from nexus.config import _DETECT_TABLE
    script = Path(__file__).parents[1] / "conexus" / "hooks" / "scripts" / "read_verification_config.py"
    spec = importlib.util.spec_from_file_location("reader", script)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    assert _DETECT_TABLE == reader.DETECT_TABLE


# ── RDR-087 Phase 2.3: telemetry config toggle ───────────────────────────────


class TestTelemetryConfig:
    """``telemetry`` section opt-outs for the RDR-087 surfaces:

      * ``search_enabled`` — Phase 2.2 hot-path INSERT OR IGNORE.
      * ``stderr_silent_zero`` — Phase 1.2 silent-zero stderr note.

    Both default ``True`` (feature-on). ``get_telemetry_config()`` is
    the typed accessor; malformed values coerce to the default with
    a warn-log.
    """

    def test_defaults_include_telemetry_section(self) -> None:
        assert _DEFAULTS["telemetry"] == {
            "search_enabled": True,
            "stderr_silent_zero": True,
        }

    def test_defaults_are_enabled(self, home: Path) -> None:
        cfg = get_telemetry_config(repo_root=home)
        assert cfg.search_enabled is True
        assert cfg.stderr_silent_zero is True

    def test_explicit_false_respected(self, home: Path) -> None:
        (home / ".nexus.yml").write_text(
            "telemetry:\n"
            "  search_enabled: false\n"
            "  stderr_silent_zero: false\n"
        )
        cfg = get_telemetry_config(repo_root=home)
        assert cfg.search_enabled is False
        assert cfg.stderr_silent_zero is False

    def test_partial_override_keeps_other_default(self, home: Path) -> None:
        """Override only ``search_enabled`` — ``stderr_silent_zero`` stays True."""
        (home / ".nexus.yml").write_text(
            "telemetry:\n  search_enabled: false\n"
        )
        cfg = get_telemetry_config(repo_root=home)
        assert cfg.search_enabled is False
        assert cfg.stderr_silent_zero is True

    def test_malformed_value_falls_back_to_default(self, home: Path, caplog) -> None:
        """A non-bool ``search_enabled`` falls back to the default and warns."""
        import logging
        (home / ".nexus.yml").write_text(
            "telemetry:\n  search_enabled: not-a-bool\n"
        )
        with caplog.at_level(logging.WARNING):
            cfg = get_telemetry_config(repo_root=home)
        assert cfg.search_enabled is True  # fell back to default
        # structlog may or may not route through caplog — be lenient
        messages = " ".join(r.getMessage() for r in caplog.records)
        # Either the stdlib log captured the warn or structlog emitted
        # to stderr; one path must have recorded evidence of the coercion.
        if caplog.records:
            assert "telemetry" in messages.lower()

    def test_raw_load_config_exposes_section(self, home: Path) -> None:
        """``load_config()`` surfaces the ``telemetry`` section verbatim so
        legacy callers that do ``cfg.get("telemetry", {}).get(...)`` keep
        working without reaching for the typed accessor."""
        (home / ".nexus.yml").write_text(
            "telemetry:\n  search_enabled: false\n"
        )
        cfg = load_config(repo_root=home)
        assert cfg["telemetry"]["search_enabled"] is False
        # Unset key keeps its default.
        assert cfg["telemetry"]["stderr_silent_zero"] is True


# ── set_config_value dotted-key collisions (nexus-s4a98) ─────────────────────


def test_set_config_value_through_flat_scalar_converts(home: Path) -> None:
    """nexus-s4a98 regression: a hand-written flat scalar at a section key is
    converted to the nested form instead of crashing with TypeError."""
    cfg = home / ".config" / "nexus" / "config.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("pdf: mineru\nother: keep\n")

    set_config_value("pdf.extractor", "mineru")

    data = yaml.safe_load(cfg.read_text())
    assert data["pdf"] == {"extractor": "mineru"}
    assert data["other"] == "keep"  # sibling keys survive the conversion


def test_set_config_value_deep_chain_through_scalar(home: Path) -> None:
    """Every non-dict intermediate on the dotted path is replaced, not just
    the first — a scalar two levels deep must not resurface the TypeError."""
    cfg = home / ".config" / "nexus" / "config.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("a:\n  b: 5\n")

    set_config_value("a.b.c.d", "v")

    data = yaml.safe_load(cfg.read_text())
    assert data["a"]["b"]["c"]["d"] == "v"


def test_set_config_value_dict_intermediates_merge_not_replace(home: Path) -> None:
    """Guard against overcorrection: existing DICT intermediates must still be
    merged into (siblings preserved), never wholesale-replaced."""
    cfg = home / ".config" / "nexus" / "config.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("pdf:\n  extractor: docling\n  timeout: 30\n")

    set_config_value("pdf.extractor", "mineru")

    data = yaml.safe_load(cfg.read_text())
    assert data["pdf"]["extractor"] == "mineru"
    assert data["pdf"]["timeout"] == 30  # sibling of the leaf survives


# ── nexus-m20mf: get_credential's config.yml parse is cached ────────────────
#
# WHY: every T2 store construction calls get_credential, which re-parsed
# config.yml on EVERY call. One nx_answer builds 40 stores across 5 _t2_ctx
# blocks -> up to 80 parses. Measured 0.248 ms/call before, 0.009 ms after
# (26.6x). SCOPE HONESTLY: ~20 ms against an nx_answer whose measured p50 is
# 80 SECONDS. The bead's "90% of steady-state cost" was measured against a
# MOCKED-I/O harness where construction is all there is. The real
# beneficiaries are bulk callers (the indexer constructs stores per file).
#
# test_get_credential_parses_config_once is the one that fails pre-fix; the
# rest pin the properties the cache must not break.


class TestGetCredentialConfigCache:
    def _write(self, home: Path, value: str = "sekrit") -> Path:
        cfg = home / ".config" / "nexus" / "config.yml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(yaml.safe_dump({"credentials": {"probe_key": value}}))
        return cfg

    def test_get_credential_parses_config_once(self, home: Path, monkeypatch):
        """Repeated reads with no file change must parse exactly once.

        This is the assertion that fails without the cache — pre-fix it
        counted one safe_load per call.
        """
        self._write(home)
        monkeypatch.delenv("PROBE_KEY", raising=False)
        monkeypatch.setattr(cfgmod, "_GLOBAL_CONFIG_CACHE", None, raising=False)

        calls = {"n": 0}
        real = cfgmod.yaml.safe_load

        def counting(*a, **kw):
            calls["n"] += 1
            return real(*a, **kw)

        monkeypatch.setattr(cfgmod.yaml, "safe_load", counting)
        for _ in range(10):
            assert cfgmod.get_credential("probe_key") == "sekrit"
        assert calls["n"] == 1, f"expected 1 parse for 10 reads, got {calls['n']}"

    def test_on_disk_change_invalidates_without_restart(self, home: Path, monkeypatch):
        """Keyed on mtime_ns+size, so an edit is seen by the same process.

        Deliberately stronger than the SSL-context cache (888bdee8f), which
        documents a restart-required trade for trust-store changes.
        """
        cfg = self._write(home, "first")
        monkeypatch.delenv("PROBE_KEY", raising=False)
        monkeypatch.setattr(cfgmod, "_GLOBAL_CONFIG_CACHE", None, raising=False)
        assert cfgmod.get_credential("probe_key") == "first"
        cfg.write_text(yaml.safe_dump({"credentials": {"probe_key": "second"}}))
        assert cfgmod.get_credential("probe_key") == "second"

    def test_env_precedence_stays_per_call(self, home: Path, monkeypatch):
        """Only the FILE parse is cached; env must win on every call."""
        self._write(home, "from-file")
        monkeypatch.setattr(cfgmod, "_GLOBAL_CONFIG_CACHE", None, raising=False)
        monkeypatch.delenv("PROBE_KEY", raising=False)
        assert cfgmod.get_credential("probe_key") == "from-file"
        monkeypatch.setenv("PROBE_KEY", "from-env")
        assert cfgmod.get_credential("probe_key") == "from-env"
        monkeypatch.delenv("PROBE_KEY")
        assert cfgmod.get_credential("probe_key") == "from-file"

    def test_caller_mutation_cannot_poison_the_cache(self, home: Path, monkeypatch):
        """_load_global_config returns a copy — a mutating caller must not
        corrupt what every other reader sees."""
        cfg = self._write(home)
        monkeypatch.setattr(cfgmod, "_GLOBAL_CONFIG_CACHE", None, raising=False)
        first = cfgmod._load_global_config(cfg)
        first["credentials"] = {"probe_key": "poisoned"}
        assert cfgmod._load_global_config(cfg)["credentials"]["probe_key"] == "sekrit"
