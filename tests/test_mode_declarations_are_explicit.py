# SPDX-License-Identifier: AGPL-3.0-only
"""RDR-109 Phase 1 lint: tests that reference cloud-mode embedder names
must opt in to the ``cloud_mode`` fixture (or be in the exclusion list).

The default test mode is local (no API keys, ONNX MiniLM EF). Without
this guard, a test that asserts ``embedding_model == "voyage-context-3"``
would silently pass in CI iff some prior PR happened to leak cloud-mode
state into the session, and fail otherwise. The lint forces the choice
to be explicit.

Implementation: grep + ``request.fixturenames`` introspection. AST-shape
analysis is out of scope (RDR-109 §Phase 1, step 4).
"""
from __future__ import annotations

import inspect
import re

import pytest

from tests.conftest import (
    _MODE_LINT_EXCLUDE_FILES,
    _MODE_LINT_EXCLUDE_NODEIDS,
)

VOYAGE_RE = re.compile(r"voyage-(context|code)-3")


def test_mode_declarations_are_explicit(request: pytest.FixtureRequest) -> None:
    offenders: list[str] = []
    for item in request.session.items:
        func = getattr(item, "function", None)
        if func is None:
            continue
        try:
            src = inspect.getsource(func)
        except (OSError, TypeError):
            continue
        if not VOYAGE_RE.search(src):
            continue
        # File-level exclusion (every test in the file is exempt).
        # ``item.nodeid`` is e.g. ``tests/test_x.py::test_func[param]``.
        nodeid = item.nodeid
        file_part = nodeid.split("::", 1)[0]
        file_basename = file_part.rsplit("/", 1)[-1]
        if file_basename in _MODE_LINT_EXCLUDE_FILES:
            continue
        # Per-test exclusion (strip parametrize suffix).
        base_nodeid = nodeid.split("[", 1)[0]
        if base_nodeid in _MODE_LINT_EXCLUDE_NODEIDS:
            continue
        fixturenames = set(getattr(item, "fixturenames", ()))
        if "cloud_mode" in fixturenames:
            continue
        offenders.append(nodeid)

    if offenders:
        sample = "\n  ".join(offenders[:20])
        suffix = (
            f"\n  ... (+{len(offenders) - 20} more)"
            if len(offenders) > 20
            else ""
        )
        pytest.fail(
            "RDR-109 Phase 1: the following tests reference voyage-"
            "(context|code)-3 but do not opt in to the `cloud_mode` "
            "fixture and are not listed in `_MODE_LINT_EXCLUDE`:\n  "
            + sample
            + suffix
            + "\n\nFix: add `cloud_mode` to the test's fixture list "
            "(or `pytestmark = pytest.mark.usefixtures(\"cloud_mode\")` "
            "at module/class scope) if the test asserts cloud-mode "
            "behavior; or add the nodeid to `_MODE_LINT_EXCLUDE` in "
            "tests/conftest.py with a documented reason."
        )


# RDR-109 / nexus-vgq89 ratchet: these two exclusion sets may only ever
# SHRINK. A PR that grows either one is silently re-introducing the
# "Phase 1 ships excluded, subsequent PRs promote each" grandfathering
# this bead burned down (2026-07-15) -- every entry above this point
# already carries an individually documented reason; a bare growth of
# the count with no accompanying rationale comment is exactly the
# regression these two assertions exist to catch. To legitimately grow
# either number: add the new exclusion with its own documented
# rationale comment (matching the style used throughout
# `_MODE_LINT_EXCLUDE_FILES` / `_MODE_LINT_EXCLUDE_NODEIDS` above), then
# consciously bump the corresponding constant below in the same diff.
_MODE_LINT_EXCLUDE_FILES_CEILING = 72
# 43 -> 46 (6.10.1): +3 real keyed integration tests in test_integration.py
# — cloud_mode's fake credentials broke them against the live Voyage API
# (their mode declaration is the requires-key gating; see conftest entry).
# 46 -> 55 (RDR-185 P4 harvest): +9 ladder tests, reason
# "string-literal-as-name" — each builds a conformant RDR-103 collection name
# (or a classification carrying the name's model SEGMENT) and asserts on
# planning / rollback / re-id behaviour keyed off that segment. None calls a
# Voyage embedder: the rung tests inject every collaborator and the local
# bge-768 path is what runs, so cloud_mode changes nothing they assert. The
# mislabel pair (nexus-j5diu) is the sharpest case FOR exclusion rather than
# promotion: their subject is a name whose voyage token LIES, so opting them
# into cloud_mode would assert the opposite of their point.
# SIX of the nine (test_rollback_via_map, test_substrate_leg) predate P4 and
# had this lint red on develop since P2 — the arc ran narrow, path-scoped
# selections, and this lint only fires when the whole session is collected, so
# `pytest tests/upgrade/` never sees it. Rationale per entry in conftest.py.
# Unchanged by nexus-6or3m / nexus-mq42b / nexus-k1m2f (RDR-185 P5): the new
# credential-gate and billed-consent pins name voyage tokens only through
# module-level fixtures (`_GATED`, `_billed_leg`), the pattern this file's own
# `_cls` helper has always used — so the set did not need to grow. Preferred to
# an exclusion: the tests read better without duplicated magic strings, and an
# exclusion the lint does not need is dead weight the ratchet then guards.
_MODE_LINT_EXCLUDE_NODEIDS_CEILING = 55


def test_mode_lint_exclude_files_ratchet() -> None:
    assert len(_MODE_LINT_EXCLUDE_FILES) == _MODE_LINT_EXCLUDE_FILES_CEILING, (
        f"_MODE_LINT_EXCLUDE_FILES has {len(_MODE_LINT_EXCLUDE_FILES)} "
        f"entries, expected exactly {_MODE_LINT_EXCLUDE_FILES_CEILING}. "
        "This set may only shrink (promote a file's tests to `cloud_mode` "
        "or per-test `_MODE_LINT_EXCLUDE_NODEIDS` entries) or grow with a "
        "documented per-entry rationale plus a conscious bump of "
        "`_MODE_LINT_EXCLUDE_FILES_CEILING` in this file."
    )


def test_mode_lint_exclude_nodeids_ratchet() -> None:
    assert len(_MODE_LINT_EXCLUDE_NODEIDS) == _MODE_LINT_EXCLUDE_NODEIDS_CEILING, (
        f"_MODE_LINT_EXCLUDE_NODEIDS has {len(_MODE_LINT_EXCLUDE_NODEIDS)} "
        f"entries, expected exactly {_MODE_LINT_EXCLUDE_NODEIDS_CEILING}. "
        "This set may only shrink (promote a test to `cloud_mode`) or grow "
        "with a documented per-entry rationale plus a conscious bump of "
        "`_MODE_LINT_EXCLUDE_NODEIDS_CEILING` in this file."
    )
