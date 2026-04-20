# SPDX-License-Identifier: AGPL-3.0-or-later
import contextlib
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from nexus.cli import main

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"


# ── Fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def mock_reg():
    reg = MagicMock()
    reg.all.return_value = []
    return reg


def _invoke(runner, mock_reg, *, cred="sk-key", which="/usr/bin/tool",
            cloud_client=None, extra_patches=None):
    patches = [
        patch("nexus.config.is_local_mode", return_value=False),
        patch("nexus.registry.RepoRegistry", return_value=mock_reg),
    ]
    if callable(cred):
        patches.append(patch("nexus.config.get_credential",
                             side_effect=cred))
    else:
        patches.append(patch("nexus.config.get_credential",
                             return_value=cred))
    if callable(which):
        patches.append(patch("nexus.health.shutil.which",
                             side_effect=which))
    else:
        patches.append(patch("nexus.health.shutil.which",
                             return_value=which))
    if cloud_client is not None:
        patches.append(patch("nexus.health.chromadb.CloudClient",
                             **cloud_client))
    elif cred and not callable(cred):
        patches.append(patch("nexus.health.chromadb.CloudClient",
                             return_value=MagicMock()))
    patches.extend(extra_patches or [])
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return runner.invoke(main, ["doctor"])


# ── Healthy / basic output ──────────────────────────────────────────────────

def test_doctor_all_healthy(runner, mock_reg):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert "\u2713" in result.output


@pytest.mark.parametrize("expected", [
    "git hooks", "index log", "\u2713 index log", "Python", "3.12",
])
def test_doctor_healthy_output_contains(runner, mock_reg, expected):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert expected in result.output


@pytest.mark.parametrize("absent", ["nx serve start", "Nexus server"])
def test_doctor_does_not_mention_serve(runner, mock_reg, absent):
    result = _invoke(runner, mock_reg)
    assert absent not in result.output


# ── Missing credentials ─────────────────────────────────────────────────────

def test_doctor_missing_credentials_exit_1(runner, mock_reg):
    result = _invoke(runner, mock_reg, cred=None)
    assert result.exit_code == 1
    assert "\u2717" in result.output
    assert "CHROMA_API_KEY" in result.output
    assert "nx config init" in result.output


def test_doctor_missing_credential_shows_inline_fix(runner, mock_reg):
    result = _invoke(runner, mock_reg, cred=None)
    assert "nx config set chroma_api_key" in result.output
    assert "nx config set voyage_api_key" in result.output
    assert "trychroma.com" in result.output
    assert "voyageai.com" in result.output


def test_doctor_partial_credentials(runner, mock_reg):
    def cred_side_effect(key):
        return "sk-key" if key in ("chroma_api_key", "voyage_api_key") else None

    result = _invoke(runner, mock_reg, cred=cred_side_effect)
    assert result.exit_code == 1
    assert "CHROMA_TENANT" in result.output
    assert "nx config set chroma_tenant" not in result.output
    assert "nx config set chroma_database" in result.output
    assert "nx config set chroma_api_key" not in result.output
    assert "nx config set voyage_api_key" not in result.output


# ── Missing tools ───────────────────────────────────────────────────────────

def _which_missing(name):
    """which side-effect that only hides rg."""
    return None if name == "rg" else f"/usr/bin/{name}"


def test_doctor_missing_rg(runner, mock_reg):
    result = _invoke(runner, mock_reg, which=_which_missing)
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "hybrid search disabled" in result.output
    assert "brew install ripgrep" in result.output


def test_doctor_missing_rg_shows_platform_hints(runner, mock_reg):
    result = _invoke(runner, mock_reg, which=lambda _: None)
    assert "brew install ripgrep" in result.output
    assert "apt install ripgrep" in result.output
    assert "BurntSushi/ripgrep" in result.output


@pytest.mark.parametrize("tool,exit_code", [("bd", 0), ("uv", 0)])
def test_doctor_missing_optional_tool(runner, mock_reg, tool, exit_code):
    def which_side(name):
        return None if name == tool else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert result.exit_code == exit_code


def test_doctor_missing_bd_output(runner, mock_reg):
    def which_side(name):
        return None if name == "bd" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert "bd (beads" in result.output
    assert "not found" in result.output
    assert "BeadsProject/beads" in result.output


