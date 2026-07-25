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

from nexus.db import limits


# RDR-155 P4b P3: three tests here mirrored these values against
# chroma_quotas.QUOTAS to catch drift during the rehome window. That module is
# DELETED, so there is nothing left to drift against — these values are now
# limits' OWN contract. Pinned as literals so a silent edit still fails, which
# is what the parity tests were really buying.


def test_safe_chunk_bytes_is_pinned() -> None:
    assert limits.SAFE_CHUNK_BYTES == limits.QUOTAS.SAFE_CHUNK_BYTES == 12_288


def test_max_query_results_is_pinned() -> None:
    assert limits.MAX_QUERY_RESULTS == limits.QUOTAS.MAX_QUERY_RESULTS == 300


def test_quotas_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        limits.QUOTAS.MAX_RECORDS_PER_WRITE = 999  # type: ignore[misc]


def test_limits_module_does_not_import_chroma_quotas() -> None:
    # Kept after chroma_quotas.py's deletion: an AST ban on the module name is
    # what stops it being resurrected as an import target, and it is cheap.
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
