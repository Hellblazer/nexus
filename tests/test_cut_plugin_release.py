# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The plugin cut is a script with tests, never a checklist (nexus-a2wmi.8).

Every test drives ``scripts/cut_plugin_release.py`` against a MINI-NEXUS
fixture — a bare origin plus a working clone whose main carries the
channel machinery (as marker strings), a base client tag, and whose
develop is ahead with allowlisted edits, an added file, a DELETED file,
denied-prefix edits that must never ship, and off-allowlist wheel edits.
The script is never pointed at the real repository here.

The atomic-split check itself is bead .9; this file proves the SEAM —
a refusing check aborts before any branch or file mutation.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

from cut_plugin_release import (
    CutRefused,
    check_main_readiness,
    perform_cut,
    phase2_deferral_check,
)
from tests._module_seam import setattr_in

VERSION = "9.9.0"
BASE_TAG = f"v{VERSION}"

#: Marker content for each machinery path check_main_readiness demands.
_MACHINERY: dict[str, str] = {
    "tests/test_plugin_structure.py": "_assert_ref_valid_for_plugin",
    "tests/test_plugin_release_drift_ledger.py": (
        "def _drifted_paths(plugin\nplugin_in_release_window"
    ),
    ".github/workflows/plugin-drift-ledger.yml": 'grep -qE "^plugin-v${version_re}-[1-9][0-9]*$"',
    "scripts/plugin_channel.py": "INVARIANT W",
    "scripts/cut_plugin_release.py": "def perform_cut",
    ".github/workflows/plugin-release.yml": "plugin-v*",
}


def _run(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: pathlib.Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _marketplace(conexus_ref: str, sn_ref: str) -> str:
    return json.dumps(
        {
            "metadata": {"version": VERSION},
            "plugins": [
                {
                    "name": "conexus",
                    "version": VERSION,
                    "source": {"source": "git-subdir", "ref": conexus_ref},
                },
                {
                    "name": "sn",
                    "version": VERSION,
                    "source": {"source": "git-subdir", "ref": sn_ref},
                },
            ],
        },
        indent=2,
    )


LEDGER_COVERED = (
    "- `conexus/hooks/hook.py`: guard tightened (nexus-aaaaa)\n"
    "- `conexus/skills/newskill/SKILL.md`: new skill (nexus-bbbbb)\n"
)
LEDGER_UNCOVERED = (
    "- `conexus/agents/dev.md` + wheel half `src/nexus/plans/x.py`: split "
    "delivery (nexus-ccccc)\n"
)


def _mini_nexus(
    tmp_path: pathlib.Path,
    *,
    missing_machinery: tuple[str, ...] = (),
    payload: str = "conexus",
    main_extra: dict[str, str] | None = None,
) -> pathlib.Path:
    """Build origin (bare) + clone; return the clone.

    main: machinery + surface + tag v9.9.0. develop: the cut's payload —
    ``"conexus"`` (edits + an add + a delete + denied-prefix and src/
    changes that must not ship, ledger with covered and uncovered
    entries), ``"evals"`` (only conexus/evals/ — content the
    loader-visible surface does not cover), or ``"sn"`` (only an sn
    change).
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(repo)],
        check=True, capture_output=True, text=True,
    )
    _run(repo, "config", "user.email", "test@test.invalid")
    _run(repo, "config", "user.name", "test")
    _run(repo, "checkout", "-q", "-b", "main")

    for path, marker in _MACHINERY.items():
        if path not in missing_machinery:
            _write(repo, path, f"machinery fixture\n{marker}\n")
    _write(repo, "conexus/hooks/hook.py", "v1\n")
    _write(repo, "conexus/commands/doomed.md", "deleted on develop\n")
    _write(repo, "conexus/plans/builtin/p.yml", "wheel data v1\n")
    _write(repo, "conexus/daemon/d.plist", "wheel data v1\n")
    _write(repo, "conexus/evals/case.md", "eval v1\n")
    _write(repo, "sn/hooks/probe.py", "v1\n")
    _write(repo, "src/nexus/cli.py", "wheel v1\n")
    _write(repo, "pyproject.toml", f'[project]\nname = "w"\nversion = "{VERSION}"\n')
    _write(repo, ".claude-plugin/marketplace.json", _marketplace(BASE_TAG, BASE_TAG))
    _write(repo, "conexus/PENDING_RELEASE.md", "# Pending\n")
    for rel, content in (main_extra or {}).items():
        _write(repo, rel, content)
    _run(repo, "add", ".")
    _run(repo, "commit", "-q", "-m", "main baseline")
    _run(repo, "tag", BASE_TAG)

    _run(repo, "checkout", "-q", "-b", "develop")
    if payload == "conexus":
        _write(repo, "conexus/hooks/hook.py", "v2 ships\n")
        _write(repo, "conexus/skills/newskill/SKILL.md", "added on develop\n")
        _run(repo, "rm", "-q", "conexus/commands/doomed.md")
        _write(repo, "conexus/plans/builtin/p.yml", "wheel data v2 MUST NOT SHIP\n")
        _write(repo, "src/nexus/cli.py", "wheel v2 MUST NOT SHIP\n")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n" + LEDGER_COVERED + LEDGER_UNCOVERED,
        )
    elif payload == "evals":
        _write(repo, "conexus/evals/case.md", "eval v2 — the only conexus change\n")
    elif payload == "sn":
        _write(repo, "sn/hooks/probe.py", "v2 ships\n")
    elif payload == "straddle":
        # One bead's commit couples a plugin path to WHEEL content: the
        # wholesale import cannot ship one half and hold back the other.
        _write(repo, "conexus/registry.yaml", "registry v2\n")
        _write(repo, "conexus/plans/builtin/p2.yml", "new wheel plan\n")
        _write(repo, "src/nexus/straddle.py", "the wheel half\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "feat: registry work (nexus-ddddd)")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/registry.yaml`: registry work (nexus-ddddd)\n",
        )
    elif payload == "straddle-deferred":
        # The same straddling shape as "straddle" (nexus-ddddd's commit
        # couples an allowlisted NEW file to a wheel file), but the entry
        # is moved under the deferral heading (nexus-2x3qy): the cut must
        # not refuse, and must hold the channel path back from shipping
        # too -- registry.yaml never existed at main, so "held back"
        # means it stays absent on the cut branch.
        _write(repo, "conexus/registry.yaml", "registry v2\n")
        _write(repo, "conexus/plans/builtin/p2.yml", "new wheel plan\n")
        _write(repo, "src/nexus/straddle.py", "the wheel half\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "feat: registry work (nexus-ddddd)")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n\n## Deferred to the next client release\n\n"
            "- `conexus/registry.yaml`: registry work (nexus-ddddd)\n",
        )
    elif payload == "straddle-deferred-edit":
        # A deferred entry whose channel path already EXISTS at main
        # (conexus/hooks/hook.py, baseline "v1\n"): held back must mean
        # RESTORED to that content, never deleted outright.
        _write(repo, "conexus/hooks/hook.py", "v2 coupled to wheel — MUST NOT SHIP\n")
        _write(repo, "src/nexus/straddle2.py", "the wheel half\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "feat: hook rework (nexus-fffff)")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n\n## Deferred to the next client release\n\n"
            "- `conexus/hooks/hook.py`: coupled rework (nexus-fffff)\n",
        )
    elif payload == "cleanbead":
        # Ride-alongs OUTSIDE the shipped surface (tests/, docs/) do not
        # straddle: they ship nowhere, so nothing is stranded.
        _write(repo, "conexus/hooks/hook.py", "v2 guarded\n")
        _write(repo, "tests/hooks/test_new_guard.py", "test ride-along\n")
        _write(repo, "docs/note.md", "doc ride-along\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "fix: hook guard (nexus-eeeee)")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/hooks/hook.py`: guard tightened (nexus-eeeee)\n",
        )
    else:  # pragma: no cover - fixture misuse
        raise ValueError(payload)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "develop payload")

    _run(repo, "push", "-q", "origin", "main", "develop", "--tags")
    _run(repo, "checkout", "-q", "main")
    return repo


def _cut(repo: pathlib.Path, **kwargs):
    """perform_cut with the battery stubbed to a recorder (DI, not a flag)."""
    ran: list[str] = []
    kwargs.setdefault("battery", lambda cut_repo: ran.append("battery"))
    result = perform_cut(repo, BASE_TAG, **kwargs)
    result["battery_ran"] = ran
    return result


class TestMainReadiness:
    def test_ready_main_passes(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        assert check_main_readiness(repo) == []

    def test_refuses_naming_every_missing_path(self, tmp_path: pathlib.Path) -> None:
        """One scan, ALL misses named — not abort-on-first."""
        missing = ("scripts/plugin_channel.py", ".github/workflows/plugin-release.yml")
        repo = _mini_nexus(tmp_path, missing_machinery=missing)
        with pytest.raises(CutRefused) as exc:
            _cut(repo)
        for path in missing:
            assert path in str(exc.value)

    def test_marker_less_content_is_a_miss(self, tmp_path: pathlib.Path) -> None:
        """The path existing is not enough: the RULE must be on main."""
        repo = _mini_nexus(tmp_path)
        _write(repo, "scripts/plugin_channel.py", "present but pre-invariant\n")
        _run(repo, "add", "scripts/plugin_channel.py")
        _run(repo, "commit", "-q", "-m", "gut the machinery")
        _run(repo, "push", "-q", "origin", "main")
        with pytest.raises(CutRefused, match="scripts/plugin_channel.py"):
            _cut(repo)


class TestSequenceNumber:
    def test_first_cut_derives_one(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        assert result["n"] == 1
        assert result["tag"] == f"plugin-v{VERSION}-1"

    def test_counts_past_existing_cuts(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        for existing in (1, 2):
            _run(repo, "tag", f"plugin-v{VERSION}-{existing}", BASE_TAG)
        _run(repo, "push", "-q", "origin", "--tags")
        result = _cut(repo)
        assert result["n"] == 3

    def test_refuses_when_the_derived_tag_already_exists_on_origin(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Belt and suspenders past the fetch: a tag on origin the local
        enumeration somehow missed still refuses. Remove the existence
        refusal and this re-mints plugin-v9.9.0-1."""
        repo = _mini_nexus(tmp_path)
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)],
            check=True, capture_output=True, text=True,
        )
        _run(other, "tag", f"plugin-v{VERSION}-1", BASE_TAG)
        _run(other, "push", "-q", "origin", f"plugin-v{VERSION}-1")
        #

        import cut_plugin_release as mod

        real = mod.next_plugin_tag_number
        try:
            mod.next_plugin_tag_number = lambda version, *, cwd=None: 1
            with pytest.raises(CutRefused, match="already exists"):
                _cut(repo)
        finally:
            mod.next_plugin_tag_number = real


