# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx doctor`` block of the ``nx command-context nx-preflight`` preamble.

Split out of :mod:`nexus.commands.command_context` (over its size cap): doctor is the one preamble call that is slow when
healthy (GH #1689), so it has its own bound and, before it runs, a liveness probe of the local engine.
"""
from __future__ import annotations

import subprocess

from nexus.bounded_subprocess import run_bounded
from nexus.util.nx_argv import nx_argv

# ``nx doctor`` is the one preamble call that is slow when healthy: 44.9 s for 105 steps on a Windows box (GH #1689),
# so the short bound above made check 2 report "unavailable" on every healthy install.  It still has a bound, for the
# wedged-daemon case the comment above describes; raise this when doctor grows.
_DOCTOR_TIMEOUT: int = 120

# The preamble runs synchronously when the slash command starts, so a wedged engine must not stall it for the whole
# doctor bound: a liveness probe of the engine's /health runs first, under this short bound, and a local engine that
# does not answer is reported at once.
_ENGINE_PROBE_TIMEOUT: float = 5.0


def _engine_not_answering() -> str | None:
    """Why the local engine is not answering ``/health`` within ``_ENGINE_PROBE_TIMEOUT``, or ``None``.

    ``None`` also when no local engine is discoverable (a managed endpoint, or no service configured: ``nx doctor`` is the
    judge of those) and when the probe itself cannot be made: only a confirmed address that gives no answer returns a
    reason.  Any HTTP answer, a 503 included, counts as answering; the engine is up and doctor says what is wrong.  The
    endpoint and the probe are the ones ``nx doctor`` and ``nx daemon service status`` use, not copies.
    """
    try:
        from nexus import config as _config  # noqa: PLC0415 — deferred: only the doctor block pays for them
        from nexus.commands.daemon import _probe_health  # noqa: PLC0415, PLC2701 — the status verb's own probe
        from nexus.health import _resolve_service_endpoint  # noqa: PLC0415, PLC2701 — one rule for "where is the engine"

        endpoint = _resolve_service_endpoint(_config.nexus_config_dir())
        if endpoint is None:
            return None
        host, port = endpoint
        answer = _probe_health(host, port, timeout=_ENGINE_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 — a probe that cannot be made never costs the preamble
        return None
    if answer == "unreachable":
        return f"the engine at {host}:{port} did not answer /health within {_ENGINE_PROBE_TIMEOUT:g}s"
    return None


def nx_doctor_block() -> list[str]:
    """Return the nx-doctor status block.

    First asks the local engine's ``/health`` under a short bound (a wedged engine is reported at once, not after the
    doctor bound).  Then runs ``nx doctor`` (bounded by ``_DOCTOR_TIMEOUT``, not the short tool bound) and reports PASS/FAIL/SKIP based on
    exit code.  SKIP when nx is not installed or doctor outlasts its bound (said so, not called unavailable).

    Returns:
        List of lines for the ### 2. nx configuration section.
    """
    heading = "### 2. nx configuration (nx doctor)"
    lines: list[str] = [heading, ""]
    not_answering = _engine_not_answering()
    if not_answering is not None:
        lines.append(f"Status: SKIP - engine not answering: {not_answering}; run 'nx doctor' directly for details")
        lines.append("")
        return lines
    try:
        proc = run_bounded(
            nx_argv("doctor"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=_DOCTOR_TIMEOUT,
        )
        lines += proc.stdout.splitlines()
        lines.append("")
        if proc.returncode == 0:
            lines.append("Status: PASS")
        else:
            lines.append("Status: FAIL - run 'nx doctor' for details")
    except FileNotFoundError:
        lines.append("Status: SKIP - nx not installed")
    except subprocess.TimeoutExpired:
        # A wedged daemon or a slow box: not a statement about the install's health (GH #1689).
        lines.append(f"Status: SKIP - nx doctor exceeded {_DOCTOR_TIMEOUT}s; not a failure; run 'nx doctor' directly")
    except Exception as exc:  # noqa: BLE001 — preamble block builder: degrade to placeholder text rather than crash CLI
        lines.append(f"Status: SKIP - nx doctor could not run ({type(exc).__name__})")
    lines.append("")
    return lines
