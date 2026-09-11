# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-143 P1.3: ``version_lockstep_hook.py`` logic tests.

The hook is the blocking, stdlib-only SessionStart entry point for the
plugin<->CLI version lockstep (Shape B). Contract:

- Read plugin version from ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json``.
- Read the marker ``~/.config/nexus/cli_lockstep_marker``.
- marker == plugin version  -> silent, no stdout, no dispatch.
- mismatch (or missing marker) -> emit additionalContext nudge JSON AND
  dispatch the detached action passing the target plugin version, then
  return immediately (never wedge synchronous SessionStart, CA-4).
- The hook NEVER writes the marker (the detached action owns that, on
  confirmed upgrade only).
- Any failure is swallowed: the hook always completes without raising
  and emits nothing on error (fail-safe exit 0).

These tests import the script as a module (stdlib-only, so it imports
cleanly under the test interpreter) and exercise the logic functions
with monkeypatched seams. A separate test pins that the script also runs
end-to-end under a bare interpreter.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus" / "hooks" / "scripts" / "version_lockstep_hook.py"
)


def _load_module():
    """Load the hook script as a fresh module object."""
    spec = importlib.util.spec_from_file_location("version_lockstep_hook", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load_module()


@pytest.fixture()
def plugin_root(tmp_path: Path) -> Path:
    """A fake CLAUDE_PLUGIN_ROOT with a plugin.json carrying a version."""
    pj = tmp_path / ".claude-plugin"
    pj.mkdir(parents=True)
    (pj / "plugin.json").write_text(json.dumps({"name": "conexus", "version": "9.9.9"}))
    return tmp_path


class TestScriptPresence:
    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), (
            f"hooks.json wiring (P1.5) references {SCRIPT}; "
            f"if it moves SessionStart breaks silently"
        )


