# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus D9: NX_ONNX_LOCAL_UPSERT_CHUNK_CAP must never corrupt `nx --json` stdout.

Mechanism (verified 2026-08-10, develop @ ce4dfee9): the cap-override
warning that used to live at ``nexus.db.http_vector_client``'s MODULE
scope ran through structlog's UNCONFIGURED default (``PrintLoggerFactory``,
which writes to **STDOUT**, not stderr — verified against structlog's own
source) because it fired at IMPORT time, before any CLI entry point calls
``configure_logging``. Any ``nx <cmd> --json`` invocation with the env var
set had a log line prepended to its JSON payload on stdout, corrupting it
for every machine consumer, regardless of whether the invoked subcommand
ever touched the onnx-local upsert path. The trigger was the env var's
PRESENCE, not its value (``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP=16``, the
default, reproduced identically).

These tests spawn a FRESH subprocess deliberately — the bug only
manifests on a fresh interpreter, since the module-level statement that
emits the warning runs exactly once, at first import. An in-process
``CliRunner`` invocation would not re-trigger it (the module is already
cached from test collection, long before the test body sets the env var).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_ENV_VAR = "NX_ONNX_LOCAL_UPSERT_CHUNK_CAP"
_EVENT = "onnx_local_upsert_chunk_cap_overridden"


def _run_nx_json(*args: str, cap: str | None) -> subprocess.CompletedProcess[str]:
    """Invoke the real ``nx`` entry point in a fresh subprocess.

    ``--check-mcp-logs`` is chosen deliberately: it only walks a local
    filesystem cache path (no live service, network, or DB), so it is
    deterministic regardless of ambient install state — while still going
    through the real ``nexus.cli`` import chain, which eagerly imports
    every command module (and, transitively, ``nexus.db.http_vector_client``)
    exactly as a real ``nx`` invocation would.
    """
    env = dict(os.environ)
    if cap is None:
        env.pop(_ENV_VAR, None)
    else:
        env[_ENV_VAR] = cap
    return subprocess.run(
        [sys.executable, "-m", "nexus.cli", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_json_command_stdout_is_parseable_with_cap_override_set() -> None:
    """`nx doctor --check-mcp-logs --json` with the override env var SET
    must produce stdout that is pure, parseable JSON — nothing else.

    Non-vacuity: against the pre-fix tree this fails with
    ``json.JSONDecodeError: Extra data`` because the warning line is
    prepended to the JSON payload (confirmed RED before the fix in
    ``nexus.db.http_vector_client``/``nexus.logging_setup``).
    """
    result = _run_nx_json("doctor", "--check-mcp-logs", "--json", cap="300")
    assert result.returncode == 0, result.stderr
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"stdout was not pure JSON (D9 regression): {exc}\nstdout={result.stdout!r}"
        )
    assert "cache_dir" in payload
    assert _EVENT not in result.stdout


def test_json_command_stdout_parseable_with_cap_at_default_value() -> None:
    """The discriminator is the env var's PRESENCE, not its value — setting
    it to the documented default (16) must reproduce identically to a
    non-default value, and the fix must cover both."""
    result = _run_nx_json("doctor", "--check-mcp-logs", "--json", cap="16")
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)  # must not raise
    assert _EVENT not in result.stdout


def test_cap_override_warning_still_reaches_stderr() -> None:
    """Fixing stdout corruption must not delete the observability: the
    override warning is the ONLY signal a non-default onnx-local upsert
    cap is active (nexus-rn9n7 tracks a separate, still-open silent route
    to an unsafe cap — this warning is not that bug's fix, but it must
    keep firing so an active override is never invisible)."""
    result = _run_nx_json("doctor", "--check-mcp-logs", "--json", cap="300")
    assert _EVENT in result.stderr
    assert "value=300" in result.stderr
    assert "default=16" in result.stderr
    assert _EVENT not in result.stdout


def test_cap_override_warning_absent_when_env_unset_control() -> None:
    """Control: without the override, neither stream carries the warning,
    and stdout is still pure JSON."""
    result = _run_nx_json("doctor", "--check-mcp-logs", "--json", cap=None)
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)  # must not raise
    assert _EVENT not in result.stdout
    assert _EVENT not in result.stderr


def test_no_import_time_structlog_emission_lands_on_stdout() -> None:
    """Class-wide regression guard, not just this one instance: a bare
    ``import nexus.cli`` (no command execution) eagerly imports every
    command module and, transitively, every T1/T2/T3 client module. This
    is the general shape of nexus D9 — ANY module-level structlog call
    reachable from that import chain must never write to stdout, whether
    or not it is the one instance fixed here. A future module-level
    ``_log.warning(...)`` (using the ambient, not-yet-configured shared
    logger) would reproduce the same corruption and this test would catch
    it, independent of which specific env var or code path triggers it.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import nexus.cli"],
        capture_output=True, text=True,
        env={**os.environ, _ENV_VAR: "300"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", (
        f"import-time code wrote to stdout (D9-class regression): {result.stdout!r}"
    )
    assert _EVENT in result.stderr
