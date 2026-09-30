"""A failed advisory heal logs its cause without a stack (nexus-z0o2p.16).

``_heal_failed_document`` runs inside a write's failure path. The CLI prints
WARNING logs to the terminal, so a stack here lands beside the command's own
clean ``Error:`` line. On CI the heal's T3 probe failed for the same reason the
write did (an embedding-profile mismatch) and the stack broke
``test_store_put_profile_refusal_is_a_clean_click_error``'s no-traceback pin.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from nexus import doc_indexer


def test_a_failed_heal_logs_the_cause_without_a_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse() -> None:
        raise RuntimeError("engine profile mismatch: restart the service")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", _refuse)

    with capture_logs() as logs:
        doc_indexer._heal_failed_document("1.1.1")

    events = [e for e in logs if e["event"] == "index_run_fail_heal_failed"]
    assert len(events) == 1, logs
    event = events[0]
    assert "exc_info" not in event and "exception" not in event, event
    assert event["error_class"] == "RuntimeError"
    assert "profile mismatch" in event["error"]