class TestReadPluginVersion:
    def test_reads_version_from_plugin_json(self, mod, plugin_root, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        assert mod.read_plugin_version() == "9.9.9"

    def test_missing_root_returns_none(self, mod, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert mod.read_plugin_version() is None

    def test_missing_file_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        assert mod.read_plugin_version() is None

    def test_malformed_json_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        pj = tmp_path / ".claude-plugin"
        pj.mkdir()
        (pj / "plugin.json").write_text("{not json")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        assert mod.read_plugin_version() is None


class TestMarker:
    def test_marker_path_honors_env_override(self, mod, tmp_path, monkeypatch) -> None:
        target = tmp_path / "marker"
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(target))
        assert mod.marker_path() == target

    def test_default_marker_location(self, mod, monkeypatch) -> None:
        monkeypatch.delenv("NX_LOCKSTEP_MARKER", raising=False)
        p = mod.marker_path()
        assert p.name == "cli_lockstep_marker"
        assert p.parent.name == "nexus"
        assert ".config" in str(p)

    def test_read_marker_missing_returns_none(self, mod, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        assert mod.read_marker() is None

    def test_read_marker_strips_whitespace(self, mod, tmp_path, monkeypatch) -> None:
        m = tmp_path / "marker"
        m.write_text("5.7.0\n")
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(m))
        assert mod.read_marker() == "5.7.0"


class TestNudgeContract:
    def test_build_context_is_sessionstart_additional_context(self, mod) -> None:
        payload = json.loads(mod.build_context("9.9.9"))
        hs = payload["hookSpecificOutput"]
        assert hs["hookEventName"] == "SessionStart"
        assert "9.9.9" in hs["additionalContext"]

    def test_nudge_has_no_em_dash(self, mod) -> None:
        assert "—" not in mod.build_context("9.9.9")


class TestMainOrchestration:
    def test_match_is_silent_and_no_dispatch(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"
        marker.write_text("9.9.9")
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert capsys.readouterr().out.strip() == ""
        assert dispatched == []

    def test_mismatch_emits_nudge_and_dispatches(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"
        marker.write_text("1.0.0")  # stale
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert dispatched == ["9.9.9"]

    def test_missing_marker_treated_as_mismatch(
        self, mod, plugin_root, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert dispatched == ["9.9.9"]

    def test_hook_never_writes_marker(
        self, mod, plugin_root, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        marker = tmp_path / "marker"  # does not exist
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        monkeypatch.setattr(mod, "dispatch_action", lambda v: None)

        mod.main()

        assert not marker.exists(), "the HOOK must never write the marker (action owns it)"

    def test_unreadable_plugin_version_is_silent_no_dispatch(
        self, mod, tmp_path, monkeypatch, capsys
    ) -> None:
        # No plugin.json -> read_plugin_version None -> nothing to do.
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(tmp_path / "absent"))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert capsys.readouterr().out.strip() == ""
        assert dispatched == []

    def test_main_swallows_exceptions(self, mod, monkeypatch, capsys) -> None:
        def boom() -> str:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(mod, "read_plugin_version", boom)
        # Must not raise; fail-safe exit 0.
        mod.main()
        assert capsys.readouterr().out.strip() == ""


class TestDispatchIsNonBlocking:
    def test_dispatch_returns_immediately_for_slow_action(
        self, mod, monkeypatch
    ) -> None:
        """dispatch_action must detach (not wait). We stub Popen and assert
        the hook does not call .wait()/.communicate() on the child."""
        calls: dict[str, object] = {}

        class FakePopen:
            def __init__(self, *a, **k):
                calls["args"] = a[0] if a else k.get("args")
                calls["started"] = True
                calls["start_new_session"] = k.get("start_new_session")
                calls["stdout"] = k.get("stdout")
                calls["stderr"] = k.get("stderr")
                calls["stdin"] = k.get("stdin")

            def wait(self, *a, **k):  # pragma: no cover - must not be called
                calls["waited"] = True

            def communicate(self, *a, **k):  # pragma: no cover
                calls["communicated"] = True

        monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
        mod.dispatch_action("9.9.9")

        assert calls.get("started") is True
        assert "waited" not in calls
        assert "communicated" not in calls
        # Detach contract: own session (so a SIGTERM to the parent group does
        # not kill the in-flight upgrade) and no inherited stdio.
        assert calls.get("start_new_session") is True
        assert calls.get("stdout") is mod.subprocess.DEVNULL
        assert calls.get("stderr") is mod.subprocess.DEVNULL
        assert calls.get("stdin") is mod.subprocess.DEVNULL
        # The detached command must carry the target version as an argv token.
        flat = " ".join(map(str, calls["args"])) if isinstance(calls["args"], (list, tuple)) else str(calls["args"])
        assert "9.9.9" in flat
        assert "version_lockstep_action.py" in flat


class TestPluginChannelInstallSilence:
    """RDR-197 P1e (nexus-a2wmi.5) pinned Critical Assumption 2 as
    originally stated: a plugin-channel cut (``plugin-vX.Y.Z-n``) moves
    marketplace.json's ``source.ref`` only, never plugin.json's
    ``version`` field, so the VERSION comparison below must stay silent
    on a plugin cut. That half is still true and still pinned here.

    CA-2 ITSELF IS REVISED by nexus-konsk (P0, 2026-09-11; see RDR-197's
    Revision History and Critical Assumptions section): "the lockstep
    hook stays silent on a plugin-cut install" was the actual defect a
    same-version cut (``plugin-v7.41.0-1``) shipped to nobody through,
    measured for real. The hook now ALSO runs the independent, no-network
    ``detect_ref_drift`` check (below, and see
    ``TestRefDriftDetection`` / ``TestRefDriftOrchestration``), which DOES
    read ``known_marketplaces.json`` and a local clone's
    ``marketplace.json`` -- deliberately, not a regression. The two
    fixtures in THIS class carry no registry/marketplace data at all, so
    ``detect_ref_drift`` finds nothing and the silence they assert is real
    but incidental to their own fixture shape, not evidence CA-2's
    original form still holds.
    """

    @pytest.fixture()
    def plugin_channel_install(self, tmp_path: Path) -> Path:
        """A fixture repo layout mimicking a plugin-channel cut: a
        marketplace.json at the repo root carrying a ``plugin-vX.Y.Z-n``
        source.ref, and a conexus/.claude-plugin/plugin.json (the actual
        CLAUDE_PLUGIN_ROOT payload) whose version is UNCHANGED. Returns the
        plugin root.
        """
        repo_root = tmp_path
        mp_dir = repo_root / ".claude-plugin"
        mp_dir.mkdir(parents=True)
        (mp_dir / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "nexus-plugins",
                    "plugins": [
                        {
                            "name": "conexus",
                            "source": {
                                "source": "git-subdir",
                                "url": "https://github.com/Hellblazer/nexus.git",
                                "path": "conexus",
                                "ref": "plugin-v7.15.0-1",
                            },
                            "version": "7.15.0",
                        }
                    ],
                }
            )
        )
        plugin_root = repo_root / "conexus"
        pj_dir = plugin_root / ".claude-plugin"
        pj_dir.mkdir(parents=True)
        (pj_dir / "plugin.json").write_text(
            json.dumps({"name": "conexus", "version": "7.15.0"})
        )
        return plugin_root

    def test_plugin_cut_with_unchanged_version_is_silent(
        self, mod, plugin_channel_install, tmp_path, monkeypatch, capsys
    ) -> None:
        """Steps 2-3: a plugin cut (source.ref moved, version unchanged)
        with a marker already matching plugin.json's version -- the hook
        must attempt no upgrade."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_channel_install))
        marker = tmp_path / "marker"
        marker.write_text("7.15.0")  # already in lockstep with plugin.json
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        assert capsys.readouterr().out.strip() == "", (
            "a plugin-channel cut must not provoke a nudge (RDR-197 CA-2)"
        )
        assert dispatched == [], (
            "a plugin-channel cut must not provoke an upgrade attempt (RDR-197 CA-2)"
        )

    def test_hook_still_upgrades_when_plugin_version_is_genuinely_ahead(
        self, mod, plugin_channel_install, tmp_path, monkeypatch, capsys
    ) -> None:
        """Step 4 (falsifier): same plugin-channel fixture, but the marker
        is stale relative to plugin.json's version. The hook must still
        nudge + dispatch -- proving the silence above is not because this
        test suite (or the hook) has been neutered. Red-verified manually
        by temporarily stubbing the hook's dispatch call and restoring it
        (see nexus-a2wmi.5 completion report; not part of the committed
        suite)."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_channel_install))
        marker = tmp_path / "marker"
        marker.write_text("7.14.0")  # stale -- plugin.json says 7.15.0
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: dispatched.append(v))

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert dispatched == ["7.15.0"]


# ---------------------------------------------------------------------------
# nexus-konsk: the hook's own no-network ref-drift check.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _make_marketplace_clone(tmp_path: Path, plugin_name: str = "conexus") -> tuple[Path, str, str]:
    """A real local git repo standing in for an already-cloned
    marketplace (mirrors ``tests/test_plugin_lockstep.py``'s
    ``marketplace`` fixture): a client-tag pin (``v9.9.9``, resolving to
    ``sha_before``), then a same-version anchored plugin-only cut
    (``plugin-v9.9.9-1``, resolving to ``sha_after``) on the same
    branch -- the RDR-197 channel shape verbatim, version never moves,
    the ref does."""
    repo = tmp_path / "mp-src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    cp_dir = repo / ".claude-plugin"
    cp_dir.mkdir()

    def write_marketplace(ref: str) -> None:
        (cp_dir / "marketplace.json").write_text(json.dumps({"plugins": [
            {"name": plugin_name, "version": "9.9.9",
             "source": {"source": "git-subdir", "url": "https://example.invalid/x.git",
                        "path": plugin_name, "ref": ref}}]}))

    write_marketplace("v9.9.9")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "-a", "v9.9.9", "-m", "v9.9.9")
    sha_before = _git_out(repo, "rev-parse", "HEAD")

    write_marketplace("plugin-v9.9.9-1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plugin-only cut")
    _git(repo, "tag", "-a", "plugin-v9.9.9-1", "-m", "plugin-v9.9.9-1")
    sha_after = _git_out(repo, "rev-parse", "HEAD")
    return repo, sha_before, sha_after


