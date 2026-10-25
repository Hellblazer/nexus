# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A generation built without the release's dependency lock is rebuilt by
`nx upgrade`, once. nexus-au0s3.

The hop out of a release whose installer is older (7.85.0 to the first release
that holds dependencies to the lock) is built by the OLD installer, which has no
target-installer code, so that generation resolved fresh from the index. The
receipt's ``constraints`` field is what tells the new code what the build did:
the rung rebuilds only a registry build of the running version that recorded
``none`` or nothing at all. A deliberate ``unpinned`` build is never re-pinned
silently. The build itself is proven through the real builder in
tests/scripts/test_installer_rebuild_through_builder.py; here the decision, the
post-condition, the backoff and the rung are proven with the build faked.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

import pytest

from nexus import _vcrt, install_layout, pdf_stack
from nexus.commands import self_cmd
from nexus.commands import upgrade as upgrade_cmd

RUNNING = "9.9.9"
SENTINEL = "installer-rebuild-failed"
_REAL_INSTALL_PLAN = pdf_stack.install_plan
_REAL_REBUILD_GENERATION = pdf_stack._rebuild_generation


def _receipt(*, constraints: str = "unknown", version: str = RUNNING, extras: list[str] | None = None,
             source_kind: str = "registry", installer_schema: int = install_layout.INSTALLER_SCHEMA,
             ) -> install_layout.Receipt:
    extras = extras or []
    spec = f"conexus[{','.join(extras)}]" if extras else "conexus"
    return install_layout.Receipt(
        version=version, spec=spec, source_kind=source_kind, source="conexus", python="3.12",
        base_interpreter="/usr/bin", created_at="2026-10-01T00:00:00Z", extras=extras,
        installer_schema=installer_schema, constraints=constraints,
    )


@pytest.fixture
def bed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class _Bed:
        current: install_layout.Receipt | None = _receipt(constraints="none", extras=["local"])
        host: install_layout.Receipt = _receipt(constraints="applied")
        plan = pdf_stack.InstallPlan("generation", True, pdf_stack.INSTALL_COMMAND)
        builds: list[tuple[str, ...]] = []
        error: BaseException | None = None
        #: what the faked build leaves in `current`'s receipt
        leaves = "applied"
        ships_constraints = True

    b = _Bed()
    b.builds = []
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.delenv("NX_INSTALL_UNPINNED", raising=False)
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: RUNNING)
    monkeypatch.setattr(pdf_stack, "_package_ships_constraints", lambda: b.ships_constraints)
    monkeypatch.setattr(pdf_stack, "_current_generation_receipt",
                        lambda: None if b.current is None else (tmp_path / "gen-cur", b.current))
    # carry_forward_pending reads the RUNNING generation's receipt
    monkeypatch.setattr("nexus.install_layout.read_receipt", lambda _gen: b.host)
    monkeypatch.setattr("nexus.commands.self_cmd.running_generation", lambda: tmp_path / "gen-run")
    monkeypatch.setattr(pdf_stack, "install_plan", lambda: b.plan)

    def _build(extras: tuple[str, ...], *, after_flip=None) -> None:
        b.builds.append(extras)
        if b.error is not None:
            raise b.error
        b.current = _receipt(constraints=b.leaves, extras=list(extras))
        if after_flip is not None:
            after_flip(tmp_path / "gen-new")

    monkeypatch.setattr(pdf_stack, "_rebuild_generation", _build)
    return b


# ── the rule ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("recorded", [
    pytest.param("none", id="the-package-shipped-no-constraints"),
    pytest.param("unknown", id="an-older-installer-recorded-nothing"),
])
def test_a_current_generation_built_without_the_lock_is_rebuilt_once_at_its_own_version_and_extras(
    bed, recorded,
) -> None:
    bed.current = _receipt(constraints=recorded, extras=["local"])
    assert pdf_stack.installer_rebuild_pending()
    line = pdf_stack.rebuild_stale_installer()
    assert bed.builds == [("local",)], "one build, keeping current's extras, adding none"
    assert line is not None and "rebuilt" in line


