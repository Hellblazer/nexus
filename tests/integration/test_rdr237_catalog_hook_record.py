# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.3): the PDF catalog hook's extraction record against the live service
catalog. Moved out of tests/test_catalog_pdf_hook.py so the unit suite's tenant-saving ratchet
(tests/storage_touch_ceiling.toml) does not carry them: every test here registers and reads a real
catalog document, so each mints a tenant, and the integration job runs them."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, count_documents, only_document

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim ``catalog_path()`` at the dir ``_make_catalog`` initialises
    (nexus-aqbrk) — ActiveCatalog resolves through the same factories the
    PDF hook uses, and the SQLite arm of those reads ``catalog_path()``."""
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> tuple[Path, ActiveCatalog]:
    # nexus-i711w: the local Catalog.init that used to run here died with
    # the local catalog; the PDF hook registers into the live service catalog.
    catalog_dir = tmp_path / "catalog"
    return catalog_dir, ActiveCatalog()


_DEGRADED = {"extraction_method": "mineru+degraded", "extraction_pages": {"mineru-no-formula": [3, 7], "pymupdf-text": [12]}}
_CLEAN = {"extraction_method": "mineru", "extraction_pages": {}}


def _meta() -> dict:
    return dict(only_document().meta)


class TestPdfCatalogHookExtractionRecord:
    """Both branches of the hook merge ``extraction_method`` and ``extraction_pages`` into the
    document's ``meta``; ``extraction_pages`` is written on every extraction, ``{}`` when clean."""

    def test_register_branch_records_method_and_pages(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        _catalog_pdf_hook(
            pdf_path=Path("/data/papers/rdr237-register.pdf"), collection_name="docs__v1",
            title="Paper", extraction=_DEGRADED,
        )
        assert _meta()["extraction_method"] == "mineru+degraded"
        assert _meta()["extraction_pages"] == _DEGRADED["extraction_pages"]

    def test_update_branch_records_method_and_pages(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-update.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper")  # the pre-flight shape
        assert "extraction_pages" not in _meta()
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        assert count_documents() == 1
        assert _meta()["extraction_method"] == "mineru+degraded"
        assert _meta()["extraction_pages"] == _DEGRADED["extraction_pages"]

    def test_a_clean_extraction_writes_an_empty_map_on_both_branches(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-clean.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_CLEAN)  # register
        assert _meta()["extraction_pages"] == {} and _meta()["extraction_method"] == "mineru"
        # Not absent: an absent key could not clear a map on the update below.
        assert "extraction_pages" in _meta()
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_CLEAN)  # update
        assert _meta()["extraction_pages"] == {} and _meta()["extraction_method"] == "mineru"

    def test_a_clean_re_extract_clears_an_old_map(self, tmp_path, monkeypatch):
        """The service merges meta key by key and never removes one; the clear works only because
        the hook writes the whole ``extraction_pages`` value, ``{}`` included."""
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-clear.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        assert _meta()["extraction_pages"]
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_CLEAN)
        assert _meta()["extraction_pages"] == {}
        assert _meta()["extraction_method"] == "mineru"

    def test_a_degraded_re_extract_replaces_the_old_map_whole(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-replace.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        _catalog_pdf_hook(
            pdf_path=p, collection_name="docs__v1", title="Paper",
            extraction={"extraction_method": "mineru+degraded", "extraction_pages": {"unextracted": [2]}},
        )
        assert _meta()["extraction_pages"] == {"unextracted": [2]}, "rungs of the old map do not linger"

    def test_a_registration_that_resolves_onto_an_existing_row_still_records(self, tmp_path, monkeypatch):
        """The engine's register returns an existing row untouched (its idempotency legs do not
        merge ``meta``), so the hook follows with an update. Forced here by hiding the row from the
        two lookups that send the hook down its update branch."""
        from nexus.catalog import factory as _factory
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-resolve.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper")  # the row exists
        real_make_reader = _factory.make_catalog_reader

        def blind_reader():
            reader = real_make_reader()

            class _Blind:
                def __getattr__(self, name):
                    return getattr(reader, name)

                def by_source_uri(self, *_a, **_kw):
                    return None

                def by_file_path(self, *_a, **_kw):
                    return None

            return _Blind()

        monkeypatch.setattr(_factory, "make_catalog_reader", blind_reader)
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        assert count_documents() == 1, "register resolved onto the row; no second document"
        assert _meta()["extraction_pages"] == _DEGRADED["extraction_pages"]
        assert _meta()["extraction_method"] == "mineru+degraded"

    def test_without_an_extraction_nothing_is_written_to_meta(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-none.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper")
        assert "extraction_pages" not in _meta() and "extraction_method" not in _meta()

    def test_an_unnamed_method_does_not_overwrite_a_recorded_one(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-noname.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        _catalog_pdf_hook(
            pdf_path=p, collection_name="docs__v1", title="Paper",
            extraction={"extraction_method": "", "extraction_pages": {}},
        )
        assert _meta()["extraction_method"] == "mineru+degraded"

    def test_legacy_values_stay_readable_and_a_new_extraction_replaces_them(self, tmp_path, monkeypatch):
        """``docling``, ``mineru+docling-degraded`` and ``pymupdf_normalized`` are written by older
        versions and need no migration; the hook neither reads nor rejects them."""
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-legacy.pdf")
        for legacy in ("docling", "mineru+docling-degraded", "pymupdf_normalized"):
            _catalog_pdf_hook(
                pdf_path=p, collection_name="docs__v1", title="Paper",
                extraction={"extraction_method": legacy, "extraction_pages": {}},
            )
            assert _meta()["extraction_method"] == legacy



class TestPdfCatalogHookNamesTheRunsThatRecordNothing:
    """The exits that touch the live catalog or its audit rows; the others stay in the unit file."""

    def _warnings(self, cap, reason):
        return [e for e in cap if e.get("event") == "catalog_pdf_extraction_not_recorded" and e.get("reason") == reason]

    def test_ephemeral_path_registration_refused(self, tmp_path, monkeypatch):
        from structlog.testing import capture_logs

        import nexus.repo_identity as repo_identity
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        monkeypatch.setattr(repo_identity, "should_skip_ephemeral_registration", lambda *a, **k: True)
        with capture_logs() as cap:
            _catalog_pdf_hook(
                pdf_path=Path("/data/papers/rdr237-ephemeral.pdf"), collection_name="docs__v1",
                title="Paper", extraction=_DEGRADED,
            )
        (w,) = self._warnings(cap, "ephemeral_path")
        assert w["file_path"].endswith("rdr237-ephemeral.pdf")
        assert count_documents() == 0, "nothing was registered"

    def test_a_hook_failure_names_the_document(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from structlog.testing import capture_logs

        from nexus.catalog import factory as _factory
        from nexus.pipeline_stages import _catalog_pdf_hook

        _make_catalog(tmp_path)
        p = Path("/data/papers/rdr237-fail.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper")
        real_writer = _factory.make_catalog_writer()

        class _RefusingWriter:
            def __getattr__(self, name):
                return getattr(real_writer, name)

            def update(self, *_a, **_kw):
                raise RuntimeError("engine refused the write")

        with capture_logs() as cap, patch.object(_factory, "make_catalog_writer", return_value=_RefusingWriter()):
            _catalog_pdf_hook(pdf_path=p, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        (w,) = self._warnings(cap, "hook_failed")
        assert w["file_path"].endswith("rdr237-fail.pdf")
        (failed,) = [e for e in cap if e.get("event") == "catalog_pdf_hook_failed"]
        assert failed["file_path"].endswith("rdr237-fail.pdf"), "the pre-existing failure warning names it too"
        assert "extraction_pages" not in _meta()

    def test_a_path_that_cannot_resolve_still_names_the_document(self, tmp_path, monkeypatch):
        """``Path.resolve`` raising (a loop, a permission) must not leave the failure handler with
        an unbound name: the hook logs, records the audit row and returns."""
        from unittest.mock import MagicMock

        from structlog.testing import capture_logs

        from nexus.pipeline_stages import _catalog_pdf_hook

        pdf = MagicMock(spec=Path)
        pdf.resolve.side_effect = OSError("symlink loop")
        pdf.__str__.return_value = "/data/papers/rdr237-loop.pdf"
        with capture_logs() as cap:
            _catalog_pdf_hook(pdf_path=pdf, collection_name="docs__v1", title="Paper", extraction=_DEGRADED)
        (w,) = self._warnings(cap, "hook_failed")
        assert w["file_path"] == "/data/papers/rdr237-loop.pdf"
