# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite refuses to START when the engine substrate's prerequisites are
missing, instead of erroring every substrate-backed test at setup.

Two prerequisites behaved the same way: the service jar (missing, or older
than ``service/`` sources) and the pinned engine tag's PG bundle (the tag not
yet published, as on a release branch whose engine is still building).
Measured 2026-10-08: 25,636 setup errors on a full ``-n auto`` run, one fact
each time, at the cost of a full run. The preflight reads both once, on the
controller, after the lease gates, and exits 75 with one line naming the
cause and the remedy (the same footing as ``_gate_on_build_lease``).

Two layers. The first drives ``tests/_substrate_preflight.refusal`` and the
conftest wrapper with injected seams. The second drives a real child pytest,
because the property that matters is "the run exits 75 at second 2", and only
a real session start can show that. The child's prerequisites are made absent
through a small plugin that patches the substrate's own functions, so no test
here touches the network or the real ``service/`` tree.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import tests.conftest as ct
from tests import _substrate_preflight

REPO_ROOT = Path(__file__).resolve().parents[1]
#: A file with no substrate need of its own, so only the session start can
#: decide the child's exit status.
TARGET = "tests/scripts/test_git_push_develop_sh.py"
TAG = "engine-service-v0.0.0-unpublished"


# ---------------------------------------------------------------- the decision


def _bin(tmp_path: Path) -> Path:
    d = tmp_path / "pgbin"
    d.mkdir(exist_ok=True)
    return d


def test_a_healthy_setup_has_nothing_to_refuse(tmp_path: Path) -> None:
    got = _substrate_preflight.refusal(
        jar_reason=lambda: None, resolve_pg_bin=lambda: _bin(tmp_path),
        provision_failure=lambda: None, pinned_tag=lambda: TAG,
    )
    assert got is None


def test_a_jar_problem_is_refused_with_its_reason_and_the_escape() -> None:
    got = _substrate_preflight.refusal(
        jar_reason=lambda: "service jar is STALE: x.jar predates A.java (run: scripts/build-gate-jar.sh)",
        resolve_pg_bin=lambda: pytest.fail("the bundle must not be touched when the jar is bad"),
        provision_failure=lambda: None, pinned_tag=lambda: TAG,
    )
    assert got is not None
    assert got.startswith("engine substrate: refusing to start — ")
    assert "STALE" in got and "scripts/build-gate-jar.sh" in got
    assert "NX_TEST_T2_SUBSTRATE=none" in got


def test_an_unprovisionable_bundle_names_the_tag_and_says_it_may_be_unpublished(tmp_path: Path) -> None:
    got = _substrate_preflight.refusal(
        jar_reason=lambda: None, resolve_pg_bin=lambda: tmp_path / "absent",
        provision_failure=lambda: "BinaryAssetAbsentError: failed to download https://github.com/x",
        pinned_tag=lambda: TAG,
    )
    assert got is not None
    assert got.startswith("engine substrate: refusing to start — ")
    assert TAG in got
    assert "is not published (or its PG bundle is not downloadable)" in got
    assert "BinaryAssetAbsentError" in got
    assert "NX_TEST_T2_SUBSTRATE=none" in got and "wait for the engine release" in got.lower()


def test_a_bundle_resolution_that_raises_is_a_refusal_not_a_crash(tmp_path: Path) -> None:
    def _boom() -> Path:
        raise RuntimeError("explicit NEXUS_PG_BIN is broken")

    got = _substrate_preflight.refusal(
        jar_reason=lambda: None, resolve_pg_bin=_boom,
        provision_failure=lambda: None, pinned_tag=lambda: TAG,
    )
    assert got is not None and "RuntimeError" in got and "explicit NEXUS_PG_BIN is broken" in got


# ------------------------------------------------------------ the conftest wrapper


def _exit_code(fn) -> int | None:
    try:
        fn()
    except pytest.exit.Exception as exc:
        return exc.returncode
    return None


def test_the_wrapper_exits_75_on_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    monkeypatch.delenv("NX_SUITE_LEASE_HELD_BY", raising=False)
    monkeypatch.setattr(_substrate_preflight, "refusal", lambda: "engine substrate: refusing to start — boom")
    assert _exit_code(ct._preflight_engine_substrate) == 75


def test_the_wrapper_is_silent_when_nothing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    monkeypatch.delenv("NX_SUITE_LEASE_HELD_BY", raising=False)
    monkeypatch.setattr(_substrate_preflight, "refusal", lambda: None)
    assert _exit_code(ct._preflight_engine_substrate) is None


def test_a_no_substrate_run_skips_the_whole_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "none")
    monkeypatch.setattr(_substrate_preflight, "refusal", lambda: pytest.fail("must not be consulted"))
    assert _exit_code(ct._preflight_engine_substrate) is None


def test_a_nested_run_inside_a_lease_holder_skips_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    monkeypatch.setenv("NX_SUITE_LEASE_HELD_BY", str(os.getppid()))  # a live pid that is not this process
    monkeypatch.setattr(_substrate_preflight, "refusal", lambda: pytest.fail("must not be consulted"))
    assert _exit_code(ct._preflight_engine_substrate) is None