def _write_registry(tmp_path: Path, plugin_id: str, sha: str | None) -> Path:
    p = tmp_path / "installed_plugins.json"
    entry: dict = {"version": "9.9.9", "scope": "user"}
    if sha is not None:
        entry["gitCommitSha"] = sha
    p.write_text(json.dumps({"version": 2, "plugins": {plugin_id: [entry]}}))
    return p


def _write_known_marketplaces(tmp_path: Path, marketplace_name: str, install_location: Path) -> Path:
    p = tmp_path / "known_marketplaces.json"
    p.write_text(json.dumps({marketplace_name: {"installLocation": str(install_location)}}))
    return p


class TestRefDriftDetection:
    """``detect_ref_drift`` -- mirrors
    ``nexus.plugin_lockstep._check_ref_drift``'s contract but reads
    files already on disk (no ``claude plugin marketplace update``
    fetch)."""

    def test_detects_a_moved_ref(self, mod, tmp_path: Path, monkeypatch) -> None:
        repo, sha_before, sha_after = _make_marketplace_clone(tmp_path)
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", sha_before)
        known = _write_known_marketplaces(tmp_path, "nexus-plugins", repo)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(known))

        assert mod.detect_ref_drift() == [("conexus@nexus-plugins", sha_before, sha_after)]

    def test_no_drift_when_sha_already_matches(self, mod, tmp_path: Path, monkeypatch) -> None:
        repo, _sha_before, sha_after = _make_marketplace_clone(tmp_path)
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", sha_after)
        known = _write_known_marketplaces(tmp_path, "nexus-plugins", repo)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(known))

        assert mod.detect_ref_drift() == []

    def test_missing_registry_is_silent(self, mod, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(tmp_path / "absent-registry.json"))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / "absent-known.json"))

        assert mod.detect_ref_drift() == []

    def test_no_git_commit_sha_field_is_silent(self, mod, tmp_path: Path, monkeypatch) -> None:
        """A registry shape/entry carrying no ``gitCommitSha`` is
        "cannot tell", never treated as a drift."""
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", None)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / "absent-known.json"))

        assert mod.detect_ref_drift() == []

    def test_missing_known_marketplaces_entry_refuses_not_guesses(
        self, mod, tmp_path: Path, monkeypatch
    ) -> None:
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", "d" * 40)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / "absent-known.json"))

        assert mod.detect_ref_drift() == []

    def test_unresolvable_ref_refuses_not_guesses(self, mod, tmp_path: Path, monkeypatch) -> None:
        """The registry names a plugin/marketplace pair with real clone
        data, but the marketplace.json's own pinned ref does not exist
        in that clone (a stale/mismatched fixture): skip silently rather
        than raise or invent a sha."""
        repo, sha_before, _sha_after = _make_marketplace_clone(tmp_path)
        # Point the clone's marketplace.json at a ref that was never tagged.
        (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": [
            {"name": "conexus", "version": "9.9.9",
             "source": {"source": "git-subdir", "url": "https://example.invalid/x.git",
                        "path": "conexus", "ref": "v0.0.0-does-not-exist"}}]}))
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", sha_before)
        known = _write_known_marketplaces(tmp_path, "nexus-plugins", repo)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(known))

        assert mod.detect_ref_drift() == []

    def test_other_plugins_registry_entries_are_ignored(self, mod, tmp_path: Path, monkeypatch) -> None:
        p = tmp_path / "installed_plugins.json"
        p.write_text(json.dumps({"version": 2, "plugins": {
            "beads@beads-marketplace": [{"version": "1.2.3", "gitCommitSha": "e" * 40}],
        }}))
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(p))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / "absent-known.json"))

        assert mod.detect_ref_drift() == []


