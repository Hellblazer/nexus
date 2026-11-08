# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Version 1 has read verbs only (RDR-242 Technical Design 10; nexus-mdbds.7).

A write verb is a separate entry in the verb table, granted only when declared
and covered by an approval (Technical Design 1); none ships before that
document. This census is the tripwire: a verb name outside the read-only set
fails here until someone decides it on purpose.
"""

from __future__ import annotations

from nexus.lua_runtime.verbs import VERB_TABLE

#: RDR-242 Technical Design 10, the whole of version 1.
READ_ONLY_VERBS_V1 = frozenset(
    {
        "memory.get",
        "memory.list",
        "memory.search",
        "transcripts.sessions",
        "git.diff_names",
        "git.diff_stat",
        "tables.get",
    }
)


def test_no_write_verb_is_registered() -> None:
    extra = set(VERB_TABLE) - READ_ONLY_VERBS_V1
    assert not extra, f"verbs outside the read-only version-1 set: {sorted(extra)}"


def test_this_steps_verbs_are_registered() -> None:
    # non-vacuity: the census above examined the memory and tables verbs
    assert {"memory.get", "memory.list", "memory.search", "tables.get"} <= set(
        VERB_TABLE
    )
