# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-nj4ch: HttpAspectQueue.enqueue_many batches N document enqueues
into one round trip, completing the register_many/update_many/delete_many/
get_all_metadata batch pattern for the aspect-extraction queue.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.db.t2.http_aspect_queue import HttpAspectQueue


@pytest.fixture
def queue(monkeypatch) -> HttpAspectQueue:
    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    return HttpAspectQueue(base_url="http://mock.test")


class TestEnqueueManyRealUrlConstruction:
    """Genuine end-to-end URL-construction check via httpx.MockTransport —
    NOT mocking _post itself, which (as found during this bead's own
    shakeout) can pass while asserting the SAME bug it should catch: a
    prior version of this test mocked _post and asserted the WRONG path
    ("/queue/enqueue_many"), matching the actual doubled-segment bug
    ("/v1/aspects/queue/queue/enqueue_many") that 404'd against the live
    service. This test drives the real httpx.Client + _post's hard-coded
    "/v1/aspects/queue" prefix, so a future path regression 404s HERE,
    not only in production.
    """

    def test_enqueue_many_hits_the_real_service_path(self, monkeypatch) -> None:
        import httpx

        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            return httpx.Response(200, json={"enqueued": 1})

        monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
        queue = HttpAspectQueue(base_url="http://mock.test")
        queue._client = httpx.Client(
            base_url="http://mock.test",
            transport=httpx.MockTransport(handler),
        )

        n = queue.enqueue_many([{"collection": "code__x", "source_path": "a.py"}])

        assert requests == ["/v1/aspects/queue/enqueue_many"]
        assert n == 1


class TestEnqueueMany:
    def test_posts_to_enqueue_many_endpoint(self, queue: HttpAspectQueue) -> None:
        rows = [
            {"collection": "code__x", "source_path": "a.py"},
            {"collection": "code__x", "source_path": "b.py", "doc_id": "1.1.1"},
        ]
        with patch(
            "nexus.db.t2.http_aspect_queue.HttpAspectQueue._post",
            return_value={"enqueued": 2},
        ) as mock_post:
            n = queue.enqueue_many(rows)

        path, body = mock_post.call_args[0]
        # NOT "/queue/enqueue_many" -- self._base_url already ends in
        # ".../v1/aspects/queue" (confirmed via a real shakeout: the
        # doubled-segment path 404'd against the live service and this
        # test's own mock-only assertion had matched the SAME bug it
        # should have caught, since it never exercises _post's real base_url
        # join).
        assert path == "/enqueue_many"
        assert body == {"rows": rows}
        assert n == 2

    def test_empty_list_is_noop(self, queue: HttpAspectQueue) -> None:
        with patch("nexus.db.t2.http_aspect_queue.HttpAspectQueue._post") as mock_post:
            assert queue.enqueue_many([]) == 0
        mock_post.assert_not_called()

    def test_batch_failure_falls_back_to_per_row_enqueue(self, queue: HttpAspectQueue) -> None:
        # nexus-nj4ch: the whole batch shares one Postgres transaction
        # server-side, so a single row's constraint violation fails the
        # WHOLE batch -- must fall back to per-row enqueue(), isolating
        # the bad row from its batch-mates.
        rows = [
            {"collection": "code__x", "source_path": "a.py"},
            {"collection": "code__x", "source_path": "b.py"},
        ]
        single_calls: list[tuple] = []

        def _fake_post(path: str, body: dict) -> dict:
            if path == "/queue/enqueue_many":
                raise RuntimeError("500: FK violation in batch")
            assert path == "/enqueue"
            single_calls.append((body["collection"], body["source_path"]))
            return {"enqueued": True}

        with patch(
            "nexus.db.t2.http_aspect_queue.HttpAspectQueue._post",
            side_effect=_fake_post,
        ):
            n = queue.enqueue_many(rows)

        assert single_calls == [("code__x", "a.py"), ("code__x", "b.py")]
        assert n == 2

    def test_partial_per_row_fallback_failure_isolated(self, queue: HttpAspectQueue) -> None:
        """One bad row in the per-row fallback must not sink its siblings."""
        rows = [
            {"collection": "code__x", "source_path": "good-a.py"},
            {"collection": "code__x", "source_path": "bad.py"},
            {"collection": "code__x", "source_path": "good-b.py"},
        ]

        def _fake_post(path: str, body: dict) -> dict:
            if path == "/queue/enqueue_many":
                raise RuntimeError("batch failed")
            if body["source_path"] == "bad.py":
                raise RuntimeError("still fails per-row")
            return {"enqueued": True}

        with patch(
            "nexus.db.t2.http_aspect_queue.HttpAspectQueue._post",
            side_effect=_fake_post,
        ):
            n = queue.enqueue_many(rows)

        assert n == 2, "the 2 good rows must still be counted despite the bad row"

    def test_malformed_row_skipped_in_fallback(self, queue: HttpAspectQueue) -> None:
        rows = [
            {"collection": "", "source_path": "missing-collection.py"},
            {"collection": "code__x", "source_path": "good.py"},
        ]

        def _fake_post(path: str, body: dict) -> dict:
            if path == "/queue/enqueue_many":
                raise RuntimeError("batch failed")
            assert body["source_path"] == "good.py"
            return {"enqueued": True}

        with patch(
            "nexus.db.t2.http_aspect_queue.HttpAspectQueue._post",
            side_effect=_fake_post,
        ):
            n = queue.enqueue_many(rows)

        assert n == 1
