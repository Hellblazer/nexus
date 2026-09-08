# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8ooxn / nexus-hcy4w: a service error body that echoes the rejected
credential is redacted before it reaches a structlog line. The doctor's
printed output already omitted it; the log line did not, and under xdist
the worker's console renderer landed that line in the CliRunner output.
"""
from __future__ import annotations

from nexus.health import redact_credentials


def test_api_key_value_is_redacted() -> None:
    assert redact_credentials("HTTP 401: invalid api_key SUPERSECRET") == "HTTP 401: invalid api_key [redacted]"
    assert "SUPERSECRET" not in redact_credentials("token=SUPERSECRET; retry")
    assert redact_credentials("Bearer abc.def.ghi expired") == "Bearer [redacted] expired"


def test_text_without_credentials_is_unchanged() -> None:
    msg = "connection refused: 127.0.0.1:26901"
    assert redact_credentials(msg) == msg
