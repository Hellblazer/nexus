# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An edge refusal must be named as one, attributed to a file, and never retried.

nexus-1jtob. Every `nx index repo` run against production died on exactly one
HTTP 403 from POST /v1/vectors/upsert-chunks between 2026-08-19 and 08-23 --
151 occurrences, one per run, in 151 of 397 runs. Three defects turned one
blocked request into four days of chasing the wrong cause:

1. DIAGNOSTIC MISDIRECTED. `_managed_remedy` reframed the 403 as "check
   NX_SERVICE_URL is reachable and NX_SERVICE_TOKEN is valid". The token was
   provably fine -- 477 successful upserts and 1 x 403 in the SAME run, with
   the same token. A bad token fails 100% of the time.
2. UNATTRIBUTABLE. `run_file_loop` re-raised `failures[0][1]`, which named no
   file, so the traceback surfaced at the phase boundary with no indication of
   which document was rejected. The suppressed sibling failures WERE logged
   with `file=`; the one that ended the run was not.
3. AMPLIFIED. `retry._RETRYABLE_ETL_HTTP_STATUSES` contained 403 on a
   "transient edge 403" premise that was never measured.

The real cause was AWS WAF `KnownBadInputs` / `JavaDeserializationRCE_BODY`
matching a JVM root package prefix in the request body: RDR-195 quotes a JVM
stack trace, so the document documenting the bug could not be indexed because
the bug report reads as an exploit payload.

THE DISCRIMINATOR IS A POSITIVE TEST ON `awselb/`, and the tests below pin
that shape rather than the outcome alone, because the first proposed
discriminators were BOTH inverted:
  - "nginx means the app answered" -- wrong; the app emits NO Server header.
  - "a non-JSON body means the app did not answer" -- wrong; the app answers
    in PLAIN TEXT, not JSON.
Both were caught only by measuring the live edge (conexus-a5, 2026-08-23):

  WAF/ALB refusal:  server: awselb/2.0, content-type: text/html, 118-byte page
  application:      no Server header, no content-type, plain-text body

`test_detector_rejects_the_two_inverted_discriminators` below is the
regression pin for exactly that pair.
"""
from __future__ import annotations

import email.message
import urllib.error

import pytest

from nexus.db.http_vector_client import (
    VectorServiceError,
    _edge_refusal_remedy,
    _edge_server,
)

#: Byte-for-byte the ALB block page (118 bytes, CRLF). Note the ABSENCE of the
#: `<hr><center>nginx</center>` footer stock nginx emits -- that absence is the
#: tell that identified this as an ALB page rather than the nginx page the bead
#: originally called it.
_ALB_403_BODY = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n"
    b"<body>\r\n<center><h1>403 Forbidden</h1></center>\r\n</body>\r\n</html>\r\n"
)


def _headers(**pairs: str) -> email.message.Message:
    m = email.message.Message()
    for k, v in pairs.items():
        m[k.replace("_", "-")] = v
    return m


def _waf_403() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.conexus-nexus.com/v1/vectors/upsert-chunks",
        403,
        "Forbidden",
        _headers(Server="awselb/2.0", Content_Type="text/html"),
        None,
    )


def _app_401() -> urllib.error.HTTPError:
    """The control plane's own rejection: NO Server header, plain text."""
    return urllib.error.HTTPError(
        "https://api.conexus-nexus.com/v1/vectors/upsert-chunks",
        401,
        "Unauthorized",
        _headers(),
        None,
    )


class TestEdgeDetector:
    def test_flags_the_alb(self) -> None:
        assert _edge_server(_waf_403().headers) == "awselb/2.0"

    def test_clears_the_application(self) -> None:
        assert _edge_server(_app_401().headers) is None

    def test_detector_rejects_the_two_inverted_discriminators(self) -> None:
        """Both first-pass discriminators were backwards. Pin them dead."""
        # "nginx means the app answered" -- an nginx-fronted ENGINE response is
        # NOT an edge refusal, and must not be read as one.
        assert _edge_server(_headers(Server="nginx/1.24.0")) is None
        # ...and, critically, absence of a Server header must NOT be read as
        # "edge". The application is the population with no Server header.
        assert _edge_server(_headers()) is None
        # "a non-JSON body means the app did not answer" -- the app answers in
        # plain text, so body shape cannot be the discriminator at all. The
        # detector must key on headers only and ignore the body entirely.
        assert _edge_server(_headers(Content_Type="text/plain")) is None

    def test_case_and_whitespace_tolerant(self) -> None:
        assert _edge_server(_headers(server="  AWSELB/2.0  ")) == "AWSELB/2.0"

    @pytest.mark.parametrize("bad", [None, object(), "not-a-header-bag"])
    def test_never_raises_on_a_junk_header_bag(self, bad: object) -> None:
        """Diagnostics must not turn one failure into a different failure."""
        assert _edge_server(bad) is None


