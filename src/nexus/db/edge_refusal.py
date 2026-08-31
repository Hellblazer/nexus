# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Edge/WAF refusal rendering shared by the httpx store family (nexus-cmzib).

The T3 vector client grew edge-refusal detection at nexus-1jtob: a 403 from
the AWS WAF and a 403 from the application are the same integer and mean
opposite things, and the WAF matches on REQUEST BODY CONTENT, so the refusal
is deterministic — retrying, or chasing credentials, cannot help. T2 and T1
never adopted it: a ``memory_put`` / ``scratch put`` whose body contained
shell-substitution text (a dollar sign immediately followed by an open
parenthesis or an open brace — spelled in prose here and in every message
below, deliberately, so THIS module's own text can be persisted through the
same edge it describes) surfaced as raw ``<html>403 Forbidden`` with no hint
(measured 2026-08-20: three CLI puts, plain text stored, the
substitution-bearing one refused — WAF KnownBadInputs ``Log4JRCE_BODY``).
The specific rule was fixed edge-side 2026-08-23 (conexus PR #230: the
managed group's three ``_BODY`` sub-rules count-only on ``/v1/*``); this
module remains the guard for the RECURRENCE class — the managed rule group
grows new body signatures over time (conexus-5jm5), and each new one
reproduces the same raw-HTML-403 experience until classified here.

This module reuses the T3 classifier (``_edge_server`` — a POSITIVE test on
the ``Server: awselb/...`` header, duck-typed ``.get`` so httpx headers work
unchanged) and remedy text as the single source of truth, and adds the
body-content hint for the substitution-text trigger.
"""
from __future__ import annotations

# nexus-1ytp6 precedent: the classifier/remedy are IMPORTED from the T3
# reference implementation, not redefined — one source of truth.
from nexus.db.http_vector_client import _edge_refusal_remedy, _edge_server

#: The measured WAF trigger for T2/T1 write bodies (nexus-cmzib). Assembled,
#: not literal, so this module's own source and any persisted rendering of
#: its messages cannot themselves trip the rule.
_SUBSTITUTION_TOKENS: tuple[str, ...] = ("$" + "(", "$" + "{")


def shell_substitution_hint(request_body_text: str) -> str | None:
    """The defang hint, when *request_body_text* carries the known trigger.

    Prose-only on purpose: the hint itself may be persisted (an error
    captured into a T2 record), so it must not contain the trigger.
    """
    if not any(tok in request_body_text for tok in _SUBSTITUTION_TOKENS):
        return None
    return (
        "This request body contains shell-substitution text (a dollar sign "
        "immediately followed by an open parenthesis or an open brace) — the "
        "measured WAF trigger class for T2/T1 write bodies (nexus-cmzib; the "
        "2026-08 rule was fixed edge-side, but the class recurs whenever the "
        "managed rule group grows a new body signature). Relay the block to "
        "the edge operator; as an immediate workaround, defang before "
        "storing: insert a space between the dollar sign and the bracket."
    )


def edge_refusal_message(
    op: str,
    status_code: int,
    headers: object,
    request_body_text: str = "",
) -> str | None:
    """Structured message when the EDGE (not the application) refused *op*.

    Returns ``None`` unless the response carries the edge's positive
    signature (``Server: awselb/...``) — an application error, whatever its
    status, is never reframed. The raw response body (an HTML error page) is
    deliberately NOT included: surfacing it verbatim is the exact complaint
    this closes.
    """
    server = _edge_server(headers)
    if server is None:
        return None
    if status_code >= 500:
        # Edge-authored 5xx is an availability event (a deploy window, an
        # unreachable upstream), NOT the WAF's deterministic body match — the
        # 1jtob "check the WAF logs" prose would mislead an operator during a
        # genuine outage (substantive-critic ql1wf, 2026-08-31). The mixin's
        # gateway-retry axis has already retried 502/503/504 by the time this
        # renders; this is the terminal message.
        return (
            f"{op}: HTTP {status_code} — answered at the EDGE "
            f"(server={server}), not by the nexus application: the edge could "
            "not reach or complete against the upstream (deploy window or "
            "outage). Transient by nature — the client's gateway retries "
            "already ran; retry later rather than changing credentials or "
            "payload."
        )
    msg = f"{op}: HTTP {status_code} — {_edge_refusal_remedy(server, status_code)}"
    hint = shell_substitution_hint(request_body_text)
    if hint is not None:
        msg = f"{msg} {hint}"
    return msg
