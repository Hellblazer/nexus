#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared "bridge structlog to stderr/logfile before importing nexus" helper
for hook scripts that import the ``nexus`` package (nexus-cnzei.2 fix round
2, critic Significant: the instance-level fix in ``rdr_hook.py`` and
``routing/phase_review_close_requires_gate.py`` was hand-duplicated in each
file with no shared home, no lint, and no repo-wide guard, so a THIRD hook
script that imports ``nexus.*`` tomorrow would reintroduce the exact defect
this closes with nothing to catch it).

THE DEFECT THIS PREVENTS: structlog's DEFAULT ``PrintLoggerFactory`` writes
to STDOUT (not stderr; see :mod:`nexus.logging_setup`'s own docstring for
the corrected history). A hook script that imports any ``nexus.*`` module
before bridging structlog to stdlib logging lets every
``structlog.get_logger(...).info/warning(...)`` call along that import path
land on the SAME stdout stream the hook's own output goes out on, which is
exactly the channel that reaches the model's context (or, for a PreToolUse
hook, the channel the harness parses as JSON). Confirmed by hand
(nexus-cnzei.2 item 2): a pre-fix copy of ``rdr_hook.py``, run as a real
subprocess, printed ``[debug ] catalog_reader_service_mode``,
``[warning ] rdr_hook_collection_resolution_failed``, and a full
Rich-rendered traceback to stdout.

THE FIX: call :func:`nexus.logging_setup.configure_logging` with
``mode="hook"`` BEFORE any other ``nexus.*`` import in the same script.
That bridges structlog through stdlib logging to stderr plus
``<config>/logs/hook.log`` instead of stdout print.

USAGE: a hook script that imports ``nexus.*`` calls
:func:`configure_hook_logging` as the FIRST statement of whichever function
first imports ``nexus.*`` (or at module scope, if the nexus import is
itself at module scope), before that import executes. See ``rdr_hook.py``
and ``routing/phase_review_close_requires_gate.py`` for the two shipped
call sites; ``tests/hooks/test_hook_scripts_configure_logging_before_nexus_import.py``
enumerates every ``conexus/hooks/scripts/**/*.py`` file with a real
``import nexus`` / ``from nexus import ...`` statement (AST-detected, not a
substring grep) and asserts this module is imported and called before the
first such import in that file.

Import pattern for a script directly under ``conexus/hooks/scripts/``
(mirrors the sibling ``_endpoint_resolve.py``)::

    sys.path.insert(0, os.path.dirname(__file__))
    import _hook_logging  # noqa: E402

For a script one directory deeper (e.g. under ``routing/``), insert the
PARENT of that directory instead (mirrors ``routing/_lib.py``'s own
``_endpoint_resolve`` import)::

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import _hook_logging  # noqa: E402

Best-effort by design, matching every hook script's own established
posture toward its nexus imports: an interpreter with neither ``nexus`` nor
``structlog`` installed is a real, observed case (nexus-4ti7e), and a
logging-setup failure must never turn into a hook failure. There is
nothing useful to report if the log-plumbing call itself can't run.
"""
from __future__ import annotations


def configure_hook_logging() -> None:
    """Bridge structlog to stderr/logfile via
    ``nexus.logging_setup.configure_logging(mode="hook")``, swallowing any
    failure (missing ``nexus``, missing ``structlog``, or a genuine bug in
    the logging setup itself). Call this BEFORE the first ``nexus.*``
    import in the calling script.
    """
    try:
        from nexus.logging_setup import configure_logging  # noqa: PLC0415 -- deferred: only needed once, right before the nexus imports it protects

        configure_logging(mode="hook")
    except Exception:  # noqa: BLE001 -- best-effort; must never break the calling hook
        pass
