# SPDX-License-Identifier: AGPL-3.0-or-later
"""Credential redaction for text that came back from a service and is about
to be logged, printed, or carried in an exception message (nexus-8ooxn,
nexus-hcy4w). The engine's 401/403 bodies can echo the rejected credential;
redaction happens where the body enters a message, so every path that
renders the message (structlog, ``nx search``, ``nx doc``, the doctor)
sees the redacted form.
"""
from __future__ import annotations

import re

_CREDENTIAL_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|bearer|password|secret|authorization)\b(\s*[:=]\s*|\s+)(\S+)"
)
_ALWAYS_VALUE_KEYS = frozenset({"bearer", "authorization"})
_MIN_VALUE_LEN = 6


def _looks_like_a_value(key: str, sep: str, value: str) -> bool:
    if sep.strip() in (":", "=") or key.lower() in _ALWAYS_VALUE_KEYS:
        return True
    # A bare lowercase word after the key word is prose ("token endpoint",
    # "secret sauce"); anything else long enough to be a credential is one.
    if value.isalpha() and value.islower():
        return False
    return len(value) >= _MIN_VALUE_LEN


def redact_credentials(text: str) -> str:
    """*text* with the value after any credential key word replaced by
    ``[redacted]``; prose that merely uses the word is left alone."""

    def _sub(m: re.Match[str]) -> str:
        key, sep, value = m.group(1), m.group(2), m.group(3)
        if not _looks_like_a_value(key, sep, value):
            return m.group(0)
        return f"{key}{sep}[redacted]"

    return _CREDENTIAL_RE.sub(_sub, text)
