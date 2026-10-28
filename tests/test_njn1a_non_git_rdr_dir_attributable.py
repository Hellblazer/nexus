# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-njn1a (7.88.0 shakedown, finding 12): `nx index rdr <dir>` on a directory outside any git
repo gave `nx enrich aspects` nothing to attribute.

The documents registered under a curator owner, which carries no repo_root, with a path relative
to the directory and no source_uri. `nx enrich aspects` then logged
`aspect_uri_for_relative_without_root` and skipped every document as `unattributable_identity`.
The registration now stamps the file's absolute `file://` source_uri (the directory is the root),
and a re-run backfills a document an earlier run registered without one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog

_COLLECTION = "rdr__1-1__test-embed-768__v1"
_SEQ = [0]


def _next() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


def _non_git_rdr_dir(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / f"plain-{_next()}"
    (root / "docs" / "rdr").mkdir(parents=True)
    f = root / "docs" / "rdr" / "rdr-901-example.md"
    f.write_text("# RDR-901\n\nbody\n")
    return root, f


def _register(f: Path, root: Path, corpus: str) -> str:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    return _register_or_lookup_doc_id(
        f, corpus, content_type="rdr", physical_collection=_COLLECTION, base_path=root,
    )


@pytest.mark.t2_tenant
def test_a_document_in_a_non_git_rdr_dir_carries_its_absolute_source_uri(tmp_path):
    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    doc_id = _register(f, root, f"plain-{_next()}")
    assert doc_id
    doc = cat.resolve(doc_id)
    assert doc.file_path == "docs/rdr/rdr-901-example.md", doc.file_path
    assert doc.source_uri == f.resolve().as_uri(), doc.source_uri


@pytest.fixture
def aspects_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The isolated T2 db and fake LLM backend `nx enrich aspects` needs (the setup
    tests/test_enrich_aspects.py uses); extraction itself is faked at the extract_aspects boundary."""
    db_path = tmp_path / "enrich_aspects.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
    monkeypatch.setattr("nexus.mcp_infra.default_db_path", lambda: db_path)
    monkeypatch.setattr("nexus.operators.llm_backend.resolve_backend_name", lambda req: "cli")
    monkeypatch.setattr("nexus.commands.enrich._claude_on_path", lambda: True)
    return db_path


@pytest.mark.t2_tenant
def test_enrich_aspects_attributes_a_non_git_rdr_document(tmp_path, aspects_env, monkeypatch):
    """The user-visible half: after `nx index rdr` on a non-git directory, `nx enrich aspects`
    stores a row carrying the document's source_uri instead of skipping it as
    `unattributable_identity`. The fake extractor leaves source_uri empty, the shape the real one
    produces for a relative path with no repo_root (`aspect_uri_for_relative_without_root`)."""
    import dataclasses as dc
    from datetime import UTC, datetime

    from click.testing import CliRunner

    from nexus.aspect_extractor import AspectRecord
    from nexus.commands.enrich import enrich
    from nexus.db.t2 import T2Database

    root, f = _non_git_rdr_dir(tmp_path)
    doc_id = _register(f, root, f"plain-{_next()}")
    assert doc_id

    def fake_extract(content, source_path, collection, **_kw):
        rec = AspectRecord(
            collection=collection, source_path=source_path, problem_formulation="P", proposed_method="M",
            experimental_datasets=[], experimental_baselines=[], experimental_results="R", extras={},
            confidence=0.9, extracted_at=datetime.now(UTC).isoformat(), model_version="m",
            extractor_name="rdr-frontmatter-v1", source_uri="", doc_id=doc_id,
        )
        return dc.replace(rec, source_uri="")

    monkeypatch.setattr("nexus.aspect_extractor.extract_aspects", fake_extract)
    result = CliRunner().invoke(enrich, ["aspects", _COLLECTION, "--validate-sample", "0"])
    assert result.exit_code == 0, result.output
    assert "unattributable_identity" not in result.output, result.output
    assert "1 extracted" in result.output, result.output
    with T2Database(aspects_env) as db:
        rows = db.document_aspects.list_by_collection(_COLLECTION)
    assert len(rows) == 1
    assert rows[0].source_uri == f.resolve().as_uri(), rows[0].source_uri


@pytest.mark.t2_tenant
def test_a_rerun_backfills_a_document_registered_without_a_source_uri(tmp_path):
    root, f = _non_git_rdr_dir(tmp_path)
    cat = ActiveCatalog()
    corpus = f"plain-{_next()}"
    doc_id = _register(f, root, corpus)
    # an earlier release registered it with no source_uri
    cat.update(cat.resolve(doc_id).tumbler, source_uri="")
    assert cat.resolve(doc_id).source_uri == ""
    again = _register(f, root, corpus)
    assert again == doc_id, "a re-run must not mint a second document"
    assert cat.resolve(doc_id).source_uri == f.resolve().as_uri()
