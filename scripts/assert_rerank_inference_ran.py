# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assert the cross-encoder REAL-inference rerank tests actually ran (not skipped).

RDR-188 P1 critic finding (T2 nexus/rdr188-p1-substantive-critique-2026-07-22):
``CrossEncoderRerankerInferenceTest`` and the integration suite's cross-encoder
success path gate on the ~91MB ms-marco ONNX via JUnit ``Assumptions`` — correct
locally, but a JUnit skip does not fail the Maven build, so without this assert
the "Testcontainers all green" phase-gate criterion is satisfiable with the
real-inference coverage permanently skipped (the exact bge/RDR-160 CA-1 risk
class: an ONNX export onnxruntime-java cannot run must be caught by CI, not by
the first provisioned user). ``prime-crossencoder-onnx`` provisions the
artifact in CI; this script proves the priming actually reached the tests.
Mirrors ``assert_rehearsal_ran.py`` (gates-scripted-not-ambient).

Usage::

    python scripts/assert_rerank_inference_ran.py \
        service/target/surefire-reports/TEST-dev.nexus.service.vectors.CrossEncoderRerankerInferenceTest.xml \
        service/target/surefire-reports/TEST-dev.nexus.service.RerankStageIntegrationTest.xml

Exits non-zero when either report is missing, the inference suite recorded
zero testcases or ANY skip, or the integration suite's
``crossEncoderPathReranksWithoutAnyVoyageKey`` testcase did not run.
(``crossEncoderAbsentModelDegradesLoud`` is the complementary twin and is
EXPECTED to skip when the artifact is primed — it is not counted here.)
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
            "The RDR-188 cross-encoder real-inference gate is NOT covered by this run."
        )
    return ET.parse(path).getroot()


def main(inference_xml: str, integration_xml: str) -> None:
    inf = _parse(inference_xml, "CrossEncoderRerankerInferenceTest")
    cases = list(inf.iter("testcase"))
    if not cases:
        raise SystemExit("CrossEncoderRerankerInferenceTest recorded zero testcases — vacuous run.")
    skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
    if skipped:
        raise SystemExit(
            f"CrossEncoderRerankerInferenceTest SKIPPED {skipped} — the ms-marco ONNX "
            "was not provisioned. In CI the prime-crossencoder-onnx action guarantees "
            "it, so any skip here is vacuous: the real-inference gate did not run."
        )

    integ = _parse(integration_xml, "RerankStageIntegrationTest")
    target = "crossEncoderPathReranksWithoutAnyVoyageKey"
    tc = [c for c in integ.iter("testcase") if c.get("name") == target]
    if not tc:
        raise SystemExit(
            f"RerankStageIntegrationTest has no '{target}' testcase — renamed without "
            "updating this assert? The cross-encoder success-path gate is unverified."
        )
    if tc[0].find("skipped") is not None:
        raise SystemExit(
            f"RerankStageIntegrationTest.{target} SKIPPED — the cross-encoder "
            "success path never ran against the primed artifact."
        )
    print(
        f"rerank inference gate ran: {len(cases)} inference testcases + "
        f"integration '{target}', zero vacuous skips."
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])
