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
