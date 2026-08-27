# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 P1.4: ``version_lockstep_action.py`` detached-action tests.

The action is the fire-and-forget, stdlib-only worker dispatched by the
hook. It owns the editable gate and the marker write. Contract:

- EDITABLE GATE FIRST (CA-3): no uv-tool receipt -> dev/editable tree ->
  SKIP (never clobber). Inline re-implementation of init.py:52-68.
- NO-OP FAST PATH: if installed ``nx --version`` already == target ->
  write marker, do not upgrade.
- TWO-COMMAND SAFE ACTION (CA-2), in strict order:
    1. ``uv tool upgrade conexus``  (binary, extras-preserving)
    2. ``nx upgrade``               (migrations only)
  NEVER raw ``uv tool install`` / ``--force`` / ``--reinstall`` (the
  dominant hazard that strips the ``[local]`` extra).
- MARKER ON CONFIRMED SUCCESS ONLY: after both commands succeed, re-read
  ``nx --version``; write the marker only if it now equals the target.
  Any failure leaves the marker stale so the next session retries.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "version_lockstep_action.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("version_lockstep_action", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


@pytest.fixture()
def marker(tmp_path: Path, monkeypatch) -> Path:
    m = tmp_path / "nexus" / "cli_lockstep_marker"
    monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(m))
    # nexus-pfuns: `mod.main()` calls `log_event()` (-> NX_LOCKSTEP_LOG,
    # real fallback `~/.config/nexus/lockstep.log`) on every code path past
    # the no-op fast path -- not just the two failure branches the
    # standalone `log` fixture below was originally written for. Every
    # test in this module that requests only `marker` and then calls
    # `mod.main(...)` (TestTwoCommandOrdering, TestMarkerOnConfirmedSuccess,
    # TestFailureLeavesMarkerStale, ...) leaked into the real path before
    # this. Isolating it here too -- the identical path `log` computes, so
    # a test requesting BOTH fixtures just re-sets it to the same value.
    monkeypatch.setenv("NX_LOCKSTEP_LOG", str(tmp_path / "nexus" / "lockstep.log"))
    return m


@pytest.fixture()
def log(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "nexus" / "lockstep.log"
    monkeypatch.setenv("NX_LOCKSTEP_LOG", str(p))
    return p


class TestMarkerFixtureIsolatesLockstepLogToo:
    def test_marker_only_still_isolates_log_env(self, mod, marker, monkeypatch) -> None:
        """nexus-pfuns regression pin: a test requesting only `marker` (no
        explicit `log`) must still get an isolated NX_LOCKSTEP_LOG, since
        `mod.main()` writes it on every path past the no-op fast path.
        Before the `marker` fixture set this env var, NX_LOCKSTEP_LOG was
        simply absent here and `log_path()`'s fallback resolved to the
        real `~/.config/nexus/lockstep.log`."""
        real_log = Path.home() / ".config" / "nexus" / "lockstep.log"
        isolated = os.environ.get("NX_LOCKSTEP_LOG", "")
        assert isolated, "marker fixture must set NX_LOCKSTEP_LOG"
        assert Path(isolated) != real_log
        assert Path(isolated).parent == marker.parent

    def test_marker_only_main_call_writes_under_tmp_not_real_home(
        self, mod, marker, monkeypatch
    ) -> None:
        """End-to-end: drive `mod.main()` down a path that logs (past the
        no-op fast path) using only the `marker` fixture, and assert the
        write landed under the isolated NX_LOCKSTEP_LOG, never the real
        home. Reproduces the exact leak shape evidenced in T2 nexus/
        gc-purge-marker-xdist-leak-2026-08-20 (installed=1.0.0/1.0.1 rows
        in Sam's real lockstep.log)."""
        real_log = Path.home() / ".config" / "nexus" / "lockstep.log"
        real_mtime_before = real_log.stat().st_mtime if real_log.exists() else None
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "1.0.1"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        isolated_log = Path(os.environ["NX_LOCKSTEP_LOG"])
        assert isolated_log.exists(), "log_event must have written somewhere"
        assert "installed=1.0.1" in isolated_log.read_text()
        real_mtime_after = real_log.stat().st_mtime if real_log.exists() else None
        assert real_mtime_after == real_mtime_before, (
            "must never touch the real ~/.config/nexus/lockstep.log"
        )


def _wire(mod, monkeypatch, *, receipt: bool, installed_versions, run_results):
    """Install the standard set of seam patches.

    installed_versions: list popped left-to-right on each installed_nx_version()
    run_results: dict mapping the first two argv tokens (e.g. "uv tool") -> bool
                 and records the call order in the returned list.
    """
    monkeypatch.setattr(mod, "uv_receipt_present", lambda: receipt)
    # Default the generation probe OFF so every pre-existing test keeps
    # exercising the uv-tool branch it was written against (nexus-utpuw.15).
    monkeypatch.setattr(mod, "generation_install_present", lambda: False)

    versions = list(installed_versions)

    def fake_installed() -> str | None:
        return versions.pop(0) if versions else None

    monkeypatch.setattr(mod, "installed_nx_version", fake_installed)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 0) -> bool:
        calls.append(list(cmd))
        key = " ".join(cmd[:2])
        return run_results.get(key, True)

    monkeypatch.setattr(mod, "run_cmd", fake_run)
    return calls