class TestRefDriftOrchestration:
    """``main``'s ref-drift branch: nudge + dispatch on a real detected
    drift, and the "one clear line naming the drift" contract."""

    def test_main_nudges_and_dispatches_on_drift(
        self, mod, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        repo, sha_before, sha_after = _make_marketplace_clone(tmp_path)
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", sha_before)
        known = _write_known_marketplaces(tmp_path, "nexus-plugins", repo)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(known))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)  # no version-mismatch path
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_ref_drift_action", lambda: dispatched.append("fired"))

        mod.main()

        payload = json.loads(capsys.readouterr().out)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        assert "conexus@nexus-plugins" in msg
        assert sha_before[:7] in msg and sha_after[:7] in msg
        assert dispatched == ["fired"]

    def test_no_drift_is_silent_and_no_dispatch(self, mod, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(tmp_path / "absent-registry.json"))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / "absent-known.json"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_ref_drift_action", lambda: dispatched.append("fired"))

        mod.main()

        assert capsys.readouterr().out.strip() == ""
        assert dispatched == []

    def test_version_mismatch_and_ref_drift_together_fold_into_one_payload(
        self, mod, plugin_root: Path, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """Both paths firing in the same session must still print exactly
        ONE JSON line (two would leave the second unparsed)."""
        repo, sha_before, sha_after = _make_marketplace_clone(tmp_path)
        registry = _write_registry(tmp_path, "conexus@nexus-plugins", sha_before)
        known = _write_known_marketplaces(tmp_path, "nexus-plugins", repo)
        monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(registry))
        monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(known))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))  # plugin.json version 9.9.9
        marker = tmp_path / "marker"
        marker.write_text("1.0.0")  # stale -> version-mismatch path also fires
        monkeypatch.setenv("NX_LOCKSTEP_MARKER", str(marker))
        version_dispatched: list[str] = []
        ref_dispatched: list[str] = []
        monkeypatch.setattr(mod, "dispatch_action", lambda v: version_dispatched.append(v))
        monkeypatch.setattr(mod, "dispatch_ref_drift_action", lambda: ref_dispatched.append("fired"))

        mod.main()

        out = capsys.readouterr().out
        assert out.count("\n") <= 1  # exactly one printed line (plus trailing newline)
        payload = json.loads(out)
        msg = payload["hookSpecificOutput"]["additionalContext"]
        assert "9.9.9" in msg
        assert "conexus@nexus-plugins" in msg
        assert version_dispatched == ["9.9.9"]
        assert ref_dispatched == ["fired"]


