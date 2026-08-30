# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Channel primitives for the plugin-only release channel (RDR-197 P1a, nexus-a2wmi.1).

Tests-first for ``scripts/plugin_channel.py`` — importable here because
pyproject sets ``pythonpath = ["scripts"]``, and deliberately OFF the
wheel surface (asserted below): the channel's machinery must never ship
in the wheel it polices.

The module's docstring is the single home of invariants R and W and the
anchoring rule. These tests pin the behaviours; they do not restate the
doctrine.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from plugin_channel import (
    ALLOWED_EXACT,
    ALLOWED_PREFIXES,
    DENIED_PREFIXES,
    NEVER_DELIVERED_PREFIXES,
    PLUGIN_BY_ALLOWLIST_PREFIX,
    GitCommandError,
    TagVisibilityError,
    current_branch_name,
    format_plugin_tag,
    is_cut_branch_for,
    next_plugin_tag_number,
    parse_plugin_tag,
    path_has_prefix,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

VERSION = "7.15.0"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path, *, base_tag: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@test.invalid", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    _git("add", "seed", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    if base_tag:
        _git("tag", f"v{VERSION}", cwd=repo)
    return repo


@pytest.fixture
def sighted_repo(tmp_path: Path) -> Path:
    """A checkout where the base client tag resolves: the sentinel passes."""
    return _make_repo(tmp_path, base_tag=True)


@pytest.fixture
def blind_repo(tmp_path: Path) -> Path:
    """A checkout that cannot see the base client tag.

    Indistinguishable, from the tag list alone, from a world with no
    cuts — which is exactly why the sentinel exists.
    """
    return _make_repo(tmp_path, base_tag=False)


class TestTagShape:
    def test_format_produces_the_anchored_form(self) -> None:
        assert format_plugin_tag(VERSION, 3) == "plugin-v7.15.0-3"

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("plugin-v7.15.0-1", ("7.15.0", 1)),
            ("plugin-v7.15.0-12", ("7.15.0", 12)),
        ],
    )
    def test_parse_accepts_the_anchored_form(
        self, ref: str, expected: tuple[str, int]
    ) -> None:
        assert parse_plugin_tag(ref) == expected

    @pytest.mark.parametrize(
        "ref",
        [
            "plugin-v7.15.0",
            "plugin-v7.15.0-0",
            "plugin-v7.15.0-01",
            "v7.15.0",
            "plugin-v7.15-1",
            "plugin-v7.15.0-1-2",
            "engine-service-v0.1.85",
            "plugin-v7.15.0-1\n",
        ],
    )
    def test_parse_rejects_everything_else(self, ref: str) -> None:
        assert parse_plugin_tag(ref) is None

    def test_format_parse_round_trip(self) -> None:
        assert parse_plugin_tag(format_plugin_tag(VERSION, 7)) == (VERSION, 7)