def test_a_current_generation_is_never_rebuilt_twice_so_the_rule_cannot_loop(bed) -> None:
    pdf_stack.rebuild_stale_installer()
    assert bed.current.constraints == "applied"
    assert not pdf_stack.installer_rebuild_pending()
    for _ in range(3):
        assert pdf_stack.rebuild_stale_installer() is None
        assert pdf_stack.rebuild_stale_installer(auto_mode=True) is None
    assert len(bed.builds) == 1


@pytest.mark.parametrize("current", [
    pytest.param(None, id="no-readable-current"),
    pytest.param(_receipt(constraints="applied"), id="lock-applied"),
    pytest.param(_receipt(constraints="unpinned"), id="a-deliberate-unpinned-install-is-never-re-pinned"),
    pytest.param(_receipt(constraints="none", version="9.9.8"), id="older-release-is-the-ordinary-upgrades"),
    pytest.param(_receipt(constraints="none", version="10.0.0"), id="newer-release-than-this-process"),
    pytest.param(_receipt(constraints="none", source_kind="directory"), id="checkout-build"),
    pytest.param(_receipt(constraints="none", installer_schema=1), id="current-predates-the-pdf-split-the-carry-owns-it"),
])
def test_nothing_is_owed(bed, current) -> None:
    bed.current = current
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None
    assert bed.builds == []


def test_nothing_is_owed_when_the_running_package_ships_no_constraints_file(bed) -> None:
    """The rebuild would resolve fresh again: a build that cannot apply a lock is not owed."""
    bed.ships_constraints = False
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None and bed.builds == []


def test_nothing_is_owed_while_the_operator_has_opted_out_of_the_lock(bed, monkeypatch) -> None:
    """NX_INSTALL_UNPINNED=1 in this process would make the rebuild unpinned again."""
    monkeypatch.setenv("NX_INSTALL_UNPINNED", "1")
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None and bed.builds == []


@pytest.mark.parametrize("kind", ["directory", "legacy", "dev", "mcpb", "generation"])
def test_a_layout_the_auto_path_does_not_touch_is_left_alone(bed, kind) -> None:
    bed.plan = pdf_stack.InstallPlan(kind, False, "x")
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None and bed.builds == []


def test_while_the_pdf_carry_forward_is_owed_it_makes_the_build_and_this_makes_none(bed) -> None:
    """The 7.85.0 hop: one build. A pdf-less rebuild first would stamp a receipt that no
    longer predates the split; the stack would then never be carried."""
    bed.host = _receipt(installer_schema=1)  # pre-split, no pdf: the carry is owed
    assert pdf_stack.carry_forward_pending()
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None
    assert bed.builds == []


# ── the guards that read the REAL install plan and versions ──────────────────


