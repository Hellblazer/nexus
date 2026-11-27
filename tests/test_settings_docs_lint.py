# SPDX-License-Identifier: AGPL-3.0-or-later
"""docs/settings.md agrees with ``SETTINGS`` (RDR-259 Decision 11, Stage 3 ship-when; nexus-sby49.19).

The key table on the page is compared row by row with the table in ``_audience.py``: key, type and limits, default, project
yes or no, user yes or no, how two values combine, and the value a bad value falls to. The advice row ids on the page are held
to ``ADVICE_ROW_IDS``. The lint fails when the page is absent or carries no key table, so it cannot pass with nothing to check.
The negative cases tamper with a copy of the page and must each fail. Regenerate the table with ``uv run python -m tests._settings_doc``.
"""
from __future__ import annotations

import pytest

from nexus.hooks import _audience as aud
from tests import _settings_doc as sd


@pytest.fixture(scope="module")
def page() -> str:
    assert sd.DOC.is_file(), "docs/settings.md is absent: the lint has nothing to check"
    return sd.DOC.read_text(encoding="utf-8")


def test_the_page_agrees_with_settings(page) -> None:
    found = sd.problems(page)
    assert found == [], "docs/settings.md and SETTINGS disagree:\n  " + "\n  ".join(found)


def test_the_lint_examined_every_key_and_every_advice_row(page) -> None:
    """Non-vacuity: the page's table has a row per key, and the number is the number SETTINGS has."""
    key_tables = [t for t in sd.parse_tables(page) if t and t[0] == sd.HEADER]
    id_tables = [t for t in sd.parse_tables(page) if t and t[0][:1] == sd.ID_HEADER]
    assert len(key_tables) == 1 and len(id_tables) == 1
    assert len(key_tables[0]) - 1 == len(aud.SETTINGS) >= 10
    assert len(id_tables[0]) - 1 == len(aud.ADVICE_ROW_IDS) >= 8


def test_an_absent_page_fails() -> None:
    assert sd.problems(None) == ["docs/settings.md does not exist"]


@pytest.mark.parametrize("text", ["", "# Settings\n\nno table here\n", "| Condition | Project |\n| --- | --- |\n| absent | none |\n"])
def test_a_page_with_no_key_table_fails(text) -> None:
    found = sd.problems(text)
    assert any("needs exactly one" in p and "key tables" in p for p in found), found


def _tampered(page: str, old: str, new: str) -> str:
    assert page.count(old) >= 1, old
    return page.replace(old, new, 1)


def _row(page: str, key: str) -> str:
    return next(line for line in page.splitlines() if line.startswith(f"| `{key}` |"))


def test_a_tampered_default_fails(page) -> None:
    row = _row(page, "t1_nudge")
    cells = row.split(" | ")
    assert cells[2] == "`true`"
    bad = page.replace(row, row.replace("| `true` |", "| `false` |", 1), 1)
    assert any("`t1_nudge` Default" in p for p in sd.problems(bad)), sd.problems(bad)


def test_a_tampered_limit_fails(page) -> None:
    bad = _tampered(page, "at most 160 bytes of UTF-8", "at most 161 bytes of UTF-8")
    assert any("Type and limits" in p for p in sd.problems(bad))


@pytest.mark.parametrize("key,column,old,new", [
    ("telemetry", "Project", "| no | yes |", "| yes | yes |"),
    ("preflight", "User", "| yes | no |", "| yes | yes |"),
    ("subagent_git", "Combination", "strictest wins", "last wins"),
    ("subagent_git", "On a bad value", '`"strict"` |', '`"seatbelt"` |'),
])
def test_a_tampered_column_fails(page, key, column, old, new) -> None:
    row = _row(page, key)
    assert old in row, (key, old, row)
    bad = page.replace(row, row.replace(old, new, 1), 1)
    assert any(p.startswith(f"`{key}` {column}") for p in sd.problems(bad)), sd.problems(bad)


def test_a_missing_row_fails(page) -> None:
    bad = page.replace(_row(page, "ci_wait") + "\n", "", 1)
    assert any("no row for `ci_wait`" in p for p in sd.problems(bad)), sd.problems(bad)


def test_an_extra_row_fails(page) -> None:
    row = _row(page, "ci_wait")
    bad = page.replace(row, row + "\n" + row.replace("`ci_wait`", "`made_up`", 1), 1)
    found = sd.problems(bad)
    assert any("`made_up`" in p and "SETTINGS does not have" in p for p in found), found


def test_a_key_that_settings_gains_must_reach_the_page(page) -> None:
    added = dict(aud.SETTINGS)
    added["new_key"] = aud.Setting("new_key", "bool", False, True, True, "union", False)
    found = sd.problems(page, settings=added)
    assert any("no row for `new_key`" in p for p in found), found


def test_a_changed_advice_id_fails(page) -> None:
    found = sd.problems(page, ids=tuple(aud.ADVICE_ROW_IDS) + ("a_ninth_row",))
    assert any("ADVICE_ROW_IDS gives" in p for p in found), found
    bad = page.replace("| `pattern_kill` |", "| `pattern_stop` |", 1)
    assert any("advice row ids" in p for p in sd.problems(bad))


def test_the_renderer_covers_every_kind_settings_uses() -> None:
    """A new kind in SETTINGS must reach the renderer, not slip through as text the lint never compares."""
    assert {s.kind for s in aud.SETTINGS.values()} <= {"int", "enum", "ids", "lines", "line", "branch", "bool"}
    assert len(sd.render_rows()) == len(aud.SETTINGS)
