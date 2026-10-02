# SPDX-License-Identifier: AGPL-3.0-or-later
"""The oversize writer's request size is measurable (RDR-223 Phase 2 gate, nexus-z0o2p.35, F3).

``tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh`` proves its per-request chunk cap
non-vacuous by reading the largest request out of the index run's log. It read
``http_vector_upsert_chunks_request``, which only ``HttpVectorClient.upsert_chunks`` emits; since the
oversize fallback writes through ``MultiBatchDocumentWriter`` (nexus-z0o2p.14) that event never
appears for it, so the probe saw only the small-file flush and would grade "cap NEVER BOUND".

Here the real writer sends a 21-chunk document through the real catalog client (with the HTTP POST
stubbed), the events it emits are rendered as log lines, and the script's own
``_largest_flush_in_log`` shell function reads them back. The probe is the script's, run as bash,
against what the code emits, so renaming either side fails here.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.multi_batch_write import write_document

_SCRIPT = Path(__file__).resolve().parents[1] / "e2e" / "migration-rehearsal" / "rehearse_shakeout_e2e.sh"
_COLLECTION = "code__z0o2p35-probe__bge-base-en-v15-768__v1"
_CAP = 16
_DOC = "1.1.1"


def _chash(i: int) -> str:
    return hashlib.sha256(f"z0o2p35 chunk {i}".encode()).hexdigest()


@pytest.fixture(autouse=True)
def _info_events_are_kept():
    """The suite filters structlog at WARNING (``tests/conftest.py``), which drops the INFO event
    under test before ``capture_logs`` sees it; the suite's own autouse fixture restores the level."""
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


def _client(monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
    import nexus.corpus as corpus

    monkeypatch.setattr(corpus, "ensure_collection_registered", lambda *a, **k: None)
    c = HttpCatalogClient.__new__(HttpCatalogClient)

    def _post(path: str, body: dict, **kw: Any) -> dict:
        n = len(body.get("chunks") or ())
        if path == "/manifest/write_many":
            return {"failed_doc_ids": [], "chunks_written": n, "complete_refused": [],
                    "complete_refused_count": 0, "swept": 0, "sweep_skipped": 0, "sweep_detail": [],
                    "dropped_chashes": {_DOC: []}, "dropped_count": {_DOC: 0}}
        if path == "/manifest/append":
            return {"ok": True, "count": len(body["rows"]), "chunks_written": n, "chunks_unreferenced": 0,
                    "swept": len(body.get("sweep_chashes") or ()), "sweep_skipped": 0, "sweep_detail": {}}
        raise AssertionError(f"unexpected POST {path}")

    monkeypatch.setattr(c, "_post", _post, raising=False)
    monkeypatch.setattr(c, "begin_index_run", lambda *a, **k: {"ok": True, "prior_chashes": [], "prior_count": 0},
                        raising=False)
    monkeypatch.setattr(c, "complete_index_run", lambda *a, **k: {"referenced": 21}, raising=False)
    monkeypatch.setattr(c, "fail_index_run", lambda *a, **k: {}, raising=False)
    return c


def _render(events: list[dict]) -> str:
    """Events as the console renderer prints them: ``event key=value ...``."""
    return "\n".join(
        " ".join([e["event"], *(f"{k}={v}" for k, v in e.items() if k not in ("event", "log_level"))])
        for e in events)


def _probe(log_text: str, tmp_path: Path) -> int:
    """The script's own ``_largest_flush_in_log``, run as bash over *log_text*."""
    src = _SCRIPT.read_text()
    m = re.search(r"^_largest_flush_in_log\(\) \{.*?^\}\n", src, re.S | re.M)
    assert m, "the probe function moved or was renamed; update this test with it"
    log = tmp_path / "index.log"
    log.write_text(log_text + "\n")
    out = subprocess.run(
        ["bash", "-c", f'{m.group(0)}\n_largest_flush_in_log "{log}"'],
        capture_output=True, text=True, check=True, timeout=30)
    return int(out.stdout.strip())


def _write_oversize_document(c: HttpCatalogClient) -> None:
    rows = [{"chash": _chash(i), "position": i} for i in range(21)]
    chunks = [{"chash": _chash(i), "text": f"t{i}", "metadata": {}} for i in range(21)]
    write_document(c, [(rows, chunks)], doc_id=_DOC, collection=_COLLECTION,
                   content_hash="h", chunk_cap=_CAP)


def test_the_oversize_writers_largest_request_is_the_cap(monkeypatch, tmp_path):
    c = _client(monkeypatch)
    with capture_logs() as logs:
        _write_oversize_document(c)
    counts = [e["count"] for e in logs if e["event"] == "http_catalog_combined_write_request"]
    assert counts == [_CAP, 21 - _CAP], counts            # non-vacuity: two requests, the first at the cap
    assert not [e for e in logs if e["event"] == "http_vector_upsert_chunks_request"], (
        "the oversize writer must not be measured through the legacy upsert event")
    assert _probe(_render(logs), tmp_path) == _CAP


def test_the_probe_reads_the_flush_event_too(tmp_path):
    assert _probe("chunk_flush_complete collection=c chunks=3 files=1\n"
                  "http_catalog_combined_write_request path=/manifest/append collection=c count=2", tmp_path) == 3


def test_the_probe_no_longer_reads_the_legacy_event(tmp_path):
    """The probe must not report a request size from an event the oversize writer never emits."""
    assert _probe("http_vector_upsert_chunks_request collection=c page=1 pages=1 count=16", tmp_path) == 0