class TestNextPluginTagNumber:
    def test_empty_tag_list_with_visibility_is_one(self, sighted_repo: Path) -> None:
        assert next_plugin_tag_number(VERSION, cwd=sighted_repo) == 1

    def test_counts_past_existing_tags(self, sighted_repo: Path) -> None:
        _git("tag", "plugin-v7.15.0-1", cwd=sighted_repo)
        _git("tag", "plugin-v7.15.0-2", cwd=sighted_repo)
        assert next_plugin_tag_number(VERSION, cwd=sighted_repo) == 3

    def test_counts_from_the_highest_not_the_count(self, sighted_repo: Path) -> None:
        _git("tag", "plugin-v7.15.0-3", cwd=sighted_repo)
        assert next_plugin_tag_number(VERSION, cwd=sighted_repo) == 4

    def test_ignores_a_malformed_tag(self, sighted_repo: Path) -> None:
        _git("tag", "plugin-v7.15.0-x", cwd=sighted_repo)
        _git("tag", "plugin-v7.15.0-1", cwd=sighted_repo)
        assert next_plugin_tag_number(VERSION, cwd=sighted_repo) == 2

    def test_is_scoped_to_the_version(self, sighted_repo: Path) -> None:
        _git("tag", "plugin-v7.14.0-5", cwd=sighted_repo)
        assert next_plugin_tag_number(VERSION, cwd=sighted_repo) == 1

    def test_raises_when_the_enumeration_fails(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable tag list must never read as an empty one."""
        import plugin_channel

        real = plugin_channel._run_git

        def failing(args: list[str], cwd: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "tag":
                return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")
            return real(args, cwd)

        monkeypatch.setattr(plugin_channel, "_run_git", failing)
        with pytest.raises(GitCommandError):
            next_plugin_tag_number(VERSION, cwd=sighted_repo)


class TestBlindCheckoutSentinel:
    def test_a_blind_checkout_raises_instead_of_returning_one(
        self, blind_repo: Path
    ) -> None:
        """The whole point of the sentinel: unfetched must never read as empty.

        Window condition (b) consults next_plugin_tag_number; in a blind
        checkout that evaluation raises, so the window composition (.4)
        cannot grant a window here. Remove assert_tag_visibility from
        next_plugin_tag_number and this test fails — by design.
        """
        with pytest.raises(TagVisibilityError):
            next_plugin_tag_number(VERSION, cwd=blind_repo)

    def test_existing_plugin_tags_do_not_substitute_for_the_base_tag(
        self, blind_repo: Path
    ) -> None:
        """Visibility means the BASE CLIENT tag resolves, nothing weaker."""
        _git("tag", "plugin-v7.15.0-2", cwd=blind_repo)
        with pytest.raises(TagVisibilityError):
            next_plugin_tag_number(VERSION, cwd=blind_repo)


class TestCurrentBranchName:
    def test_prefers_github_head_ref(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "plugin-release/7.15.0-2")
        assert current_branch_name(cwd=sighted_repo) == "plugin-release/7.15.0-2"

    def test_falls_back_to_the_git_branch(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        assert current_branch_name(cwd=sighted_repo) == "main"

    def test_an_empty_env_hint_is_no_hint(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_HEAD_REF", "")
        assert current_branch_name(cwd=sighted_repo) == "main"

    def test_detached_head_with_no_hint_is_none(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never guess: no name means no window, not a fabricated name."""
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        _git("checkout", "-q", "--detach", cwd=sighted_repo)
        assert current_branch_name(cwd=sighted_repo) is None


class TestIsCutBranchFor:
    def test_true_only_on_the_exactly_matching_cut_branch(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        _git("checkout", "-q", "-b", "plugin-release/7.15.0-2", cwd=sighted_repo)
        assert is_cut_branch_for(VERSION, 2, cwd=sighted_repo) is True
        assert is_cut_branch_for(VERSION, 3, cwd=sighted_repo) is False
        assert is_cut_branch_for("7.14.0", 2, cwd=sighted_repo) is False

    @pytest.mark.parametrize("branch", ["main", "develop"])
    def test_false_on_ordinary_branches(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch, branch: str
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        if branch != "main":
            _git("checkout", "-q", "-b", branch, cwd=sighted_repo)
        assert is_cut_branch_for(VERSION, 1, cwd=sighted_repo) is False

    def test_false_when_no_branch_name_resolves(
        self, sighted_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
        _git("checkout", "-q", "--detach", cwd=sighted_repo)
        assert is_cut_branch_for(VERSION, 1, cwd=sighted_repo) is False


class TestPathPrefixHelper:
    """Step 8's reason to exist: the hatch force-include keys carry no
    trailing slash while DENIED_PREFIXES does, so a bare startswith
    comparison reports a carved-out path as uncovered — and matches
    "conexus/plansible" against "conexus/plans"."""

    def test_trailing_slash_is_immaterial_on_either_side(self) -> None:
        assert path_has_prefix("conexus/plans", "conexus/plans/") is True
        assert path_has_prefix("conexus/plans/", "conexus/plans") is True
        assert path_has_prefix("conexus/plans/builtin/x.yml", "conexus/plans") is True
        assert path_has_prefix("conexus/plans/builtin/x.yml", "conexus/plans/") is True

    def test_matches_on_component_boundaries_only(self) -> None:
        assert path_has_prefix("conexus/plansible", "conexus/plans/") is False
        assert path_has_prefix("conexus/plansible", "conexus/plans") is False
        assert path_has_prefix("conexus/plansible/y.yml", "conexus/plans") is False


def _wheel_and_sdist_entries() -> list[str]:
    cfg = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    targets = cfg["tool"]["hatch"]["build"]["targets"]
    force_include = list(targets["wheel"]["force-include"])
    sdist_include = list(targets["sdist"]["include"])
    return force_include + sdist_include


def test_scripts_is_off_the_wheel_and_sdist_surface() -> None:
    """The channel's machinery must not ship in the wheel it polices."""
    for entry in _wheel_and_sdist_entries():
        assert not path_has_prefix(entry, "scripts"), entry
        assert not path_has_prefix("scripts", entry), entry


def test_never_delivered_prefixes_are_off_the_wheel_and_sdist_surface() -> None:
    """The proof's carve-out may only ever name paths that cannot reach an
    installed user: none of NEVER_DELIVERED_PREFIXES may appear in, or
    contain, a wheel force-include or sdist include entry. Add one that
    does and this fails — the carve-out can never hide wheel content."""
    assert NEVER_DELIVERED_PREFIXES, "the carve-out must not be empty"
    for never in NEVER_DELIVERED_PREFIXES:
        for entry in _wheel_and_sdist_entries():
            assert not path_has_prefix(entry, never), (entry, never)
            assert not path_has_prefix(never, entry), (entry, never)


def test_never_delivered_paths_are_not_offenders(tmp_path: Path) -> None:
    """A range whose only off-channel changes are never-delivered
    (tests/, docs/, scripts/, .github/) proves clean; src/ still offends."""
    from plugin_channel import wheel_surface_offenders

    repo = tmp_path / "r"
    repo.mkdir()

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t.invalid")
    run("config", "user.name", "t")
    (repo / "seed").write_text("seed\n", encoding="utf-8")
    run("add", "seed")
    run("commit", "-q", "-m", "seed")
    run("tag", "v0.0.1")
    for rel in ("tests/test_a.py", "docs/a.md", "scripts/a.py", ".github/workflows/a.yml", "conexus/skills/a.md"):
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        run("add", rel)
    run("commit", "-q", "-m", "machinery + plugin")
    assert wheel_surface_offenders("v0.0.1", "HEAD", cwd=repo) == []
    (repo / "src").mkdir()
    (repo / "src" / "w.py").write_text("wheel\n", encoding="utf-8")
    run("add", "src/w.py")
    run("commit", "-q", "-m", "wheel")
    assert wheel_surface_offenders("v0.0.1", "HEAD", cwd=repo) == ["src/w.py"]


def test_the_allowlist_carves_out_every_wheel_path() -> None:
    """Every wheel-shipped path inside the channel allowlist must be an
    explicit DENIED_PREFIXES carve-out.

    Iterates the PARSED config, not today's three force-include keys, so
    a new wheel inclusion under conexus/ or sn/ fails here until it is
    either moved off the plugin tree or added to DENIED_PREFIXES.
    """
    uncovered = []
    for entry in _wheel_and_sdist_entries():
        inside = entry in ALLOWED_EXACT or any(
            path_has_prefix(entry, prefix) for prefix in ALLOWED_PREFIXES
        )
        if not inside:
            continue
        if not any(path_has_prefix(entry, denied) for denied in DENIED_PREFIXES):
            uncovered.append(entry)
    assert not uncovered, (
        f"{uncovered} ship in the wheel from inside the channel allowlist "
        f"but are not carved out by DENIED_PREFIXES — a plugin cut could "
        f"silently alter wheel content"
    )


def test_the_prefix_mapping_covers_every_marketplace_plugin() -> None:
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    plugins = {p["name"] for p in marketplace["plugins"]}
    assert set(PLUGIN_BY_ALLOWLIST_PREFIX.values()) == plugins


def test_nothing_under_src_imports_the_channel_module() -> None:
    """Acceptance pin: the channel is repo machinery, never wheel code.

    git grep exits 1 on no match (the pass state) and >=2 on error — an
    error also prints nothing, so the return code is checked too
    (test-validator finding, a2wmi.6)."""
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "grep", "-l", "plugin_channel", "--", "src/nexus/"],
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 1, (listing.returncode, listing.stderr)
    assert listing.stdout.strip() == "", listing.stdout


def test_no_counter_file_exists() -> None:
    """The channel is counter-less (RDR-197 as amended by 874bd681c):
    git's tag list is the only record of cuts. Do not reintroduce one."""
    assert not (REPO_ROOT / "conexus" / "PLUGIN_CHANNEL_VERSION").exists()
