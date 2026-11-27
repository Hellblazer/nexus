# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings table of ``docs/settings.md``, rendered from ``SETTINGS`` and compared with the file (RDR-259 Decision 11).

``SETTINGS`` in ``_audience.py`` is the one table of keys. ``render_table()`` writes the markdown table from it; the docs page
carries that output, and ``problems()`` parses the page and says where it disagrees. Regenerate the table with::

    uv run python -m tests._settings_doc

The page may hold any other tables; the one headed ``Key | Type and limits | ...`` is the checked one, and the one headed ``Id`` is
held to ``ADVICE_ROW_IDS``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from nexus.hooks import _audience as aud

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "settings.md"

HEADER = ("Key", "Type and limits", "Default", "Project", "User", "Combination", "On a bad value")
ID_HEADER = ("Id",)

#: Text a key's row adds to what its kind and limit say. The kind and the limit come from SETTINGS; these are the rules the
#: reader applies beyond them.
_NOTES: "dict[str, str]" = {"ci_wait": "; may contain the literal `<sha>`"}


def _code(value: object) -> str:
    if value is None:
        return "none"
    return "`" + json.dumps(value) + "`"


def _type_cell(s: "aud.Setting") -> str:
    limit = s.limit
    if s.kind == "int":
        text = "integer, 1 or more"
    elif s.kind == "enum":
        text = f'`"{aud.STRICT}"` or `"{aud.SEATBELT}"`'
    elif s.kind == "ids":
        text = "list of advice row ids"
    elif s.kind == "lines":
        text = (
            f"list of strings, each one line of at most {limit} bytes of UTF-8; "
            f"at most {aud.MAX_BRIEF_QUESTIONS} added in total across both files"
        )
    elif s.kind == "line":
        text = f"one line, at most {limit} bytes of UTF-8"
    elif s.kind == "branch":
        text = f"a branch name (no whitespace, no `..`, no leading `-`), at most {limit} bytes"
    elif s.kind == "bool":
        text = "boolean"
    else:
        raise AssertionError("the renderer does not know the kind " + s.kind)
    return text + _NOTES.get(s.key, "")


def render_row(s: "aud.Setting") -> "tuple[str, ...]":
    """The cells of one key's row."""
    failure = "absent" if s.failure is None else _code(s.failure)
    return (
        "`" + s.key + "`", _type_cell(s), _code(s.default), "yes" if s.project else "no", "yes" if s.user else "no",
        s.combine, failure,
    )


def render_rows(settings: "Mapping[str, aud.Setting] | None" = None) -> "list[tuple[str, ...]]":
    return [render_row(s) for s in (aud.SETTINGS if settings is None else settings).values()]


def _line(cells: "tuple[str, ...]") -> str:
    return "| " + " | ".join(cells) + " |"


def render_table(settings: "Mapping[str, aud.Setting] | None" = None) -> str:
    out = [_line(HEADER), _line(tuple("---" for _ in HEADER))]
    out += [_line(r) for r in render_rows(settings)]
    return "\n".join(out) + "\n"


def render_ids(ids: "tuple[str, ...] | None" = None) -> str:
    out = [_line(ID_HEADER), _line(("---",))]
    out += [_line(("`" + i + "`",)) for i in (aud.ADVICE_ROW_IDS if ids is None else ids)]
    return "\n".join(out) + "\n"


def parse_tables(text: str) -> "list[list[tuple[str, ...]]]":
    """Every markdown table in *text* as a list of rows of cells (the separator row left out)."""
    tables: "list[list[tuple[str, ...]]]" = []
    block: "list[tuple[str, ...]]" = []
    for raw in text.splitlines() + [""]:
        line = raw.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = tuple(c.strip() for c in line.strip("|").split("|"))
            if not all(set(c) <= set("-: ") and c for c in cells):
                block.append(cells)
        elif block:
            tables.append(block)
            block = []
    return tables


def problems(text: "str | None", settings: "Mapping[str, aud.Setting] | None" = None, ids: "tuple[str, ...] | None" = None) -> "list[str]":
    """Where *text* (the page's content, ``None`` for an absent file) disagrees with ``SETTINGS`` and the advice row ids."""
    if text is None:
        return ["docs/settings.md does not exist"]
    expected = render_rows(settings)
    want_ids = tuple(aud.ADVICE_ROW_IDS if ids is None else ids)
    tables = parse_tables(text)
    key_tables = [t for t in tables if t and t[0] == HEADER]
    id_tables = [t for t in tables if t and t[0][:1] == ID_HEADER]
    found: "list[str]" = []
    if len(key_tables) != 1:
        found.append(f"docs/settings.md has {len(key_tables)} key tables with the header {_line(HEADER)}; it needs exactly one")
    else:
        rows = key_tables[0][1:]
        have = {r[0]: r for r in rows if r}
        if len(have) != len(rows):
            found.append("the key table names a key twice")
        for exp in expected:
            got = have.get(exp[0])
            if got is None:
                found.append(f"the key table has no row for {exp[0]}")
                continue
            for name, e, g in zip(HEADER, exp, got):
                if e != g:
                    found.append(f"{exp[0]} {name}: the page says {g!r}, SETTINGS gives {e!r}")
            if len(got) != len(exp):
                found.append(f"{exp[0]} has {len(got)} columns, expected {len(exp)}")
        wanted = {e[0] for e in expected}
        for key in have:
            if key not in wanted:
                found.append(f"the key table has a row for {key}, which SETTINGS does not have")
    if len(id_tables) != 1:
        found.append(f"docs/settings.md has {len(id_tables)} advice row id tables whose first column is headed `Id`; it needs exactly one")
    else:
        got_ids = [r[0] for r in id_tables[0][1:]]
        want = ["`" + i + "`" for i in want_ids]
        if got_ids != want:
            found.append(f"the advice row ids on the page are {got_ids}; ADVICE_ROW_IDS gives {want}")
    return found


if __name__ == "__main__":
    print(render_table())
    print(render_ids())
