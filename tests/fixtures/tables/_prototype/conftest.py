"""Archived reference copy (RDR-201 P1.1, nexus-j9z30.1 STEP ZERO) — excluded
from pytest collection.

This tree is a read-only snapshot of the enumcheck research prototype
(``checker.py``, ``tests/test_checker.py``, ``models/*.toml``), kept
verbatim for porting fidelity, not executed as part of the production
suite. Its own ``tests/test_checker.py`` exercises the same 17 scenarios
against the prototype's pre-RDR-201 schema (``outcome`` / ``guard_all`` /
``ModelError``) — collecting it here would duplicate
``tests/tables/test_check.py`` under a different, frozen API. Do not edit
the sibling files; this conftest is new infrastructure, not a rewrite of
the ported artifacts.
"""

collect_ignore = ["tests"]
