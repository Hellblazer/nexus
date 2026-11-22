# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ratchet (nexus-88aj0): a remedy hint that prints an ``nx`` command with a collection label in it quotes the label.

Since RDR-229 P3.6 a label may hold a space, ``;``, ``%``, ``:`` and non-ASCII letters, so ``nx collection delete
{name}`` printed bare is a command that splits, or runs something else, when pasted. Every such site passes the label
through :func:`shlex.quote`, as ``nexus.quarantine`` already did. A label in plain prose (``collection {name!r}``)
is not a command and is not watched.
"""
from __future__ import annotations

import pathlib
import re
import shlex

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: A collection flag or a verb whose next argument is a label, then a bare ``{expr}`` interpolation.
_BARE_LABEL = re.compile(
    r"(?:--collection|\s-c|nx collection (?:delete|info|reindex|rename|re-embed)|nx taxonomy (?:project|reset -c))"
    r" \{(?!shlex\.quote\(|quoted_)[A-Za-z_][\w.\[\]'\"]*\}"
)

#: Sites left for a series that is editing the same file now: "<path>" -> the line texts and why. Each must still
#: match (a stale entry fails), so the list empties as the sites are quoted.
_PENDING: dict[str, tuple[tuple[str, ...], str]] = {}


def _findings(text: str, rel: str) -> list[str]:
    return [f"{rel}:{n}: {line.strip()[:120]}" for n, line in enumerate(text.splitlines(), 1) if _BARE_LABEL.search(line)]


def test_the_detector_finds_the_shapes_it_claims_to() -> None:
    src = (
        'a = f"Re-run `nx collection delete {name}`."\n'  # 1: bare
        'b = f"nx collection delete {shlex.quote(name)}"\n'  # 2: quoted
        'c = f"nx t3 gc -c {other} --dry-run"\n'  # 3: bare
        'd = f"pass --collection {holder}"\n'  # 4: bare
        'e = f"collection {name!r} is gone"\n'  # 5: prose
        'f = f"nx collection rename {shlex.quote(old)} {shlex.quote(new)}"\n'  # 6: quoted
        'g = "nx taxonomy reset -c {collection} and discover"\n'  # 7: a .format template, bare
        'h = "nx taxonomy reset -c {quoted_collection} and discover"\n'  # 8: filled with shlex.quote by convention
    )
    assert [f.split(":")[1] for f in _findings(src, "x.py")] == ["1", "3", "4", "7"]


def test_no_command_hint_prints_a_label_unquoted() -> None:
    bad: list[str] = []
    scanned = 0
    for path in sorted((REPO_ROOT / "src" / "nexus").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        scanned += 1
        rel = path.relative_to(REPO_ROOT).as_posix()
        found = _findings(text, rel)
        if rel in _PENDING:
            texts, _why = _PENDING[rel]
            for t in texts:
                assert any(t in f for f in found), f"{rel}: pending site {t!r} is gone; drop it from _PENDING"
            found = [f for f in found if not any(t in f for t in texts)]
        bad += found
    assert scanned > 300, scanned
    assert not bad, "quote the label with shlex.quote (nexus-88aj0):\n" + "\n".join(bad)


def test_a_label_with_a_space_and_a_semicolon_survives_the_round_trip() -> None:
    label = "notes 50%: café; rm -rf x"
    assert shlex.split(f"nx collection delete {shlex.quote(label)}") == ["nx", "collection", "delete", label]


def test_every_module_that_quotes_a_label_imports_shlex() -> None:
    """A hint's f-string is evaluated only when it prints, so a missing ``import shlex`` is a NameError on the error
    path and nowhere else (the sweep's first cut put the import inside two module docstrings; the migrate-fallback
    test was the only thing that reached one)."""
    import ast  # noqa: PLC0415 — this test only

    missing: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "nexus").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "shlex.quote(" not in text:
            continue
        tree = ast.parse(text)
        imported = any(
            isinstance(node, ast.Import) and any(alias.name == "shlex" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            missing.append(path.relative_to(REPO_ROOT).as_posix())
    assert not missing, "uses shlex.quote with no import shlex:\n" + "\n".join(missing)
