# SPDX-License-Identifier: AGPL-3.0-or-later
"""Maps the harness's ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` into a spawned
``claude`` child's ``CLAUDE_CODE_OAUTH_TOKEN`` (RDR-219 amendment, "The
nx-mcp dispatch grant", nexus-wauo1.35).

nx-mcp is always Claude's own child, and Claude Code deletes
``CLAUDE_CODE_OAUTH_TOKEN`` from its own ``process.env`` immediately after
reading it (RDR-219 "Transport rules"), so nothing nx-mcp starts --
including its own ``claude -p`` subprocesses -- inherits the operator's
credential by the ordinary environment-inheritance path. A harness that
needs those subprocesses to authenticate (an operator tool, the aspect
worker, a nested tool-granting dispatch) grants nx-mcp its own, separate
automation token under the unprotected name ``NX_HARNESS_CLAUDE_OAUTH_TOKEN``
(delivered by ``tests/e2e/lib/claude_mcp_grant.sh``'s piped MCP config).
This module is the one place that name is mapped into the protected one, and
only in a child's own environment -- never in ``os.environ``, so the harness
name never reaches a Bash-tool child, a nested MCP server, or any other
process nx-mcp did not build this env dict for.

Both launch sites in ``src/nexus`` that start a ``claude`` subprocess build
their child's environment through :func:`apply_harness_oauth_grant`:
``operators.dispatch._build_dispatch_env`` (every operator MCP tool, the
plan runner, ``nx_tidy``, ``nx_enrich_beads``, ``nx_plan_audit``) and
``aspect_extractor._run_claude_isolated``. The RDR-173 aspect worker daemon
(``daemon/aspect_worker_daemon.py``) needs no change of its own: it spawns
the worker process by ordinary inheritance from nx-mcp's own environment,
and the mapping happens at the leaf -- ``_run_claude_isolated`` -- not at
the daemon's spawn.
"""
from __future__ import annotations

from collections.abc import Mapping

import structlog

_log = structlog.get_logger(__name__)

#: The harness-side name. Never deleted by Claude Code (only the protected
#: name below is), so it survives in nx-mcp's own environment for as long
#: as a harness-granted session runs. Set by a harness launched through
#: ``tests/e2e/lib/claude_mcp_grant.sh``.
HARNESS_OAUTH_TOKEN_ENV_VAR = "NX_HARNESS_CLAUDE_OAUTH_TOKEN"

#: The protected name Claude Code deletes from its own environment after
#: reading it. A ``claude -p`` process nx-mcp starts never has this set
#: unless something maps the harness name into it first.
CLAUDE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: The grant is applied on every claude dispatch and aspect-extraction
#: retry, so it is logged once per process, not per call; nx-mcp's
#: startup warning already names the grant.
_grant_logged = False


def apply_harness_oauth_grant(base: Mapping[str, str]) -> dict[str, str]:
    """Return a NEW child-environment dict with the harness grant applied.

    Never mutates ``base`` or ``os.environ`` -- callers pass either as
    ``base`` and get an independent dict back. When
    ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` is present and non-empty in ``base``
    and ``CLAUDE_CODE_OAUTH_TOKEN`` is NOT already present there, the
    result carries ``CLAUDE_CODE_OAUTH_TOKEN`` set to the harness value; an
    existing ``CLAUDE_CODE_OAUTH_TOKEN`` always wins over the grant. The
    harness name itself is always kept in the result (never stripped, so a
    tool-granting dispatch whose nested nx-mcp dispatches again still has
    it to map). Neither value is ever logged -- at most one log line
    records, once per process, that a grant was applied, naming neither.
    """
    result = dict(base)
    harness_value = result.get(HARNESS_OAUTH_TOKEN_ENV_VAR)
    if harness_value and CLAUDE_OAUTH_TOKEN_ENV_VAR not in result:
        result[CLAUDE_OAUTH_TOKEN_ENV_VAR] = harness_value
        # WARNING, not INFO: applying the grant is rare (harness-only) and
        # security-relevant, so it must survive this project's default
        # WARNING-level quiet test/production log threshold rather than
        # being filtered out before any processor -- including a
        # ``capture_logs()`` test -- ever sees it.
        global _grant_logged
        if not _grant_logged:
            _grant_logged = True
            _log.warning("claude_child_env_harness_oauth_grant_applied")
    return result
