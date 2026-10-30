# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read the endpoint a user-facing message quotes, as a parsed URL (nexus-nfdiw).

The client's messages name the service by ``host[:port]`` (``log_endpoint``,
never userinfo or path). A test that wants to prove the message names the right
service reads that token out of the message and compares its parsed hostname and
port, rather than looking for the host as a substring of free text (CodeQL
py/incomplete-url-substring-sanitization, and a real hole: ``evil.com/x`` or
``api.conexus-nexus.com.evil.net`` contains the substring too).
"""
from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit


def quoted_endpoint(text: str, after: str) -> SplitResult:
    """Parse the ``host[:port]`` token that directly follows *after* in *text*.

    Fails the test (AssertionError) when *after* is absent, so a reworded
    message cannot pass vacuously. A repr-quoted token is unquoted.
    """
    match = re.search(re.escape(after) + r"""['"]?([^\s'",)]+)""", text)
    assert match, f"no endpoint after {after!r} in {text!r}"
    return urlsplit("//" + match.group(1))
