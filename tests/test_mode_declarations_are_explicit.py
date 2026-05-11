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