def test_doctor_missing_npx_is_non_fatal_with_plugin_hint(runner, mock_reg):
    """Missing npx is plugin-only — non-fatal for the CLI but reported with
    a clear hint that the plugin's MCP servers will fail without it."""
    def which_side(name):
        return None if name == "npx" else f"/usr/bin/{name}"
    result = _invoke(runner, mock_reg, which=which_side)
    assert result.exit_code == 0, "missing npx must not fail nx doctor (plugin-only)"
    assert "npx (Node.js, plugin-only)" in result.output
    assert "not found" in result.output
    assert "sequential-thinking" in result.output or "context7" in result.output
    assert "nodejs.org" in result.output


# ── Python version ──────────────────────────────────────────────────────────

def test_doctor_python_version_too_old_fails(runner, mock_reg):
    result = _invoke(runner, mock_reg, extra_patches=[
        patch("nexus.health._python_ok", return_value=(False, "3.11.0")),
    ])
    assert result.exit_code == 1
    assert "\u2717" in result.output
    assert "3.12" in result.output
    assert "python.org" in result.output


# ── Hooks ───────────────────────────────────────────────────────────────────

def test_doctor_hooks_no_repos_registered(runner, mock_reg):
    result = _invoke(runner, mock_reg)
    assert result.exit_code == 0
    assert "no repos registered" in result.output
    assert "nx index repo" in result.output
    assert "\u2713 git hooks" in result.output


def test_doctor_hooks_installed(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)
        for name in ("post-commit", "post-merge", "post-rewrite"):
            (hooks_dir / name).write_text(
                f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo ...\n")
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus.commands.hooks._effective_hooks_dir",
                  return_value=hooks_dir),
        ])
    assert result.exit_code == 0
    assert "\u2713 git hooks" in result.output
    assert "/some/repo" in result.output
    assert "post-commit" in result.output


def test_doctor_hooks_not_installed(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    with tempfile.TemporaryDirectory() as td:
        result = _invoke(runner, reg, extra_patches=[
            patch("nexus.commands.hooks._effective_hooks_dir",
                  return_value=Path(td)),
        ])
    assert result.exit_code == 0
    assert "\u2713 git hooks" in result.output
    assert "not installed" in result.output
    assert "nx hooks install /some/repo" in result.output
    assert "\u2717 git hooks" not in result.output


def test_doctor_hooks_exception_does_not_propagate(runner):
    reg = MagicMock()
    reg.all.return_value = ["/some/repo"]
    result = _invoke(runner, reg, extra_patches=[
        patch("nexus.commands.hooks._effective_hooks_dir",
              side_effect=RuntimeError("git error")),
    ])
    assert result.exit_code == 0
    assert "git hooks" in result.output


# ── Index log ───────────────────────────────────────────────────────────────

def test_doctor_index_log_not_created_yet(runner, mock_reg):
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_home = Path(tmpdir)
        (fake_home / ".config" / "nexus").mkdir(parents=True, exist_ok=True)
        result = _invoke(runner, mock_reg, extra_patches=[
            patch.object(Path, "home", return_value=fake_home),
        ])
    assert "index log" in result.output
    assert "not created yet" in result.output


# ── Single-database check ───────────────────────────────────────────────────

def test_doctor_single_db_calls_cloud_client(runner, mock_reg):
    import chromadb.errors
    mock_client = MagicMock()
    mock_client.list_collections.return_value = []

    def cloud_side(**kwargs):
        if kwargs.get("database", "").endswith("_code"):
            raise chromadb.errors.NotFoundError("probe: not found")
        return mock_client

    result = _invoke(runner, mock_reg,
                     cloud_client={"side_effect": cloud_side})
    assert "reachable" in result.output


def test_doctor_single_db_unreachable_fails(runner, mock_reg):
    result = _invoke(runner, mock_reg, cloud_client={
        "side_effect": RuntimeError("connection refused"),
    })
    assert result.exit_code == 1
    assert "not reachable" in result.output
    assert "nx config init" in result.output


def test_doctor_single_db_no_secret_leak(runner, mock_reg):
    result = _invoke(runner, mock_reg, cloud_client={
        "side_effect": RuntimeError("HTTP 401: invalid api_key SUPERSECRET"),
    })
    assert "SUPERSECRET" not in result.output
    assert "not reachable" in result.output


# ── _check helper ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ok,expected", [
    (True, "\u2713"), (False, "\u2717"),
])
def test_check_helper_format(ok, expected):
    from nexus.commands.doctor import _check
    assert expected in _check("Test", ok)


