# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-112 6shq.4 (nexus-w6hj) — ``nx t3`` CLI under
``NX_STORAGE_MODE=daemon``.

Scope: smoke coverage of the two direct ``Catalog`` opens in
``commands/t3.py`` flipped under the w6hj batch:

1. ``_make_catalog`` factory (line 134): used by ``nx t3 gc``. Mutator
   path, so the flip routes through ``open_catalog`` (fresh instance)
   with a ``RuntimeError -> ClickException`` translation at the
   factory boundary, matching the ``commands/catalog.py:_get_catalog``
   precedent (nexus-3gdg).

2. ``prune-stale`` owner-lookup (line 268): read-only ``SELECT
   tumbler_prefix, repo_root FROM owners`` to anchor relative
   source-paths. Read-only path, so the flip routes through
   ``open_cached``; the surrounding ``except Exception`` swallows the
   daemon-down ``RuntimeError`` and the prune still proceeds for
   absolute-path entries (degraded mode is documented).

The daemon-down ClickException for ``_make_catalog`` is the primary
smoke test here; under the existing test_t3_gc.py the factory is
patched out, so this file is the only place the daemon-aware factory
is contract-pinned.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def daemon_env(monkeypatch, config_dir: Path):
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config_dir))


@pytest.fixture
def catalog_dir(tmp_path: Path, monkeypatch) -> Path:
    """Initialize a real catalog under tmp_path and route
    ``nexus.config.catalog_path`` at it for the test. Mirrors the
    fixture in ``test_catalog_daemon_mode.py`` and
    ``test_doctor_daemon_mode.py``."""
    from nexus.catalog import Catalog
    cd = tmp_path / "catalog"
    cd.mkdir()
    Catalog.init(cd)
    monkeypatch.setattr(
        "nexus.config.catalog_path",
        lambda: cd,
    )
    return cd


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── Daemon-down ClickException regression ─────────────────────────────────


class TestT3GcDaemonDownClickException:
    """RDR-112 6shq.4 (nexus-w6hj) regression: ``_make_catalog`` is
    the factory consumed by ``nx t3 gc``. Under daemon mode with no
    daemon running, ``open_catalog`` raises ``DaemonNotRunningError``
    (a ``RuntimeError`` subclass). Click does NOT translate
    ``RuntimeError`` automatically; the factory must wrap the call in
    ``try/except RuntimeError`` and re-raise ``click.ClickException``
    so the operator sees a single error line instead of a Python
    traceback. Matches the ``_get_catalog`` precedent from
    nexus-3gdg.
    """

    def test_t3_gc_under_daemon_no_daemon_is_click_exception(
        self,
        runner: CliRunner,
        daemon_env,
        catalog_dir: Path,
    ) -> None:
        """``nx t3 gc --collection X`` under daemon mode with no daemon
        running surfaces a ``ClickException`` (exit 1, single error
        line), not a Python traceback. The catalog is initialized so
        the function reaches the ``open_catalog`` call site at
        ``_make_catalog``."""
        # --dry-run is the default but pass it explicitly to be clear
        # we're not trying to mutate anything.
        result = runner.invoke(
            main,
            ["t3", "gc", "--collection", "knowledge__w6hj_smoke", "--dry-run"],
        )
        assert result.exit_code == 1, (
            f"daemon-down should exit 1 (ClickException), got "
            f"{result.exit_code}; output: {result.output!r}; "
            f"exc: {result.exception!r}"
        )
        assert result.output.startswith("Error:"), (
            f"expected 'Error: ...' ClickException line; got: {result.output!r}"
        )
        assert "Traceback" not in result.output, (
            f"daemon-down should NOT surface a Python traceback; got: {result.output!r}"
        )
        assert "daemon" in result.output.lower(), result.output
