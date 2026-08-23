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
import pathlib
import subprocess

import pytest

from cut_plugin_release import (
    CutRefused,
    check_main_readiness,
    perform_cut,
)

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


class TestBatteryAndWindow:
    def test_the_battery_runs_against_the_branch(
        self, tmp_path: pathlib.Path
    ) -> None:
        repo = _mini_nexus(tmp_path)
        result = _cut(repo)
        assert result["battery_ran"] == ["battery"]

    def test_the_produced_branch_passes_parity_and_window_checks(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Step 17: a cut script that emits a branch its own battery
        rejects is a failure earlier plan revisions actually contained.
        Parity: the ref parses for the version with a clean wheel-surface
        proof. Window: conditions (a), (b) and (d) hold on the branch
        ((c) is the upstream probe, not evaluable in a fixture)."""
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