def test_check_helper_detail():
    from nexus.commands.doctor import _check
    assert "some detail" in _check("Test", True, "some detail")


# ── Local mode ──────────────────────────────────────────────────────────────

def test_doctor_local_mode_shows_local_checks(runner, mock_reg, tmp_path):
    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config._default_local_path", return_value=tmp_path / "chroma"),
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.registry.RepoRegistry", return_value=mock_reg),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "local" in result.output.lower()
    assert "Embedding model" in result.output
    assert "CHROMA_API_KEY" not in result.output
    assert "VOYAGE_API_KEY" not in result.output


def test_doctor_local_mode_shows_collection_count(runner, mock_reg, tmp_path):
    chroma_path = tmp_path / "chroma"
    import chromadb
    from nexus.db.local_ef import LocalEmbeddingFunction
    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path=str(chroma_path))
    col = client.get_or_create_collection("knowledge__test", embedding_function=ef)
    col.add(ids=["doc1"], documents=["test content"])

    with (
        patch("nexus.config.is_local_mode", return_value=True),
        patch("nexus.config._default_local_path", return_value=chroma_path),
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.registry.RepoRegistry", return_value=mock_reg),
    ):
        result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "1 collections" in result.output
    assert "on disk" in result.output


# ── doctor --fix-paths ─────────────────────────────────────────────────────


class TestFixPaths:
    """doctor --fix-paths migration tests."""

    @pytest.fixture(autouse=True)
    def _git_identity(self, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")

    def _make_catalog_with_entries(self, tmp_path, entries):
        """Create catalog with specified entries.

        entries: list of (owner_type, repo_hash, repo_root, file_path, collection).
        """
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat = Catalog.init(cat_dir)
        for owner_type, repo_hash, repo_root, file_path, collection in entries:
            owner = cat.register_owner(
                f"test-{repo_hash or 'curator'}",
                owner_type,
                repo_hash=repo_hash,
                repo_root=repo_root,
            )
            cat.register(
                owner,
                "test-doc",
                content_type="code",
                file_path=file_path,
                physical_collection=collection,
            )
        return cat, cat_dir

    def test_fix_paths_dry_run(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(tmp_path / "repo"),
             str(tmp_path / "repo" / "src" / "foo.py"), "code__test"),
        ])
        mock_t3 = MagicMock()
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths", "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output
        assert "src/foo.py" in result.output
        # Verify nothing was actually changed
        row = cat._db.execute("SELECT file_path FROM documents").fetchone()
        assert row[0].startswith("/")  # still absolute

    def test_fix_paths_writes_relative(self, tmp_path, runner):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(repo_dir),
             str(repo_dir / "src" / "foo.py"), "code__test"),
        ])
        mock_t3 = MagicMock()
        mock_t3.update_source_path.return_value = 5
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        assert "Fixed 1" in result.output
        row = cat._db.execute("SELECT file_path FROM documents").fetchone()
        assert row[0] == "src/foo.py"
        mock_t3.update_source_path.assert_called_once()

    def test_fix_paths_skips_curator(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("curator", "", "", "/abs/path/paper.pdf", "docs__papers"),
        ])
        mock_t3 = MagicMock()
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        mock_t3.update_source_path.assert_not_called()

    def test_fix_paths_idempotent(self, tmp_path, runner):
        cat, cat_dir = self._make_catalog_with_entries(tmp_path, [
            ("repo", "abc12345", str(tmp_path / "repo"),
             "src/foo.py", "code__test"),  # already relative
        ])
        mock_t3 = MagicMock()
        with (
            patch("nexus.config.catalog_path", return_value=cat_dir),
            patch("nexus.db.make_t3", return_value=mock_t3),
        ):
            result = runner.invoke(main, ["doctor", "--fix-paths"])
        assert result.exit_code == 0
        assert "No absolute" in result.output


# ── --check-quotas (nexus-c590) ─────────────────────────────────────────────


