# SPDX-License-Identifier: AGPL-3.0-or-later
"""What ``nx backup`` says it leaves out cannot drift from what the engine leaves out (nexus-csayy.3, RDR-248).

The engine's registry (``TenantExport.java``) classes some tables ``excluded``: operational state a backup does not
carry. The client prints a sentence about them built from ``EXCLUDED_TABLE_PHRASES``. This test parses the engine's
``excluded(...)`` entries and fails when an excluded table has no phrase (a new exclusion nobody told the user
about) and when the phrase table names a table that is no longer excluded (a stale claim).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.backup.cloud_export import EXCLUDED_TABLE_PHRASES, OMITTED_NOTICE

pytestmark = pytest.mark.lint

REGISTRY = Path(__file__).resolve().parent.parent / "service/src/main/java/dev/nexus/service/db/TenantExport.java"
_QUOTED = re.compile(r'^"([a-z0-9_]+)"$')


def _call_args(text: str, start: int) -> list[str]:
    """The top-level arguments of the call whose '(' is just before *start*."""
    depth, args, cur, quote = 1, [], "", False
    for ch in text[start:]:
        if quote:
            cur += ch
            quote = ch != '"'
            continue
        if ch == '"':
            quote = True
            cur += ch
        elif ch in "([":
            depth += 1
            cur += ch
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                args.append(cur.strip())
                return args
            cur += ch
        elif ch == "," and depth == 1:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    raise AssertionError("unbalanced excluded( call")


def excluded_tables(java: str) -> set[str]:
    """Table names of every ``excluded(table, reason)`` and ``excluded(schema, table, reason)`` registry entry
    (``schema.table`` when the schema is not ``nexus``). The two helper bodies, whose arguments are not literals,
    are skipped."""
    names: set[str] = set()
    for m in re.finditer(r"\bexcluded\(", java):
        args = _call_args(java, m.end())
        first = _QUOTED.match(args[0])
        if first is None:
            continue
        if len(args) == 2:
            names.add(first.group(1))
        elif len(args) == 3 and (second := _QUOTED.match(args[1])):
            names.add(f"{first.group(1)}.{second.group(1)}")
    return names


def test_the_parser_reads_both_call_shapes_and_skips_the_helpers() -> None:
    sample = '''
    private static Entry excluded(String table, String reason) { return excluded("nexus", table, reason); }
    private static Entry excluded(String schema, String table, String reason) { return null; }
    excluded("pdf_pipeline", IN_FLIGHT).pk("tenant_id text", "pipeline_id bigint")
    excluded("t1", "scratch", "session scratch: it lives for one session").pk("id text")
    '''
    assert excluded_tables(sample) == {"pdf_pipeline", "t1.scratch"}


def test_every_engine_excluded_table_has_a_phrase_and_no_phrase_is_stale() -> None:
    engine = excluded_tables(REGISTRY.read_text(encoding="utf-8"))
    assert len(engine) >= 20, f"the parse found only {len(engine)} excluded tables: {sorted(engine)}"
    assert {"tuples", "t1.scratch", "request_ledger", "pdf_pipeline", "reembed_jobs"} <= engine
    missing = sorted(engine - set(EXCLUDED_TABLE_PHRASES))
    stale = sorted(set(EXCLUDED_TABLE_PHRASES) - engine)
    assert not missing, f"excluded by the engine, not in EXCLUDED_TABLE_PHRASES (add a phrase): {missing}"
    assert not stale, f"in EXCLUDED_TABLE_PHRASES, no longer excluded by the engine (remove it): {stale}"


def test_the_notice_says_every_phrase() -> None:
    for phrase in set(EXCLUDED_TABLE_PHRASES.values()):
        assert phrase in OMITTED_NOTICE
