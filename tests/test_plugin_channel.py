# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Channel allowlist and helpers for the plugin-only fast lane (nexus-a2wmi.1, pruned by nexus-rbmja.9).

Tests for ``scripts/plugin_channel.py`` — importable here because
pyproject sets ``pythonpath = ["scripts"]``, and deliberately OFF the
wheel surface (asserted below): the channel's machinery must never ship
in the wheel it polices.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from check_plugin_only_eligibility import FREE_EXACT, FREE_PREFIXES
from plugin_channel import (
    ALLOWED_EXACT,
    ALLOWED_PREFIXES,
    DENIED_PREFIXES,
    PLUGIN_BY_ALLOWLIST_PREFIX,
    TagVisibilityError,
    assert_tag_visibility,
    client_version_of,
    is_channel_path,
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


class TestBlindCheckoutSentinel:
    def test_a_sighted_checkout_passes(self, sighted_repo: Path) -> None:
        assert_tag_visibility(VERSION, cwd=sighted_repo)

    def test_a_blind_checkout_raises(self, blind_repo: Path) -> None:
        """Unfetched must never read as clean: the eligibility gate calls this
        before it trusts a diff from the base tag."""
        with pytest.raises(TagVisibilityError, match="blind to tags"):
            assert_tag_visibility(VERSION, cwd=blind_repo)

    def test_other_tags_do_not_substitute_for_the_base_tag(
        self, blind_repo: Path
    ) -> None:
        """Visibility means the BASE CLIENT tag resolves, nothing weaker."""
        _git("tag", "unrelated-tag", cwd=blind_repo)
        with pytest.raises(TagVisibilityError):
            assert_tag_visibility(VERSION, cwd=blind_repo)


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


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("v7.24.0", "7.24.0"),
        ("plugin-v7.24.0-1", None),  # the retired anchored form
        ("7.24.0", None),
        ("v7.24", None),
        ("v7.24.0\n", None),
        ("", None),
    ],
)
def test_client_version_of_reads_the_client_form_only(ref: str, expected: str | None) -> None:
    """The canonical pin reader: the client form yields its version,
    everything else None (never a ValueError from int('plugin-v7'))."""
    assert client_version_of(ref) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("conexus/skills/a/SKILL.md", True),
        ("sn/hooks/hooks.json", True),
        ("tool-spill/hooks/x.py", True),
        ("prose-edit/skills/p/SKILL.md", True),
        (".claude-plugin/marketplace.json", True),
        ("conexus/plans/builtin/x.yml", False),  # wheel package data, carved out
        ("conexus/daemon/x.yml", False),
        ("src/nexus/cli.py", False),
        ("tests/test_a.py", False),
    ],
)
def test_is_channel_path(path: str, expected: bool) -> None:
    """Empty DENIED_PREFIXES and the two carve-out rows fail: the plugin trees
    hold wheel content that a plugin-only release may never alter."""
    assert is_channel_path(path) is expected


def test_free_paths_are_off_the_wheel_and_sdist_surface() -> None:
    """The eligibility gate treats FREE_PREFIXES and FREE_EXACT as paths that
    never reach the nx code under test. That holds only while none of them is
    in, or contains, a wheel force-include or sdist include entry (nexus-rbmja.9;
    this replaces the pin that went with the wheel-surface proof). Add an entry
    that overlaps and this fails: a free path can never hide wheel content."""
    assert FREE_PREFIXES and FREE_EXACT, "the free set must not be empty"
    entries = _wheel_and_sdist_entries()
    assert entries, "no wheel or sdist entries parsed: the check examined nothing"
    for free in (*FREE_PREFIXES, *FREE_EXACT):
        for entry in entries:
            assert not path_has_prefix(entry, free), (entry, free)
            assert not path_has_prefix(free, entry), (entry, free)


@pytest.mark.parametrize("planted", ["docs/", "tests/e2e/lib", "CHANGELOG.md", "tests"])
def test_the_free_path_pin_fires_on_a_planted_entry(
    planted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity: a scratch copy of the entry list with a free path planted
    in it (the real pyproject is never touched) must fail the pin."""
    real = _wheel_and_sdist_entries()
    monkeypatch.setattr(
        sys.modules[__name__], "_wheel_and_sdist_entries", lambda: [*real, planted]
    )
    with pytest.raises(AssertionError):
        test_free_paths_are_off_the_wheel_and_sdist_surface()


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


def test_every_marketplace_plugins_directory_is_in_the_allowlist_under_its_own_name() -> None:
    """The name is not always the directory (conexus-tool-spill installs from
    tool-spill/). A plugin whose source.path is outside ALLOWED_PREFIXES
    could never ship through a cut, and one mapped to another name would move
    the wrong ref."""
    marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    problems = []
    for plugin in marketplace["plugins"]:
        prefix = plugin["source"]["path"].rstrip("/") + "/"
        if prefix not in ALLOWED_PREFIXES:
            problems.append(f"{plugin['name']}: {prefix} is not in ALLOWED_PREFIXES")
        elif PLUGIN_BY_ALLOWLIST_PREFIX.get(prefix) != plugin["name"]:
            problems.append(
                f"{plugin['name']}: {prefix} maps to "
                f"{PLUGIN_BY_ALLOWLIST_PREFIX.get(prefix)!r}"
            )
    assert not problems, problems
    assert set(ALLOWED_PREFIXES) == set(PLUGIN_BY_ALLOWLIST_PREFIX)


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
