# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Verify that every Nexus on-disk path honours ``NEXUS_CONFIG_DIR``.

Sandbox + test isolation depends on a single env var redirecting the
entire footprint (T2, catalog, sessions, checkpoints, pipeline buffer,
logs, locks, ripgrep caches, MinerU output, PID files). The review that
spawned this test discovered that ``default_db_path()`` hard-coded
``~/.config/nexus/memory.db`` and silently routed sandbox runs back to
the user's production T2. Every helper listed here must resolve under
the override directory.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def sandbox_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect NEXUS_CONFIG_DIR and reload modules that cache the path."""
    sandbox = tmp_path / "nexus-sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(sandbox))

    # Reload modules that resolve their path constants at import time so
    # the new env var takes effect within the test scope. Also reload
    # nexus.db.t2.memory_store because it imports
    # ``read_claude_session_id`` from nexus.session by name — a bare
    # reload of nexus.session would leave memory_store pointing at the
    # old function object and break the identity-check assertion in
    # tests/test_memory.py.
    import nexus.session
    import nexus.context
    import nexus.checkpoint
    import nexus.db.pipeline_buffer  # real module (post-RDR-112 P0-gate)
    import nexus.pipeline_buffer  # back-compat shim
    import nexus.commands.search_cmd
    import nexus.db.t2.memory_store
    importlib.reload(nexus.session)
    importlib.reload(nexus.context)
    importlib.reload(nexus.checkpoint)
    # nexus-yqeu moved pipeline_buffer into src/nexus/db/. Reload the
    # real module BEFORE the shim so the shim's star-import picks up
    # the fresh PIPELINE_DB_PATH.
    importlib.reload(nexus.db.pipeline_buffer)
    importlib.reload(nexus.pipeline_buffer)
    importlib.reload(nexus.commands.search_cmd)
    importlib.reload(nexus.db.t2.memory_store)

    yield sandbox

    # Reload once more without the env var to restore the module-level
    # constants for any test that runs later in the same process.
    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    importlib.reload(nexus.session)
    importlib.reload(nexus.context)
    importlib.reload(nexus.checkpoint)
    importlib.reload(nexus.db.pipeline_buffer)
    importlib.reload(nexus.pipeline_buffer)
    importlib.reload(nexus.commands.search_cmd)
    importlib.reload(nexus.db.t2.memory_store)


class TestCanonicalHelper:
    def test_nexus_config_dir_default(self, monkeypatch):
        """Without the env var, returns ~/.config/nexus."""
        from nexus.config import nexus_config_dir

        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        assert nexus_config_dir() == Path.home() / ".config" / "nexus"

    def test_nexus_config_dir_override(self, tmp_path, monkeypatch):
        from nexus.config import nexus_config_dir

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        assert nexus_config_dir() == tmp_path


class TestAutouseConfigDirIsolation:
    """nexus-mrmq regression: the autouse ``_isolate_config_dir`` fixture
    must redirect ``NEXUS_CONFIG_DIR`` to a tmp dir so child processes
    spawned by integration tests (``claude_dispatch -p``, plan-runner,
    nx_answer equivalence suite) inherit the redirect via
    ``os.environ`` and write ``current_session`` /
    ``t1_addr.<claude_pid>`` files under tmp instead of the user's
    real ``~/.config/nexus/``.

    These tests intentionally do NOT delenv ``NEXUS_CONFIG_DIR`` —
    they assert the autouse fixture's effect.
    """

    def test_env_var_set_to_non_home_path(self):
        env_path = os.environ.get("NEXUS_CONFIG_DIR", "")
        assert env_path, (
            "NEXUS_CONFIG_DIR is unset — the autouse _isolate_config_dir "
            "fixture in tests/conftest.py is missing or has been disabled. "
            "This is the guardrail that prevents integration-test "
            "subprocesses from rewriting the user's real "
            "~/.config/nexus/current_session and t1_addr.<pid> files."
        )
        real_config = Path.home() / ".config" / "nexus"
        assert Path(env_path) != real_config, (
            "NEXUS_CONFIG_DIR resolves to the user's real config "
            "directory; integration-test isolation is broken."
        )

    def test_config_dir_resolves_under_tmp(self):
        """``nexus_config_dir()`` follows the autouse redirect."""
        from nexus.config import nexus_config_dir

        resolved = nexus_config_dir()
        real_config = Path.home() / ".config" / "nexus"
        assert resolved != real_config

    def test_subprocess_inherits_redirected_path(self):
        """A subprocess that imports nexus and reads
        ``nexus_config_dir()`` must resolve to the autouse tmp dir,
        not the user's home. This is the actual leak path: a child
        process inheriting ``os.environ`` from pytest must see the
        override.
        """
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable, "-c",
                "from nexus.config import nexus_config_dir; "
                "print(nexus_config_dir())",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        child_path = result.stdout.strip()
        real_config = str(Path.home() / ".config" / "nexus")
        assert child_path != real_config, (
            f"Child subprocess resolved nexus_config_dir() to "
            f"{child_path!r}, the user's real home. The autouse "
            "fixture's NEXUS_CONFIG_DIR did not propagate via "
            "os.environ inheritance."
        )

    def test_user_real_config_untouched_by_leak_pattern(self, tmp_path):
        """End-to-end: simulate the leak pattern (a child process
        writing ``current_session``) and verify it lands under tmp,
        not under the user's home."""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable, "-c",
                "from nexus.session import write_claude_session_id; "
                "write_claude_session_id('test-leak-uuid'); "
                "from nexus.session import claude_session_file; "
                "print(claude_session_file())",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        written_path = Path(result.stdout.strip())
        real_session_file = Path.home() / ".config" / "nexus" / "current_session"
        assert written_path != real_session_file, (
            f"Child wrote current_session to {written_path!r}, the "
            "user's real session file. Integration-test isolation is "
            "broken."
        )
        # Confirm the autouse tmp dir actually received the write.
        assert written_path.exists()
        assert written_path.read_text().strip() == "test-leak-uuid"


