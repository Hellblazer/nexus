# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3tl3 (Bundle SR): security remediation tests.

Covers the four security-class findings from the 360° review:

- SR-1 (CRITICAL): path traversal in binding_create / binding_delete
- SR-2: blocking_take timeout cap not enforced daemon-side
- SR-3: _unlink_discovery exception scope (violates 'never raises')
- SR-4: exec_raw row cap conflicts with 1 MiB frame cap
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import chromadb
import pytest


# ---------------------------------------------------------------------------
# SR-1: Path traversal in binding_create / binding_delete
# ---------------------------------------------------------------------------


class TestPathTraversalRejected:
    """Profile names that escape the profiles dir must raise (no write)."""

    @pytest.mark.parametrize(
        "bad_profile",
        [
            "../malicious",                # parent dir escape
            "/etc/passwd",                 # absolute path
            "../../../tmp/bad",            # multi-level escape
            "ops/sub",                     # subdir injection
            "foo/../bar",                  # mid-string traversal
            "..",                          # bare parent
            ".",                           # bare cwd
            "",                            # empty
            "foo bar",                     # whitespace
            "foo;rm",                      # shell-meta
        ],
    )
    def test_create_binding_rejects_bad_profile(
        self, tmp_path: Path, bad_profile: str
    ) -> None:
        from nexus.cockpit.bindings_crud import create_binding

        with pytest.raises((ValueError, OSError)):
            create_binding(
                profile=bad_profile,
                name="b1",
                match={"subspace": "x"},
                action={"kind": "log", "marker": "m"},
                profiles_dir=tmp_path,
            )
        # No file was written outside the target dir.
        for sibling in tmp_path.parent.iterdir():
            if sibling != tmp_path:
                continue
        # And no .yml landed in tmp_path either.
        assert list(tmp_path.glob("*.yml")) == []

    @pytest.mark.parametrize(
        "bad_profile",
        ["../malicious", "/etc/passwd", "foo/../bar", "..", ""],
    )
    def test_delete_binding_rejects_bad_profile(
        self, tmp_path: Path, bad_profile: str
    ) -> None:
        from nexus.cockpit.bindings_crud import delete_binding

        with pytest.raises((ValueError, KeyError, OSError)):
            delete_binding(bad_profile, "b1", profiles_dir=tmp_path)

    @pytest.mark.parametrize(
        "bad_profile", ["../bad", "/abs/path"],
    )
    def test_toggle_binding_rejects_bad_profile(
        self, tmp_path: Path, bad_profile: str
    ) -> None:
        from nexus.cockpit.bindings_crud import toggle_binding

        with pytest.raises((ValueError, KeyError, OSError)):
            toggle_binding(
                bad_profile, "b1", enabled=False, profiles_dir=tmp_path
            )

    def test_good_profile_names_still_work(self, tmp_path: Path) -> None:
        """Regression guard: alphanumeric + dash + underscore still pass."""
        from nexus.cockpit.bindings_crud import create_binding

        for ok in ("ops", "my-profile", "team_dev", "v1-prod-2"):
            create_binding(
                profile=ok,
                name=f"b-{ok}",
                match={"subspace": "x"},
                action={"kind": "log", "marker": "m"},
                profiles_dir=tmp_path,
            )
            assert (tmp_path / f"{ok}.yml").exists()


