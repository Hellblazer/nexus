# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-eqxxh: the engine substrate must not accept a pgvector-less PG.

THE DEFECT (PR #1426, first CI run of the substrate port). ``pg_bin_dir()``
called ``discover_pg_binaries()`` first and only self-provisioned the pinned
bundle on ``PgBinaryNotFoundError``. That made the bug invisible on a dev box —
no host PG, so discovery raises and the bundle leg runs — while on GitHub
runners, which ship PostgreSQL, discovery SUCCEEDED and returned a system PG
with no pgvector. Every engine-substrate test then died at boot::

    RuntimeError: T2 engine substrate: service did not bind port 54655
      [Failed SQL: (0) CREATE EXTENSION IF NOT EXISTS vector]
      ERROR: extension "vector" is not available

73 errors on BOTH python jobs, deterministic, with 13,396 passed and ZERO
test-logic failures — the port was fine, the substrate could not start. The
pinned bundle was never even downloaded, because nothing asked for it.

The rule this restores is the standing one that functional gates are
self-provisioning scripts, never ambient machine state. Discovery-first quietly
inverted it: finding *a* PG is not the question, finding one that can SERVE is.

These tests never launch PG. They drive the resolution DECISION over synthetic
trees, which is the part that was wrong.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.db._service_fixture import _has_pgvector, pg_bin_dir


def _fake_pg(root: Path, *, with_pgvector: bool, sharedir: str = "share/postgresql") -> Path:
    """A PG tree with no binaries — only the layout _has_pgvector inspects."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ext = root / sharedir / "extension"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "plpgsql.control").touch()
    if with_pgvector:
        (ext / "vector.control").touch()
    return bin_dir


class TestHasPgvector:
    """``vector.control`` presence — the same check build_pg_bundle.sh asserts."""

    def test_detects_pgvector_present(self, tmp_path: Path) -> None:
        assert _has_pgvector(_fake_pg(tmp_path, with_pgvector=True)) is True

    def test_detects_pgvector_absent(self, tmp_path: Path) -> None:
        # A PG that HAS contrib (plpgsql) but not pgvector — precisely the
        # GitHub-runner shape. Asserting against a tree with other extensions
        # present rules out a probe that merely fails to find any sharedir.
        assert _has_pgvector(_fake_pg(tmp_path, with_pgvector=False)) is False

    def test_handles_bare_share_layout(self, tmp_path: Path) -> None:
        """Trees that use <prefix>/share rather than <prefix>/share/postgresql."""
        assert _has_pgvector(_fake_pg(tmp_path, with_pgvector=True, sharedir="share")) is True

    def test_missing_tree_is_false_not_an_exception(self, tmp_path: Path) -> None:
        """An unreadable answer and an unusable one lead to the same decision."""
        assert _has_pgvector(tmp_path / "nonexistent" / "bin") is False


class TestPgBinDirRejectsPgvectorLessDiscovery:
    """The resolution decision itself — the half that was actually broken."""

    def test_pgvector_less_discovery_falls_through_to_the_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE CI CASE. Discovery succeeds, and is still not the answer.

        Before the fix this returned the ambient bin dir and every engine test
        died at boot. Delete the ``_has_pgvector`` guard in ``pg_bin_dir`` and
        this test fails with the ambient path.
        """
        ambient = _fake_pg(tmp_path / "ambient", with_pgvector=False)
        bundle = _fake_pg(tmp_path / "bundle", with_pgvector=True)
        monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
        monkeypatch.setattr(
            "nexus.db.pg_provision.discover_pg_binaries",
            lambda: type("R", (), {"initdb": ambient / "initdb"})(),
        )
        monkeypatch.setattr(
            "tests.db._service_fixture._self_provision_pg_bundle", lambda: bundle
        )

        assert pg_bin_dir() == bundle, "a pgvector-less PG must never be returned"

    def test_pgvector_capable_discovery_is_used_as_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NON-VACUITY. The guard must not send everything to the bundle.

        Without this, the test above passes against a `pg_bin_dir` that ignores
        discovery entirely — which would re-download the bundle on every dev box
        that already has a perfectly good pgvector PG.
        """
        ambient = _fake_pg(tmp_path / "ambient", with_pgvector=True)
        monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
        monkeypatch.setattr(
            "nexus.db.pg_provision.discover_pg_binaries",
            lambda: type("R", (), {"initdb": ambient / "initdb"})(),
        )
        monkeypatch.setattr(
            "tests.db._service_fixture._self_provision_pg_bundle",
            lambda: pytest.fail("must not self-provision when discovery is usable"),
        )

        assert pg_bin_dir() == ambient

    def test_explicit_override_without_pgvector_fails_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Product policy, preserved: an explicit NEXUS_PG_BIN is a user
        statement, so a broken one raises rather than being silently swapped for
        the bundle underneath the caller. Silently substituting here would mean
        a developer debugging against a specific PG gets results from a
        different one.
        """
        ambient = _fake_pg(tmp_path / "ambient", with_pgvector=False)
        monkeypatch.setenv("NEXUS_PG_BIN", str(ambient))
        monkeypatch.setattr(
            "nexus.db.pg_provision.discover_pg_binaries",
            lambda: type("R", (), {"initdb": ambient / "initdb"})(),
        )

        with pytest.raises(RuntimeError, match=re.compile("pgvector", re.I)):
            pg_bin_dir()
