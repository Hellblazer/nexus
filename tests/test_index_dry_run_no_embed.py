# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx index pdf --dry-run`` embeds nothing (RDR-155 P4b, decision 3 site 2).

The dry-run used to build a ``MiniLMDirectEmbeddingFunction``, embed every
chunk with it, and then read back only ``documents`` and ``metadatas`` for the
preview — the vectors were computed and discarded unread. That made the local
384d ONNX model a hard dependency of a code path whose entire output is a chunk
count, a page range, and a text preview.

So the dry-run now passes the same empty-placeholder ``embed_fn`` the indexer
already uses for service mode (``indexer.py``: ``lambda texts: [[]] *
len(texts)``) and attaches a fail-loud sentinel EF to the throwaway handle.
The sentinel is the point: "nothing embeds" becomes a property the code
ENFORCES rather than one a reader has to re-derive, so a future change that
adds a query to the preview fails loudly instead of silently loading a model.

Both tests below are written to FAIL against the pre-change implementation:
the first because it constructed the ONNX EF unconditionally, the second
because its ``embed_fn`` returned real vectors.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main

pytestmark = pytest.mark.usefixtures("cloud_mode")

PDF_RESULT_CHUNKS = 3


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_pdf(home: Path) -> Path:
    p = home / "doc.pdf"
    p.write_bytes(b"fake pdf")
    return p


def _run_dry_run(runner: CliRunner, pdf: Path) -> tuple[object, dict]:
    """Invoke ``--dry-run`` with ``index_pdf`` stubbed; capture its kwargs.

    ``index_pdf`` does real extraction (Docling), which is integration-scope.
    The contract under test is what the dry-run HANDS it, so stub it and
    inspect the call.
    """
    captured: dict = {}

    def _fake_index_pdf(path, **kwargs):
        captured.update(kwargs)
        return PDF_RESULT_CHUNKS

    # The command wraps ``doc_indexer.index_pdf`` in a function-local closure
    # (credential-error -> ClickException), so patch the SOURCE symbol.
    with patch("nexus.doc_indexer.index_pdf", _fake_index_pdf):
        result = runner.invoke(main, ["index", "pdf", str(pdf), "--dry-run"])
    return result, captured


def test_dry_run_does_not_load_the_local_onnx_model(
    runner: CliRunner, fake_pdf: Path,
) -> None:
    """The dry-run must succeed with the MiniLM EF made unconstructable.

    Falsification, not description: patching the ONNX EF to raise on
    construction reproduces "the model is unavailable". The pre-change
    dry-run built it eagerly and would die here; the current one never
    touches it.
    """
    boom = MagicMock(side_effect=AssertionError(
        "--dry-run must not construct the local ONNX embedding function",
    ))
    with patch("nexus.db.minilm_direct.MiniLMDirectEmbeddingFunction", boom):
        result, _ = _run_dry_run(runner, fake_pdf)

    assert result.exit_code == 0, result.output
    assert boom.call_count == 0
    assert "no cloud write" in result.output


def test_dry_run_embed_fn_returns_empty_placeholders(
    runner: CliRunner, fake_pdf: Path,
) -> None:
    """The embed_fn handed to index_pdf produces no vectors.

    Mirrors the service-mode placeholder contract in ``indexer.py``. Asserted
    on the actual callable rather than on a log line, so a regression to real
    embedding fails here even if the output text is unchanged.
    """
    result, captured = _run_dry_run(runner, fake_pdf)
    assert result.exit_code == 0, result.output

    embed_fn = captured.get("embed_fn")
    assert embed_fn is not None, f"index_pdf got no embed_fn: {captured.keys()}"

    embeddings, model = embed_fn(["alpha", "beta"], "voyage-context-3")
    assert embeddings == [[], []], (
        f"--dry-run must hand index_pdf empty placeholders; got {embeddings}"
    )
    assert model == "voyage-context-3", "the reported target model must pass through"


def test_dry_run_handle_refuses_to_embed(
    runner: CliRunner, fake_pdf: Path,
) -> None:
    """The throwaway T3 handle's EF raises if anything tries to use it.

    This is what makes "nothing embeds" enforced rather than incidental:
    ``T3Database.get_or_create_collection`` resolves an EF eagerly, so without
    an explicit sentinel the dry-run would build a REAL one (a Voyage client or
    a local model load) purely to satisfy the plumbing.
    """
    _, captured = _run_dry_run(runner, fake_pdf)
    t3 = captured.get("t3")
    assert t3 is not None, f"index_pdf got no t3 handle: {captured.keys()}"

    ef = t3._ef_override
    assert ef is not None, "the dry-run handle must pin a sentinel EF, not lazy-build one"
    with pytest.raises(AssertionError, match="dry-run"):
        ef(["anything"])


def test_dry_run_fires_no_post_store_hooks(
    runner: CliRunner, fake_pdf: Path,
) -> None:
    """A dry run passes an EMPTY hook registry, not ``None``.

    ``None`` makes ``index_pdf`` install the defaults — taxonomy assign,
    catalog manifest write, aspect-extraction enqueue — so a command that
    prints "(no cloud write)" fired two side-effecting hooks against a
    throwaway collection. Asserted on the registry's own chains so the test
    fails if a future default is added and silently reaches the dry run.
    """
    _, captured = _run_dry_run(runner, fake_pdf)
    hooks = captured.get("hooks")
    assert hooks is not None, (
        "index_pdf got hooks=None and will install the DEFAULT hooks"
    )
    assert hooks._batch == [] and hooks._document == [] and hooks._single == [], (
        f"dry-run registry must be empty; got batch={hooks._batch} "
        f"document={hooks._document} single={hooks._single}"
    )