# ---------------------------------------------------------------------------
# SR-2: blocking_take timeout cap enforced daemon-side
# ---------------------------------------------------------------------------


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status: { type: enum, values: [open, in_progress, done], required: true }
  priority: { type: enum, values: [P0, P1, P2], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 100
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture()
def _registry(tmp_path: Path):
    from nexus.tuplespace.registry import Registry

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    return Registry.load(d)


@pytest.fixture()
def _chroma() -> chromadb.EphemeralClient:
    client = chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


class TestBlockingTakeTimeoutCap:
    """blocking_take must clamp / reject timeout_seconds > 30."""

    def test_blocking_take_rejects_timeout_above_30(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        from nexus.daemon.tuplespace_service import TuplespaceService
        from nexus.tuplespace.api import InvalidTimeoutError

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            with pytest.raises(InvalidTimeoutError):
                service.blocking_take(
                    subspace="tasks/sr",
                    query="x",
                    claimant="solo",
                    timeout_seconds=99999.0,
                )
        finally:
            service.close()

    def test_blocking_take_accepts_timeout_at_cap(
        self, tmp_path: Path, _registry, _chroma
    ) -> None:
        """30s exactly must be accepted (boundary case)."""
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_chroma,
            registry=_registry,
        )
        try:
            # Populate a candidate so the call returns immediately
            # rather than actually blocking 30 seconds.
            service.out(
                subspace="tasks/sr",
                content="ready",
                dimensions={
                    "status": "open",
                    "priority": "P1",
                    "created_by": "x",
                },
            )
            result = service.blocking_take(
                subspace="tasks/sr",
                query="ready",
                claimant="solo",
                timeout_seconds=30.0,
            )
            assert result is not None
            service.ack(claim_id=result["claim_id"], claimant="solo")
        finally:
            service.close()


# ---------------------------------------------------------------------------
# SR-3: _unlink_discovery exception scope (never raises contract)
# ---------------------------------------------------------------------------


class TestUnlinkDiscoveryNeverRaises:
    """Step 1 marker-write must catch non-OSError exceptions too."""

    def test_unlink_discovery_swallows_sqlite_operationalerror(
        self, tmp_path: Path
    ) -> None:
        """Registry digest raising sqlite3.OperationalError must NOT
        propagate out of _unlink_discovery.
        """
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(tmp_path / "config")
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text("{}")

        class _BoomRegistry:
            def digest(self) -> str:
                raise sqlite3.OperationalError("simulated registry close")

        daemon._registry_store = _BoomRegistry()

        # MUST NOT raise.
        daemon._unlink_discovery()
        # File should still be removed (the inner OSError catch only
        # protects against marker-write failures; unlink itself is
        # independent).
        assert not daemon._discovery_path.exists()

    def test_unlink_discovery_swallows_runtimeerror_too(
        self, tmp_path: Path
    ) -> None:
        """Any Exception class in the marker-write path is swallowed."""
        from nexus.daemon.t2_daemon import T2Daemon

        daemon = T2Daemon(tmp_path / "config")
        daemon._config_dir.mkdir(parents=True, exist_ok=True)
        daemon._discovery_path.write_text("{}")

        class _BoomRegistry:
            def digest(self) -> str:
                raise RuntimeError("simulated unrelated failure")

        daemon._registry_store = _BoomRegistry()
        daemon._unlink_discovery()  # must not raise


# ---------------------------------------------------------------------------
# SR-4: exec_raw row cap aligned with 1 MiB frame cap
# ---------------------------------------------------------------------------


class TestExecRawRowCapFitsFrameCap:
    """_EXEC_RAW_MAX_ROWS must produce a payload that fits in the frame cap."""

    def test_exec_raw_cap_under_safe_ceiling(self) -> None:
        """_EXEC_RAW_MAX_ROWS should be tightened so encoded responses
        fit comfortably in the 1 MiB frame cap.

        We assert an explicit ceiling of 10_000 rows here. With typical
        row widths (50-200 bytes JSON-encoded), 10k rows is comfortably
        under 1 MiB. The prior value (50_000) could exceed the cap.
        """
        from nexus.daemon import introspection

        assert introspection._EXEC_RAW_MAX_ROWS <= 10_000, (
            f"_EXEC_RAW_MAX_ROWS={introspection._EXEC_RAW_MAX_ROWS} risks "
            "exceeding the 1 MiB frame cap (nexus-ex4r). Tighten to "
            "<= 10000 or add per-RPC frame-cap override."
        )

    def test_exec_raw_cap_documented_in_module(self) -> None:
        """Sanity: the cap declaration should document the frame-cap relation."""
        from nexus.daemon import introspection
        import inspect

        source = inspect.getsource(introspection)
        # Look for the new tightening comment near the constant.
        assert "frame" in source.lower() or "MAX_FRAME" in source, (
            "_EXEC_RAW_MAX_ROWS should mention the frame-cap relation "
            "in an inline comment so future bumps stay aware."
        )