@pytest.fixture
def real_plan(bed, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The host is a generation under the tools dir, and the plan is the real
    ``install_plan`` reading its receipt, not a faked plan."""
    host_dir = tmp_path / "tools" / "gen-20260101T000000Z"
    host_dir.mkdir(parents=True)
    monkeypatch.setattr("nexus.commands.self_cmd.running_generation", lambda: host_dir)
    monkeypatch.setattr(pdf_stack, "install_plan", _REAL_INSTALL_PLAN)
    return bed


def test_a_registry_built_host_with_a_stale_current_is_rebuilt(real_plan) -> None:
    """The control for the checkout-host test below."""
    assert pdf_stack.install_plan().kind == "generation"
    assert pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is not None and real_plan.builds == [("local",)]


def test_a_checkout_built_host_leaves_a_registry_current_alone(real_plan) -> None:
    """The process runs from a generation built from a checkout (reinstall-tool.sh): the auto path
    never touches a directory-source install, whatever `current` says."""
    real_plan.host = _receipt(constraints="applied", source_kind="directory")
    assert pdf_stack.install_plan().kind == "directory"
    assert real_plan.current.source_kind == "registry"
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None and real_plan.builds == []


@pytest.mark.parametrize("current_version", ["9.9.8", "10.0.0"])
def test_a_host_whose_version_differs_from_current_is_not_this_rungs(real_plan, current_version) -> None:
    """The process runs 9.9.9 and `current` names another release: the ordinary upgrade replaces an
    older one, and a newer one was built by something newer than this process."""
    real_plan.current = _receipt(constraints="none", version=current_version, extras=["local"])
    assert not pdf_stack.installer_rebuild_pending()
    assert pdf_stack.rebuild_stale_installer() is None and real_plan.builds == []


def test_a_peer_that_flipped_current_to_another_release_while_we_waited_is_left_alone(bed, monkeypatch) -> None:
    real_lock = pdf_stack._install_lock

    @contextlib.contextmanager
    def _peer_upgrades_first(*a, **k):
        with real_lock(*a, **k):
            bed.current = _receipt(constraints="none", version="10.0.0")
            yield

    monkeypatch.setattr(pdf_stack, "_install_lock", _peer_upgrades_first)
    pdf_stack.rebuild_stale_installer()
    assert bed.builds == []


# ── the lock, the post-condition, the failure ───────────────────────────────


def test_a_peer_that_rebuilt_while_we_waited_for_the_lock_leaves_nothing_to_do(bed, monkeypatch) -> None:
    real_lock = pdf_stack._install_lock

    @contextlib.contextmanager
    def _peer_finishes_first(*a, **k):
        with real_lock(*a, **k):
            bed.current = _receipt(constraints="applied")
            yield

    monkeypatch.setattr(pdf_stack, "_install_lock", _peer_finishes_first)
    line = pdf_stack.rebuild_stale_installer()
    assert bed.builds == []
    assert line is not None and "already rebuilt" in line


def test_a_failed_rebuild_is_reported_not_raised_and_current_stays_as_built(bed) -> None:
    bed.error = OSError("index unreachable")
    before = bed.current
    line = pdf_stack.rebuild_stale_installer()
    assert line is not None and "could not be rebuilt" in line and "keeps working" in line
    assert "nx upgrade" in line
    assert bed.current is before
    assert pdf_stack.installer_rebuild_pending(), "still owed: the next run retries"
    bed.error = None
    assert pdf_stack.rebuild_stale_installer() is not None  # an interactive run retries at once
    assert len(bed.builds) == 2


@pytest.mark.parametrize("leaves", ["unpinned", "none", "unknown"])
def test_a_build_that_succeeds_without_the_lock_is_a_failure_and_backs_off(bed, leaves) -> None:
    """The post-condition: current's receipt must now say `applied`. A build that "succeeds"
    without the lock (an installer fallback, a package without the file) would otherwise be
    reported as done and repeated at every session start."""
    bed.leaves = leaves
    line = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert line is not None and "could not be rebuilt" in line and "lock" in line
    sentinel = Path(os.environ["NEXUS_CONFIG_DIR"]) / SENTINEL
    assert sentinel.exists(), "a build that did not take is a failed attempt"
    again = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert len(bed.builds) == 1, "the next session start must not build again inside the backoff"
    # `unpinned` is a state the rung never touches, so there it has nothing to say; the others
    # are still owed and are held back by the marker.
    assert (again is None) if leaves == "unpinned" else (again is not None and "deferred" in again)


def test_the_auto_run_backs_off_after_a_failure_so_session_start_cannot_loop(bed) -> None:
    bed.error = OSError("offline")
    pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert len(bed.builds) == 1
    sentinel = Path(os.environ["NEXUS_CONFIG_DIR"]) / SENTINEL
    assert sentinel.exists()
    assert not (Path(os.environ["NEXUS_CONFIG_DIR"]) / "pdf-carry-forward-failed").exists(), (
        "the PDF carry-forward's marker is its own; this rung's failure must not read as its failure"
    )
    again = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert len(bed.builds) == 1, "a second session start inside the backoff must not fetch again"
    assert again is not None and "deferred" in again
    old = time.time() - pdf_stack.CARRY_FORWARD_AUTO_BACKOFF_S - 5
    os.utime(sentinel, (old, old))
    bed.error = None
    pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert len(bed.builds) == 2
    assert not sentinel.exists(), "a success clears the backoff"


# ── the real rebuild call: version, extras, progress ─────────────────────────


def test_the_rebuild_installs_exactly_currents_extras_at_the_running_version(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(pdf_stack, "_running_version", lambda: RUNNING)
    monkeypatch.setattr(
        "nexus.commands.self_cmd.perform_self_install", lambda **kw: seen.update(kw),
    )
    pdf_stack._rebuild_generation(("local",))
    assert seen == {"exact_extras": ("local",), "version": RUNNING, "progress": True, "after_flip": None}


# ── what the user is told ────────────────────────────────────────────────────


def test_the_waiters_line_names_the_build_that_holds_the_lock(monkeypatch, tmp_path) -> None:
    from nexus import _locking

    said: list[str] = []
    monkeypatch.setattr(install_layout, "tools_dir", lambda: tmp_path / "tools")

    def lock_fd(fd: int, *, blocking: bool) -> None:
        if not blocking:
            raise BlockingIOError("held")

    monkeypatch.setattr(_locking, "lock_fd", lock_fd)
    monkeypatch.setattr(_locking, "unlock_fd", lambda fd: None)
    monkeypatch.setattr(pdf_stack, "_say", said.append)
    with pdf_stack._install_lock("rebuilding the current generation"):
        pass
    assert said == ["Another process is rebuilding the current generation; waiting for it to finish."]
    said.clear()
    with pdf_stack._install_lock():
        pass
    assert said == ["Another process is installing PDF support; waiting for it to finish."], "the default is unchanged"


def test_the_note_names_a_pending_rebuild_and_the_retry_command(bed) -> None:
    note = pdf_stack.installer_rebuild_note()
    assert note is not None and "nx upgrade" in note and "lock" in note and "last attempt" not in note


def test_the_note_names_a_failed_attempt_and_when(bed) -> None:
    bed.error = OSError("offline")
    pdf_stack.rebuild_stale_installer()
    note = pdf_stack.installer_rebuild_note()
    assert note is not None and "failed" in note and "nx upgrade" in note


def test_there_is_no_note_when_nothing_is_owed(bed) -> None:
    """A virgin box, a rebuilt one and a deliberately unpinned one are all silent."""
    for current in (None, _receipt(constraints="applied"), _receipt(constraints="unpinned")):
        bed.current = current
        assert pdf_stack.installer_rebuild_note() is None


def test_an_old_failed_marker_is_silent_once_nothing_is_owed(bed) -> None:
    bed.error = OSError("offline")
    pdf_stack.rebuild_stale_installer()
    bed.current = _receipt(constraints="applied")
    assert pdf_stack.installer_rebuild_note() is None


# ── the rung in `nx upgrade` ─────────────────────────────────────────────────


def test_nx_upgrade_runs_the_rebuild_and_reports_it(bed, capsys) -> None:
    upgrade_cmd._converge_installer_rebuild(auto_mode=False, skip_t3=False)
    assert bed.builds == [("local",)]
    assert "Installer: rebuilt the current generation" in capsys.readouterr().out


def test_nx_upgrade_auto_logs_instead_of_printing(bed, capsys) -> None:
    upgrade_cmd._converge_installer_rebuild(auto_mode=True, skip_t3=False)
    assert len(bed.builds) == 1
    assert capsys.readouterr().out == ""


def test_skip_t3_skips_the_download(bed) -> None:
    upgrade_cmd._converge_installer_rebuild(auto_mode=False, skip_t3=True)
    assert bed.builds == []


def test_a_crash_in_the_rung_never_fails_the_upgrade(bed, monkeypatch) -> None:
    monkeypatch.setattr(pdf_stack, "rebuild_stale_installer", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    upgrade_cmd._converge_installer_rebuild(auto_mode=False, skip_t3=False)  # does not raise


def test_the_rebuild_runs_after_the_carry_and_before_the_process_cycle(bed, monkeypatch) -> None:
    """The cycle restarts the service onto `current`; the rebuild flips it, so it
    goes first. After the pdf carry, because owing the carry suppresses the
    rebuild (they are one build)."""
    order: list[str] = []
    monkeypatch.setattr("nexus.commands.self_cmd.repair_uv_takeover", lambda **kw: order.append("takeover") or [])
    monkeypatch.setattr(
        "nexus.upgrade_ladder.preconditions.converge_preconditions",
        lambda **kw: order.append("process-cycle") or [],
    )
    monkeypatch.setattr(upgrade_cmd, "_converge_vc_runtime", lambda **kw: order.append("vc-runtime"))
    monkeypatch.setattr(upgrade_cmd, "_converge_pdf_stack", lambda **kw: order.append("carry"))
    monkeypatch.setattr(pdf_stack, "_rebuild_generation", lambda extras, **kw: order.append("rebuild"))
    upgrade_cmd._converge_preconditions(auto_mode=True)
    assert order == ["takeover", "vc-runtime", "carry", "rebuild", "process-cycle"], order


# ── the doctor row ───────────────────────────────────────────────────────────


def test_the_doctor_row_is_a_warning_with_the_retry_command_when_the_rebuild_is_owed(bed) -> None:
    from nexus import health

    (row,) = health._check_installer_rebuild()
    assert row.label == "Dependency lock"
    assert row.ok and row.warn and not row.fatal, "a warning, not red: the install keeps working"
    assert "nx upgrade" in row.detail
    assert row.fix_suggestions and "nx upgrade" in row.fix_suggestions[0]


def test_the_doctor_row_says_when_the_last_attempt_failed(bed) -> None:
    from nexus import health

    bed.error = OSError("offline")
    pdf_stack.rebuild_stale_installer()
    (row,) = health._check_installer_rebuild()
    assert row.warn and "failed" in row.detail


@pytest.mark.parametrize("current", [
    pytest.param(None, id="virgin-box-has-no-current"),
    pytest.param(_receipt(constraints="applied"), id="lock-applied"),
    pytest.param(_receipt(constraints="unpinned"), id="deliberately-unpinned"),
])
def test_the_doctor_row_is_not_applicable_when_nothing_is_owed(bed, current) -> None:
    from nexus import health

    bed.current = current
    assert health._check_installer_rebuild() == []


def test_the_default_run_asks_the_row() -> None:
    import inspect

    from nexus import health

    assert "_check_installer_rebuild()" in inspect.getsource(health.run_health_checks)


# ── a failed post-check puts current (and previous) back (nexus-ff366) ───────


def _layout_with_a_real_flip(bed, monkeypatch, tmp_path: Path, *, with_previous: bool):
    """Real ``current``/``previous`` links under the bed's tools dir, and a faked build that
    flips to a new generation with the REAL flip helper, as ``perform_self_install`` does."""
    tools = tmp_path / "tools"
    tools.mkdir()
    older, original, rebuilt = (tools / f"gen-2026100{n}T000000Z" for n in (1, 2, 3))
    for generation in (older, original, rebuilt):
        generation.mkdir()
    os.symlink(str(original), tools / "current")
    if with_previous:
        os.symlink(str(older), tools / "previous")

    def build(extras: tuple[str, ...], *, after_flip=None) -> None:
        bed.builds.append(extras)
        self_cmd._sh(self_cmd.packaged_install_dir(), f'nx_flip_current "{rebuilt}" "{tools}"')
        bed.current = _receipt(constraints=bed.leaves, extras=list(extras))
        if after_flip is not None:
            after_flip(rebuilt)

    monkeypatch.setattr(pdf_stack, "_rebuild_generation", build)
    return tools, older, original, rebuilt


@pytest.mark.parametrize("with_previous", [True, False])
def test_a_build_that_leaves_current_unpinned_ends_with_current_and_previous_back_where_they_were(
    bed, monkeypatch, tmp_path, with_previous,
) -> None:
    bed.leaves = "none"
    tools, older, original, rebuilt = _layout_with_a_real_flip(bed, monkeypatch, tmp_path, with_previous=with_previous)
    line = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert len(bed.builds) == 1
    assert os.readlink(tools / "current") == str(original), "current is back on the generation it replaced"
    if with_previous:
        assert os.readlink(tools / "previous") == str(older), "previous is what it was before the flip"
    else:
        assert not (tools / "previous").is_symlink(), "there was no previous, and the flip's is dropped"
    assert rebuilt.is_dir() and original.is_dir(), "no generation was deleted by the rollback"
    assert (Path(os.environ["NEXUS_CONFIG_DIR"]) / SENTINEL).exists(), "the backoff marker is still written"
    assert line is not None and "could not be rebuilt" in line and original.name in line


def test_a_build_that_applies_the_lock_leaves_current_on_the_new_generation(bed, monkeypatch, tmp_path) -> None:
    tools, older, original, rebuilt = _layout_with_a_real_flip(bed, monkeypatch, tmp_path, with_previous=True)
    assert "rebuilt" in pdf_stack.rebuild_stale_installer()
    assert os.readlink(tools / "current") == str(rebuilt)
    assert os.readlink(tools / "previous") == str(original)


def test_a_rollback_that_cannot_run_is_reported_and_leaves_current_on_a_working_generation(
    bed, monkeypatch, tmp_path,
) -> None:
    bed.leaves = "none"
    tools, older, original, rebuilt = _layout_with_a_real_flip(bed, monkeypatch, tmp_path, with_previous=True)

    def refuse(*args, **kwargs):
        raise self_cmd.click.ClickException("no")

    monkeypatch.setattr(self_cmd, "restore_current_generation", refuse)
    line = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert line is not None and "could not be moved back" in line
    assert os.readlink(tools / "current") == str(rebuilt) and rebuilt.is_dir(), "current names a built generation"


def test_on_windows_the_rollback_goes_through_the_junction_helpers_and_retargets_the_task(
    monkeypatch, tmp_path,
) -> None:
    """Junctions are not symlinks: the flip is `generation_core.flip_current` (which also repoints
    the logon task and the PATH entry) and `previous` is a `swap_link`, never the bash helpers."""
    tools = tmp_path / "tools"
    original, older = tools / "gen-1", tools / "gen-0"
    original.mkdir(parents=True)
    older.mkdir()
    calls: list[tuple] = []

    class Core:
        class GenerationError(Exception):
            pass

        @staticmethod
        def flip_current(generation, where, *, platform):
            calls.append(("flip", generation, where))

        @staticmethod
        def current_launcher_dir(where):
            return where / "current" / "bin"

        @staticmethod
        def ensure_user_path(entry):
            calls.append(("path", entry))
            return type("R", (), {"changed": False, "entry": entry})()

        @staticmethod
        def swap_link(target, link):
            calls.append(("previous", target, link))

    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    monkeypatch.setattr(self_cmd, "_is_windows", lambda: True)
    monkeypatch.setattr(self_cmd, "_generation", lambda: Core)
    monkeypatch.setattr(self_cmd, "_retarget_autostart_task", lambda generation: calls.append(("task", generation)))
    monkeypatch.setattr(self_cmd, "_sh", lambda *a, **k: pytest.fail("a bash helper ran on Windows"))
    self_cmd.restore_current_generation(original, previous=str(older))
    assert [c[0] for c in calls] == ["flip", "path", "task", "previous"]
    assert calls[0][1] == original and calls[3][1] == str(older)


def test_a_generation_that_is_gone_is_never_flipped_to(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(self_cmd, "_sh", lambda *a, **k: pytest.fail("flipped to a missing generation"))
    with pytest.raises(self_cmd.click.ClickException, match="is gone"):
        self_cmd.restore_current_generation(tmp_path / "tools" / "gen-gone", previous=None)


# ── the post-check runs BEFORE the reap (nexus-ff366) ─────────────────────────


def _perform_self_install_with_a_real_flip_and_reap(bed, monkeypatch, tmp_path: Path):
    """The REAL ``_rebuild_generation`` and ``perform_self_install``, with only the build faked: it
    flips to a new generation with the real flip helper. Five generations are on disk, so a keep-3
    reap that runs right after the flip removes the oldest, which is what ``previous`` named."""
    tools = tmp_path / "tools"
    tools.mkdir()
    gens = {n: tools / f"gen-2026100{n}T000000Z" for n in (1, 2, 3, 4, 5)}
    for generation in gens.values():
        generation.mkdir()
        # a COMPLETE generation: the reap counts receipted trees against keep and keeps a
        # recently written receipt-less one as a build in progress
        (generation / install_layout.RECEIPT_NAME).write_text(_receipt().to_json())
    os.symlink(str(gens[2]), tools / "current")
    os.symlink(str(gens[1]), tools / "previous")
    monkeypatch.setenv("NX_INSTALL_RUNNING_INSTALLER", "1")
    monkeypatch.setattr(pdf_stack, "_rebuild_generation", _REAL_REBUILD_GENERATION)
    monkeypatch.setattr(self_cmd, "running_generation", lambda: gens[2])
    monkeypatch.setattr(self_cmd, "_register_legacy_tree_if_present", lambda *a, **k: None)

    def fake_build(build, **kwargs):
        bed.builds.append(build)
        self_cmd._sh(self_cmd.packaged_install_dir(), f'nx_flip_current "{gens[5]}" "{tools}"')
        bed.current = _receipt(constraints=bed.leaves)
        return gens[5]

    monkeypatch.setattr(self_cmd, "_build_flip_shims", fake_build)
    return tools, gens


def test_a_failed_post_check_rolls_back_before_the_reap_can_remove_the_old_previous(
    bed, monkeypatch, tmp_path,
) -> None:
    bed.leaves = "none"
    tools, gens = _perform_self_install_with_a_real_flip_and_reap(bed, monkeypatch, tmp_path)
    line = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert os.readlink(tools / "current") == str(gens[2])
    assert os.readlink(tools / "previous") == str(gens[1]), "the generation previous named is still the rollback target"
    assert gens[1].is_dir(), "the keep-3 reap did not take it before the check ran"
    assert line is not None and "current is back on" in line and "previous was" not in line


def test_a_passing_post_check_reaps_after_it(bed, monkeypatch, tmp_path) -> None:
    tools, gens = _perform_self_install_with_a_real_flip_and_reap(bed, monkeypatch, tmp_path)
    assert "rebuilt" in pdf_stack.rebuild_stale_installer()
    assert os.readlink(tools / "current") == str(gens[5])
    assert os.readlink(tools / "previous") == str(gens[2])
    assert not gens[1].exists(), "an ordinary upgrade still reaps what keep-3 releases"


def test_the_reap_runs_after_the_check_and_still_runs_when_the_check_raises(monkeypatch, tmp_path) -> None:
    order: list[str] = []
    generation = tmp_path / "tools" / "gen-20261001T000000Z"
    host = tmp_path / "tools" / "gen-20260901T000000Z"
    for g in (generation, host):
        g.mkdir(parents=True)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setattr(install_layout, "read_receipt", lambda _g: _receipt())
    monkeypatch.setattr(self_cmd, "running_generation", lambda: host)
    monkeypatch.setattr(self_cmd, "_build_flip_shims", lambda *a, **k: generation)
    monkeypatch.setattr(self_cmd, "_reap_generations", lambda *a, **k: order.append("reap") or [])
    monkeypatch.setattr(self_cmd, "_register_legacy_tree_if_present", lambda *a, **k: None)
    monkeypatch.setenv("NX_INSTALL_RUNNING_INSTALLER", "1")

    def check(_g: Path) -> None:
        order.append("check")
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError, match="refused"):
        self_cmd.perform_self_install(after_flip=check)
    assert order == ["check", "reap"]


# ── the account says what the pointers ARE (re-read), not what was attempted ──


def _flipped_layout(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    older, original, rebuilt = (tools / f"gen-2026100{n}T000000Z" for n in (1, 2, 3))
    for generation in (older, original, rebuilt):
        generation.mkdir()
    os.symlink(str(rebuilt), tools / "current")
    os.symlink(str(original), tools / "previous")
    return tools, older, original, rebuilt


def test_a_previous_that_was_removed_after_the_flip_is_not_claimed_as_restored(monkeypatch, tmp_path) -> None:
    tools, older, original, rebuilt = _flipped_layout(tmp_path)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    shutil.rmtree(older)
    line = pdf_stack._put_current_back(original, str(older))
    assert os.readlink(tools / "current") == str(original)
    assert "current is back on" in line and "previous was gen-20261001T000000Z" in line
    assert "no rollback target" in line


def test_a_previous_swap_that_fails_after_current_moved_back_does_not_say_current_stays(
    monkeypatch, tmp_path,
) -> None:
    tools, older, original, rebuilt = _flipped_layout(tmp_path)
    monkeypatch.setenv("NX_TOOLS_DIR", str(tools))
    real_restore = self_cmd.restore_current_generation

    def moves_current_then_fails(generation, *, previous):
        self_cmd._sh(self_cmd.packaged_install_dir(), f'nx_flip_current "{generation}" "{tools}"')
        raise self_cmd.click.ClickException("could not restore `previous`")

    monkeypatch.setattr(self_cmd, "restore_current_generation", moves_current_then_fails)
    line = pdf_stack._put_current_back(original, str(older))
    assert os.readlink(tools / "current") == str(original)
    assert "stays on the rebuilt generation" not in line
    assert "current is back on" in line and "previous was gen-20261001T000000Z" in line
    assert real_restore is not moves_current_then_fails


# ── the rebuild does not download torch onto a Windows box that cannot load it (nexus-vyy65) ──


def _windows_box_without_atomic_wait(monkeypatch, tmp_path: Path) -> None:
    sysdir = tmp_path / "System32"
    sysdir.mkdir()
    monkeypatch.setattr(_vcrt, "is_windows", lambda platform=None: True)
    monkeypatch.setattr(_vcrt, "system_dir", lambda: str(sysdir))


def test_a_rebuild_with_pdf_extras_on_a_windows_box_with_a_runtime_gap_builds_nothing(
    bed, monkeypatch, tmp_path,
) -> None:
    bed.current = _receipt(constraints="none", extras=["local", "pdf"])
    _windows_box_without_atomic_wait(monkeypatch, tmp_path)
    line = pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert bed.builds == [], "no download for a stack that cannot load"
    assert line is not None and "not rebuilt" in line and pdf_stack.VCRT_URL in line
    assert bed.current.constraints == "none", "current is left as it was"
    assert not (Path(os.environ["NEXUS_CONFIG_DIR"]) / SENTINEL).exists(), "a refusal is not a failed attempt"
    # and it is not retried against the backoff window: the next run refuses again, still building nothing
    assert "not rebuilt" in pdf_stack.rebuild_stale_installer(auto_mode=True)
    assert bed.builds == []


def test_a_rebuild_without_pdf_extras_is_not_gated_by_the_windows_runtime(bed, monkeypatch, tmp_path) -> None:
    bed.current = _receipt(constraints="none", extras=["local"])
    _windows_box_without_atomic_wait(monkeypatch, tmp_path)
    line = pdf_stack.rebuild_stale_installer()
    assert bed.builds == [("local",)] and line is not None and "rebuilt" in line


def test_a_rebuild_with_pdf_extras_and_the_whole_runtime_builds(bed, monkeypatch, tmp_path) -> None:
    bed.current = _receipt(constraints="none", extras=["local", "pdf"])
    _windows_box_without_atomic_wait(monkeypatch, tmp_path)
    for n in _vcrt.PDF_STACK_DLLS:
        (tmp_path / "System32" / n).write_bytes(b"MZ")
    assert pdf_stack.rebuild_stale_installer() is not None
    assert bed.builds == [("local", "pdf")]