class TestScriptPresence:
    def test_script_exists(self) -> None:
        assert SCRIPT.exists()


class TestEditableGate:
    def test_skip_when_no_receipt(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=False,
            installed_versions=["1.0.0"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert calls == [], "editable/dev tree must not shell any upgrade"
        assert not marker.exists(), "must not clobber/write marker in a dev tree"

    def test_uv_receipt_present_inline_logic(self, mod, monkeypatch, tmp_path) -> None:
        """uv_receipt_present mirrors init.py:_uv_receipt_path: receipt file
        present under `uv tool dir`/conexus -> True."""
        tool_dir = tmp_path / "tools"
        (tool_dir / "conexus").mkdir(parents=True)
        (tool_dir / "conexus" / "uv-receipt.toml").write_text("")
        monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/uv")

        def fake_run(args, **k):
            class R:
                stdout = str(tool_dir) + "\n"
                returncode = 0
            return R()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        assert mod.uv_receipt_present() is True

    def test_uv_missing_means_no_receipt(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda c: None)
        assert mod.uv_receipt_present() is False


class TestNoOpFastPath:
    def test_already_matched_writes_marker_no_upgrade(
        self, mod, marker, monkeypatch
    ) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert calls == [], "no upgrade when already at target"
        assert marker.read_text().strip() == "9.9.9"


class TestDowngradeLoopBroken:
    def test_installed_ahead_of_target_writes_marker_no_upgrade(
        self, mod, marker, monkeypatch
    ) -> None:
        """Plugin ref pinned back below the installed CLI: `uv tool upgrade`
        could never reach the older target, so a strict-equality confirm would
        nudge forever. The >= semantics records lockstep and goes quiet."""
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["5.8.0"], run_results={},
        )
        mod.main(["action", "5.7.0"])
        assert calls == [], "no upgrade attempt when CLI is already ahead"
        assert marker.read_text().strip() == "5.7.0", (
            "marker must record the plugin target so the hook goes silent"
        )


class TestSatisfies:
    def test_equal_satisfies(self, mod) -> None:
        assert mod.satisfies("5.7.0", "5.7.0") is True

    def test_newer_satisfies(self, mod) -> None:
        assert mod.satisfies("5.8.0", "5.7.0") is True

    def test_older_does_not_satisfy(self, mod) -> None:
        assert mod.satisfies("5.6.2", "5.7.0") is False

    def test_none_does_not_satisfy(self, mod) -> None:
        assert mod.satisfies(None, "5.7.0") is False

    def test_unparseable_falls_back_to_equality(self, mod) -> None:
        assert mod.satisfies("garbage", "garbage") is True
        assert mod.satisfies("garbage", "5.7.0") is False


class TestTwoCommandOrdering:
    def test_upgrade_then_nx_upgrade_in_order(self, mod, marker, monkeypatch) -> None:
        # stale before, target after
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert len(calls) == 2
        assert calls[0][:4] == ["uv", "tool", "upgrade", "conexus"]
        assert calls[1][:2] == ["nx", "upgrade"]

    def test_never_raw_uv_tool_install(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        for cmd in calls:
            joined = " ".join(cmd)
            assert "uv tool install" not in joined
            assert "--force" not in cmd
            assert "--reinstall" not in cmd


class TestMarkerOnConfirmedSuccess:
    def test_marker_written_when_version_confirmed(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert marker.read_text().strip() == "9.9.9"

    def test_marker_creates_parent_dir(self, mod, marker, monkeypatch) -> None:
        assert not marker.parent.exists()
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert marker.parent.is_dir()


class TestFailureLeavesMarkerStale:
    def test_uv_upgrade_failure_no_marker(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"uv tool": False},
        )
        mod.main(["action", "9.9.9"])
        assert calls[0][:4] == ["uv", "tool", "upgrade", "conexus"]
        # nx upgrade must NOT run after uv failure
        assert len(calls) == 1
        assert not marker.exists()

    def test_nx_upgrade_failure_no_marker(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"nx upgrade": False},
        )
        mod.main(["action", "9.9.9"])
        assert len(calls) == 2
        assert not marker.exists()

    def test_version_still_mismatched_after_upgrade_no_marker(
        self, mod, marker, monkeypatch
    ) -> None:
        # both commands "succeed" but the installed version never reaches target
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "1.0.1"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert not marker.exists(), "marker only on confirmed match"

    def test_missing_target_arg_is_noop(self, mod, marker, monkeypatch) -> None:
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"], run_results={},
        )
        mod.main(["action"])  # no target version
        assert calls == []
        assert not marker.exists()

    def test_main_swallows_exceptions(self, mod, marker, monkeypatch) -> None:
        def boom() -> bool:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mod, "uv_receipt_present", boom)
        mod.main(["action", "9.9.9"])  # must not raise
        assert not marker.exists()


