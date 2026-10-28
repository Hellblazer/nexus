# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-scmcs, Sam 2026-10-28: the front door of /prose-edit is guided (AskUserQuestion), not a free-text wall.

The first setup run on a 7.88.0 install listed about 180 folders (tests, fixtures, agent prompts) and asked the author
to classify them in prose. `setup` now reports at most eight documentation-looking `genre_candidates`, each with a
suggested genre, all six genres ranked best-first and a real sample file; a count of everything else; `ready`; and a
`try_path` for the closing suggestion. `suggest-genre` gives the run-time question the same suggestion. The wording the
skill shows lives in one table, `GENRE_DESCRIPTIONS`, and the README repeats it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from tests.prose_edit.conftest import ROOT, Prose, git
from tests.prose_edit.test_brief import SKILL, _module, brief_ok, run_brief

README = ROOT / "prose-edit" / "README.md"
SKILL_TEXT = SKILL.read_text(encoding="utf-8")
ALL_GENRES = (
    "rdr",
    "reference-doc",
    "how-to",
    "exploration-essay",
    "changelog",
    "commit-message",
)


def _put(root: Path, rel: str, text: str = "x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _layout(bare: Prose) -> None:
    """A repository that looks like someone else's: real docs beside tests, fixtures, hidden and script folders."""
    for rel in (
        "docs/runbooks/deploy.md",
        "docs/runbooks/restore.md",
        "docs/postmortem/2026-01-outage.md",
        "README.md",
        "tests/notes.md",
        "tests/fixtures/sample.md",
        "scripts/readme.md",
        ".github/pull_request_template.md",
    ):
        _put(bare.cwd, rel)
    git(bare.cwd, "add", "-A")


def _setup(bare: Prose, genre_map: list[str] | None = None) -> dict:
    """The setup report, built in-process from the status `memory.py status` would give: no T2, no tenant.

    `genre_map` is the repo layer's mapped folders. The wiring from the real `status` is covered by the T2-backed
    test_plugin_packaging.py::test_setup_offers_what_no_genre_reaches_as_keys_a_genre_map_entry_takes and
    test_allow_rule_setup.py::test_setup_reports_only_the_rule_the_user_settings_lack."""
    status = {
        "repo": bare.cwd.name,
        "root": str(bare.cwd),
        "genre_map": {"user": [], "repo": list(genre_map or [])},
        "exemplars": {},
    }
    return _module().setup_report(sys.executable, {"reachable": True}, status)


def _settings_with_rules(bare: Prose) -> None:
    config = bare.tmp.parent / "claude-config"
    config.mkdir(exist_ok=True)
    rules = {"permissions": {"allow": _module().allow_rules()}}
    (config / "settings.json").write_text(json.dumps(rules), encoding="utf-8")


def _candidates(report: dict) -> dict[str, dict]:
    return {c["key"]: c for c in report["genre_candidates"]}


# ---------------------------------------------------------------------------
# the descriptions table
# ---------------------------------------------------------------------------


def test_every_genre_has_one_plain_description_and_the_readme_repeats_it_word_for_word() -> None:
    mod = _module()
    table = mod.GENRE_DESCRIPTIONS
    assert tuple(sorted(table)) == tuple(sorted(ALL_GENRES))
    readme = README.read_text(encoding="utf-8")
    for genre, line in table.items():
        assert line and "\n" not in line and "—" not in line, genre
        assert len(line) <= 160, (genre, len(line))
        assert f"| `{genre}` | {line} |" in readme, genre


def test_the_descriptions_say_how_the_editor_treats_the_genre() -> None:
    table = _module().GENRE_DESCRIPTIONS
    for genre in _module().UNPACK_GENRES:
        assert "unpack" in table[genre].lower(), genre
    for genre in ("changelog", "commit-message"):
        assert "only cuts" in table[genre].lower(), genre


# ---------------------------------------------------------------------------
# setup: candidates
# ---------------------------------------------------------------------------


def test_setup_offers_only_folders_that_look_like_documentation(bare: Prose) -> None:
    _layout(bare)
    report = _setup(bare)
    assert "no_genre" not in report
    found = _candidates(report)
    assert set(found) == {"docs/runbooks/", "docs/postmortem/", "README.md"}
    assert found["docs/runbooks/"]["files"] == 2
    assert found["README.md"]["files"] == 1
    # everything else the old census listed: tests/notes.md, tests/fixtures/sample.md, scripts/readme.md,
    # .github/pull_request_template.md and the .claude site-page sheet, counted and not listed
    assert report["unmapped_other"] == 5
    assert [c["key"] for c in report["genre_candidates"]][0] == "docs/runbooks/"  # most files first


def test_setup_suggests_a_genre_from_the_folder_name_and_ranks_all_six(bare: Prose) -> None:
    _layout(bare)
    found = _candidates(_setup(bare))
    assert found["docs/runbooks/"]["suggested"] == "how-to"
    assert found["docs/postmortem/"]["suggested"] == "exploration-essay"
    assert found["README.md"]["suggested"] == "reference-doc"
    for cand in found.values():
        assert cand["ranked"][0] == cand["suggested"]
        assert sorted(cand["ranked"]) == sorted(ALL_GENRES)
        assert (bare.cwd / cand["sample"]).is_file()
        assert cand["sample"].startswith(cand["key"]), cand
    assert found["docs/runbooks/"]["sample"] in {
        "docs/runbooks/deploy.md",
        "docs/runbooks/restore.md",
    }


def test_setup_prints_the_descriptions_the_skill_shows(bare: Prose) -> None:
    _layout(bare)
    assert _setup(bare)["genre_descriptions"] == _module().GENRE_DESCRIPTIONS


def test_setup_caps_the_candidates_at_eight_and_counts_the_rest(bare: Prose) -> None:
    for n in range(12):
        _put(bare.cwd, f"docs/area{n:02d}/a.md")
        _put(bare.cwd, f"docs/area{n:02d}/b.md")
    _put(bare.cwd, "tests/t.md")
    git(bare.cwd, "add", "-A")
    report = _setup(bare)
    assert len(report["genre_candidates"]) == 8
    shown = sum(c["files"] for c in report["genre_candidates"])
    assert shown == 16
    # 4 folders of 2 files left over, tests/t.md and the .claude site-page sheet
    assert report["unmapped_other"] == 4 * 2 + 2


@pytest.mark.parametrize(
    ("folder", "genre"),
    [
        ("docs/operations/", "how-to"),
        ("docs/migration/", "how-to"),
        ("docs/guides/", "how-to"),
        ("docs/howto/", "how-to"),
        ("docs/how-to/", "how-to"),
        ("docs/tutorials/", "how-to"),
        ("docs/field-report/", "exploration-essay"),
        ("docs/essays/", "exploration-essay"),
        ("docs/notes/", "reference-doc"),
        ("docs/design/adr/", "rdr"),
        ("docs/decisions/", "rdr"),
        ("docs/rdr-archive/", "rdr"),
        ("docs/changes/", "changelog"),
        ("docs/address-book/", "reference-doc"),
        ("docs/api/", "reference-doc"),
        ("src/content/blog/", "exploration-essay"),
        ("posts/", "exploration-essay"),
        ("articles/", "exploration-essay"),
    ],
)
def test_the_suggestion_follows_the_folder_name(folder: str, genre: str) -> None:
    assert _module().suggest_genre_for_key(folder) == genre


def test_a_root_file_is_keyed_by_its_own_name_and_changelog_suggests_changelog() -> None:
    mod = _module()
    assert mod.folder_key("CHANGES.md") == "CHANGES.md"
    assert mod.folder_key("docs/runbooks/a.md") == "docs/runbooks/"
    assert mod.suggest_genre_for_key("CHANGES.md") == "changelog"
    assert mod.suggest_genre_for_key("README.md") == "reference-doc"


def test_a_mapped_folder_stops_being_a_candidate(bare: Prose) -> None:
    _layout(bare)
    assert "docs/runbooks/" not in _candidates(_setup(bare, ["docs/runbooks/=how-to"]))


# ---------------------------------------------------------------------------
# setup: ready and try_path
# ---------------------------------------------------------------------------


def test_ready_needs_the_allow_rules_and_try_path_is_a_file_that_already_maps(bare: Prose) -> None:
    _layout(bare)
    report = _setup(bare)
    assert report["ready"] is False and report["allow_rules"]["missing"]
    assert report["try_path"] == "docs/x.md"  # top-level docs/*.md is a built-in reference-doc
    _settings_with_rules(bare)
    assert _setup(bare)["ready"] is True


def test_try_path_falls_back_to_the_top_candidates_sample(bare: Prose) -> None:
    git(bare.cwd, "rm", "-q", "-f", "docs/x.md")
    _layout(bare)
    report = _setup(bare)
    assert report["try_path"] == report["genre_candidates"][0]["sample"]


def test_try_path_is_the_mapped_file_once_the_author_has_mapped_a_folder(bare: Prose) -> None:
    git(bare.cwd, "rm", "-q", "-f", "docs/x.md")
    _layout(bare)
    report = _setup(bare, ["docs/runbooks/=how-to"])
    assert report["try_path"] in {"docs/runbooks/deploy.md", "docs/runbooks/restore.md"}
    assert report["genres_in_use"] == ["how-to"]
    assert "docs/runbooks/" not in _candidates(report)


def test_try_path_never_names_a_file_in_build_output(bare: Prose) -> None:
    git(bare.cwd, "rm", "-q", "-f", "docs/x.md")
    _put(bare.cwd, "dist/f.md")
    _put(bare.cwd, "node_modules/pkg/readme.md")
    git(bare.cwd, "add", "-A")
    assert _setup(bare, ["dist/=how-to"])["try_path"] is None


def test_a_repository_with_nothing_to_edit_reports_no_try_path(bare: Prose) -> None:
    git(bare.cwd, "rm", "-q", "-f", "docs/x.md")
    report = _setup(bare)
    assert report["try_path"] is None and report["genre_candidates"] == []


# ---------------------------------------------------------------------------
# suggest-genre
# ---------------------------------------------------------------------------


def test_suggest_genre_gives_the_runtime_question_its_options(bare: Prose) -> None:
    _layout(bare)
    out = json.loads(brief_ok(bare, "suggest-genre", "docs/runbooks/deploy.md"))
    assert out["path"] == "docs/runbooks/deploy.md"
    assert out["folder_key"] == "docs/runbooks/"
    assert out["suggested"] == "how-to" and out["ranked"][0] == "how-to"
    assert sorted(out["ranked"]) == sorted(ALL_GENRES)
    assert out["descriptions"] == _module().GENRE_DESCRIPTIONS


def test_suggest_genre_keys_a_root_file_by_name_and_accepts_an_absolute_path(bare: Prose) -> None:
    _layout(bare)
    out = json.loads(brief_ok(bare, "suggest-genre", str(bare.cwd / "README.md")))
    assert (out["path"], out["folder_key"], out["suggested"]) == (
        "README.md",
        "README.md",
        "reference-doc",
    )


def test_suggest_genre_refuses_a_path_outside_the_repository(bare: Prose, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    proc = run_brief(bare, "suggest-genre", str(outside))
    assert proc.returncode == 1 and "outside" in proc.stderr.lower()
    assert proc.stdout == ""


def test_the_key_suggest_genre_prints_is_one_a_genre_map_entry_takes(bare: Prose) -> None:
    # the T2-backed twin (setup's keys stored, then `genre-for` on the files) is in test_plugin_packaging.py
    _layout(bare)
    key = json.loads(brief_ok(bare, "suggest-genre", "docs/postmortem/2026-01-outage.md"))["folder_key"]
    layers = [[], [f"{key}=exploration-essay"], [], []]
    assert _module()._MEM.genre_for("docs/postmortem/2026-01-outage.md", layers) == "exploration-essay"
    assert _module()._MEM.genre_for("docs/runbooks/deploy.md", layers) is None


# ---------------------------------------------------------------------------
# the skill and the README
# ---------------------------------------------------------------------------


def _setup_run() -> str:
    start = SKILL_TEXT.index("\n## Setup run\n")
    end = SKILL_TEXT.find("\n## ", start + 1)
    return SKILL_TEXT[start:] if end < 0 else SKILL_TEXT[start:end]


def _step(n: int) -> str:
    start = SKILL_TEXT.index(f"\n{n}. ")
    end = SKILL_TEXT.find(f"\n{n + 1}. ", start)
    return SKILL_TEXT[start:end]


def test_the_skill_names_the_tool_and_says_a_question_ends_the_turns_work() -> None:
    assert "`AskUserQuestion`" in SKILL_TEXT
    asking = SKILL_TEXT[SKILL_TEXT.index("\n## Asking the author\n") :]
    asking = asking[: asking.find("\n## ", 5)]
    assert "ends the turn's work until" in asking
    for limit in ("1 to 4", "2 to 4", "five words", "12 characters", "(Recommended)"):
        assert limit in asking, limit


def test_a_session_without_the_choice_tool_asks_the_same_question_in_plain_text() -> None:
    # a headless `claude -p` session (the acceptance runners) cannot answer AskUserQuestion; the skill must still ask
    asking = SKILL_TEXT[SKILL_TEXT.index("\n## Asking the author\n") :]
    asking = asking[: asking.find("\n## ", 5)]
    assert "When `AskUserQuestion` is not available" in asking
    assert "numbered list" in asking


def test_the_setup_run_is_a_wizard_that_reads_the_new_fields() -> None:
    run = _setup_run()
    assert "no_genre" not in SKILL_TEXT
    for field in ("genre_candidates", "unmapped_other", "ready", "try_path", "genres_in_use", "genre_descriptions"):
        assert f"`{field}`" in run, field
    assert "AskUserQuestion" in run
    assert "/conexus:setup" in run
    for label in ("Add them (Recommended)", "Show me the JSON", "Skip", "Leave unmapped", "Later (Recommended)", "Add one now"):
        assert label in run, label
    assert "/conexus-prose-edit:prose-edit" in run and "Try:" in run
    assert "settings.local.json" in run
    assert "Never edit that file yourself" not in run  # the Edit-tool route replaces it, behind the harness prompt


def test_the_runtime_genre_question_runs_suggest_genre_and_still_never_picks_for_the_author() -> None:
    row = next(ln for ln in _step(4).splitlines() if "stderr contains `no genre`" in ln)
    assert "suggest-genre" in row and "AskUserQuestion" in row
    assert "Never choose a genre yourself" in row
    assert "Remember this for" in row and "Just this run" in row


def test_the_accept_and_reject_questions_are_guided() -> None:
    step11, step12 = _step(11), _step(12)
    assert "AskUserQuestion" in step11 and "Which edits do you accept?" in step11
    assert "What about the edits you did not accept?" in step11
    for label in ("Leave undecided (Recommended)", "Reject them all", "Choose which to reject"):
        assert label in step11, label
    assert "Answer a query or paragraph proposal with a change?" in step11
    assert "Apply these changes?" in step12 and "Change my picks" in step12 and "Cancel" in step12
    assert "more than 16" in step11  # the free-text numbers route stays for the overflow


def test_every_label_the_skill_marks_recommended_fits_the_tool_limits() -> None:
    for label in re.findall(r'"([^"]+) \(Recommended\)"', SKILL_TEXT):
        assert len(label.split()) <= 5, label


def test_the_readme_opens_with_a_quick_start_then_the_genre_table() -> None:
    text = README.read_text(encoding="utf-8")
    quick = text.index("## Quick start")
    assert quick < text.index("## Install") and quick < text.index("## Set up")
    block = text[quick : text.index("\n## ", quick + 5)]
    assert "/conexus:setup" in block and "/conexus-prose-edit:prose-edit setup" in block
    assert "/plugin install conexus-prose-edit@nexus-plugins" in block
    assert re.findall(r"^(\d+)\. ", block, re.MULTILINE) == ["1", "2", "3", "4", "5", "6"]
    assert "—" not in text
    for genre in ALL_GENRES:
        assert f"| `{genre}` |" in text
