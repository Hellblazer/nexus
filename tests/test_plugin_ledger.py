# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Synthetic coverage of scripts/plugin_ledger.py (nexus-rbmja.10).

The deferral parser and the RDR-228 Phase 2 guard used to be driven by
tests/test_cut_plugin_release.py against the anchored-tag cut script. That
script is retired (nexus-rbmja.8) and the guard now lives here; the drift-ledger
test reads the real tree, which says nothing about the guard's refusals. So
these tests build a small repo with a reference branch (``main``, what installs
have) and a source branch (``develop``) and drive the guard directly.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from plugin_ledger import (
    LedgerRefused,
    _deferred_entries,
    deferred_paths,
    phase2_deferral_check,
)

SETUP_PY = "conexus/launch/nx_setup.py"
LAUNCH_PY = "conexus/launch/nx_launch.py"
STANDIN_PY = "conexus/launch/nx_standin.py"
#: A credentials-page module under a name nobody has chosen yet: the check is
#: keyed on the directory, so a name it never heard of is still covered.
CREDS_PY = "conexus/launch/nx_credentials_page.py"
NOTICE_PY = "conexus/hooks/scripts/nx_setup_notice.py"
ENDPOINT_PY = "conexus/hooks/scripts/_endpoint_resolve.py"
SETUP_MD = "conexus/commands/setup.md"
HOOKS_JSON = "conexus/hooks/hooks.json"
MCP_JSON = "conexus/.mcp.json"
LOCKSTEP_HOOK = "conexus/hooks/scripts/version_lockstep_hook.py"

