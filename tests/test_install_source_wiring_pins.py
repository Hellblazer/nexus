# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""WIRING PINS (shakedown playbook §3.1) for the install-source fixes. nexus-hibpr.

Each pin asserts that a caller still INVOKES a fix, not that the fix works --
the behavioural tests do that. This is the cheap proof for the failure class
where a body stays correct and its call site quietly disappears (the
nexus-cp9b8 shape). Every one of these lines was measured absent on
2026-08-27, and each absence produced a false green somewhere.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (_REPO / rel).read_text()


def test_release_sandbox_names_repo_root_as_the_install_source() -> None:
    """The sandbox once installed the CALLER's cwd while printing $REPO_ROOT and
    ended "SMOKE PASSED" about a tree it never installed."""
    text = _text("tests/e2e/release-sandbox.sh")
    calls = re.findall(r'^\s*"\$REPO_ROOT/scripts/reinstall-tool\.sh"([^\n]*)$', text, re.M)
    assert calls, "release-sandbox.sh no longer invokes reinstall-tool.sh at all"
    for args in calls:
        assert '"$REPO_ROOT"' in args, (
            f"reinstall-tool.sh invoked without an explicit source ({args.strip()!r}); "
            "it defaults to the caller's cwd"
        )


def test_release_sandbox_asserts_the_installed_version_matches_repo_root() -> None:
    text = _text("tests/e2e/release-sandbox.sh")
    assert '"$NX_VER_OUT" == *"$_expected_ver"*' in text, (
        "the post-install version assert is gone; the sandbox can smoke the wrong tree again"
    )


def test_upgrade_shakeout_defaults_to_a_release_below_the_tree_under_test() -> None:
    """PyPI-latest as the baseline cannot pass after a release: baseline == target."""
    text = _text("tests/e2e/upgrade-shakeout.sh")
    assert "key(v) < target" in text, "FROM_VERSION no longer resolves strictly below REPO_PKG_VERSION"
    assert "NX_TARGET" in text


def test_reinstall_tool_registers_the_legacy_tree() -> None:
    text = _text("scripts/reinstall-tool.sh")
    assert '. "$_INSTALL/legacy.sh"' in text
    assert "nx_register_legacy_generation" in text, (
        "the checkout install path no longer registers a legacy uv tree; every "
        "checkout-driven box goes back to never converging"
    )


def test_self_install_registers_the_legacy_tree_on_the_generation_path() -> None:
    text = _text("src/nexus/commands/self_cmd.py")
    body = text[text.index("def perform_self_install"):text.index("def _register_legacy_tree_if_present")]
    assert "_register_legacy_tree_if_present(install_dir, tools)" in body, (
        "perform_self_install's generation branch no longer registers a legacy tree"
    )
    assert '. "{install_dir}/legacy.sh"' in text, "_sh no longer sources legacy.sh"


def test_the_census_enumerates_legacy_candidates() -> None:
    text = _text("src/nexus/install_census.py")
    body = text[text.index("def generation_match_pairs"):text.index("def legacy_tree_candidates")]
    assert "legacy_tree_candidates(tools=tools)" in body


def test_doctor_holders_row_asks_for_the_legacy_tree() -> None:
    text = _text("src/nexus/health.py")
    body = text[text.index("def _check_generation_holders"):text.index("def _check_process_skew")]
    assert "legacy_tree_candidates" in body


def test_the_builder_absolutizes_a_directory_source() -> None:
    text = _text("src/nexus/_install/install_generation.sh")
    assert 'SOURCE="$(cd "$SOURCE" && pwd -P)"' in text, (
        "install_generation.sh writes the source verbatim again; a receipt can read \"source\": \".\""
    )


def test_nx_upgrade_repairs_a_uv_takeover_as_a_precondition() -> None:
    """The SessionStart lockstep hook runs `nx upgrade --auto`; the repair must
    sit on that path or a box never heals by itself."""
    text = _text("src/nexus/commands/upgrade.py")
    body = text[text.index("def _converge_preconditions"):text.index("def _run_ladder")]
    assert "repair_uv_takeover(" in body


def test_self_install_repairs_rather_than_migrating_when_a_layout_exists() -> None:
    """Running from uv's tree beside an existing generation layout is a
    takeover; converging through migrate_legacy.sh would bridge extras from
    the rebuilt uv receipt -- the one that dropped [local]."""
    text = _text("src/nexus/commands/self_cmd.py")
    body = text[text.index("def perform_self_install"):text.index("def _build_argv")]
    assert "_generation_layout_present(tools)" in body
    assert "repair_uv_takeover(" in body


def test_doctor_points_a_reclaimed_shim_at_nx_self_install_not_a_repo_script() -> None:
    """A packaged install has no scripts/reinstall-tool.sh (the nexus-gu9zo shape)."""
    text = _text("src/nexus/health.py")
    body = text[text.index("uv has taken them back"):text.index("def _check_shims_match_template")]
    assert "nx self install" in body
    assert "reinstall-tool" not in body


# nexus-heykz: the three copies of the `av` override agree.


def _override_entries_from_toml(path: str) -> list[str]:
    data = tomllib.loads(_text(path))
    return sorted(data["tool"]["uv"]["override-dependencies"])


def _override_entries_from_overrides_txt() -> list[str]:
    lines = _text("src/nexus/_install/overrides.txt").splitlines()
    return sorted(ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def test_the_av_override_is_identical_across_its_three_homes() -> None:
    """pyproject's [tool.uv] override protects only checkout runs; the wheel
    ships src/nexus/_install/overrides.txt for the generation installer and
    mcpb/pyproject.toml carries its own for the bundle's `uv sync`. Drift
    between them is exactly how every user install got av (nexus-heykz)."""
    root = _override_entries_from_toml("pyproject.toml")
    mcpb = _override_entries_from_toml("mcpb/pyproject.toml")
    shipped = _override_entries_from_overrides_txt()
    assert root == mcpb == shipped, (root, mcpb, shipped)
    assert any(entry.startswith("av;") or entry.startswith("av ") for entry in root)
