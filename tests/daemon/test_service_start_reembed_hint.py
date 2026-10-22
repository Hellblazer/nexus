# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 6 Step 3 (nexus-cnxjk): ``nx daemon service start`` names ``nx collection re-embed --all``
when a live collection is off the engine's profile, in each of its three output branches.

* plain: after the "Storage service running on" line, on stdout;
* ``--announce-stdout``: on stderr, with stdout still exactly one JSON object;
* ``--foreground``: from the supervisor, once it owns a running engine (the branch prints no running line).

Model tokens are inert; the catalog reader is a fake (no engine).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus import reembed_job
from nexus.commands import daemon as daemon_mod
from nexus.daemon import storage_service_daemon as ssd

BGE = "bge-base-en-v15-768"
VA = "voyage-model-a"


class _Reader:
    def __init__(self, rows, profile):
        self._rows, self._profile = rows, profile

    def list_collections(self):
        return self._rows

    def embedding_profile(self):
        return [{"content_type": ct, "embedding_model": m, "dimension": 3} for ct, m in self._profile.items()]


def _row(name, model, *, state="live", superseded_by=""):
    return {
        "name": name, "content_type": "docs", "owner_id": "1.1", "embedding_model": model,
        "lifecycle_state": state, "superseded_by": superseded_by,
    }


@pytest.fixture(autouse=True)
def _no_reembed_hint_read() -> None:
    """Overrides the daemon suite's switch-off: the hint's read is the subject here, against a fake catalog."""


@pytest.fixture
def off_profile(monkeypatch: pytest.MonkeyPatch):
    """A catalog with one live bge collection while the engine's profile is the (inert) Voyage model."""
    reader = _Reader([_row("docs__a", BGE)], {"docs": VA})
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **kw: reader)
    return reader


@pytest.fixture
def on_profile(monkeypatch: pytest.MonkeyPatch):
    reader = _Reader([_row("docs__a", VA), _row("docs__b", BGE, state="quarantine")], {"docs": VA})
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda **kw: reader)
    return reader


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """``ensure_storage_supervisor`` answers with a lease: the engine is up."""
    lease = SimpleNamespace(
        endpoint={"host": "127.0.0.1", "port": 18091, "pid": 4242}, generation=3,
    )
    monkeypatch.setattr(daemon_mod, "ensure_storage_supervisor", lambda config_dir, **kw: lease)
    return tmp_path


def _start(config_dir: Path, *extra: str):
    return CliRunner().invoke(daemon_mod.service_group, ["start", "--config-dir", str(config_dir), *extra])


RUNNING_LINE = "Storage service running on 127.0.0.1:18091 (pid=4242, generation=3)."


def test_plain_branch_prints_the_hint_after_the_running_line(running, off_profile) -> None:
    result = _start(running)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines == [RUNNING_LINE, reembed_job.REEMBED_HINT]


def test_plain_branch_prints_no_hint_without_an_off_profile_collection(running, on_profile) -> None:
    result = _start(running)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [RUNNING_LINE]


def test_announce_branch_keeps_stdout_one_json_object_and_puts_the_hint_on_stderr(running, off_profile) -> None:
    result = _start(running, "--announce-stdout")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"host": "127.0.0.1", "port": 18091, "pid": 4242, "generation": 3}
    assert result.stdout.count("\n") == 1, "stdout is the one JSON line and nothing else"
    assert result.stderr.splitlines() == [reembed_job.REEMBED_HINT]


def test_announce_branch_prints_nothing_on_stderr_without_an_off_profile_collection(running, on_profile) -> None:
    result = _start(running, "--announce-stdout")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["port"] == 18091
    assert result.stderr == ""


def test_an_unreadable_catalog_costs_the_start_nothing(running, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw):
        raise RuntimeError("engine not answering")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    result = _start(running)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [RUNNING_LINE]


# ── --foreground: the supervisor prints it ───────────────────────────────────


class _OwningSupervisor:
    """The least ``_supervise_until_stopped`` needs: it owns a running engine and beats until stopped."""

    owns_process = True
    fenced = False
    _scope = "test-scope"

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop
        self.events: list[str] = []

    def start(self, *, stop_requested=None) -> None:
        self.events.append("started")

    def heartbeat_once(self):
        self.events.append("beat")
        self._stop.set()
        return True, True

    def stop(self) -> None:
        self.events.append("stopped")


class _UnownedSupervisor(_OwningSupervisor):
    """``start()`` found an existing healthy lease and spawned nothing: the supervisor exits 0 at once."""

    owns_process = False


def _hint_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "reembed-hint-announce"]


def _supervise(sup_cls) -> tuple[int, _OwningSupervisor]:
    stop = threading.Event()
    sup = sup_cls(stop)
    code = ssd._supervise_until_stopped(sup, stop, lambda: None)  # type: ignore[arg-type]
    for t in _hint_threads():
        t.join(10)
    return code, sup


def test_supervisor_prints_the_hint_on_stderr_once_the_engine_is_up(off_profile, capsys) -> None:
    code, sup = _supervise(_OwningSupervisor)
    assert code == 0 and sup.events[0] == "started"
    out = capsys.readouterr()
    assert out.err.splitlines() == [reembed_job.REEMBED_HINT]
    assert out.out == ""


def test_supervisor_prints_nothing_without_an_off_profile_collection(on_profile, capsys) -> None:
    _supervise(_OwningSupervisor)
    assert capsys.readouterr().err == ""


def test_a_supervisor_that_owns_no_engine_prints_no_hint(off_profile, capsys) -> None:
    code, _ = _supervise(_UnownedSupervisor)
    assert code == 0
    assert capsys.readouterr().err == ""


def test_foreground_start_runs_the_supervisor_that_prints_it(
    off_profile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The ``--foreground`` branch of the command reaches the same supervisor loop; its own output stays empty."""
    seen: dict[str, object] = {}

    def fake_run_storage_supervisor(*, config_dir: Path) -> int:
        seen["config_dir"] = config_dir
        return ssd._supervise_until_stopped(
            _OwningSupervisor(stop := threading.Event()), stop, lambda: None,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(ssd, "run_storage_supervisor", fake_run_storage_supervisor)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", "")  # recorded, so the teardown removes what the command exports
    monkeypatch.delenv("NEXUS_CONFIG_DIR")
    with pytest.raises(SystemExit) as exit_info:
        daemon_mod.service_start_cmd.callback(config_dir_str=str(tmp_path), foreground=True, announce_stdout=False)
    for t in _hint_threads():
        t.join(10)
    assert exit_info.value.code == 0
    assert capsys.readouterr().err.splitlines() == [reembed_job.REEMBED_HINT]
