# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: client readers of Liquibase's bookkeeping tables name the schema.

The engine pins ``databasechangelog`` / ``databasechangeloglock`` to ``public``
(``SchemaMigrator.migrate``). The client's psql readers (``nx doctor``'s schema
check, the stuck-lock release) run as the admin role with its default
``search_path``, so an unqualified name resolves to whichever schema the role is
named for: on an install bitten by the original bug that is a stray, empty
``<role>.databasechangelog``, and the doctor reports "0 rows -- migrations never
ran" against a healthy database. Qualify ``public.``, always.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"
_UNQUALIFIED = re.compile(r"\b(?:FROM|UPDATE|JOIN|INTO)\s+databasechangelog(?:lock)?\b")


def test_no_sql_in_src_reads_the_history_tables_unqualified() -> None:
    offenders = []
    for path in _SRC.rglob("*.py"):
        text = path.read_text(errors="replace")
        for m in _UNQUALIFIED.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(_SRC.parent.parent)}:{line}")
    assert not offenders, (
        "unqualified databasechangelog / databasechangeloglock in SQL (write "
        "public.databasechangelog): " + ", ".join(offenders)
    )


def test_the_lint_is_not_vacuous() -> None:
    assert _UNQUALIFIED.search("SELECT COUNT(*) FROM databasechangelog;")
    assert _UNQUALIFIED.search("UPDATE databasechangeloglock SET locked=false")
    assert not _UNQUALIFIED.search("SELECT 1 FROM public.databasechangelog;")
