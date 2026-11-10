# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline test of ``main()``'s own wiring (nexus-f3yqd.5, fix round 2).

``rehearse()`` is faked: what is under test is what ``main`` adds around it. The signal handlers are
installed by ``main``; the options that relax what is judged turn a pass into NOT THE RELEASE GATE
(exit 2) whether they come from the command line or, for the engine jar, from the environment.

* a SIGTERM reaches the handler ``main`` installed and the run leaves through its ``finally`` blocks,
* ``--standin`` and ``NEXUS_SERVICE_JAR`` each turn an otherwise clean pass into exit 2, and the
  same run without either is a PASSED exit 0 (so the exit 2 is theirs).
"""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_main", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_main"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

PUBLISHED = "7.76.0,7.99.0,8.0.0"


@pytest.fixture
def restore_signal_handlers():
    """main() installs SIGTERM/SIGHUP handlers in the pytest process: put the old ones back."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)
    ur._EXIT.__init__()


def _passing_rehearse(version: str, cfg: object) -> dict:
    steps = [
        ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0)
        for n in ur.JUDGED_STEPS
    ]
    return ur.build_verdict(
        from_version=version,
        target="conexus 9.9.9",
        steps=steps,
        findings=[],
        prepare={"verdict": "prepared", "collections": {"a": 60_000}},
        floor=1,
    )


def test_standin_turns_a_pass_into_not_the_release_gate_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_signal_handlers: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setattr(ur, "fetch_published_versions", lambda: PUBLISHED.split(","))
    monkeypatch.setattr(ur, "rehearse", _passing_rehearse)
    plain = tmp_path / "plain.json"
    rc = ur.main(
        [
            "--work",
            str(tmp_path / "w1"),
            "--target-release",
            "9.9.9",
            "--json-out",
            str(plain),
        ]
    )
    assert rc == 0 and json.loads(plain.read_text())["non_default_flags"] == [], (
        "the control: with no option set the same run is the release gate's PASSED"
    )
    assert (
        capsys.readouterr()
        .out.strip()
        .splitlines()[-1]
        .startswith("UPGRADE REHEARSAL GATE: PASSED")
    )
    flagged = tmp_path / "flagged.json"
    rc = ur.main(
        [
            "--work",
            str(tmp_path / "w2"),
            "--target-release",
            "9.9.9",
            "--standin",
            "--json-out",
            str(flagged),
        ]
    )
    verdict = json.loads(flagged.read_text())
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert rc == 2 and verdict["non_default_flags"] == ["--standin"]
    assert last.startswith("UPGRADE REHEARSAL GATE: NOT THE RELEASE GATE (--standin)")


def test_the_engine_jar_in_the_environment_turns_a_pass_into_not_the_release_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_signal_handlers: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jar = tmp_path / "engine.jar"
    jar.write_bytes(b"not a jar: only its presence is read")
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(jar))
    monkeypatch.setattr(ur, "fetch_published_versions", lambda: PUBLISHED.split(","))
    monkeypatch.setattr(ur, "rehearse", _passing_rehearse)
    out = tmp_path / "verdict.json"
    rc = ur.main(
        [
            "--work",
            str(tmp_path / "w"),
            "--target-release",
            "9.9.9",
            "--json-out",
            str(out),
        ]
    )
    verdict = json.loads(out.read_text())
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert rc == 2 and verdict["non_default_flags"] == ["--engine-jar"]
    assert last.startswith(
        "UPGRADE REHEARSAL GATE: NOT THE RELEASE GATE (--engine-jar)"
    )


def test_a_sigterm_reaches_the_handler_main_installed_and_the_run_unwinds(
    tmp_path: Path,
) -> None:
    ready, done = tmp_path / "ready", tmp_path / "finally-ran"
    script = tmp_path / "victim.py"
    script.write_text(
        "import importlib.util, pathlib, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('ur', {str(DRIVER)!r})\n"
        "ur = importlib.util.module_from_spec(spec); sys.modules['ur'] = ur; spec.loader.exec_module(ur)\n"
        "def rehearse(version, cfg):\n"
        "    try:\n"
        f"        pathlib.Path({str(ready)!r}).write_text('1')\n"
        "        time.sleep(60)\n"
        "    finally:\n"
        f"        pathlib.Path({str(done)!r}).write_text('1')\n"
        "ur.rehearse = rehearse\n"
        "sys.exit(ur.main(['--work', sys.argv[1], '--target-release', '9.9.9',"
        f" '--published-versions', {PUBLISHED!r}]))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path / "w")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), "main() never reached the rehearsal"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 128 + signal.SIGTERM
        assert done.exists(), "the run must leave through its finally blocks"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