class TestRefDriftSentinelMatchesAction:
    """The literal ``dispatch_ref_drift_action`` passes as the "target
    version" argv must equal ``version_lockstep_action.py``'s own
    ``_REF_DRIFT_SENTINEL`` -- the two bare stdlib scripts cannot import
    each other (see the hook's module docstring), so this is the only
    thing standing between a silent drift-forever bug (the action would
    try to parse the sentinel as a real version and do nothing useful)."""

    def test_sentinel_literal_matches_the_action_script(self, mod) -> None:
        action_script = SCRIPT.parent / "version_lockstep_action.py"
        spec = importlib.util.spec_from_file_location("version_lockstep_action", action_script)
        assert spec and spec.loader
        action_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(action_mod)

        assert mod._REF_DRIFT_SENTINEL == action_mod._REF_DRIFT_SENTINEL


class TestDispatchRefDriftActionIsNonBlocking:
    def test_dispatch_returns_immediately_and_carries_the_sentinel(self, mod, monkeypatch) -> None:
        calls: dict[str, object] = {}

        class FakePopen:
            def __init__(self, *a, **k):
                calls["args"] = a[0] if a else k.get("args")
                calls["started"] = True
                calls["start_new_session"] = k.get("start_new_session")
                calls["stdout"] = k.get("stdout")
                calls["stderr"] = k.get("stderr")
                calls["stdin"] = k.get("stdin")

            def wait(self, *a, **k):  # pragma: no cover - must not be called
                calls["waited"] = True

        monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
        mod.dispatch_ref_drift_action()

        assert calls.get("started") is True
        assert "waited" not in calls
        assert calls.get("start_new_session") is True
        assert calls.get("stdout") is mod.subprocess.DEVNULL
        flat = " ".join(map(str, calls["args"])) if isinstance(calls["args"], (list, tuple)) else str(calls["args"])
        assert mod._REF_DRIFT_SENTINEL in flat
        assert "version_lockstep_action.py" in flat


class TestRunsUnderBareInterpreter:
    def test_end_to_end_match_silent(self, plugin_root, tmp_path) -> None:
        """Invoke the script as a subprocess (mimics _run_python_hook.sh)
        with a matching marker: expect clean exit 0 and empty stdout."""
        import os

        marker = tmp_path / "marker"
        marker.write_text("9.9.9")
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        env["NX_LOCKSTEP_MARKER"] = str(marker)
        r = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=20, env=env,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == ""
