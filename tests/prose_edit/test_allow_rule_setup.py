# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ger02.28, nexus-scmcs: the allow rules `brief.py setup` prints match the work directories `tmpdir` makes.

Measured 2026-10-18 (T2 nexus/prose-edit-allow-rule-measurement-2026-10-18): default mode needs both a Read() and an
Edit() rule on <realpath of the temp dir>/prose-edit-*/**, and a rule on a symlinked temp path never matches. Since the
skill ships as a plugin (which cannot carry permission rules), `setup` computes the two rules and reports which ones the
user settings lack; the skill shows them and never edits the file.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from tests.prose_edit.conftest import Prose
from tests.prose_edit.test_brief import SKILL, brief_ok


def _setup_run() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = text.index("\n## Setup run\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def test_the_setup_run_offers_the_rules_and_edits_user_settings_only_on_the_authors_pick() -> None:
    # nexus-scmcs, Sam 2026-10-28: the wizard asks (AskUserQuestion) and, on "Add them", merges the two rules with the
    # Edit tool behind the harness's own approval prompt. It used to print the JSON and forbid any edit of the file.
    section = _setup_run()
    assert "BRIEF setup" in section
    assert "allow_rules.json" in section and "~/.claude/settings.json" in section
    assert "Add the two temp-folder permission rules to ~/.claude/settings.json?" in section
    assert "Add them (Recommended)" in section and "Show me the JSON" in section
    assert "permissions.allow" in section and "Edit tool" in section
    assert "never write `settings.local.json`" in section
    assert "Change nothing else in the file" in section
    assert "`Read()`" in section and "`Edit()`" in section


def _symlinked_tmp(prose: Prose) -> tuple[Path, Path]:
    # TMPDIR reached through a symlink, as macOS's /var -> /private/var: the rules must name the target.
    real = prose.tmp
    real.mkdir(parents=True, exist_ok=True)
    link = real.parent / "tmp-link"
    link.symlink_to(real, target_is_directory=True)
    prose.env["TMPDIR"] = str(link)
    return real, link


def _config_dir(prose: Prose) -> Path:
    config = prose.tmp.parent / "claude-config"
    config.mkdir()
    prose.env["CLAUDE_CONFIG_DIR"] = str(config)
    return config


def test_the_printed_rules_name_the_real_temp_path_every_work_directory_sits_under(
    prose: Prose,
) -> None:
    real, link = _symlinked_tmp(prose)
    _config_dir(prose)
    report = json.loads(brief_ok(prose, "setup"))
    printed = os.path.realpath(real)
    rules = [
        f"Read(//{printed.lstrip('/')}/prose-edit-*/**)",
        f"Edit(//{printed.lstrip('/')}/prose-edit-*/**)",
    ]
    assert report["allow_rules"] == {
        "settings_found": False,
        "missing": rules,
        "json": {"permissions": {"allow": rules}},
    }
    work = Path(
        dict(
            line.split("=", 1)
            for line in brief_ok(prose, "tmpdir", "--ready").strip().splitlines()
        )["WORK"]
    )
    try:
        assert str(work.parent) == printed, (
            "brief.py resolves the temp dir, so the rule must name the real path"
        )
        rule_glob = "/" + printed.lstrip("/") + "/prose-edit-*/**"
        assert fnmatch.fnmatch(str(work / "brief.md"), rule_glob.replace("/**", "/*"))
        assert not fnmatch.fnmatch(
            str(work / "brief.md"), str(link) + "/prose-edit-*/*"
        )
    finally:
        brief_ok(prose, "rmtmp", str(work))


def test_setup_reports_only_the_rule_the_user_settings_lack(prose: Prose) -> None:
    _symlinked_tmp(prose)
    config = _config_dir(prose)
    first = json.loads(brief_ok(prose, "setup"))["allow_rules"]["json"]["permissions"][
        "allow"
    ]
    (config / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": [first[0], "Bash(ls:*)"]},
                "env": {"CANARY_TOKEN": "canary-value-7f3a"},
            }
        ),
        encoding="utf-8",
    )
    before = (config / "settings.json").read_bytes()
    out = brief_ok(prose, "setup")
    report = json.loads(out)
    # only the computed rules come back: nothing else in the file is printed, and the file is left as it was
    assert "canary" not in out.lower() and "Bash(ls:*)" not in out
    assert (config / "settings.json").read_bytes() == before
    assert report["allow_rules"]["settings_found"] is True
    assert report["allow_rules"]["missing"] == [first[1]]
    assert report["t2"] == {"reachable": True}
    assert set(report["exemplars"]) == {
        "rdr",
        "reference-doc",
        "how-to",
        "exploration-essay",
        "changelog",
        "commit-message",
    }
    assert report["genre_map"] == {"user": [], "repo": []}