#: What the reference carries when Phase 1 HAS shipped.
_PHASE1_SHIPPED = {
    LAUNCH_PY: "launcher v1\n",
    STANDIN_PY: "standin v1\n",
    NOTICE_PY: "notice v1\n",
    HOOKS_JSON: "hooks v1\n",
    MCP_JSON: '{"mcp": "v1"}\n',
    LOCKSTEP_HOOK: "lockstep v1\n",
}
#: What the reference carries when it predates Phase 1 entirely.
_PRE_PHASE1 = {
    ENDPOINT_PY: "endpoint v1\n",
    HOOKS_JSON: "hooks v0\n",
    MCP_JSON: '{"mcp": "v0"}\n',
    LOCKSTEP_HOOK: "lockstep v0\n",
}
_P1_FILES = {
    LAUNCH_PY: "launcher v2\n",
    STANDIN_PY: "standin v2\n",
    NOTICE_PY: "notice v2\n",
    HOOKS_JSON: "hooks v2 via launcher\n",
    MCP_JSON: '{"mcp": "v2 via launcher"}\n',
    LOCKSTEP_HOOK: "lockstep v2\n",
}
_P2_FILES = {
    SETUP_PY: "setup tools\n",
    CREDS_PY: "credentials page\n",
    ENDPOINT_PY: "endpoint v2\n",
    SETUP_MD: "the /setup command\n",
}
#: Every path both phases touch, for a ledger that defers all of them.
_ALL_PATHS = sorted({*_P1_FILES, *_P2_FILES})


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write(repo: pathlib.Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _ledger(awaiting: list[str], deferred: list[str]) -> str:
    def bullets(paths: list[str]) -> str:
        return "".join(f"- `{p}`: changed (nexus-p2xxx)\n" for p in paths)

    return (
        "# Pending\n\n## Awaiting the next release (pinned: v9.9.0)\n\n"
        + bullets(awaiting)
        + "\n\n## Deferred to the next client release\n\n"
        + bullets(deferred)
    )


def _phase_repo(
    tmp_path: pathlib.Path,
    *,
    base: dict[str, str],
    develop: dict[str, str],
    awaiting: list[str],
    deferred: list[str],
    remove: tuple[str, ...] = (),
) -> tuple[pathlib.Path, str]:
    """A repo whose ``main`` is the reference and ``develop`` the source."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "README.md", "x\n")
    for rel, content in base.items():
        _write(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "develop")
    for rel in remove:
        _git(repo, "rm", "-q", rel)
    for rel, content in develop.items():
        _write(repo, rel, content)
    ledger = _ledger(awaiting, deferred)
    _write(repo, "conexus/PENDING_RELEASE.md", ledger)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: phase work (nexus-p2xxx)")
    return repo, ledger


def _check(repo: pathlib.Path, ledger: str) -> str:
    return phase2_deferral_check(repo, "main", "develop", ledger)


class TestDeferralParsing:
    def test_an_entry_under_the_heading_is_deferred(self) -> None:
        text = (
            "# Pending\n"
            "- `conexus/hooks/live.py`: not deferred (nexus-aaaaa)\n"
            "\n"
            "## Deferred to the next client release\n"
            "\n"
            "- `conexus/hooks/held.py`: deferred (nexus-bbbbb)\n"
        )
        deferred = _deferred_entries(text)
        assert len(deferred) == 1
        assert "held.py" in deferred[0]
        assert "live.py" not in "".join(deferred)

    def test_a_heading_ends_the_section(self) -> None:
        """Membership is by the LAST '## ' heading seen, not everything after
        the first Deferred heading."""
        text = (
            "# Pending\n"
            "## Deferred to the next client release\n"
            "- `conexus/hooks/held.py`: deferred (nexus-bbbbb)\n"
            "## Some other section\n"
            "- `conexus/hooks/other.py`: not deferred (nexus-ccccc)\n"
        )
        deferred = _deferred_entries(text)
        assert len(deferred) == 1
        assert "held.py" in deferred[0]

    def test_a_ledger_with_no_deferred_heading_parses_as_none(self) -> None:
        text = (
            "# Pending\n"
            "- Every file that differs MUST be declared here.\n"
            "- `conexus/hooks/h.py`: real entry (nexus-aaaaa)\n"
        )
        assert _deferred_entries(text) == []
        assert deferred_paths(text) == set()

    def test_deferred_paths_excludes_wheel_and_bare_prefix_spans(self) -> None:
        """Only allowlisted, path-shaped spans count: the wheel half of a
        split entry was never going to ship, and a bare prefix root
        (`conexus/`) is prose, not a path."""
        text = (
            "# Pending\n"
            "## Deferred to the next client release\n"
            "- `conexus/agents/dev.md` (see `conexus/`): split delivery, "
            "wheel half `src/nexus/plans/x.py` (nexus-fffff)\n"
        )
        assert deferred_paths(text) == {"conexus/agents/dev.md"}

    def test_a_wheel_span_alone_yields_no_path(self) -> None:
        text = (
            "## Deferred to the next client release\n"
            "- wheel half `src/nexus/plans/x.py` (nexus-fffff)\n"
        )
        assert deferred_paths(text) == set()

    def test_a_bare_prefix_span_alone_yields_no_path(self) -> None:
        text = (
            "## Deferred to the next client release\n"
            "- see `conexus/hooks/` and `conexus/` (nexus-fffff)\n"
        )
        assert "conexus/" not in deferred_paths(text)
        assert "conexus" not in deferred_paths(text)


class TestPhase2DeferralGuard:
    def test_phase2_path_under_awaiting_refuses_naming_path_and_heading(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[SETUP_MD], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        assert SETUP_MD in message
        assert "Deferred to the next client release" in message

    def test_a_credentials_module_under_a_name_nobody_chose_is_covered(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[CREDS_PY], deferred=[p for p in _ALL_PATHS if p != CREDS_PY],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert CREDS_PY in str(exc.value)

    def test_a_path_missing_from_the_ledger_altogether_refuses(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert SETUP_MD in str(exc.value)

    def test_every_differing_path_deferred_passes(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=_ALL_PATHS,
        )
        assert f"{len(_ALL_PATHS)} guarded path(s)" in _check(repo, ledger)

    def test_an_unchanged_listed_path_needs_no_deferral(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path,
            base={**_PRE_PHASE1, SETUP_MD: "tag copy\n"},
            develop={**_P1_FILES, **_P2_FILES, SETUP_MD: "tag copy\n"},
            awaiting=[], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        assert f"{len(_ALL_PATHS) - 1} guarded path(s)" in _check(repo, ledger)

    def test_a_deleted_launch_file_is_a_difference(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop=_P2_FILES,
            awaiting=[STANDIN_PY], deferred=sorted(_P2_FILES),
            remove=(STANDIN_PY,),
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert STANDIN_PY in str(exc.value)

    def test_phase1_only_passes_and_says_it_had_nothing_to_check(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop=_P1_FILES,
            awaiting=[LAUNCH_PY, STANDIN_PY, NOTICE_PY],
            deferred=[HOOKS_JSON, MCP_JSON],
        )
        assert "nothing to check" in _check(repo, ledger)

    def test_a_reference_that_has_nx_setup_passes_and_says_so(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path,
            base={**_PHASE1_SHIPPED, SETUP_PY: "setup tools v1\n", SETUP_MD: "v1\n"},
            develop={SETUP_PY: "setup tools v2\n", SETUP_MD: "v2\n"},
            awaiting=[SETUP_PY, SETUP_MD], deferred=[],
        )
        assert "nothing to check" in _check(repo, ledger)

    def test_the_refusal_states_the_phase1_unshipped_case(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[SETUP_PY], deferred=[],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        assert LAUNCH_PY in message
        assert "'Awaiting' for Phase 1 and 'Deferred' for Phase 2" in message
        assert "deferring holds the whole file back" in message
        assert "ship in the same client release" in message
        assert "every path of both" in message

    def test_without_the_launcher_hooks_and_mcp_must_be_deferred_too(
        self, tmp_path: pathlib.Path
    ) -> None:
        guarded = (HOOKS_JSON, MCP_JSON, LOCKSTEP_HOOK)
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=list(guarded),
            deferred=[p for p in _ALL_PATHS if p not in guarded],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        for path in guarded:
            assert path in message

    def test_with_the_launcher_hooks_and_mcp_are_not_required(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED,
            develop={**_P2_FILES, HOOKS_JSON: "hooks v3\n", MCP_JSON: '{"mcp": "v3"}\n'},
            awaiting=[HOOKS_JSON, MCP_JSON], deferred=sorted(_P2_FILES),
        )
        assert f"{len(_P2_FILES)} guarded path(s)" in _check(repo, ledger)

    @pytest.mark.parametrize("path", [
        "conexus/hooks/scripts/_cli_lookup.py",
        "conexus/hooks/scripts/_exec_path.py",
        "conexus/hooks/scripts/nx_hook_shim.py",
        LOCKSTEP_HOOK,
        "conexus/registry.yaml",
        "conexus/README.md",
    ])
    def test_a_shared_phase2_file_under_awaiting_refuses_even_with_the_launcher(
        self, tmp_path: pathlib.Path, path: str
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={**_P2_FILES, path: "phase 2\n"},
            awaiting=[path], deferred=sorted(_P2_FILES),
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert path in str(exc.value)

    def test_a_phase1_already_on_the_reference_needs_no_deferral(
        self, tmp_path: pathlib.Path
    ) -> None:
        """An earlier plugin cut shipped Phase 1 to the reference; measured
        from it, only the Phase 2 paths differ and only they need deferral."""
        repo, ledger = _phase_repo(
            tmp_path, base={**_PRE_PHASE1, **_P1_FILES},
            develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=sorted(_P2_FILES),
        )
        assert f"{len(_P2_FILES)} guarded path(s)" in _check(repo, ledger)

    def test_a_renamed_launch_file_counts_its_old_name(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED,
            develop={**_P2_FILES, "conexus/launch/nx_standin2.py": "standin v1\n"},
            remove=(STANDIN_PY,),
            awaiting=[STANDIN_PY],
            deferred=[*sorted(_P2_FILES), "conexus/launch/nx_standin2.py"],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert STANDIN_PY in str(exc.value)

    def test_setup_command_without_the_marker_still_arms(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={SETUP_MD: "early\n"},
            awaiting=[SETUP_MD], deferred=[],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert SETUP_MD in str(exc.value)

    def test_an_unknown_launch_module_without_the_marker_still_arms(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={CREDS_PY: "early\n"},
            awaiting=[CREDS_PY], deferred=[],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert CREDS_PY in str(exc.value)

    def test_phase1_files_alone_do_not_arm(self, tmp_path: pathlib.Path) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED,
            develop={LAUNCH_PY: "launcher v3\n", STANDIN_PY: "standin v3\n"},
            awaiting=[LAUNCH_PY, STANDIN_PY], deferred=[],
        )
        assert "nothing to check" in _check(repo, ledger)

    def test_an_unresolvable_reference_refuses_rather_than_passing(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=_ALL_PATHS,
        )
        for reference, source in (("v0.0.0", "develop"), ("main", "nope")):
            with pytest.raises(LedgerRefused) as exc:
                phase2_deferral_check(repo, reference, source, ledger)
            assert "does not resolve" in str(exc.value)

    def test_the_phase1_paragraph_is_absent_once_the_launcher_shipped(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop=_P2_FILES,
            awaiting=[SETUP_PY], deferred=[],
        )
        with pytest.raises(LedgerRefused) as exc:
            _check(repo, ledger)
        assert "Phase 1 has not shipped" not in str(exc.value)