class TestLoudLog:
    """nexus-otnvr item 4: the actual venv-mutating step (`uv tool upgrade
    conexus`) must be LOUD — a durable, always-on log line, never gated
    behind NX_HOOK_DEBUG (which the live incident's operator did not have
    set, and which the hook's own DEVNULL'd stdout/stderr would have
    swallowed anyway)."""

    def test_success_logs_started_then_result(
        self, mod, marker, log, monkeypatch
    ) -> None:
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "9.9.9"], run_results={},
        )
        # Prove the log is NOT gated behind NX_HOOK_DEBUG: leave it unset/0.
        monkeypatch.setattr(mod, "DEBUG", False)
        mod.main(["action", "9.9.9"])
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 2, lines
        assert "lockstep_upgrade_started" in lines[0]
        assert "target=9.9.9" in lines[0]
        assert "installed=1.0.0" in lines[0]
        assert "lockstep_upgrade_result" in lines[1]
        assert "outcome=success" in lines[1]
        assert "installed=9.9.9" in lines[1]

    def test_uv_upgrade_failure_logs_that_outcome(
        self, mod, marker, log, monkeypatch
    ) -> None:
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"uv tool": False},
        )
        mod.main(["action", "9.9.9"])
        text = log.read_text()
        assert "lockstep_upgrade_started" in text
        assert "outcome=uv_upgrade_failed" in text

    def test_nx_upgrade_failure_logs_that_outcome(
        self, mod, marker, log, monkeypatch
    ) -> None:
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0"],
            run_results={"nx upgrade": False},
        )
        mod.main(["action", "9.9.9"])
        assert "outcome=nx_upgrade_failed" in log.read_text()

    def test_version_still_mismatched_logs_that_outcome(
        self, mod, marker, log, monkeypatch
    ) -> None:
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["1.0.0", "1.0.1"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        text = log.read_text()
        assert "outcome=version_still_mismatched" in text
        assert "installed=1.0.1" in text

    def test_editable_gate_skip_logs_nothing(self, mod, marker, log, monkeypatch) -> None:
        """No venv mutation is even attempted — nothing to log."""
        _wire(
            mod, monkeypatch, receipt=False,
            installed_versions=["1.0.0"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert not log.exists()

    def test_no_op_fast_path_logs_nothing(self, mod, marker, log, monkeypatch) -> None:
        """Already at target: no mutation attempted, nothing to log."""
        _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["9.9.9"], run_results={},
        )
        mod.main(["action", "9.9.9"])
        assert not log.exists()

    def test_log_event_never_raises_on_unwritable_path(self, mod, monkeypatch) -> None:
        # A path under a file (not a directory) can never be mkdir'd into.
        monkeypatch.setenv("NX_LOCKSTEP_LOG", "/dev/null/impossible/lockstep.log")
        mod.log_event("lockstep_upgrade_started", target="9.9.9")  # must not raise

    def test_log_rotates_past_max_bytes(self, mod, log, monkeypatch) -> None:
        """code-review Important-3: unbounded append gets a cheap cap. Set
        the threshold tiny so a couple of real log_event() calls exceed it
        without writing a real megabyte in a unit test."""
        monkeypatch.setenv("NX_LOCKSTEP_LOG_MAX_BYTES", "10")
        mod.log_event("lockstep_upgrade_started", target="1.0.0")  # > 10 bytes already
        first_content = log.read_text()
        assert first_content  # sanity: something was written

        mod.log_event("lockstep_upgrade_result", target="1.0.0", outcome="success")

        rotated = log.with_name(log.name + ".1")
        assert rotated.exists(), "log over threshold must rotate to .1"
        assert rotated.read_text() == first_content
        # The active file now holds ONLY the second event, not both.
        active_content = log.read_text()
        assert "lockstep_upgrade_started" not in active_content
        assert "lockstep_upgrade_result" in active_content

    def test_log_does_not_rotate_under_threshold(self, mod, log, monkeypatch) -> None:
        monkeypatch.setenv("NX_LOCKSTEP_LOG_MAX_BYTES", "1000000")
        mod.log_event("lockstep_upgrade_started", target="1.0.0")
        mod.log_event("lockstep_upgrade_result", target="1.0.0", outcome="success")
        rotated = log.with_name(log.name + ".1")
        assert not rotated.exists()
        content = log.read_text()
        assert "lockstep_upgrade_started" in content
        assert "lockstep_upgrade_result" in content

    def test_log_max_bytes_env_parse_falls_back_on_garbage(self, mod, monkeypatch) -> None:
        monkeypatch.setenv("NX_LOCKSTEP_LOG_MAX_BYTES", "not-a-number")
        assert mod._log_max_bytes() == mod._DEFAULT_LOG_MAX_BYTES


class TestVersionParsing:
    def test_installed_nx_version_parses_cli_output(self, mod, monkeypatch) -> None:
        def fake_run(args, **k):
            class R:
                stdout = "nx, version 5.7.0\n"
                returncode = 0
            return R()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/nx")
        assert mod.installed_nx_version() == "5.7.0"

    def test_installed_nx_version_none_when_absent(self, mod, monkeypatch) -> None:
        monkeypatch.setattr(mod.shutil, "which", lambda c: None)
        assert mod.installed_nx_version() is None


class TestGenerationLayout:
    """nexus-utpuw.15: the hook died SILENTLY under the generation layout.

    Gate 1 was ``uv_receipt_present()`` — shutil.which('uv'), `uv tool dir`,
    then <dir>/conexus/uv-receipt.toml must exist. Under generations that is
    False FOREVER, so the entire auto-upgrade no-opped: no error, no nudge-loop
    escape, the marker stayed stale and the hook re-nudged forever while the
    action did nothing.

    FIXING GATE 1 ALONE WOULD HAVE BEEN WORSE THAN THE BUG. Gate 3 ran
    ``uv tool upgrade conexus``, which on a generation box rebuilds the legacy
    uv tree and re-symlinks over the nexus-owned shims — nexus-utpuw.7's
    accepted risk, fired automatically by our own hook on every session start.
    The silent no-op was accidentally protective. Both gates move together or
    neither does.
    """

    def test_a_generation_install_is_detected_as_managed(self, mod, tmp_path, monkeypatch) -> None:
        tools = tmp_path / "tools"
        gen = tools / "gen-20260826T010000Z"
        gen.mkdir(parents=True)
        (tools / "current").symlink_to(gen)
        monkeypatch.setenv("NX_TOOLS_DIR", str(tools))

        assert mod.generation_install_present() is True

    def test_a_dangling_current_is_not_a_managed_install(self, mod, tmp_path, monkeypatch) -> None:
        """Fail-safe, matching uv_receipt_present's own posture: every edge
        case resolves to False rather than proceeding on a broken layout."""
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "current").symlink_to(tools / "gen-gone")
        monkeypatch.setenv("NX_TOOLS_DIR", str(tools))

        assert mod.generation_install_present() is False

    def test_no_layout_at_all_is_not_a_managed_install(self, mod, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_TOOLS_DIR", str(tmp_path / "nothing"))
        assert mod.generation_install_present() is False

    def test_a_generation_box_upgrades_via_nx_self_install(
        self, mod, marker, monkeypatch
    ) -> None:
        """THE REWIRE. `uv tool upgrade conexus` is replaced by the packaged
        installer (.14), which is safe under live sessions and carries extras
        forward out of nexus-install.json."""
        calls = _wire(
            mod, monkeypatch, receipt=False,
            installed_versions=["5.6.2", "5.7.0"], run_results={},
        )
        monkeypatch.setattr(mod, "generation_install_present", lambda: True)

        mod.main(["action", "5.7.0"])

        joined = [" ".join(c) for c in calls]
        assert any("nx self install" in j for j in joined), (
            f"the generation box did not use the packaged installer: {joined}"
        )
        assert not any("uv tool upgrade" in j for j in joined), (
            f"`uv tool upgrade` ran on a generation box — that rebuilds the "
            f"legacy uv tree and re-symlinks over the shims: {joined}"
        )

    def test_an_unmigrated_box_still_upgrades_via_uv(
        self, mod, marker, monkeypatch
    ) -> None:
        """NOT a blanket replacement. .7 leaves a box on the uv layout until
        its legacy tree has zero holders, and `uv tool upgrade conexus` remains
        the correct mechanism there. Replacing it unconditionally would break
        auto-upgrade on every un-migrated box — trading one silent failure for
        another."""
        calls = _wire(
            mod, monkeypatch, receipt=True,
            installed_versions=["5.6.2", "5.7.0"], run_results={},
        )

        mod.main(["action", "5.7.0"])

        joined = [" ".join(c) for c in calls]
        assert any("uv tool upgrade conexus" in j for j in joined), joined

    def test_the_migration_ladder_still_runs_in_both_shapes(
        self, mod, marker, monkeypatch
    ) -> None:
        """CA-2's second command is untouched by this bead. Binary upgrade and
        migration ladder stay two commands."""
        calls = _wire(
            mod, monkeypatch, receipt=False,
            installed_versions=["5.6.2", "5.7.0"], run_results={},
        )
        monkeypatch.setattr(mod, "generation_install_present", lambda: True)

        mod.main(["action", "5.7.0"])

        assert ["nx", "upgrade"] in calls, calls

    def test_raw_uv_tool_install_never_appears(self, mod, marker, monkeypatch) -> None:
        """THE ANTI-FOOTGUN CONTRACT, ported rather than dropped. A raw
        `uv tool install` / `--reinstall` / `--force` strips the [local] extra
        and reintroduces the 5.6.2 local-search P0 (768-dim embedder silently
        replaced by 384-dim against collections built at 768). It must never
        appear in either shape."""
        for generation in (True, False):
            calls = _wire(
                mod, monkeypatch, receipt=not generation,
                installed_versions=["5.6.2", "5.7.0"], run_results={},
            )
            monkeypatch.setattr(mod, "generation_install_present", lambda g=generation: g)
            mod.main(["action", "5.7.0"])
            joined = " ".join(" ".join(c) for c in calls)
            assert "uv tool install" not in joined, joined
            assert "--reinstall" not in joined, joined
            assert "--force" not in joined, joined


class TestInlineLayoutKnowledgeMatchesThePackage:
    """The hook runs under a BARE python3 (_run_python_hook.sh probes 3.13,
    3.12, then bare) and CANNOT import nexus, so its layout knowledge is
    duplicated inline by necessity — the bead says so explicitly and forbids
    "fixing" it with an import.

    Duplication that cannot be removed can still be PINNED. This test runs in
    the normal test interpreter, which can import both halves, and fails if
    they drift — the same discipline as install_layout's twins.
    """

    def test_the_default_tools_dir_matches_install_layout(self, mod, monkeypatch) -> None:
        from nexus import install_layout

        monkeypatch.delenv("NX_TOOLS_DIR", raising=False)
        assert mod.default_tools_dir() == install_layout.tools_dir(), (
            "the hook's inline tools-dir default has drifted from the package's; "
            "the hook would look for the layout somewhere it is not"
        )

    def test_the_env_override_name_matches(self, mod) -> None:
        from nexus import install_layout

        assert mod.TOOLS_DIR_ENV == install_layout.TOOLS_DIR_ENV
