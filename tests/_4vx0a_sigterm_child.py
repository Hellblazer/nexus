# SPDX-License-Identifier: AGPL-3.0-or-later
"""Child process for tests/test_4vx0a_pipeline_sigterm_row_handback.py.

Runs the real ``pipeline_index_pdf`` with a real ``HttpPipelineDB`` pointed at
the parent's in-memory pipeline stand-in, served over a loopback port
(``NX_4VX0A_PIPELINE_URL``), so the parent's retry reads the same store across
the process boundary. The extractor streams three pages, prints ``READY`` and
then blocks in a call SIGTERM cannot interrupt (an untimed wait on its own
thread, the shape of a long in-process conversion). The catalog fence
write is recorded to ``NX_4VX0A_FENCE_LOG`` instead of reaching a catalog. The
parent sends SIGTERM after ``READY``. Exits 99 if the run ever returns.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from nexus.db.http_pipeline_client import HttpPipelineDB
from nexus.hook_registry import HookRegistry
from nexus.pipeline_stages import pipeline_index_pdf

_PDF = Path(os.environ["NX_4VX0A_PDF"])
_HASH = os.environ["NX_4VX0A_HASH"]
_COLLECTION = os.environ["NX_4VX0A_COLLECTION"]
_PIPELINE_URL = os.environ["NX_4VX0A_PIPELINE_URL"]
_FENCE_LOG = Path(os.environ["NX_4VX0A_FENCE_LOG"])


class _Extractor:
    def extract(self, pdf_path, *, extractor="mineru", on_page=None,
                allow_degraded=False, on_heartbeat=None):
        for i in range(3):
            on_page(i, f"page {i} of the nexus-4vx0a sigterm child", {"page_number": i + 1, "text_length": 40})
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        threading.Event().wait()
        raise AssertionError("the extraction returned")


def _record_fence_fail(doc_id: str, error: str) -> None:
    _FENCE_LOG.write_text(f"{doc_id}\n{error}\n")


def main() -> int:
    db = HttpPipelineDB(base_url=_PIPELINE_URL, _token="loopback-stand-in")
    with patch("nexus.pipeline_stages.PDFExtractor", return_value=_Extractor()), \
            patch("nexus.doc_indexer._fence_fail", _record_fence_fail):
        pipeline_index_pdf(
            _PDF, _HASH, _COLLECTION, MagicMock(), db=db,
            embed_fn=lambda texts, model: ([[0.1] * 4 for _ in texts], model),
            doc_id="1.1.4242", hooks=HookRegistry(),
        )
    return 99


if __name__ == "__main__":
    sys.exit(main())