def test_the_holder_itself_is_preflighted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_take_suite_lease`` puts this pid in the holder variable just before the
    preflight runs, so "a holder is alive" alone would exempt the very run that
    holds the lease. Only a live holder that is some OTHER process is nesting."""
    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    monkeypatch.setenv("NX_SUITE_LEASE_HELD_BY", str(os.getpid()))
    monkeypatch.setattr(_substrate_preflight, "refusal", lambda: "engine substrate: refusing to start — boom")
    assert _exit_code(ct._preflight_engine_substrate) == 75


def test_a_preflight_bug_never_breaks_collection(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def _boom() -> str:
        raise OSError("disk gone")

    monkeypatch.delenv("NX_TEST_T2_SUBSTRATE", raising=False)
    monkeypatch.delenv("NX_SUITE_LEASE_HELD_BY", raising=False)
    monkeypatch.setattr(_substrate_preflight, "refusal", _boom)
    assert _exit_code(ct._preflight_engine_substrate) is None
    assert "preflight skipped on its own error" in capsys.readouterr().err


# ------------------------------------------------------ a real session start

#: A pytest plugin for the child. It patches the substrate's OWN functions, so
#: the child exercises the real freshness logic and the real provisioning
#: function against states that are absent by construction.
_SEAM = '''
import functools, os, zipfile
from pathlib import Path

import tests._engine_substrate as es
import tests.db._service_fixture as sf

mode = os.environ["PREFLIGHT_SEAM"]
root = Path(os.environ["PREFLIGHT_SEAM_DIR"])

if mode in ("missing_jar", "stale_jar"):
    jar = root / "seam.jar"
    if mode == "stale_jar":
        src = root / "src"
        src.mkdir()
        (src / "New.java").write_text("class New {}")
        with zipfile.ZipFile(jar, "w") as z:
            z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\\n")
        os.utime(jar, (1, 1))
        sf._SERVICE_SRC_DIRS = (src,)
    sf.jar_freshness_skip_reason = functools.partial(sf.jar_freshness_skip_reason, jar)
else:
    sf.jar_freshness_skip_reason = lambda *a, **k: None

if mode == "no_bundle":
    import nexus.daemon.binary_install as bi
    import nexus.db.pg_provision as pp

    bi.PINNED_SERVICE_TAG = os.environ["PREFLIGHT_SEAM_TAG"]

    def _absent(tag, cache_dir):
        raise bi.BinaryAssetAbsentError(f"failed to download https://example.invalid/{tag}")

    def _no_host_pg(*a, **k):
        raise pp.PgBinaryNotFoundError("no host pg")

    bi.install_pg_bundle = _absent
    pp.discover_pg_binaries = _no_host_pg
elif mode in ("healthy", "missing_jar", "stale_jar"):
    es._pg_bin = lambda: root
'''


def _child(tmp_path: Path, mode: str, **extra: str) -> subprocess.CompletedProcess:
    seam_dir = tmp_path / "seam"
    seam_dir.mkdir()
    (tmp_path / "_preflight_seam.py").write_text(_SEAM)
    drop = {
        "NX_TEST_T2_SUBSTRATE", "NX_BUILD_LEASE_WAIT", "NX_SUITE_LEASE_HELD_BY",
        "NX_SUITE_LEASE_WAIT", "NEXUS_PG_BIN", "NX_SUITE_LEASE_UNGUARDED",
    }
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(
        # Its own lease root: the child takes its own suite lease there rather
        # than refusing against the one THIS run holds, and no Maven run on the
        # box can gate it.
        NX_BUILD_LEASE_ROOT=str(tmp_path / "leases"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        PYTHONPATH=str(tmp_path),
        PYTEST_ADDOPTS="",
        PREFLIGHT_SEAM=mode,
        PREFLIGHT_SEAM_DIR=str(seam_dir),
        PREFLIGHT_SEAM_TAG=TAG,
        **extra,
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "_preflight_seam", "--collect-only", "-q",
         "-p", "no:cacheprovider", TARGET],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=240,
    )


def test_a_missing_jar_exits_75_at_session_start(tmp_path: Path) -> None:
    proc = _child(tmp_path, "missing_jar")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 75, out
    assert "engine substrate: refusing to start" in out
    assert "service jar not built" in out and "scripts/build-gate-jar.sh" in out


def test_a_stale_jar_exits_75_at_session_start(tmp_path: Path) -> None:
    proc = _child(tmp_path, "stale_jar")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 75, out
    assert "engine substrate: refusing to start" in out
    assert "service jar is STALE" in out and "New.java" in out and "scripts/build-gate-jar.sh" in out
    assert "SERVICE JAR STALE" not in out, "the old advisory banner must be gone, not doubled"


def test_an_unpublished_pinned_tag_exits_75_naming_it(tmp_path: Path) -> None:
    proc = _child(tmp_path, "no_bundle")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 75, out
    assert "engine substrate: refusing to start" in out
    assert TAG in out and "is not published (or its PG bundle is not downloadable)" in out
    assert "NX_TEST_T2_SUBSTRATE=none" in out


def test_a_no_substrate_run_goes_ahead_with_a_missing_jar(tmp_path: Path) -> None:
    proc = _child(tmp_path, "missing_jar", NX_TEST_T2_SUBSTRATE="none")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_healthy_setup_proceeds(tmp_path: Path) -> None:
    proc = _child(tmp_path, "healthy")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "refusing to start" not in proc.stdout + proc.stderr


def test_a_nested_run_inside_a_holder_is_not_preflighted(tmp_path: Path) -> None:
    proc = _child(tmp_path, "missing_jar", NX_SUITE_LEASE_HELD_BY=str(os.getppid()))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_preflight_runs_after_the_suite_lease_gate(tmp_path: Path) -> None:
    lease = tmp_path / "leases" / "suite"
    lease.mkdir(parents=True)
    (lease / "pid").write_text(f"{os.getpid()}\n")
    (lease / "label").write_text("a peer run\n")
    proc = _child(tmp_path, "missing_jar")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 75, out
    assert "suite lease: refusing to start" in out, out
    assert "service jar not built" not in out