class TestRemedyText:
    def test_names_the_edge_and_not_the_token(self) -> None:
        text = _edge_refusal_remedy("awselb/2.0", 403)
        assert "EDGE" in text
        assert "awselb/2.0" in text
        assert "deterministic" in text.lower()
        # The whole defect: it must NOT send the operator after credentials.
        assert "NX_SERVICE_TOKEN is valid" not in text
        assert "nx doctor" not in text

    def test_names_the_documentation_trigger(self) -> None:
        """The recurring class, not just this incident: a WAF tuned for exploit
        payloads fires on documentation ABOUT exploits."""
        text = _edge_refusal_remedy("awselb/2.0", 403).lower()
        assert "body" in text
        assert "stack trace" in text or "exploit" in text


class TestVectorServiceErrorFlag:
    def test_defaults_false_so_existing_callers_are_unchanged(self) -> None:
        assert VectorServiceError("boom", code=500).edge_refusal is False

    def test_carries_the_flag(self) -> None:
        assert VectorServiceError("boom", code=403, edge_refusal=True).edge_refusal


class TestRetryClassification:
    """Defect 3: the ETL retry set had the two 403 populations backwards."""

    def test_403_is_not_retryable(self) -> None:
        from nexus.retry import _is_retryable_etl_error

        assert _is_retryable_etl_error(_waf_403()) is False

    def test_the_genuinely_transient_statuses_still_retry(self) -> None:
        """The edge is not blanket-untrusted. An ALB 503 (no healthy target)
        carries server: awselb/2.0 and IS transient -- narrowing must not
        collateral-damage it."""
        from nexus.retry import _is_retryable_etl_error

        for code in (429, 502, 503, 504):
            err = urllib.error.HTTPError(
                "https://api.conexus-nexus.com/v1/x", code, "x",
                _headers(Server="awselb/2.0"), None,
            )
            assert _is_retryable_etl_error(err) is True, f"{code} must still retry"

    def test_edge_refusal_4xx_wrapper_is_not_retryable(self) -> None:
        from nexus.retry import _is_retryable_etl_error

        assert _is_retryable_etl_error(
            VectorServiceError("blocked", code=403, edge_refusal=True)
        ) is False

    def test_transport_drops_still_retry(self) -> None:
        """Regression guard on the code=None wrapper path (the code-review gap
        that this set's history already once re-opened)."""
        from nexus.retry import _is_retryable_etl_error

        assert _is_retryable_etl_error(urllib.error.URLError("dropped")) is True
        assert _is_retryable_etl_error(TimeoutError("slow")) is True


class TestFailureNamesTheFile:
    """Defect 2: the one failure that ENDS the run was the one nothing named."""

    @staticmethod
    def _loop(files, index_one, concurrency):
        from nexus.indexer_utils import run_file_loop

        return run_file_loop(
            files, index_one,
            concurrency=concurrency, on_file=None, on_stage_timers=None,
        )

    @pytest.mark.parametrize("concurrency", [1, 4], ids=["sequential", "concurrent"])
    def test_the_raised_failure_names_its_file(self, tmp_path, concurrency: int) -> None:
        """BOTH loop paths, because the bead described only the concurrent one
        while the sequential path drops the same attribution."""
        doomed = tmp_path / "rdr-195-voyage-batch-token-limit.md"
        files = [(1.0, tmp_path / f"ok-{i}.md") for i in range(3)]
        files.insert(2, (1.0, doomed))

        def index_one(file, score, timers):
            if file == doomed:
                raise RuntimeError("POST /v1/vectors/upsert-chunks → HTTP 403")
            return 1

        with pytest.raises(RuntimeError) as caught:
            self._loop(files, index_one, concurrency)

        notes = " ".join(getattr(caught.value, "__notes__", []))
        assert doomed.name in notes, (
            "the failing document must be identifiable from the raised error; "
            f"got notes={notes!r}"
        )

    def test_the_exception_type_and_message_are_untouched(self, tmp_path) -> None:
        """add_note was chosen over a wrapper BECAUSE callers classify by type.
        If this ever becomes a wrapper, the containment tests that gate
        UnextractableContentError / skip-floor behaviour break silently."""
        doomed = tmp_path / "boom.md"

        class Marker(RuntimeError):
            pass

        def index_one(file, score, timers):
            raise Marker("exact text")

        with pytest.raises(Marker) as caught:
            self._loop([(1.0, doomed)], index_one, 1)

        assert type(caught.value) is Marker
        assert str(caught.value) == "exact text"
