"""onnxruntime never loads unless something actually embeds (nexus-ct17r, nexus-b7s8t).

``mineru.cli.common`` imports magika, which imports onnxruntime at module
scope; ``fastembed`` imports onnxruntime too. Both were reachable from the
import of ``nexus.pdf_extractor`` (eager ``from mineru.cli.common import
do_parse``) and from ``_fastembed_available()`` (an ``import_module`` probe
that ran on every local-mode collection-name derivation). Result: every
``nx store put`` carried an ORT runtime it never used, and ORT's telemetry
dispatcher raced interpreter teardown ("recursive_mutex lock failed",
Abort trap 6 — the ct17r crash on the fresh-install MVV).

Each case runs in a subprocess so the assertion sees a clean ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_CASES = {
    "pdf_extractor": "import nexus.pdf_extractor",
    "store_command": "import nexus.commands.store",
    "doc_indexer": "import nexus.doc_indexer",
    "indexer": "import nexus.indexer",
    "cli": "from nexus.cli import main",
    "fastembed_probe": (
        "import nexus.db.local_ef as m; m._fastembed_available()"
    ),
    "local_ef_construct_and_model_name": (
        "import os; os.environ['NX_LOCAL'] = '1';"
        "from nexus.db.local_ef import LocalEmbeddingFunction as L; L().model_name"
    ),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_import_does_not_load_onnxruntime_or_mineru(case: str) -> None:
    code = (
        f"{_CASES[case]}\n"
        "import sys\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('onnxruntime', 'mineru', 'fastembed', 'magika'))\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("LOADED=")][-1]
    assert line == "LOADED=", (
        f"{case}: {line.removeprefix('LOADED=')} imported eagerly — onnxruntime "
        "rides in on every nx command and races interpreter teardown (nexus-ct17r)"
    )