class TestAtomicSplitSeam:
    def test_a_refusing_check_aborts_before_any_mutation(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The .9 seam: refusal precedes branch creation and every write."""
        repo = _mini_nexus(tmp_path)

        def refusing(cut_repo, base_tag, allowlisted):
            raise CutRefused("straddling entry: defer it in the ledger")

        with pytest.raises(CutRefused, match="straddling"):
            _cut(repo, split_check=refusing)
        branches = _run(repo, "branch", "--list", "plugin-release/*")
        assert branches == ""
        assert _run(repo, "status", "--porcelain") == ""
        assert _run(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


class TestTheImport:
    def test_added_edited_and_deleted_files_all_land(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Deletions are why the import is diff-and-apply: a pathspec
        checkout stages no deletion, and this test fails under one."""
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        assert (repo / "conexus/hooks/hook.py").read_text() == "v2 ships\n"
        assert (repo / "conexus/skills/newskill/SKILL.md").exists()
        assert not (repo / "conexus/commands/doomed.md").exists()

    def test_excluded_paths_are_never_imported(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        assert (repo / "src/nexus/cli.py").read_text() == "wheel v1\n"

    def test_denied_prefixes_match_main_byte_for_byte(
        self, tmp_path: pathlib.Path
    ) -> None:
        """conexus/plans/ and conexus/daemon/ are wheel package data."""
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        assert (repo / "conexus/plans/builtin/p.yml").read_text() == "wheel data v1\n"
        _run(repo, "diff", "--quiet", "origin/main", "--",
             "conexus/plans", "conexus/daemon")

    def test_no_version_field_moves(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        _run(repo, "diff", "--quiet", "origin/main", "--", "pyproject.toml")
        data = json.loads(
            (repo / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        assert data["metadata"]["version"] == VERSION
        assert all(p["version"] == VERSION for p in data["plugins"])


class TestRefMovement:
    def test_conexus_only_cut_leaves_sn_untouched(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        data = json.loads(
            (repo / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        refs = {p["name"]: p["source"]["ref"] for p in data["plugins"]}
        assert refs["conexus"] == result["tag"]
        assert refs["sn"] == BASE_TAG
        assert result["moved_plugins"] == ["conexus"]

    def test_a_cut_on_the_other_plugin_moves_only_that_ref(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path, payload="sn")
        result = _cut(repo)
        data = json.loads(
            (repo / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        refs = {p["name"]: p["source"]["ref"] for p in data["plugins"]}
        assert refs["sn"] == result["tag"]
        assert refs["conexus"] == BASE_TAG
        assert result["moved_plugins"] == ["sn"]

    def test_an_evals_only_cut_still_moves_the_conexus_ref(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Ref movement keys on the ALLOWLIST mapping, not SURFACE_BY_PLUGIN:
        conexus/evals/ is real shipped content the loader-visible surface
        does not cover. Key on SURFACE_BY_PLUGIN and this fails."""
        repo = _mini_nexus(tmp_path, payload="evals")
        result = _cut(repo)
        assert "conexus" in result["moved_plugins"]
        data = json.loads(
            (repo / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        refs = {p["name"]: p["source"]["ref"] for p in data["plugins"]}
        assert refs["conexus"] == result["tag"]


class TestBranchAndAgreement:
    def test_branch_name_ref_and_n_agree(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        branch = _run(repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert branch == f"plugin-release/{VERSION}-{result['n']}"
        assert result["tag"] == f"plugin-v{VERSION}-{result['n']}"

    def test_the_agreement_check_fails_on_a_renamed_branch(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Condition (d) of invariant W keys on the branch NAME, so a
        rename after the ref is written must be caught, not cosmetic."""
        from cut_plugin_release import assert_branch_agreement

        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        _run(repo, "branch", "-m", f"plugin-release/{VERSION}-99")
        with pytest.raises(CutRefused, match="agree"):
            assert_branch_agreement(repo, VERSION, result["n"])


class TestLedger:
    def test_covered_entries_empty_and_uncovered_survive(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        ledger = (repo / "conexus/PENDING_RELEASE.md").read_text(encoding="utf-8")
        assert "conexus/hooks/hook.py" not in ledger
        assert "conexus/skills/newskill/SKILL.md" not in ledger
        assert "nexus-ccccc" in ledger  # the split-delivery entry survives

    def test_prose_spans_with_slashes_do_not_hold_an_entry_back(
        self, tmp_path: pathlib.Path
    ) -> None:
        """a2wmi.12 spike, first real cut (nexus-znvjd, 2026-08-30): the
        entry's prose carried backticked spans containing ``/`` — a
        config-dir placeholder and an HTTP route — and the rewrite read
        them as un-shippable paths, left the covered entry in place, and
        the window tests refused the branch. Prose is not a path; a
        tests/ ride-along is not a wheel half; only a shipped-surface
        path (src/, the denied plugin-tree prefixes, mcpb/, dt/) holds an
        entry back."""
        from cut_plugin_release import _rewrite_ledger

        covered = (
            "- `conexus/hooks/scripts/t2_prefix_scan.py` — bead nexus-znvjd: prefers\n"
            "  the lease at `<config_dir>/data_token_lease.<digest>` (written by\n"
            "  `nexus.db.data_token`); a `WARNING: ... HTTP 401 for /v1/memory/projects`\n"
            "  line names it. Test ride-along: `tests/hooks/test_t2_prefix_scan.py`.\n"
        )
        split = (
            "- `conexus/agents/dev.md`: guidance half; wheel half\n"
            "  `src/nexus/plans/x.py` ships at the next client release (nexus-fffff)\n"
        )
        no_path = "- `git stash -u` is covered by the routing guard (nexus-ggggg)\n"
        # R2 of the spike fix: a bare prefix root in prose is not a path —
        # is_channel_path("conexus/") is True, and this entry ships nothing.
        bare_root = "- everything under `conexus/` is the allowlist root; nothing here ships yet (nexus-hhhhh)\n"
        text = "# Pending\n" + covered + split + no_path + bare_root
        repo = tmp_path / "ledgeronly"
        (repo / "conexus").mkdir(parents=True)
        (repo / "conexus" / "PENDING_RELEASE.md").write_text(text, encoding="utf-8")
        _rewrite_ledger(repo)
        after = (repo / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")
        assert "nexus-znvjd" not in after  # covered: emptied
        assert after == "# Pending\n" + split + no_path + bare_root  # the rest survives verbatim


BACK_MERGE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "plugin_cut_back_merge.sh"


def _merge_cut_into_origin_main(repo: pathlib.Path, branch: str) -> str:
    """What the human's `gh pr merge --merge` leaves on origin: main with the
    cut merged (no-ff), pushed. Returns main's ledger text."""
    _run(repo, "checkout", "-q", "main")
    _run(repo, "merge", "-q", "--no-ff", "--no-edit", branch)
    _run(repo, "push", "-q", "origin", "main")
    return (repo / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")


class TestBackMerge:
    """scripts/plugin_cut_back_merge.sh — the back-merge after a cut.

    Rehearsal finding (2026-08-30): the cut commit empties the ledger
    entries it ships while develop still carries them, so a bare
    `git merge origin/main` on develop conflicts on PENDING_RELEASE.md
    EVERY time (the RDR said "expected conflict-free"). The script owns
    the resolution; these pin it against the real cut fixture.
    """

    def test_a_bare_merge_conflicts_on_the_ledger_by_construction(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path)
        branch = _cut(repo)["branch"]
        _merge_cut_into_origin_main(repo, branch)
        _run(repo, "checkout", "-q", "develop")
        proc = subprocess.run(
            ["git", "merge", "--no-edit", "origin/main"],
            cwd=repo, capture_output=True, text=True,
        )
        assert proc.returncode != 0, "the conflict this script exists for did not occur"
        unmerged = _run(repo, "diff", "--name-only", "--diff-filter=U").split()
        assert unmerged == ["conexus/PENDING_RELEASE.md"], unmerged
        _run(repo, "merge", "--abort")

    def test_the_ledger_conflict_is_resolved_main_wins(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        branch = _cut(repo)["branch"]
        main_ledger = _merge_cut_into_origin_main(repo, branch)
        assert "nexus-aaaaa" not in main_ledger and "nexus-ccccc" in main_ledger
        develop_before = _run(repo, "rev-parse", "develop").strip()
        proc = subprocess.run(
            ["bash", str(BACK_MERGE), str(repo), "--no-verify"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "resolved to main's version" in proc.stdout
        assert _run(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "develop"
        after = (repo / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")
        assert after == main_ledger
        assert "<<<<<<<" not in after
        # develop now contains origin/main, and moved past where it was.
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "origin/main", "develop"],
            cwd=repo, capture_output=True, text=True,
        )
        assert ancestor.returncode == 0
        assert _run(repo, "rev-parse", "develop").strip() != develop_before
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_the_verify_net_runs_after_the_merge_and_fails_loud(
        self, tmp_path: pathlib.Path
    ) -> None:
        """VERIFY=1 is the script's whole safety net for an entry develop
        added after the cut: it must run AFTER the resolution commit and a
        red verify must exit 1 with the merge commit standing (the operator
        re-adds on top of it). Plumbing pinned with a stub verify command;
        the drift contract's own semantics live in its own module."""
        repo = _mini_nexus(tmp_path)
        branch = _cut(repo)["branch"]
        main_ledger = _merge_cut_into_origin_main(repo, branch)
        marker = tmp_path / "verify-ran"
        env = dict(os.environ, PLUGIN_CUT_BACK_MERGE_VERIFY_CMD=f"touch {marker} && false")
        proc = subprocess.run(
            ["bash", str(BACK_MERGE), str(repo)], capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert marker.exists(), "verify command never ran"
        assert "drift contract is RED" in proc.stderr
        # The merge commit stands (resolution kept) — the operator fixes on top.
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        assert (repo / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8") == main_ledger
        # A green verify is a clean exit 0 on the same state.
        env["PLUGIN_CUT_BACK_MERGE_VERIFY_CMD"] = "true"
        proc = subprocess.run(
            ["bash", str(BACK_MERGE), str(repo)], capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_a_failed_resolution_commit_aborts_the_merge(self, tmp_path: pathlib.Path) -> None:
        """If the resolution commit itself fails (a rejecting hook stands in
        for GPG/disk), the script must not leave the checkout mid-merge."""
        repo = _mini_nexus(tmp_path)
        branch = _cut(repo)["branch"]
        _merge_cut_into_origin_main(repo, branch)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        _run(repo, "checkout", "-q", "develop")
        develop_before = _run(repo, "rev-parse", "develop").strip()
        proc = subprocess.run(
            ["bash", str(BACK_MERGE), str(repo), "--no-verify"], capture_output=True, text=True,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "merge ABORTED" in proc.stderr
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        assert _run(repo, "rev-parse", "develop").strip() == develop_before
        assert _run(repo, "status", "--porcelain").strip() == ""

    def test_a_conflict_outside_the_ledger_is_refused_and_aborted(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Only the cut's own conflict is the script's to resolve."""
        repo = _mini_nexus(tmp_path)
        branch = _cut(repo)["branch"]
        _merge_cut_into_origin_main(repo, branch)
        # A competing edit to a plugin file on BOTH sides after the cut.
        _write(repo, "conexus/hooks/hook.py", "main-side edit after the cut\n")
        _run(repo, "commit", "-q", "-am", "main edit")
        _run(repo, "push", "-q", "origin", "main")
        _run(repo, "checkout", "-q", "develop")
        _write(repo, "conexus/hooks/hook.py", "develop-side edit after the cut\n")
        _run(repo, "commit", "-q", "-am", "develop edit")
        develop_before = _run(repo, "rev-parse", "develop").strip()
        proc = subprocess.run(
            ["bash", str(BACK_MERGE), str(repo), "--no-verify"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "conflicts outside the ledger" in proc.stderr
        assert "conexus/hooks/hook.py" in proc.stderr
        assert not (repo / ".git" / "MERGE_HEAD").exists(), "merge must be aborted"
        assert _run(repo, "rev-parse", "develop").strip() == develop_before
        assert _run(repo, "status", "--porcelain").strip() == ""


class TestBatteryAndWindow:
    def test_the_battery_runs_against_the_branch(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        assert result["battery_ran"] == ["battery"]

    def test_the_produced_branch_passes_parity_and_window_checks(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step 17: a cut script that emits a branch its own battery
        rejects is a failure earlier plan revisions actually contained.
        Parity: the ref parses for the version with a clean wheel-surface
        proof. Window: conditions (a), (b) and (d) hold on the branch
        ((c) is the upstream probe, not evaluable in a fixture).

        GITHUB_HEAD_REF must be cleared: on PR CI it names the PR's own
        branch and current_branch_name prefers it over the fixture
        repo's real branch (first hit on the 7.16.0 release PR)."""
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        from plugin_channel import (
            is_cut_branch_for,
            next_plugin_tag_number,
            parse_plugin_tag,
            wheel_surface_offenders,
        )

        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        version, n = parse_plugin_tag(result["tag"])
        assert (version, n) == (VERSION, result["n"])  # (a)
        assert next_plugin_tag_number(VERSION, cwd=repo) == result["n"]  # (b)
        assert is_cut_branch_for(VERSION, result["n"], cwd=repo) is True  # (d)
        head = _run(repo, "rev-parse", "HEAD")
        assert wheel_surface_offenders(BASE_TAG, head, cwd=repo) == []

    def test_no_counter_file_is_written(self, tmp_path: pathlib.Path) -> None:
        repo = _mini_nexus(tmp_path)
        _cut(repo)
        assert not (repo / "conexus" / "PLUGIN_CHANNEL_VERSION").exists()
        listing = _run(repo, "diff", "--name-only", "origin/main")
        assert "PLUGIN_CHANNEL_VERSION" not in listing

    def test_prints_the_does_not_reach_installs_instructions(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """nexus-konsk: a same-version anchored cut moves no version field,
        so `perform_cut` must print the recovery instructions naming both
        the automatic path (`nx upgrade`'s plugin-lockstep ref-drift check)
        and the manual forcing command (uninstall then install) -- the
        review round that shipped the fix noted no test asserted this text
        (`nexus/critique-nexus-konsk-2026-09-11` [25316] Q5)."""
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)

        out = capsys.readouterr().out
        assert f"this cut does NOT reach installs by itself ({result['tag']} moves no version field):" in out
        assert "nx upgrade" in out
        assert "already at the latest version" in out
        assert "claude plugin uninstall <id>@<marketplace> -s <scope> -y" in out
        assert "claude plugin install <id>@<marketplace> -s <scope> -y" in out


# ---------------------------------------------------------------------------
# The atomic-split precondition (nexus-a2wmi.9): attribution + refusal.
# ---------------------------------------------------------------------------


class TestAttribution:
    """The attribution rule, stated so it needs no interpreting."""

    def test_a_structured_marker_wins_over_everything(self) -> None:
        from cut_plugin_release import attribute_entry

        entry = (
            "- `conexus/hooks/h.py`: thing nexus-aaaaa did\n"
            "  bead: nexus-bbbbb\n"
        )
        assert attribute_entry(entry) == "nexus-bbbbb"

    def test_first_bead_token_on_the_first_line_only(self) -> None:
        from cut_plugin_release import attribute_entry

        entry = "- `conexus/hooks/h.py`: nexus-aaaaa then nexus-bbbbb\n"
        assert attribute_entry(entry) == "nexus-aaaaa"

    def test_body_ids_are_prose_never_attribution(self) -> None:
        """The real nexus-7zup9 entry's shape: a second bead id in the
        body attributed the entry to a bead that never touched it."""
        from cut_plugin_release import attribute_entry

        entry = (
            "- `conexus/evals/` (NEW): nexus-7zup9 — the first eval corpus\n"
            "  supersedes the nexus-77cct plan that never shipped\n"
        )
        assert attribute_entry(entry) == "nexus-7zup9"

    def test_an_unattributable_entry_is_refused_by_name(self) -> None:
        from cut_plugin_release import CutRefused, attribute_entry

        entry = "- `conexus/hooks/orphan.py`: a change nobody claimed\n"
        with pytest.raises(CutRefused, match="orphan"):
            attribute_entry(entry)

    def test_header_prose_bullets_are_not_entries(self) -> None:
        """The ledger's contract bullets carry no path span and are never
        attributed (the real file opens with three of them)."""
        from cut_plugin_release import path_entries

        text = (
            "# Pending\n"
            "- Every file that differs MUST be declared here.\n"
            "- Do NOT fix a failure by deleting entries.\n"
            "- `conexus/hooks/h.py`: real entry (nexus-aaaaa)\n"
        )
        entries = path_entries(text)
        assert len(entries) == 1
        assert "real entry" in entries[0]


class TestAtomicSplit:
    def test_a_straddling_entry_refuses_naming_both_excluded_paths(
        self, tmp_path: pathlib.Path
    ) -> None:
        """RDR Critical Assumption 3's synthetic fixture: the bead's
        commit couples conexus/registry.yaml (allowed) to wheel content
        (excluded). Refusal, never a warning: the operator's only
        correct action is deferring the entry in the ledger."""
        repo = _mini_nexus(tmp_path, payload="straddle")
        with pytest.raises(CutRefused) as exc:
            _cut(repo)
        message = str(exc.value)
        assert "nexus-ddddd" in message
        assert "conexus/plans/builtin/p2.yml" in message
        assert "src/nexus/straddle.py" in message

    def test_the_refusal_precedes_any_mutation(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A refused cut leaves no branch, no modified file, nothing."""
        repo = _mini_nexus(tmp_path, payload="straddle")
        with pytest.raises(CutRefused):
            _cut(repo)
        assert _run(repo, "branch", "--list", "plugin-release/*") == ""
        assert _run(repo, "status", "--porcelain") == ""
        assert _run(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_a_single_surface_bead_with_repo_ride_alongs_is_allowed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """SCOPE (recorded deviation, judged at R2): offending means
        outside the allowlist AND inside the SHIPPED surface. tests/ and
        docs/ ride-alongs ship nowhere, so nothing is stranded — the
        literal any-path form would refuse every real cut ever attempted
        (today's real nexus-2v0v7 entry carries test ride-alongs)."""
        repo = _mini_nexus(tmp_path, payload="cleanbead")
        result = _cut(repo)
        assert result["moved_plugins"] == ["conexus"]

    def test_no_override_flag_exists(self) -> None:
        """Deferral in the ledger is the only path past a refusal."""
        from cut_plugin_release import _build_parser

        options = {
            string
            for action in _build_parser()._actions
            for string in action.option_strings
        }
        assert options == {"-h", "--help", "--repo"}

    def test_a_two_paragraph_entry_stays_one_entry(self, tmp_path: pathlib.Path) -> None:
        """R2 finding: a blank line inside a bullet (a CommonMark
        paragraph break within one list item) was treated as an entry
        boundary — the first half was judged covered and ERASED while
        the second half's wheel warning and bead: marker survived as
        orphaned prose. Both parsers must keep the item whole."""
        from cut_plugin_release import _rewrite_ledger, attribute_entry, path_entries

        entry = (
            "- `conexus/agents/dev.md`: guidance half of a split delivery\n"
            "\n"
            "  the wheel half is `src/nexus/plans/x.py` and ships at the\n"
            "  next client release, not on a plugin cut.\n"
            "  bead: nexus-fffff\n"
        )
        text = "# Pending\n" + entry
        entries = path_entries(text)
        assert len(entries) == 1
        assert "wheel half" in entries[0]
        assert attribute_entry(entries[0]) == "nexus-fffff"

        repo = tmp_path / "ledgeronly"
        (repo / "conexus").mkdir(parents=True)
        (repo / "conexus" / "PENDING_RELEASE.md").write_text(text, encoding="utf-8")
        _rewrite_ledger(repo)
        after = (repo / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")
        assert after == text  # uncovered split-delivery entry survives WHOLE

    def test_bead_id_matching_is_fixed_string(self, tmp_path: pathlib.Path) -> None:
        """R2 finding: --grep's default BRE let the dot in nexus-xxxxx.5
        match any character, attributing near-miss commit text. With -F
        the near-miss commit is not scanned and the cut proceeds."""
        repo = _mini_nexus(tmp_path, payload="cleanbead")
        # The fixture returns checked out on main; the scenario commits
        # must land on DEVELOP or the push republishes the old branch
        # and the test goes vacuous (test-validator finding, R2 gate).
        _run(repo, "checkout", "-q", "develop")
        # A wheel-touching commit whose text is a NEAR MISS for the
        # ledger bead id under BRE dot-matching (nexus-eeeee vs a
        # dotted-id shape): use a dotted ledger bead to expose it.
        _write(repo, "src/nexus/near_miss.py", "wheel content\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "unrelated: nexus-eeeeeX5 work")
        _write(
            repo,
            "conexus/PENDING_RELEASE.md",
            "# Pending\n- `conexus/hooks/hook.py`: guard tightened (nexus-eeeee.5)\n",
        )
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "fix: hook guard (nexus-eeeee.5)")
        _run(repo, "push", "-q", "origin", "develop")
        result = _cut(repo)
        assert result["moved_plugins"] == ["conexus"]

    def test_an_unattributed_straddling_commit_escapes_the_scan(
        self, tmp_path: pathlib.Path
    ) -> None:
        """KNOWN BOUNDARY, pinned so it is a decision and not a surprise:
        attribution is by commit message, so a straddling commit that
        never names its bead is not scanned. Backstops: the stray-path
        assert (wheel content cannot ship regardless) and human review
        of the cut PR. Documented in atomic_split_check's docstring."""
        repo = _mini_nexus(tmp_path, payload="cleanbead")
        _run(repo, "checkout", "-q", "develop")  # see the fixed-string test
        _write(repo, "src/nexus/unnamed.py", "wheel half, bead never named\n")
        _write(repo, "conexus/hooks/hook.py", "v3 coupled to the wheel half\n")
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "a commit that names no bead")
        _run(repo, "push", "-q", "origin", "develop")
        result = _cut(repo)  # proceeds: the scan cannot see the coupling
        assert result["moved_plugins"] == ["conexus"]

    def test_the_real_ledger_attributes_cleanly(self) -> None:
        """Property smoke over the LIVE ledger: every path-carrying entry
        attributes to exactly one bead. Asserts the property, never a
        specific bead id — the ledger changes every release."""
        from cut_plugin_release import attribute_entry, path_entries

        real = (
            pathlib.Path(__file__).resolve().parent.parent
            / "conexus"
            / "PENDING_RELEASE.md"
        )
        entries = path_entries(real.read_text(encoding="utf-8"))
        if not entries:
            # The release window: the pin advance just emptied the ledger,
            # a legitimate recurring state (first hit on the 7.16.0 cut's
            # own PR CI). Nothing to attribute is a pass; the attribution
            # unit tests above carry the mechanism's non-vacuity.
            return
        for entry in entries:
            bead = attribute_entry(entry)  # raises = the test fails, by name
            assert bead.startswith("nexus-")


# ---------------------------------------------------------------------------
# The real deferral mechanism (nexus-2x3qy): Sam's disposition was to build
# it, not weaken atomic_split_check's wording. A "## Deferred to the next
# client release" ledger entry is exempt from the straddle refusal AND its
# channel path(s) are held back from THIS cut's import -- the whole bead
# ships together at the next client release, never split across two.
# ---------------------------------------------------------------------------


class TestDeferralParsing:
    """The parser, isolated from perform_cut."""

    def test_an_entry_under_the_heading_is_deferred(self) -> None:
        from cut_plugin_release import _deferred_entries

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
        """An entry after a DIFFERENT '## ' heading following the
        deferral section is not deferred -- section membership is by the
        LAST heading seen, not "everything after the first Deferred
        heading in the file"."""
        from cut_plugin_release import _deferred_entries

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

    def test_old_format_ledgers_with_no_deferred_heading_still_parse(self) -> None:
        """Backward compatibility: a ledger written before this bead has
        no deferral section at all, and must parse as zero deferred
        entries, never an error."""
        from cut_plugin_release import _deferred_entries, deferred_paths, path_entries

        text = (
            "# Pending\n"
            "- Every file that differs MUST be declared here.\n"
            "- `conexus/hooks/h.py`: real entry (nexus-aaaaa)\n"
        )
        assert _deferred_entries(text) == []
        assert deferred_paths(text) == set()
        assert len(path_entries(text)) == 1  # unaffected by the new parser

    def test_deferred_paths_excludes_wheel_and_bare_prefix_spans(self) -> None:
        """Only allowlisted, path-shaped spans count -- the wheel half of
        a split entry was never going to ship regardless of deferral,
        and a bare prefix root (`conexus/`) is prose, not a path."""
        from cut_plugin_release import deferred_paths

        text = (
            "# Pending\n"
            "## Deferred to the next client release\n"
            "- `conexus/agents/dev.md` (see `conexus/`): split delivery, "
            "wheel half `src/nexus/plans/x.py` (nexus-fffff)\n"
        )
        assert deferred_paths(text) == {"conexus/agents/dev.md"}


class TestDeferralInAtomicSplitCheck:
    def test_a_deferred_straddling_entry_does_not_refuse(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path, payload="straddle-deferred")
        result = _cut(repo)
        assert result["moved_plugins"] == ["conexus"]

    def test_a_new_deferred_path_never_ships(self, tmp_path: pathlib.Path) -> None:
        """registry.yaml never existed at origin/main: held back means it
        stays absent on the cut branch, exactly as if the import had
        never touched it."""
        repo = _mini_nexus(tmp_path, payload="straddle-deferred")
        _cut(repo)
        assert not (repo / "conexus/registry.yaml").exists()
        _run(repo, "diff", "--quiet", "origin/main", "--", "conexus/registry.yaml")

    def test_the_coupled_wheel_half_still_never_ships(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path, payload="straddle-deferred")
        _cut(repo)
        assert not (repo / "src/nexus/straddle.py").exists()

    def test_a_preexisting_deferred_path_is_restored_not_deleted(
        self, tmp_path: pathlib.Path
    ) -> None:
        """conexus/hooks/hook.py already exists at main (baseline "v1\\n");
        holding it back must restore that content, never delete the file."""
        repo = _mini_nexus(tmp_path, payload="straddle-deferred-edit")
        _cut(repo)
        assert (repo / "conexus/hooks/hook.py").read_text() == "v1\n"
        assert not (repo / "src/nexus/straddle2.py").exists()

    def test_the_deferred_entry_survives_the_cut_untouched(
        self, tmp_path: pathlib.Path
    ) -> None:
        """_rewrite_ledger must never empty a deferred entry: it reads as
        "covered" under the plain heuristic (an allowlisted span, no
        wheel span ON THE ENTRY ITSELF), which would erase it on the
        very cut that is deliberately not shipping it."""
        repo = _mini_nexus(tmp_path, payload="straddle-deferred")
        _cut(repo)
        ledger = (repo / "conexus/PENDING_RELEASE.md").read_text(encoding="utf-8")
        assert "## Deferred to the next client release" in ledger
        assert "conexus/registry.yaml" in ledger
        assert "nexus-ddddd" in ledger

    def test_the_stray_path_assert_still_passes(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The final stray-path check in perform_cut must see nothing
        outside the allowlist -- the hold-back restore must leave the
        branch clean, not merely absent from the two files checked above."""
        repo = _mini_nexus(tmp_path, payload="straddle-deferred")
        _cut(repo)
        stray = [
            line.strip()
            for line in _run(
                repo, "diff", "--name-only", "origin/main"
            ).splitlines()
        ]
        from cut_plugin_release import _is_allowlisted

        assert all(_is_allowlisted(p) for p in stray), stray


class TestBatterySubstrateEnv:
    """Each battery step declares whether it needs the engine (nexus-ps1gx).

    On 2026-09-08 the plugin-v7.36.1-1 cut ran from a fresh clone with no
    engine jar. Its lint step errored 1216 substrate-backed tests at SETUP and
    the cut refused, leaving a half-created branch behind — not on any finding,
    just on an absent jar the lint bucket does not need. The container
    rehearsal passed the identical step because its environment already sets
    ``NX_TEST_T2_SUBSTRATE=none``, exactly as ``plugin-release.yml`` does, so
    the rehearsal could not reproduce the refusal. That is why it survived to
    bite a real cut.

    A step that does need the engine must keep the ambient setting, so this
    pins the SPLIT, not the presence of a flag.
    """

    def test_pytest_legs_are_marked_substrate_free(self) -> None:
        import cut_plugin_release as mod

        pytest_legs = [
            (cmd, needs) for cmd, needs in mod._BATTERY if cmd[:2] == ["uv", "run"]
        ]
        assert pytest_legs, "no pytest legs found — the battery shape changed"
        for cmd, needs_substrate in pytest_legs:
            assert not needs_substrate, (
                f"{' '.join(cmd)} is marked as needing the engine substrate. The lint "
                f"bucket and these unit legs do not, and marking them so reintroduces "
                f"the 1216-setup-error refusal on a clone with no jar (nexus-ps1gx)."
            )

    def test_substrate_free_legs_actually_receive_the_env(self, monkeypatch) -> None:
        """The marking has to reach subprocess.run, not just sit in a table."""
        import cut_plugin_release as mod

        seen: list[tuple[list[str], str | None]] = []

        class _OK:
            returncode = 0

        def fake_run(command, cwd=None, text=None, env=None):
            value = None if env is None else env.get("NX_TEST_T2_SUBSTRATE")
            seen.append((command, value))
            return _OK()

        setattr_in(monkeypatch, mod, "subprocess.run", fake_run)
        mod._run_real_battery(pathlib.Path("."))

        assert len(seen) == len(mod._BATTERY)
        for (command, value), (_cmd, needs_substrate) in zip(seen, mod._BATTERY):
            if needs_substrate:
                assert value is None, (
                    f"{' '.join(command)} must inherit the ambient environment"
                )
            else:
                assert value == "none", (
                    f"{' '.join(command)} did not receive NX_TEST_T2_SUBSTRATE=none"
                )


# --- RDR-228 Phase 2 guard (nexus-sv2wx.13) ---------------------------------

SETUP_PY = "conexus/launch/nx_setup.py"
LAUNCH_PY = "conexus/launch/nx_launch.py"
STANDIN_PY = "conexus/launch/nx_standin.py"
#: A credentials-page module under a name nobody has chosen yet: the check
#: is keyed on the directory, so a name it never heard of is still covered.
CREDS_PY = "conexus/launch/nx_credentials_page.py"
NOTICE_PY = "conexus/hooks/scripts/nx_setup_notice.py"
ENDPOINT_PY = "conexus/hooks/scripts/_endpoint_resolve.py"
SETUP_MD = "conexus/commands/setup.md"
HOOKS_JSON = "conexus/hooks/hooks.json"
MCP_JSON = "conexus/.mcp.json"
LOCKSTEP_HOOK = "conexus/hooks/scripts/version_lockstep_hook.py"

#: What the pinned tag carries when Phase 1 HAS shipped: the launcher, its
#: stand-in, the notice, and the entries that call the launcher.
_PHASE1_SHIPPED = {
    LAUNCH_PY: "launcher v1\n",
    STANDIN_PY: "standin v1\n",
    NOTICE_PY: "notice v1\n",
    HOOKS_JSON: "hooks v1\n",
    MCP_JSON: "mcp v1\n",
    LOCKSTEP_HOOK: "lockstep v1\n",
}
#: What the pinned tag carries when it predates Phase 1 entirely (today).
_PRE_PHASE1 = {
    ENDPOINT_PY: "endpoint v1\n",
    HOOKS_JSON: "hooks v0\n",
    MCP_JSON: "mcp v0\n",
    LOCKSTEP_HOOK: "lockstep v0\n",
}

_P1_FILES = {
    LAUNCH_PY: "launcher v2\n",
    STANDIN_PY: "standin v2\n",
    NOTICE_PY: "notice v2\n",
    HOOKS_JSON: "hooks v2 via launcher\n",
    MCP_JSON: "mcp v2 via launcher\n",
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


def _ledger(awaiting: list[str], deferred: list[str]) -> str:
    def bullets(paths: list[str]) -> str:
        return "".join(f"- `{p}`: changed (nexus-p2xxx)\n" for p in paths)

    return (
        "# Pending\n\n## Awaiting the next release or plugin cut (pinned: v9.9.0)\n\n"
        + bullets(awaiting)
        + "\n\n## Deferred to the next client release\n\n"
        + bullets(deferred)
    )


def _land_on_develop(
    repo: pathlib.Path, files: dict[str, str], ledger: str, *, remove: tuple[str, ...] = ()
) -> None:
    """Commit *files* and the ledger on develop and publish it to origin."""
    _run(repo, "checkout", "-q", "develop")
    for rel in remove:
        _run(repo, "rm", "-q", rel)
    for rel, content in files.items():
        _write(repo, rel, content)
    _write(repo, "conexus/PENDING_RELEASE.md", ledger)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "feat: phase work (nexus-p2xxx)")
    _run(repo, "push", "-q", "origin", "develop")
    _run(repo, "checkout", "-q", "main")


def _phase_repo(
    tmp_path: pathlib.Path,
    *,
    base: dict[str, str],
    develop: dict[str, str],
    awaiting: list[str],
    deferred: list[str],
    remove: tuple[str, ...] = (),
    main_after_tag: dict[str, str] | None = None,
) -> tuple[pathlib.Path, str]:
    """*main_after_tag*: files an earlier plugin cut shipped to main after
    the client tag, so origin/main (what installs have) is ahead of it."""
    repo = _mini_nexus(tmp_path, payload="sn", main_extra=base)
    if main_after_tag:
        for rel, content in main_after_tag.items():
            _write(repo, rel, content)
        _run(repo, "add", "-A")
        _run(repo, "commit", "-q", "-m", "plugin cut #1 shipped these")
        _run(repo, "push", "-q", "origin", "main")
    ledger = _ledger(awaiting, deferred)
    _land_on_develop(repo, develop, ledger, remove=remove)
    return repo, ledger


def _check(repo: pathlib.Path, ledger: str) -> str:
    """The cut's own reference: origin/main, never the client tag."""
    return phase2_deferral_check(repo, "origin/main", "origin/develop", ledger)


class TestPhase2DeferralGuard:
    def test_phase2_path_under_awaiting_refuses_naming_path_and_heading(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[SETUP_MD], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        assert SETUP_MD in message
        assert "Deferred to the next client release" in message

    def test_a_credentials_module_under_a_name_nobody_chose_is_covered(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The check keys on conexus/launch/, not on file names: a module
        it has never heard of is still held to the rule."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[CREDS_PY], deferred=[p for p in _ALL_PATHS if p != CREDS_PY],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        assert CREDS_PY in str(exc.value)

    def test_a_path_missing_from_the_ledger_altogether_refuses(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        with pytest.raises(CutRefused) as exc:
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
        """setup.md equals the pinned tag's copy: it differs from nothing,
        so it need not appear anywhere."""
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
        """The pinned tag has the stand-in; develop removes it. A deletion
        reaches installs through a cut the way an edit does."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop=_P2_FILES,
            awaiting=[STANDIN_PY], deferred=sorted(_P2_FILES),
            remove=(STANDIN_PY,),
        )
        with pytest.raises(CutRefused) as exc:
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

    def test_a_pinned_tag_that_has_nx_setup_passes_and_says_so(
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
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        assert LAUNCH_PY in message
        # The case itself: a file cannot be Awaiting for Phase 1 and
        # Deferred for Phase 2; both phases ship in one client release.
        assert "'Awaiting' for Phase 1 and 'Deferred' for Phase 2" in message
        assert "deferring holds the whole file back" in message
        assert "ship in the same client release" in message
        assert "every path of both" in message

    def test_without_the_launcher_at_the_tag_hooks_and_mcp_must_be_deferred_too(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Phase 1 not shipped: nx_launch.py is held back to a base that
        lacks it, so entries that call it must not ship either."""
        guarded = (HOOKS_JSON, MCP_JSON, LOCKSTEP_HOOK)
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=list(guarded),
            deferred=[p for p in _ALL_PATHS if p not in guarded],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        message = str(exc.value)
        for path in guarded:
            assert path in message

    def test_with_the_launcher_at_the_tag_hooks_and_mcp_are_not_required(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED,
            develop={**_P2_FILES, HOOKS_JSON: "hooks v3\n", MCP_JSON: "mcp v3\n"},
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
        """The hook shim calls the lookup's new function, the lockstep hook
        leaves a CLI that cannot start to setup, and the registry and README
        name /conexus:setup: none of them ships ahead of the files they need
        (nexus-sv2wx.22)."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={**_P2_FILES, path: "phase 2\n"},
            awaiting=[path], deferred=sorted(_P2_FILES),
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        assert path in str(exc.value)

    def test_perform_cut_refuses_before_any_mutation(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, _ = _phase_repo(
            tmp_path, base=_PRE_PHASE1, develop={**_P1_FILES, **_P2_FILES},
            awaiting=[SETUP_MD], deferred=[p for p in _ALL_PATHS if p != SETUP_MD],
        )
        # A stubbed split check must not be able to skip the guard.
        with pytest.raises(CutRefused) as exc:
            _cut(repo, split_check=lambda *args: None)
        assert SETUP_MD in str(exc.value)
        assert _run(repo, "branch", "--list", "plugin-release/*") == ""
        assert _run(repo, "status", "--porcelain") == ""

    def test_perform_cut_holds_the_deferred_phase2_paths_back_to_the_base(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, _ = _phase_repo(
            tmp_path,
            base={**_PRE_PHASE1, SETUP_MD: "tag copy\n"},
            develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=_ALL_PATHS,
        )
        result = _cut(repo)
        assert "nothing to check" not in result["phase2_check"]
        assert not (repo / SETUP_PY).exists()
        assert not (repo / CREDS_PY).exists()
        assert (repo / SETUP_MD).read_text() == "tag copy\n"
        assert (repo / ENDPOINT_PY).read_text() == "endpoint v1\n"

    def test_a_phase1_plugin_cut_already_on_main_needs_no_deferral(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A plugin cut shipped Phase 1 to main; the client tag still lacks
        it. Measured from the client tag, the guard demanded Deferred
        entries for Phase 1 files that no longer differ from the pin, which
        the stale-entry test rejects: no later cut could pass. Measured from
        origin/main, only the Phase 2 paths count."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PRE_PHASE1, main_after_tag=_P1_FILES,
            develop={**_P1_FILES, **_P2_FILES},
            awaiting=[], deferred=sorted(_P2_FILES),
        )
        assert f"{len(_P2_FILES)} guarded path(s)" in _check(repo, ledger)
        result = _cut(repo)
        assert f"{len(_P2_FILES)} guarded path(s)" in result["phase2_check"]
        assert (repo / LAUNCH_PY).read_text() == _P1_FILES[LAUNCH_PY]
        assert not (repo / SETUP_PY).exists()

    def test_a_renamed_launch_file_counts_its_old_name(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Rename detection lists only the new name; the old name's
        deletion ships through a cut too."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED,
            develop={**_P2_FILES, "conexus/launch/nx_standin2.py": "standin v1\n"},
            remove=(STANDIN_PY,),
            awaiting=[STANDIN_PY],
            deferred=[*sorted(_P2_FILES), "conexus/launch/nx_standin2.py"],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        assert STANDIN_PY in str(exc.value)

    def test_setup_command_without_the_marker_still_arms(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A Phase 2 part landing ahead of nx_setup.py is still guarded."""
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={SETUP_MD: "early\n"},
            awaiting=[SETUP_MD], deferred=[],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        assert SETUP_MD in str(exc.value)

    def test_an_unknown_launch_module_without_the_marker_still_arms(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop={CREDS_PY: "early\n"},
            awaiting=[CREDS_PY], deferred=[],
        )
        with pytest.raises(CutRefused) as exc:
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
        for reference, source in (("v0.0.0", "origin/develop"),
                                  ("origin/main", "origin/nope")):
            with pytest.raises(CutRefused) as exc:
                phase2_deferral_check(repo, reference, source, ledger)
            assert "does not resolve" in str(exc.value)

    def test_the_phase1_paragraph_is_absent_once_the_launcher_shipped(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo, ledger = _phase_repo(
            tmp_path, base=_PHASE1_SHIPPED, develop=_P2_FILES,
            awaiting=[SETUP_PY], deferred=[],
        )
        with pytest.raises(CutRefused) as exc:
            _check(repo, ledger)
        assert "Phase 1 has not shipped" not in str(exc.value)
