# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-182 P2.2: pre-emission read-only lint over diagnostic SQL.

Any SQL a DIAGNOSTIC (forensics) playbook carries is classified BEFORE
emission; a statement passes only when it provably matches a read-only,
metadata-scoped shape. Fail-closed ALLOWLIST semantics: unrecognized shapes
are violations, not passes. This is deliberately stronger than the test
suite's ``_DML_TARGET_RE`` leading-keyword pattern (which the plan audit
flagged as insufficient): mutating keywords are denied ANYWHERE in the
statement — catching data-modifying CTEs (``WITH x AS (DELETE ...)``),
``DO`` blocks, ``SELECT ... INTO``, and locking reads (``FOR UPDATE``) —
and store-table references are restricted to aggregate-only select lists so
diagnostics can COUNT rows but never read row/document/note CONTENT.

Lints the SQL the PRODUCT emits (our own statements, not arbitrary user
input) — a false positive here means we simplify our diagnostic, never that
we weaken the lint.
"""
from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "DiagnosticSqlViolation",
    "assert_read_only_diagnostics",
    "is_read_only_diagnostic",
]


class DiagnosticSqlViolation(ValueError):
    """A diagnostic playbook tried to emit non-read-only / content SQL."""


#: Keywords that mutate data, schema, grants, or session state — denied as
#: standalone words ANYWHERE in the statement (word-boundary match, so column
#: names like ``updated_at`` / ``deleted_at`` do not trip it).
_MUTATING_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|MERGE|"
    r"CALL|DO|COPY|EXECUTE|LOCK|VACUUM|REINDEX|CLUSTER|COMMENT|REFRESH|"
    r"PREPARE|DEALLOCATE|LISTEN|NOTIFY|SET|RESET|DISCARD)\b",
    re.IGNORECASE,
)

#: ``SELECT ... INTO`` creates a table; ``FOR UPDATE/SHARE`` takes row locks.
_SELECT_INTO = re.compile(r"\bINTO\b", re.IGNORECASE)
_ROW_LOCK = re.compile(r"\bFOR\s+(UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b", re.IGNORECASE)

#: A statement must START as a plain SELECT or a WITH...SELECT chain.
_STARTS_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

#: Store-table references (row/document/note CONTENT lives here). Matches the
#: same nexus/t1 schema scope the changelog lint uses.
_STORE_TABLE_RE = re.compile(r"\b(nexus|t1)\.(\w+)", re.IGNORECASE)

#: FROM targets that are PROVABLY metadata/catalog (content-free by nature):
#: the pg_* system catalogs (qualified or bare), information_schema, and the
#: Liquibase journal. Anything NOT matching this AND not a same-statement CTE
#: is treated as a potential store table and must be aggregate-only —
#: fail-closed on "cannot prove it is metadata" (critic-final M1: an
#: UNQUALIFIED ``SELECT content FROM chunks_768`` must not slip past just
#: because it lacks a ``nexus.`` prefix).
_METADATA_TARGET_RE = re.compile(
    r"^(?:pg_catalog\.pg_\w+|pg_\w+|information_schema\.\w+"
    r"|(?:public\.)?databasechangelog(?:lock)?)$",
    re.IGNORECASE,
)
#: CTE names defined in the same statement (``WITH x AS (...)``, ``, y AS``).
_CTE_NAME_RE = re.compile(r"(?:\bWITH\b|,)\s+(\w+)\s+AS\s*\(", re.IGNORECASE)

#: Aggregate-only select list: every top-level select expression must be an
#: aggregate call. Conservative: COUNT/MIN/MAX/SUM/AVG (optionally nested
#: expressions inside), nothing else.
_AGGREGATE_ITEM = re.compile(r"^\s*(COUNT|MIN|MAX|SUM|AVG)\s*\(", re.IGNORECASE)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _strip_comments(stmt: str) -> str:
    return _COMMENT_RE.sub(" ", stmt)


def _select_segments(stmt: str) -> list[tuple[str, str]]:
    """Every ``SELECT <list> FROM <target>`` pair in the statement.

    Regex-level extraction (no SQL parser by design — stdlib only): good
    enough because this lints OUR OWN emitted statements, and any shape this
    helper cannot see cleanly fails the aggregate check → fail-closed.
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(
        r"\bSELECT\b(.*?)\bFROM\b\s+([\w.\"]+)", stmt,
        re.IGNORECASE | re.DOTALL,
    ):
        out.append((m.group(1), m.group(2)))
    return out


def is_read_only_diagnostic(stmt: str) -> tuple[bool, str]:
    """Classify one statement. Returns ``(ok, reason)``; reason set when not ok."""
    text = _strip_comments(stmt).strip()
    if not text:
        return False, "empty statement"
    if not _STARTS_READ_ONLY.match(text):
        return False, "statement does not start with SELECT/WITH (allowlist is read-only queries)"
    if (m := _MUTATING_KEYWORDS.search(text)) is not None:
        return False, f"mutating/session keyword {m.group(1).upper()!r} present"
    if _SELECT_INTO.search(text):
        return False, "SELECT ... INTO creates a table"
    if _ROW_LOCK.search(text):
        return False, "locking read (FOR UPDATE/SHARE) mutates lock state"

    # Content protection, FAIL-CLOSED (critic-final M1): a SELECT's FROM target
    # must have an aggregate-only select list UNLESS the target is PROVABLY
    # content-free — a same-statement CTE name (its body is checked as its own
    # segment) or a metadata/catalog object (pg_*, information_schema, the
    # Liquibase journal). Everything else — a nexus.*/t1.* store table OR an
    # UNQUALIFIED bare table name we cannot prove is metadata — must be
    # aggregate-only. Diagnostics may COUNT rows, never project content, and
    # an author who omits the schema prefix no longer slips content past.
    cte_names = {m.lower() for m in _CTE_NAME_RE.findall(text)}
    for select_list, target in _select_segments(text):
        clean = target.strip().strip('"')
        if clean.lower() in cte_names:
            continue
        if _METADATA_TARGET_RE.match(clean):
            continue
        for item in (i.strip() for i in select_list.split(",")):
            if not _AGGREGATE_ITEM.match(item):
                return False, (
                    f"table {target} referenced with a non-aggregate select "
                    f"item {item!r} — diagnostics may count rows, never read "
                    "content (unqualified/unknown targets are fail-closed)"
                )
    return True, ""


def assert_read_only_diagnostics(statements: Iterable[str]) -> None:
    """Raise :class:`DiagnosticSqlViolation` on the first non-conforming
    statement (fail-closed, names the offending SQL and the reason)."""
    for stmt in statements:
        ok, reason = is_read_only_diagnostic(stmt)
        if not ok:
            raise DiagnosticSqlViolation(
                f"diagnostic SQL failed the read-only lint ({reason}): {stmt}"
            )
