# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assert the bge-768 model-dependent Java gates actually ran (not skipped).

nexus-zbwgb: ``Bge768ParityTest`` (the RDR-160 go/no-go parity gate) and
``Bge768BatchCompositionTest`` (the nexus-zu4ma sub-batch equivalence gate)
both load the ~416MB standard fp32 bge ONNX export and SKIP via JUnit
``Assumptions`` when it is absent locally. A JUnit skip does not fail the
Maven build, so without this assert a "Testcontainers all green" phase-gate
criterion is satisfiable with both gates permanently skipped — the identical
risk class ``scripts/assert_rerank_inference_ran.py`` closes for the
cross-encoder ONNX (RDR-188 P1), and this script mirrors its shape exactly.
CI's ``prime-bge-onnx`` action (``.github/workflows/service-ci.yml``)
provisions the model unconditionally, so on CI both gates always execute;
this script exists to catch a future regression (a dropped priming step)
loudly instead of leaving surefire at a silent 0/0/0/0.

Usage::

    python scripts/assert_bge_gates_ran.py \
        service/target/surefire-reports/TEST-dev.nexus.service.Bge768ParityTest.xml \
        service/target/surefire-reports/TEST-dev.nexus.service.vectors.Bge768BatchCompositionTest.xml

Exits non-zero when either report is missing, or either suite recorded zero
testcases or any skip.
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET


def _parse(path: str, label: str) -> ET.Element:
    if not os.path.exists(path):
        raise SystemExit(
            f"{label} Surefire report not found at '{path}' — the class never "
            "executed (renamed? excluded? mvn crashed before the test phase?). "
            "The bge-768 gate is NOT covered by this run."
        )
    return ET.parse(path).getroot()


def _assert_ran_no_skips(path: str, label: str) -> int:
    root = _parse(path, label)
    cases = list(root.iter("testcase"))
    if not cases:
        raise SystemExit(f"{label} recorded zero testcases — vacuous run.")
    skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
    if skipped:
        raise SystemExit(
            f"{label} SKIPPED {skipped} — the standard fp32 bge ONNX was not "
            "provisioned. In CI the prime-bge-onnx action guarantees it, so "
            "any skip here is vacuous: the bge gate did not run. Provision via "
            "`nx init --service` (RDR-160 P3), or check the CI priming step."
        )
    return len(cases)


def main(parity_xml: str, batch_composition_xml: str) -> None:
    parity_count = _assert_ran_no_skips(parity_xml, "Bge768ParityTest")
    batch_count = _assert_ran_no_skips(batch_composition_xml, "Bge768BatchCompositionTest")
    print(
        f"bge-768 gates ran: Bge768ParityTest ({parity_count} testcases) + "
        f"Bge768BatchCompositionTest ({batch_count} testcases), zero vacuous skips."
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
