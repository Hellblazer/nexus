# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 2 (nexus-u5m5g.3): the engine's reconcile commit hard-codes three rules that today live in Python.

``nexus.catalog_reconcile_commit`` (``catalog-048-reconcile.xml``) carries the fill-only stamp keys, the two-miss
sweep threshold and the ``non_evidentiary`` truthiness set as SQL literals, because the commit must give what the
client path gives (the parity bar). While both implementations exist, a change to one that is not made to the other
is silent drift: the parity fixture only exercises the values it happens to contain. This test reads the literals
out of the changeset and compares them to the Python constants. It goes away with the client path (Step 6).

The comparison is a function over the changeset text so that it can be shown to fail: ``test_the_comparison_catches_a_drifted_literal``
feeds it a copy of the text with one literal changed at a time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nexus.catalog.indexed_meta import FILL_ONLY_KEYS, is_unset
from nexus.catalog.types import NON_EVIDENTIARY_META_KEY
from nexus.indexer import _ORPHAN_DELETE_MISSES

CHANGESET = (
    Path(__file__).resolve().parent.parent
    / "service" / "src" / "main" / "resources" / "db" / "changelog" / "catalog-048-reconcile.xml"
)

#: JSON values spanning every shape the three rules can see; each rule is compared over all of them.
_CANDIDATES = [None, False, True, 0, 0.0, 1, -1, "", "x", "0", "false", [], [0], ["x"], {}, {"a": 1}]


def _literal_list(body: str) -> set[str]:
    """``'null'::jsonb, '""'::jsonb`` -> ``{'null', '""'}``; ``'tags', 'category'`` -> ``{'tags', 'category'}``."""
    return {m.group(1) for m in re.finditer(r"'((?:[^']|'')*)'(?:::jsonb)?", body)}


def _norm(text: str) -> str:
    return "0" if text in ("0", "0.0") else text   # jsonb compares 0 and 0.0 equal


def _json_texts(pred) -> set[str]:
    return {_norm(json.dumps(v)) for v in _CANDIDATES if pred(v)}


def mismatches(sql: str) -> list[str]:
    """Every way the changeset text disagrees with the Python constants; empty when they agree."""
    out: list[str] = []

    fill = re.findall(r"e\.key NOT IN \(([^)]*)\)", sql)
    if len(fill) != 1:
        out.append(f"expected exactly one fill-only key list (e.key NOT IN (...)), found {len(fill)}")
    elif _literal_list(fill[0]) != set(FILL_ONLY_KEYS):
        out.append(f"fill-only keys: SQL {sorted(_literal_list(fill[0]))} != Python FILL_ONLY_KEYS {sorted(FILL_ONLY_KEYS)}")

    unset = re.findall(r"\(d\.metadata -> e\.key\) IN \(([^)]*)\)", sql)
    if len(unset) != 1:
        out.append(f"expected exactly one 'held value is unset' list, found {len(unset)}")
    else:
        want = _json_texts(lambda v: is_unset(v))
        if "(d.metadata -> e.key) IS NULL" not in sql:
            out.append("the fill-only rule must treat an absent key as unset (IS NULL)")
        if _literal_list(unset[0]) != want:
            out.append(f"unset values: SQL {sorted(_literal_list(unset[0]))} != Python is_unset {sorted(want)}")

    miss = re.findall(r"o\.miss \+ 1 >= (\d+)", sql)
    if len(miss) != 1:
        out.append(f"expected exactly one sweep threshold (o.miss + 1 >= N), found {len(miss)}")
    elif int(miss[0]) != _ORPHAN_DELETE_MISSES:
        out.append(f"sweep threshold: SQL {miss[0]} != Python _ORPHAN_DELETE_MISSES {_ORPHAN_DELETE_MISSES}")

    if f"'{NON_EVIDENTIARY_META_KEY}'" not in sql:
        out.append(f"the changeset never names the key {NON_EVIDENTIARY_META_KEY!r}")
    falsy = re.findall(rf"\(\w\.\w+ -> '{NON_EVIDENTIARY_META_KEY}'\) (?:NOT )?IN\s*\(([^)]*)\)", sql)
    if len(falsy) != 2:
        out.append(f"expected two non_evidentiary truthiness lists (old row has it, new row lacks it), found {len(falsy)}")
    want_falsy = _json_texts(lambda v: not v)
    for i, body in enumerate(falsy):
        got = {_norm(t) for t in _literal_list(body)}
        if got != want_falsy:
            out.append(f"non_evidentiary falsy set #{i + 1}: SQL {sorted(got)} != Python falsy {sorted(want_falsy)}")
    return out


@pytest.fixture(scope="module")
def changeset_text() -> str:
    assert CHANGESET.exists(), f"{CHANGESET} is missing"
    text = CHANGESET.read_text(encoding="utf-8")
    assert "catalog_reconcile_commit" in text and len(text) > 20_000, "non-vacuity: the changeset text was not read"
    return text


def test_the_commit_literals_match_the_python_constants(changeset_text: str) -> None:
    assert mismatches(changeset_text) == []


def _nth_falsy_list_without(n: int, literal: str):
    """A drift that removes ``literal`` from the n-th non_evidentiary truthiness list."""
    pattern = re.compile(rf"(\(\w\.\w+ -> '{NON_EVIDENTIARY_META_KEY}'\) (?:NOT )?IN\s*\()([^)]*)(\))")

    def drift(text: str) -> str:
        seen = -1

        def sub(m: re.Match) -> str:
            nonlocal seen
            seen += 1
            return m.group(1) + m.group(2).replace(f"'{literal}'::jsonb, ", "", 1) + m.group(3) if seen == n else m.group(0)
        return pattern.sub(sub, text)
    return drift


_DRIFTS = {
    "fill-only key removed": lambda t: t.replace("e.key NOT IN ('tags', 'category')", "e.key NOT IN ('tags')", 1),
    "fill-only key added": lambda t: t.replace("e.key NOT IN ('tags', 'category')", "e.key NOT IN ('tags', 'category', 'author')", 1),
    "miss threshold": lambda t: t.replace("o.miss + 1 >= 2", "o.miss + 1 >= 3", 1),
    "unset value removed": lambda t: t.replace("'[]'::jsonb, '{}'::jsonb)", "'[]'::jsonb)", 1),
    "first falsy list loses 0": _nth_falsy_list_without(0, "0"),
    "second falsy list loses false": _nth_falsy_list_without(1, "false"),
}


@pytest.mark.parametrize("name", sorted(_DRIFTS))
def test_the_comparison_catches_a_drifted_literal(changeset_text: str, name: str) -> None:
    drifted = _DRIFTS[name](changeset_text)
    assert drifted != changeset_text, f"non-vacuity: the drift {name!r} changed nothing"
    assert mismatches(drifted), f"the drift {name!r} went unnoticed"