class TestT2IsolatedUnderOverride:
    def test_default_db_path_redirects(self, sandbox_dir: Path):
        from nexus.commands._helpers import default_db_path

        assert default_db_path() == sandbox_dir / "memory.db"

    def test_hooks_db_path_redirects(self, sandbox_dir: Path):
        from nexus.hooks import _default_db_path

        assert _default_db_path() == sandbox_dir / "memory.db"


class TestCatalogIsolatedUnderOverride:
    def test_catalog_path_redirects(self, sandbox_dir: Path, monkeypatch):
        # Make sure the narrower NEXUS_CATALOG_PATH override isn't set so
        # we're testing the NEXUS_CONFIG_DIR path.
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        from nexus.config import catalog_path

        assert catalog_path() == sandbox_dir / "catalog"

    def test_default_registry_path_redirects(self, sandbox_dir: Path):
        from nexus.catalog.catalog import _default_registry_path

        assert _default_registry_path() == sandbox_dir / "repos.json"

    def test_commands_catalog_registry_redirects(self, sandbox_dir: Path):
        from nexus.commands.catalog import _make_registry

        reg = _make_registry()
        # RepoRegistry stores the path on ._path
        assert Path(reg._path) == sandbox_dir / "repos.json"

    def test_commands_index_registry_redirects(self, sandbox_dir: Path):
        from nexus.commands.index import _registry_path

        assert _registry_path() == sandbox_dir / "repos.json"


class TestSessionIsolatedUnderOverride:
    def test_claude_session_file_redirects(self, sandbox_dir: Path):
        from nexus.session import CLAUDE_SESSION_FILE

        assert CLAUDE_SESSION_FILE == sandbox_dir / "current_session"



class TestCheckpointAndBufferRedirects:
    def test_checkpoint_dir_redirects(self, sandbox_dir: Path):
        from nexus.checkpoint import CHECKPOINT_DIR

        assert CHECKPOINT_DIR == sandbox_dir / "checkpoints"

    def test_pipeline_db_redirects(self, sandbox_dir: Path):
        from nexus.pipeline_buffer import PIPELINE_DB_PATH

        assert PIPELINE_DB_PATH == sandbox_dir / "pipeline.db"


class TestContextRedirects:
    def test_context_l1_dir_redirects(self, sandbox_dir: Path):
        from nexus.context import CONTEXT_L1_DIR

        assert CONTEXT_L1_DIR == sandbox_dir / "context"

    def test_context_l1_path_redirects(self, sandbox_dir: Path):
        from nexus.context import CONTEXT_L1_PATH

        assert CONTEXT_L1_PATH == sandbox_dir / "context_l1.txt"


class TestSearchRipgrepCacheRedirects:
    def test_search_config_dir_redirects(self, sandbox_dir: Path):
        from nexus.commands.search_cmd import _CONFIG_DIR

        assert _CONFIG_DIR == sandbox_dir


class TestCommandHelpers:
    def test_mineru_pid_path_redirects(self, sandbox_dir: Path):
        from nexus.commands.mineru import _pid_file_path

        assert _pid_file_path() == sandbox_dir / "mineru.pid"

    def test_mineru_output_root_redirects(self, sandbox_dir: Path):
        from nexus.commands.mineru import _mineru_output_root

        # Output root is created as a side effect so clean it up.
        root = _mineru_output_root()
        assert root == sandbox_dir / "mineru-output"
        assert root.exists()
        assert root.stat().st_mode & 0o777 == 0o700

    def test_console_config_dir_redirects(self, sandbox_dir: Path):
        from nexus.commands.console import _config_dir

        assert _config_dir() == sandbox_dir

    def test_logging_config_dir_redirects(self, sandbox_dir: Path):
        from nexus.logging_setup import _config_dir

        assert _config_dir() == sandbox_dir


class TestLocalChromaPathUnaffected:
    """The local chroma path lives under ``~/.local/share`` by design; the
    NEXUS_CONFIG_DIR override only controls the ``.config/nexus`` surface."""

    def test_local_chroma_path_uses_xdg_data_home(self, sandbox_dir: Path, monkeypatch):
        from nexus.config import _default_local_path

        # Not redirected by NEXUS_CONFIG_DIR.
        monkeypatch.delenv("NX_LOCAL_CHROMA_PATH", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        p = _default_local_path()
        assert p == Path.home() / ".local" / "share" / "nexus" / "chroma"


class TestNoProductionT2Writes:
    """End-to-end assurance: a sandbox-scoped ``T2Database(default_db_path())``
    writes ONLY under the override, never under the user's home."""

    def test_t2_writes_land_in_sandbox(self, sandbox_dir: Path):
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database

        path = default_db_path()
        assert path.parent == sandbox_dir

        with T2Database(path) as db:
            db.memory.put(
                project="sandbox-test", title="hello",
                content="isolation check",
            )

        # T2 file lives under the sandbox, not under ~/.config/nexus
        assert path.exists()
        assert path.is_relative_to(sandbox_dir)
        # No file created under the user's production config dir by this test.
        production = Path.home() / ".config" / "nexus" / "memory.db"
        # Can't assert absence (user may have a real one), but assert that
        # the one we just wrote is not that one.
        assert path != production