class TestCheckQuotas:
    """``nx doctor --check-quotas`` surfaces the free-tier cloud limits,
    Voyage AI per-model token caps, and any retry load observed this
    process. Exits 0 when the cloud tenant is reachable (or when we're
    in local mode); exits 1 when the cloud probe fails so the report is
    actionable via exit code (nexus-c590 acceptance)."""

    @pytest.fixture(autouse=True)
    def _reset_retry_counters_between_tests(self):
        """The retry accumulator is process-local state; isolate each
        test so prior runs can't leak counts into the next case's
        assertion (same contract as tests/test_voyage_retry.py)."""
        from nexus.retry import reset_retry_stats
        reset_retry_stats()
        yield
        reset_retry_stats()

    def test_reachable_cloud_exits_zero_and_reports_all_sections(
        self, runner: CliRunner,
    ) -> None:
        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock()),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        # ChromaDB section + a representative limit
        assert "ChromaDB Cloud" in result.output
        assert "cloud tenant reachable" in result.output
        assert "max_records_per_write" in result.output
        assert "300" in result.output  # MAX_RECORDS_PER_WRITE / MAX_QUERY_RESULTS
        # Voyage section
        assert "Voyage AI" in result.output
        assert "voyage-code-3" in result.output
        assert "32,000" in result.output  # 32k token cap rendered with comma
        # Retry-accumulator quiet case
        assert "no transient backoffs observed" in result.output

    def test_unreachable_cloud_exits_one(self, runner: CliRunner) -> None:
        """A quota report without a client connection is not actionable.
        The command must exit 1 so CI / operators can gate on it."""

        def _raise(*_a, **_kw):
            raise RuntimeError("simulated cloud outage")

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", side_effect=_raise),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 1, result.output
        assert "unreachable" in result.output
        assert "RuntimeError" in result.output

    def test_local_mode_exits_zero_even_without_cloud(
        self, runner: CliRunner,
    ) -> None:
        """Local mode (``NX_LOCAL=1``) doesn't need cloud — the limits
        are reference-only in that context. Exit 0."""
        with (
            patch("nexus.config.is_local_mode", return_value=True),
            patch("nexus.config.get_credential", return_value=""),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "local mode" in result.output

    def test_voyage_key_absent_shows_warn_marker(
        self, runner: CliRunner,
    ) -> None:
        """When ``VOYAGE_API_KEY`` is not set the voyage section must
        use the warn marker (``✗``) so the report is scannable at a
        glance, even though cloud itself may be reachable."""
        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock()),
            patch("nexus.config.get_credential", return_value=""),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "VOYAGE_API_KEY: absent" in result.output

    def test_retry_counters_surface_when_nonzero(
        self, runner: CliRunner,
    ) -> None:
        """A session that has hit transient errors shows the cumulative
        backoff time + count under "Observed transient-error retries"."""
        from nexus.retry import _add_chroma_retry, _add_voyage_retry

        _add_voyage_retry(1.5)
        _add_voyage_retry(3.0)
        _add_chroma_retry(2.0)

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock()),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(main, ["doctor", "--check-quotas"])
        assert result.exit_code == 0, result.output
        assert "Observed transient-error retries" in result.output
        assert "voyage:" in result.output
        assert "2 retries" in result.output       # voyage count
        assert "chroma:" in result.output
        assert "1 retries" in result.output       # chroma count
        # Total aggregates both sides
        assert "3 retries" in result.output       # total_count
        assert "6.5s" in result.output            # 1.5 + 3.0 + 2.0 total_seconds

    def test_json_output_has_structured_schema(
        self, runner: CliRunner,
    ) -> None:
        """``--json`` returns a parseable dict with the three expected
        top-level sections so downstream tools (dashboards, CI gates)
        can key off the schema."""
        import json as _json

        with (
            patch("nexus.config.is_local_mode", return_value=False),
            patch("nexus.db.make_t3", return_value=MagicMock()),
            patch("nexus.config.get_credential", return_value="sk-voyage-key"),
        ):
            result = runner.invoke(
                main, ["doctor", "--check-quotas", "--json"]
            )
        assert result.exit_code == 0, result.output
        data = _json.loads(result.output)
        assert set(data.keys()) == {"chromadb", "voyage", "retry"}
        assert data["chromadb"]["reachable"] is True
        assert data["chromadb"]["limits"]["max_records_per_write"] == 300
        assert "voyage-code-3" in data["voyage"]["models"]
        assert data["voyage"]["api_key_set"] is True
        assert data["retry"]["total_count"] == 0
