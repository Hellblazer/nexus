# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-ru672: the indexer's git_meta plumbing is gone.

RDR-236 Phase 0 removed the repo-PDF ``git_*`` writer, which left
``_git_metadata(repo)`` running git subprocesses on every index run for a
value no reader consumed. The owner's ``head_hash`` (RDR-101 5c) is a
separate path (``_current_head``) and stays. The subprocess-count pin is in
``tests/test_index_since_head.py::test_since_head_without_base_logs_fallback``
(it needs a tenant, and a new one would raise the tenant count).
"""
from __future__ import annotations

import dataclasses

from nexus import index_context
from nexus import indexer as idx


def test_index_context_has_no_git_meta_field():
    names = {f.name for f in dataclasses.fields(index_context.IndexContext)}
    assert "git_meta" not in names


def test_git_metadata_function_is_gone():
    assert not hasattr(idx, "_git_metadata")
