# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P1.C (nexus-aim84) — T3 daemon MVV: two-subprocess round trip.

Minimum Viable Validation for Phase 1: end-to-end demo that two
separate OS processes, each reaching the daemon via NX_T3_ADDR through
``make_t3_client()``, share a T3 collection round trip.

The bead description references "two claude -p subprocesses"; the
substantive contract is "two separate processes connecting via
NX_T3_ADDR" (not in-process Task dispatches). We use bare
``python -c`` subprocesses because the ``claude`` CLI is not installed
in CI and what we are validating is OS-level process isolation +
discovery + transport, not anything model-specific.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


# The MVV exercises T3Database's public surface (put / search) over
# the daemon, not the raw chromadb.HttpClient. P1.B's surface-parity
# claim is that make_t3_client returns the same T3Database class as
# direct-mode make_t3; the MVV is the end-to-end check that the
# claim holds when the injected _client is an HttpClient.
WRITER = dedent("""
    import json
    from nexus.daemon.t3_client import make_t3_client
    t3 = make_t3_client()
    id_a = t3.put("knowledge__rdr120_p1c_mvv", "alpha doc from writer", title="alpha")
    id_b = t3.put("knowledge__rdr120_p1c_mvv", "beta doc from writer", title="beta")
    info = t3.collection_info("knowledge__rdr120_p1c_mvv")
    print(json.dumps({"role": "writer", "count": info["count"], "ids": [id_a, id_b]}))
""")

READER = dedent("""
    import json
    from nexus.daemon.t3_client import make_t3_client
    t3 = make_t3_client()
    hits = t3.search("alpha", ["knowledge__rdr120_p1c_mvv"], n_results=2)
    titles = sorted([h.get("title", "") for h in hits])
    print(json.dumps({"role": "reader", "count": len(hits), "titles": titles}))
""")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    cd = tmp_path / "nexus_config"
    cd.mkdir()
    return cd


@pytest.fixture
def local_path(tmp_path: Path) -> Path:
    p = tmp_path / "chroma_t3"
    p.mkdir()
    return p


@pytest.fixture
def force_local_mode(monkeypatch):
    monkeypatch.setenv("NX_LOCAL", "1")


@pytest.fixture
def live_daemon(config_dir, local_path, force_local_mode):
    from nexus.daemon.t3_daemon import start_t3_daemon, stop_t3_daemon

    payload = start_t3_daemon(config_dir=config_dir, local_path=local_path)
    try:
        yield payload
    finally:
        stop_t3_daemon(config_dir=config_dir)


def _run(script: str, env: dict[str, str], label: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{label} exited {result.returncode}: stdout={result.stdout!r}, "
        f"stderr={result.stderr!r}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTwoSubprocessRoundTrip:
    """Acceptance for nexus-aim84: writer + reader subprocesses both
    connect via NX_T3_ADDR and share collection state."""

    def test_writer_then_reader_share_collection_via_env(
        self, live_daemon, monkeypatch
    ) -> None:
        addr = f"{live_daemon['tcp_host']}:{live_daemon['tcp_port']}"
        # Mirror parent env to subprocesses + override NX_T3_ADDR.
        import os
        env = {**os.environ, "NX_LOCAL": "1", "NX_T3_ADDR": addr}

        writer_out = _run(WRITER, env, "writer")
        assert writer_out["role"] == "writer"
        assert writer_out["count"] == 2

        reader_out = _run(READER, env, "reader")
        assert reader_out["role"] == "reader"
        assert reader_out["count"] == 2
        assert reader_out["titles"] == ["alpha", "beta"]

    def test_subprocesses_fail_loud_without_env_or_file(
        self, force_local_mode, tmp_path
    ) -> None:
        """RDR-120 C2: with no NX_T3_ADDR and no discovery file in
        view, ``make_t3_client()`` must raise T3DaemonError naming
        ``nx daemon t3 start`` as the fix. The subprocess should exit
        non-zero with the recovery hint on stderr."""
        import os

        # Point the subprocess at an empty config dir so the discovery
        # file lookup misses (NEXUS_CONFIG_DIR override).
        empty_config = tmp_path / "empty_config"
        empty_config.mkdir()
        env = {
            **os.environ,
            "NX_LOCAL": "1",
            "NEXUS_CONFIG_DIR": str(empty_config),
        }
        env.pop("NX_T3_ADDR", None)

        script = dedent("""
            from nexus.daemon.t3_client import make_t3_client
            make_t3_client()
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "nx daemon t3 start" in result.stderr
