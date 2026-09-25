# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 amendment (nexus-wauo1.35 / .38): nx-mcp's startup warning for
the harness dispatch grant.

``_warn_if_harness_oauth_grant_present`` is unit-testable standalone --
it only reads ``NX_HARNESS_CLAUDE_OAUTH_TOKEN`` from the process
environment and logs, so these tests do not need to drive the full
``_t1_lifespan`` (which needs a live T1/engine substrate).
"""
from __future__ import annotations

from structlog.testing import capture_logs

from nexus.mcp.core import _warn_if_harness_oauth_grant_present


def test_absent_logs_nothing(monkeypatch) -> None:
    monkeypatch.delenv("NX_HARNESS_CLAUDE_OAUTH_TOKEN", raising=False)

    with capture_logs() as logs:
        _warn_if_harness_oauth_grant_present()

    assert logs == []


def test_present_logs_one_warning_naming_no_value(monkeypatch) -> None:
    monkeypatch.setenv("NX_HARNESS_CLAUDE_OAUTH_TOKEN", "super-secret-fake-value-zzz")

    with capture_logs() as logs:
        _warn_if_harness_oauth_grant_present()

    warnings = [e for e in logs if e.get("log_level") == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "nx_mcp_harness_oauth_grant_active"
    rendered = repr(warnings)
    assert "super-secret-fake-value-zzz" not in rendered


def test_empty_value_is_treated_as_absent(monkeypatch) -> None:
    monkeypatch.setenv("NX_HARNESS_CLAUDE_OAUTH_TOKEN", "")

    with capture_logs() as logs:
        _warn_if_harness_oauth_grant_present()

    assert logs == []
