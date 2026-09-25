# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 amendment (nexus-wauo1.35 / .38): ``apply_harness_oauth_grant``
maps ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` into ``CLAUDE_CODE_OAUTH_TOKEN`` in a
claude child's OWN environment only -- never in ``os.environ``, never
mutating its input.
"""
from __future__ import annotations

import os

import pytest
from structlog.testing import capture_logs

import nexus.claude_child_env as claude_child_env

from nexus.claude_child_env import (
    CLAUDE_OAUTH_TOKEN_ENV_VAR,
    HARNESS_OAUTH_TOKEN_ENV_VAR,
    apply_harness_oauth_grant,
)


def test_no_harness_name_leaves_env_unchanged() -> None:
    """Production unchanged: with no harness name in ``base``, the result
    is byte-identical to ``base`` (no key added, none removed, none
    changed) -- the no-op path a non-harness dispatch always takes."""
    base = {"PATH": "/usr/bin", "HOME": "/home/x"}

    result = apply_harness_oauth_grant(base)

    assert result == base
    assert CLAUDE_OAUTH_TOKEN_ENV_VAR not in result
    assert HARNESS_OAUTH_TOKEN_ENV_VAR not in result


def test_empty_harness_name_is_treated_as_absent() -> None:
    """An empty-string harness value (present but empty) grants nothing --
    'present and non-empty' per the RDR, not merely 'present'."""
    base = {"PATH": "/usr/bin", HARNESS_OAUTH_TOKEN_ENV_VAR: ""}

    result = apply_harness_oauth_grant(base)

    assert CLAUDE_OAUTH_TOKEN_ENV_VAR not in result
    assert result[HARNESS_OAUTH_TOKEN_ENV_VAR] == ""


def test_harness_name_present_grants_the_token_and_keeps_the_harness_name() -> None:
    """With the harness name present and no existing
    CLAUDE_CODE_OAUTH_TOKEN, the child gets CLAUDE_CODE_OAUTH_TOKEN set to
    the harness value, and the harness name itself is kept in the result
    (so a nested tool-granting dispatch's own nx-mcp can map it again)."""
    base = {"PATH": "/usr/bin", HARNESS_OAUTH_TOKEN_ENV_VAR: "fake-harness-token-123"}

    result = apply_harness_oauth_grant(base)

    assert result[CLAUDE_OAUTH_TOKEN_ENV_VAR] == "fake-harness-token-123"
    assert result[HARNESS_OAUTH_TOKEN_ENV_VAR] == "fake-harness-token-123"
    assert result["PATH"] == "/usr/bin"


def test_existing_claude_token_wins_over_the_grant() -> None:
    """An existing CLAUDE_CODE_OAUTH_TOKEN in base is never overwritten by
    the harness grant."""
    base = {
        HARNESS_OAUTH_TOKEN_ENV_VAR: "fake-harness-token",
        CLAUDE_OAUTH_TOKEN_ENV_VAR: "fake-existing-token",
    }

    result = apply_harness_oauth_grant(base)

    assert result[CLAUDE_OAUTH_TOKEN_ENV_VAR] == "fake-existing-token"


def test_never_mutates_the_input_mapping() -> None:
    """``base`` itself must be untouched -- the function returns a NEW dict."""
    base = {HARNESS_OAUTH_TOKEN_ENV_VAR: "fake-harness-token"}
    base_copy = dict(base)

    result = apply_harness_oauth_grant(base)

    assert base == base_copy, "input mapping must never be mutated"
    assert result is not base


def test_never_mutates_os_environ(monkeypatch) -> None:
    """Passing ``os.environ`` itself as ``base`` must not leave
    CLAUDE_CODE_OAUTH_TOKEN (or anything else) behind in the real
    environment -- the function must copy before writing."""
    monkeypatch.setenv(HARNESS_OAUTH_TOKEN_ENV_VAR, "fake-harness-token")
    monkeypatch.delenv(CLAUDE_OAUTH_TOKEN_ENV_VAR, raising=False)

    result = apply_harness_oauth_grant(os.environ)

    assert result[CLAUDE_OAUTH_TOKEN_ENV_VAR] == "fake-harness-token"
    assert CLAUDE_OAUTH_TOKEN_ENV_VAR not in os.environ, (
        "os.environ must never be mutated by the grant"
    )


def test_never_logs_either_value() -> None:
    """The helper's own log line (when it fires) must never contain either
    the harness value or the granted token value -- only the fact that a
    grant was applied, at most once."""
    base = {HARNESS_OAUTH_TOKEN_ENV_VAR: "super-secret-fake-value-zzz"}
    with capture_logs() as logs:
        apply_harness_oauth_grant(base)

    assert logs, "the grant path must log at least one event"
    assert len(logs) == 1, "at most one line, per the helper's contract"
    rendered = repr(logs)
    assert "super-secret-fake-value-zzz" not in rendered


def test_no_grant_logs_nothing() -> None:
    """No harness name -> no log call at all (not just no value logged)."""
    with capture_logs() as logs:
        apply_harness_oauth_grant({"PATH": "/usr/bin"})

    assert logs == []


@pytest.fixture(autouse=True)
def _fresh_grant_log_flag(monkeypatch) -> None:
    """The grant logs once per process; reset that state per test."""
    monkeypatch.setattr(claude_child_env, "_grant_logged", False)


def test_grant_logs_once_per_process_not_per_dispatch() -> None:
    """Review of nexus-wauo1.38: every operator dispatch and aspect
    extraction retry applies the grant, so a per-call warning floods a
    harness run's log. One line per process is enough; nx-mcp's startup
    warning already names the grant."""
    base = {HARNESS_OAUTH_TOKEN_ENV_VAR: "fake-value"}
    with capture_logs() as logs:
        for _ in range(5):
            apply_harness_oauth_grant(base)
    assert len(logs) == 1


def test_existing_token_wins_and_logs_nothing() -> None:
    base = {HARNESS_OAUTH_TOKEN_ENV_VAR: "fake-h", CLAUDE_OAUTH_TOKEN_ENV_VAR: "fake-c"}
    with capture_logs() as logs:
        apply_harness_oauth_grant(base)
    assert logs == []
