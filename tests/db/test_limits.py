# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rn3wo.2: pgvector-neutral limits module — value-parity regression.

RDR-155 P4b Phase 0 prerequisite: ``nexus.db.limits`` is the rehomed home for
the generic size/batch/concurrency ceilings that 22 non-Chroma-coupled
callers reuse from ``nexus.db.chroma_quotas`` (paging ceiling, chunk-size
cap, etc.) for the live PG-serving path. It must export ``QUOTAS``,
``SAFE_CHUNK_BYTES``, and ``MAX_QUERY_RESULTS`` with values IDENTICAL to
``chroma_quotas`` at the moment of rehoming, and it must NOT import from
``chroma_quotas`` — the whole point of rehoming is to survive
``chroma_quotas.py``'s eventual deletion (nexus-g37fr).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus.db import chroma_quotas, limits


def test_quotas_values_identical_to_chroma_quotas() -> None:
    for field in chroma_quotas.QUOTAS.__dataclass_fields__:
        assert getattr(limits.QUOTAS, field) == getattr(chroma_quotas.QUOTAS, field), (
            f"limits.QUOTAS.{field} drifted from chroma_quotas.QUOTAS.{field}"
        )


def test_safe_chunk_bytes_identical() -> None:
    assert limits.SAFE_CHUNK_BYTES == chroma_quotas.SAFE_CHUNK_BYTES == 12_288


def test_max_query_results_identical() -> None:
    assert limits.MAX_QUERY_RESULTS == chroma_quotas.QUOTAS.MAX_QUERY_RESULTS == 300


def test_quotas_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        limits.QUOTAS.MAX_RECORDS_PER_WRITE = 999  # type: ignore[misc]


def test_limits_module_does_not_import_chroma_quotas() -> None:
    # The rehoming's entire point is independence from chroma_quotas.py so
    # that nexus-g37fr can delete it without breaking this module.
    src = Path(limits.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "chroma_quotas" in node.module:
            raise AssertionError(
                "nexus.db.limits must not import from chroma_quotas — "
                "it needs to survive that module's Phase-4b deletion"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "chroma_quotas" not in alias.name
