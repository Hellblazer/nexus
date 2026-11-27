# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx doctor`` block of the ``nx command-context nx-preflight`` preamble.

Split out of :mod:`nexus.commands.command_context` (over its size cap): doctor is the one preamble call that is slow when
healthy (GH #1689), so it has its own bound and, before it runs, a liveness probe of the local engine.
"""
from __future__ import annotations

import socket
import subprocess

from nexus.bounded_subprocess import run_bounded
from nexus.util.nx_argv import nx_argv

# ``nx doctor`` is the one preamble call that is slow when healthy: 44.9 s for 105 steps on a Windows box (GH #1689), so
# the 15 s bound every other preamble tool call has made check 2 report "unavailable" on every healthy install.  It still
# has a bound, for the wedged-engine case; raise this when doctor grows.
_DOCTOR_TIMEOUT: int = 120

# The preamble runs synchronously when the slash command starts, so a wedged engine must not stall it for the whole doctor
# bound.  Before doctor runs, the local engine is asked for ``/health`` under this short bound, and only a listener that
# accepts the connection and sends nothing back skips doctor.  Read at call time, so a test can shorten it.
_ENGINE_PROBE_TIMEOUT: float = 5.0


def _engine_wedged() -> str | None:
    """Why doctor should not run behind the local engine, or ``None``: the engine accepted a connection and did not answer
    ``/health`` within ``_ENGINE_PROBE_TIMEOUT``.

    Every other outcome is ``None`` and doctor runs: no local engine discoverable (a managed endpoint, or no service
    configured), a refused connection (the engine is down, which doctor reports fast and with the fix steps and its other
    checks), any HTTP answer at all (a 500, a 404 or a proxy's 502 included; the engine is up and doctor says what is
    wrong), and a probe that cannot be made.  Only the raw reply is waited for; no status is judged.  The address is the
    one ``nx doctor`` itself resolves.
    """
    try:
        from nexus import config as _config  # noqa: PLC0415 — deferred: only the doctor block pays for it
        from nexus.health import _resolve_service_endpoint  # noqa: PLC0415, PLC2701 — one rule for "where is the engine"

        endpoint = _resolve_service_endpoint(_config.nexus_config_dir())
    except Exception:  # noqa: BLE001 — a probe that cannot be made never costs the preamble
        return None
    if endpoint is None:
        return None
    host, port = endpoint
    try:
        conn = socket.create_connection((host, port), timeout=_ENGINE_PROBE_TIMEOUT)
    except TimeoutError:
        # A full backlog or a dropped SYN: the address does not answer, which is a wedge and not a refusal.
        return f"the engine at {host}:{port} did not accept a connection within {_ENGINE_PROBE_TIMEOUT:g}s"
    except OSError:
        return None
    with conn:
        try:
            conn.settimeout(_ENGINE_PROBE_TIMEOUT)
            conn.sendall(b"GET /health HTTP/1.0\r\nHost: engine\r\nConnection: close\r\n\r\n")
            conn.recv(1)
        except TimeoutError:
            return f"the engine at {host}:{port} accepted a connection and did not answer /health within {_ENGINE_PROBE_TIMEOUT:g}s"
        except OSError:
            return None
    return None


def nx_doctor_block() -> list[str]:
    """Return the nx-doctor status block.

    First asks the local engine for ``/health`` under a short bound: a wedged engine (accepts, never answers) is a WARN
    naming it, at once, and doctor is not run.  Otherwise runs ``nx doctor`` under ``_DOCTOR_TIMEOUT`` and reports
    PASS/FAIL from its exit code.  SKIP when nx is not installed or doctor outlasts its bound (said so, not called
    unavailable); the command's summary reads a SKIP as a WARN.

    Returns:
        List of lines for the ### 2. nx configuration section.
    """
    heading = "### 2. nx configuration (nx doctor)"
    lines: list[str] = [heading, ""]
    wedged = _engine_wedged()
    if wedged is not None:
        lines.append(f"Status: WARN - engine not answering: {wedged}; nx doctor not run (it would wait on the same engine); run 'nx doctor' directly")
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
