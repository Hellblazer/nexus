# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-scmcs: prose-edit ships as the conexus-prose-edit plugin and runs in any repository.

A document named by an absolute path is edited in the checkout that holds it (the first run of 2026-10-27 refused a
worktree document as outside the repository, then had the editor read the primary's copy); `setup` reports what a
first run needs; a repository without a site-page style sheet has no site layer instead of an error.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

from tests.prose_edit.conftest import ROOT, SCRIPT, Prose, git
from tests.prose_edit.test_brief import AGENT, SKILL, brief_ok, run_brief, seed

PLUGIN = ROOT / "prose-edit"


def _frontmatter(text: str) -> dict[str, str]:
    block = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in block.strip().splitlines())


def test_the_skill_dispatches_the_plugins_own_agent_and_preapproves_exactly_its_three_scripts() -> (
    None
):
    manifest = json.loads(
        (PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    agent = _frontmatter(AGENT.read_text(encoding="utf-8"))["name"]
    skill = SKILL.read_text(encoding="utf-8")
    assert f"`subagent_type` `{manifest['name']}:{agent}`" in skill
    rule = _frontmatter(skill)["allowed-tools"]
    pattern = re.fullmatch(r"Bash\((.*)\)", rule)
    assert pattern, rule
    prefixes = re.findall(
        r"^\| (?:BRIEF|MEMORY|REVIEW) \| `([^`]+)` \|$", skill, re.MULTILINE
    )
    assert len(prefixes) == 3
    for prefix in prefixes:
        assert fnmatch.fnmatchcase(f"{prefix} parse 'x.md'", pattern.group(1)), prefix
    assert not fnmatch.fnmatchcase(
        "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/x.py", pattern.group(1)
    )


def _worktree(prose: Prose) -> Path:
    wt = prose.cwd.parent / "wt"
    git(prose.cwd, "worktree", "add", "-q", "-b", "side", str(wt))
    (wt / "docs" / "x.md").write_text("worktree one\ntwo\n", encoding="utf-8")
    return wt


def test_an_absolute_path_in_another_checkout_names_that_checkout_and_the_same_repo(
    prose: Prose,
) -> None:
    wt = _worktree(prose)
    doc = str(wt / "docs" / "x.md")
    where = json.loads(
        subprocess.run(
            [sys.executable, str(SCRIPT), "repo", doc],
            cwd=prose.cwd,
            env=prose.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert (where["root"], where["path"]) == (str(wt.resolve()), "docs/x.md")
    assert (
        where["repo"] == prose.cwd.name
    )  # one style sheet for the primary and every worktree
    out = brief_ok(prose, "build", doc, "--genre", "reference-doc", "--work")
    work = dict(line.split("=", 1) for line in out.split("\n\n", 1)[0].splitlines())[
        "WORK"
    ]
    try:
        brief = out.split("\n\n", 1)[1]
        assert f"Document: {wt.resolve() / 'docs' / 'x.md'}\n" in brief
        assert f"Repository: {wt.resolve()} " in brief
    finally:
        brief_ok(prose, "rmtmp", work)


def test_a_relative_path_still_means_the_directory_the_session_started_in(
    prose: Prose,
) -> None:
    _worktree(prose)
    out = brief_ok(prose, "build", "docs/x.md", "--genre", "reference-doc", "--work")
    work = dict(line.split("=", 1) for line in out.split("\n\n", 1)[0].splitlines())[
        "WORK"
    ]
    try:
        assert f"Document: {prose.cwd.resolve() / 'docs' / 'x.md'}\n" in out
    finally:
        brief_ok(prose, "rmtmp", work)


def test_a_repository_without_a_site_page_builds_a_how_to_brief_with_no_site_layer(
    prose: Prose,
) -> None:
    assert not (prose.cwd / ".claude").exists()
    out = brief_ok(prose, "build", "docs/x.md", "--genre", "how-to", "--work")
    work = dict(line.split("=", 1) for line in out.split("\n\n", 1)[0].splitlines())[
        "WORK"
    ]
    brief_ok(prose, "rmtmp", work)
    gone = run_brief(prose, "site-layer")
    assert gone.returncode == 1 and "no site-page style sheet" in gone.stderr


def test_setup_lists_the_folders_no_genre_reaches_and_drops_one_once_mapped(
    prose: Prose,
) -> None:
    (prose.cwd / "notes").mkdir()
    (prose.cwd / "notes" / "a.md").write_text("a\n", encoding="utf-8")
    (prose.cwd / "notes" / "b.md").write_text("b\n", encoding="utf-8")
    git(prose.cwd, "add", "notes")
    report = json.loads(brief_ok(prose, "setup"))
    assert report["nexus"]["found"] is True and report["t2"] == {"reachable": True}
    assert report["no_genre"] == {
        "notes": 2
    }  # docs/x.md is top-level docs, a reference-doc by default
    assert report["site_page"] is False
    seed(prose, "repo", lists={"genre_map": ["notes/=how-to"]})
    mapped = json.loads(brief_ok(prose, "setup"))
    assert mapped["no_genre"] == {}
    assert mapped["genre_map"]["repo"] == ["notes/=how-to"]
