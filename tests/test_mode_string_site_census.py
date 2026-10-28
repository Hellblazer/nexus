# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census of the client sites that decide something from the engine's mode string
(RDR-210 Technical Design 10, Gap 8, bead nexus-yx833).

A ``dual`` engine reports a third value, so every site that branches on
``"onnx-local"``, ``== "voyage"`` or ``embedding_mode`` has to be reviewed for the
third value. This test finds those sites by AST (so comments and docstrings do not
count) and fails on any file whose count differs from the reviewed list: a new site
is a prompt to review it, not a lint to silence. Non-vacuity: it asserts it found at
least the reviewed total.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"

#: file (relative to src/nexus) -> (number of mode-string decision sites, why they are right
#: for a `dual` engine). A disposition per file, not just a count: swapping a reviewed site
#: for a different compare must change a number or a reason a reviewer reads.
REVIEWED: dict[str, tuple[int, str]] = {
    "commands/collection.py": (4, "the orphan finder decides on the engine's embedding_models; the mode string is display only"),
    "commands/daemon.py": (2, "prints /version embedding_mode in nx daemon status; display only, any string"),
    "commands/init.py": (1, "prints the probed embedding_mode; display only"),
    "commands/service_cmd.py": (1, "prints the capabilities embedding_mode; display only"),
    "corpus.py": (4, "the four write-model sites compare local_embed_profile() == 'voyage' (the profile, not an engine mode); counted by the voyage-compare pattern"),
    "daemon/storage_service_daemon.py": (2, "a log field naming onnx-local when no key is plumbed; Phase 3 gates the key plumbing on local_embed_profile() (nexus-wypwq)"),
    "db/http_engine_status.py": (2, "formats the /v1/status mode into one line; the 'voyage' branch picks wording only, dual prints through"),
    "db/http_vector_client.py": (6, "onnx-local keeps the engine-wide memory cap; voyage and dual decide by the collection's model (_collection_is_bge); the memo and probe seeding accept dual"),
    "db/managed_endpoint.py": (5, "the capabilities dataclass carries embedding_mode verbatim; no decision is made on it"),
    "errors.py": (1, "serving_mode is a diagnostic in the profile-mismatch error; the remedy is chosen by the collection's family"),
    "health.py": (8, "the Embedding family doctor row (nexus-wypwq): voyage mode only suppresses the client-profile fallback (the engine serves Voyage whatever the client intends); dual and onnx-local are read against the profile, and the 'voyage' compare is the profile (bge or voyage), not an engine mode; display and warning only. nexus-njn1a added four embedding_mode reads that only render the not-applicable and both-families detail text, and one embedding_mode == 'voyage' that picks the not-applicable reason wording; no decision changes for dual"),
    "mcp_infra.py": (3, "a warm-up probe step named embedding_mode; no decision"),
    "reembed_job.py": (2, "both compare a target MODEL's family (model_family(...) == 'voyage': the billed cap and the plan's cost lines), never the engine's embedding_mode; a dual engine reaches them by the target model it was asked for"),
    "search_engine.py": (3, "the threshold gate is embedding_mode() in _THRESHOLD_ENGINE_MODES (voyage and dual; P1.5, nexus-hf29s); _engine_is_dual reads the same embedding_mode() so a model-less name gets no threshold on a dual engine (nexus-ndr0t S3); the rest is the serving_mode diagnostic"),
}


def _sites(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "onnx-local":
            n += 1
        elif isinstance(node, ast.Compare) and any(
            isinstance(c, ast.Constant) and c.value == "voyage" for c in node.comparators
        ) and any(isinstance(o, (ast.Eq, ast.NotEq)) for o in node.ops):
            n += 1
        elif isinstance(node, ast.Attribute) and node.attr == "embedding_mode":
            n += 1
        elif isinstance(node, ast.Name) and node.id == "embedding_mode":
            n += 1
        elif isinstance(node, ast.Constant) and node.value == "embedding_mode":
            n += 1
        elif isinstance(node, ast.keyword) and node.arg == "embedding_mode":
            n += 1
    return n


def _census() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        count = _sites(ast.parse(path.read_text(encoding="utf-8")))
        if count:
            found[str(path.relative_to(SRC))] = count
    return found


def test_every_mode_string_site_is_on_the_reviewed_list() -> None:
    found = _census()
    reviewed = {f: n for f, (n, _why) in REVIEWED.items()}
    assert sum(found.values()) >= sum(reviewed.values()), "census found fewer sites than reviewed: vacuous"
    drift = {
        f: (reviewed.get(f, 0), found.get(f, 0))
        for f in sorted(set(reviewed) | set(found))
        if reviewed.get(f, 0) != found.get(f, 0)
    }
    assert not drift, (
        "mode-string decision sites changed (file: reviewed, found). Review each new site "
        f"for the `dual` value (RDR-210 TD10), then update REVIEWED: {drift}"
    )


def test_every_reviewed_file_carries_a_reason() -> None:
    thin = [f for f, (_n, why) in REVIEWED.items() if len(why.strip()) < 25]
    assert not thin, f"a reviewed file needs a disposition, not a bare count: {thin}"
