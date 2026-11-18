# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filter catalog documents on their own ``meta`` (nexus-6pfgb.16, RDR-236 Phase 2).

``nx search --where quality_gate_overridden=true`` and ``--where extraction_method=mineru`` read the CHUNK ROW,
and a row shared by two documents holds the last writer's value: a degraded document A that shares a chunk
with a clean B indexed later disappears from the audit, because B's index erased A's marker from the row.
Each document's catalog ``meta`` holds its own (nexus-6pfgb.6), so the audit that lists the documents accepted
with known-degraded text, or extracted by one backend, filters the documents themselves: ``nx catalog list
--meta KEY=VALUE`` (repeatable, ANDed). Pure functions; the verb does the scan.

Reading rules, so the filter says what the catalog says and no more:

- a value compares as text, a boolean as ``true`` / ``false``: ``quality_gate_overridden=true`` matches the
  documents whose key is the boolean ``true``;
- an ABSENT key matches nothing, because absent is unknown (``quality_gate_overridden=false`` finds the
  documents that recorded an explicit ``false``, not every document with no record) -- with one exception
  the key table states: an absent ``extraction_source`` means ``file``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: A key whose absence has a stated meaning (``src/nexus/tables/chunk-metadata-keys.toml``).
ABSENT_MEANS: Mapping[str, str] = {"extraction_source": "file"}


def parse_meta_filters(specs: Iterable[str]) -> list[tuple[str, str]]:
    """``["k=v", ...]`` as ``[(k, v), ...]``. A spec with no ``=`` or an empty key raises ``ValueError``."""
    out: list[tuple[str, str]] = []
    for spec in specs:
        key, sep, value = spec.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"--meta takes KEY=VALUE, got {spec!r}")
        out.append((key, value.strip()))
    return out


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def meta_matches(meta: Any, filters: Sequence[tuple[str, str]]) -> bool:
    """Whether a document's ``meta`` satisfies every ``(key, value)`` of *filters*."""
    meta = meta if isinstance(meta, Mapping) else {}
    for key, wanted in filters:
        # A key with a stated default reads None and "" as absent, as extraction_facts does (nexus-6pfgb.48).
        if key in meta and meta[key] is not None and not (key in ABSENT_MEANS and meta[key] == ""):
            have = _text(meta[key])
        elif key in ABSENT_MEANS:
            have = ABSENT_MEANS[key]
        else:
            return False
        if have != wanted:
            return False
    return True


_INTEGER_TEXT = re.compile(r"-?[0-9]+")


def meta_at_least(meta: Any, key: str, n: int) -> bool:
    """Whether ``meta[key]`` is an integer that is at least *n*: the one integer lower-bound rule
    (nexus-uwggq.2.2; ``nx catalog gc`` uses it for ``miss_count >= 2``).

    - The value is read as text, as :func:`meta_matches` reads it (a boolean is ``true`` / ``false``, anything
      else ``str()``).
    - An absent key, or one whose value is ``None``, matches nothing. The :data:`ABSENT_MEANS` defaults do
      NOT apply to a bound, unlike :func:`meta_matches`. A *meta* that is not a mapping matches nothing.
    - The text must fully match ``^-?[0-9]+$``: no spaces, no plus sign, no decimal point, no exponent.
      Anything else matches nothing. So the int ``3`` and the string ``"3"`` are integers; the floats
      ``3.0`` and ``2.9``, the strings ``"2.9"``, ``"abc"``, ``""``, ``" 3"``, ``"+3"``, and ``True`` are not.
    - An integer matches when its value is at least *n*.

    The same rule in SQL: ``metadata->>key ~ '^-?[0-9]+$' AND (metadata->>key)::numeric >= n``. One known
    difference: Python writes a float of 1e16 or more as ``"1e+16"``, which is not an integer here, while
    jsonb text holds it as a plain integer, so the engine may include a row this function excludes. That is
    a widening, and the client's final filter removes the row.
    """
    meta = meta if isinstance(meta, Mapping) else {}
    value = meta.get(key)
    if value is None:
        return False
    text = _text(value)
    return _INTEGER_TEXT.fullmatch(text) is not None and int(text) >= n


def documents_with_meta(entries: Iterable[Any], filters: Sequence[tuple[str, str]]) -> list[Any]:
    """The *entries* whose ``meta`` satisfies *filters*, in order."""
    return [e for e in entries if meta_matches(getattr(e, "meta", None), filters)]
