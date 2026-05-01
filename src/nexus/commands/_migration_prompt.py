# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-101 Phase 3 follow-up D (nexus-o6aa.9.9): TTY-gated upgrade prompt.

When ``Catalog._ensure_consistent`` decides to fall back to legacy reads
because ``events.jsonl`` is non-empty but sparse vs ``documents.jsonl``
(``Catalog.bootstrap_fallback_active = True`` after the call), this
module surfaces an operator-visible warning to stderr — once per
process, only when stderr is a TTY, and only when ``NEXUS_NO_PROMPTS``
is not set.

The hook lives here (and is wired into ``cli.main``) rather than in
``Catalog.__init__`` because:

  * The MCP server, library consumers, and tests all construct
    ``Catalog`` instances; emitting from ``__init__`` would fire in
    every context. The CLI group's callback fires once per invocation.
  * The ``stderr.isatty()`` gate is meaningful only at the CLI surface.
    Library callers may have replaced stderr; MCP runs without a TTY.

structlog already logs the underlying warning every time the guardrail
fires. The doctor verb's ``--json`` output also surfaces the
``bootstrap_fallback`` key for machine consumers. This module is the
human-visible nudge layer that complements both, not a replacement.
"""
from __future__ import annotations

import os
import sys

import click

# Module-scoped sentinel — fire the prompt at most once per process.
# Multiple Catalog constructions in one CLI run (e.g. doctor's
# pre-check + the actual check) must not double-emit. The flag is
# never reset; it dies with the process.
_PROMPTED: bool = False

# Env var name documented at the top of `docs/migration/rdr-101.md`.
# When set to a truthy value, suppresses the prompt unconditionally.
# Same semantics as pip's --quiet or npm's NPM_CONFIG_LOGLEVEL.
_NO_PROMPTS_ENV = "NEXUS_NO_PROMPTS"


def maybe_emit_bootstrap_prompt() -> None:
    """Emit the bootstrap-fallback prompt to stderr if all gates clear:

    1. ``_PROMPTED`` is False (haven't already fired in this process).
    2. ``stderr`` is a TTY (operator at a real terminal).
    3. ``NEXUS_NO_PROMPTS`` is not set to a truthy value.
    4. The catalog is currently in bootstrap-fallback mode.

    Best-effort — any exception in the gate logic suppresses the
    prompt rather than disrupting the CLI invocation. The prompt is
    advisory; the underlying state is already surfaced via structlog
    and ``nx catalog doctor``.
    """
    global _PROMPTED
    if _PROMPTED:
        return

    # Gate 2: TTY check.
    try:
        if not sys.stderr.isatty():
            return
    except (AttributeError, ValueError):
        return

    # Gate 3: NEXUS_NO_PROMPTS escape hatch.
    raw = os.environ.get(_NO_PROMPTS_ENV, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return

    # Gate 4: actual catalog state. Do this last because it touches
    # the filesystem; the cheap gates run first.
    try:
        from nexus.commands.catalog import _check_bootstrap_status
        status = _check_bootstrap_status()
    except Exception:
        return

    if not status.get("fallback_active"):
        return

    _PROMPTED = True
    click.echo(
        "WARNING: catalog bootstrap-fallback active.\n"
        "  events.jsonl is sparse vs documents.jsonl; ES writes are\n"
        "  landing in the log but reads come from legacy JSONL. Run\n"
        "  `nx catalog migrate` to rebuild events.jsonl from the\n"
        "  legacy state and align T3 chunks. See docs/migration/rdr-101.md.\n"
        "  Suppress this prompt with NEXUS_NO_PROMPTS=1.",
        err=True,
    )
