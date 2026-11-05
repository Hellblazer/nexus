# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 3c (nexus-rfvyy.6.10) and its residuals nexus-rfvyy.2 (S-B) and nexus-rfvyy.4 (S-D):
the input-side external rules of ``--sensitive``.

* Census row 21: a sensitive PDF never reaches any MinerU URL, loopback or not, and no server is resolved
  (not an explicit URL, not the pid-file local server, not the default port).
* Q3: ``--sensitive`` into a Voyage collection needs ``--ack-external-embedder``; without it a local-mode
  document goes to the bge sibling of the same subject, for every prefix; cloud mode refuses.
* Residual S-B: a document already registered in a Voyage collection is not re-routed; it refuses.
* Census row 22 (residual S-D): the cloud tenant disclosure rides the acknowledgement.
* MCP ``store_put(sensitive=True)``: cloud mode refuses with the reason, local mode stores into the bge sibling.

The mode-lint exemption: the Voyage tokens in this file are collection-name labels; each test names its mode
(the default is local, ``cloud_mode`` is requested where cloud matters).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.sensitive_write as sw
from nexus.cli import main
from tests._module_seam import setattr_in

BGE = "bge-base-en-v15-768"
_CONTENT = "RDR-243 external-rule pin: the harbour lease memo, second draft. " * 12


class Stop(Exception):
    """Raised by a fake at the seam under watch, after it has recorded its arguments."""


def _reader():
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader()


def _md(tmp_path: Path, name: str = "ext.md") -> Path:
    p = tmp_path / name
    p.write_text(f"# {name}\n\n{_CONTENT}\n", encoding="utf-8")
    return p


def _pdf(tmp_path: Path, name: str = "ext.pdf") -> Path:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760), _CONTENT, fontsize=11)
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


# --------------------------------------------------------------------------- #
# Row 21: MinerU never receives a sensitive PDF.
# --------------------------------------------------------------------------- #

@pytest.fixture
def server_watch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Record every way a MinerU server could be resolved or contacted."""
    from nexus.pdf_extractor import PDFExtractor

    calls: dict[str, list] = {"resolve": [], "probe": [], "http": [], "autostart": []}
    monkeypatch.setattr("nexus.config.get_mineru_server_url", lambda *a, **k: calls["resolve"].append(1) or "http://127.0.0.1:8123")
    monkeypatch.setattr(
        PDFExtractor, "_probe_mineru_health", lambda self, url: calls["probe"].append(url) or (True, ""))
    # Patched on pdf_extractor's own binding of httpx, never on the httpx module the substrate shares.
    setattr_in(monkeypatch, "nexus.pdf_extractor", "httpx.post", lambda url, *a, **k: calls["http"].append(url) or (_ for _ in ()).throw(Stop()))
    setattr_in(monkeypatch, "nexus.pdf_extractor", "httpx.get", lambda url, *a, **k: calls["http"].append(url) or (_ for _ in ()).throw(Stop()))
    monkeypatch.setattr(
        "nexus.daemon.mineru_lifecycle.ensure_mineru_running", lambda *a, **k: calls["autostart"].append(1))
    return calls


@pytest.mark.parametrize("url", ["http://203.0.113.9:8010", "http://127.0.0.1:8123"], ids=["non-loopback", "loopback"])
def test_a_sensitive_extractor_never_resolves_or_contacts_a_mineru_server(
    server_watch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str,
) -> None:
    from nexus.pdf_extractor import PDFExtractor

    monkeypatch.setattr("nexus.config.get_mineru_server_url", lambda *a, **k: server_watch["resolve"].append(url) or url)
    sentinel = ("md", [], [])
    ext = PDFExtractor(sensitive=True)
    monkeypatch.setattr(ext, "_mineru_run_subprocess", lambda *a, **k: sentinel)
    pdf = _pdf(tmp_path)

    assert ext._mineru_server_available() is False
    assert ext._mineru_run_isolated(pdf, 0, 1) == sentinel
    assert ext._mineru_run_isolated(pdf, 0, 1, formula_enable=False) == sentinel
    with pytest.raises(RuntimeError, match="never sent to a MinerU server"):
        ext._mineru_run_via_server(pdf, 0, 1)
    assert server_watch == {"resolve": [], "probe": [], "http": [], "autostart": []}


def test_the_watch_sees_a_plain_extractor_reach_the_server(server_watch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The negative control of the pin above: with the sensitive flag off, the same watch records a resolution
    and a probe, so an empty record for a sensitive extractor means something."""
    from nexus.pdf_extractor import PDFExtractor

    ext = PDFExtractor()
    monkeypatch.setattr(ext, "_mineru_run_via_server", lambda *a, **k: ("served", [], []))
    assert ext._mineru_run_isolated(_pdf(tmp_path), 0, 1)[0] == "served"
    assert server_watch["resolve"] and server_watch["probe"]


def test_a_sensitive_extractor_with_no_subprocess_refuses_instead_of_falling_back(
    server_watch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from nexus.pdf_extractor import PDFExtractor

    ext = PDFExtractor(sensitive=True)

    def no_subprocess(*a, **k):
        raise RuntimeError("mineru is not installed")

    monkeypatch.setattr(ext, "_mineru_run_subprocess", no_subprocess)
    with pytest.raises(RuntimeError, match="mineru is not installed"):
        ext._mineru_run_isolated(_pdf(tmp_path), 0, 1)
    assert server_watch["resolve"] == [] and server_watch["probe"] == [] and server_watch["http"] == []


@pytest.mark.parametrize("streaming", ["never", "always"])
def test_index_pdf_builds_a_sensitive_extractor_on_both_extraction_paths(
    t2_service_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, streaming: str,
) -> None:
    """``nx index pdf --sensitive`` reaches both PDFExtractor constructions: the all-at-once chunker
    (``doc_indexer._pdf_chunks``) and the streaming pipeline (``pipeline_stages.extractor_loop``)."""
    from nexus.pdf_extractor import PDFExtractor

    built: list[bool] = []
    real_init = PDFExtractor.__init__

    def spy(self, *a, **k):
        built.append(bool(k.get("sensitive", False)))
        real_init(self, *a, **k)

    monkeypatch.setattr(PDFExtractor, "__init__", spy)
    out = CliRunner().invoke(
        main, ["index", "pdf", str(_pdf(tmp_path)), "--corpus", "rfvyy-ext", "--extractor", "pymupdf",
               "--streaming", streaming, "--sensitive"])
    assert out.exit_code == 0, out.output
    assert built and all(built), f"an extractor was built without the sensitive flag: {built}"


def test_a_sensitive_batch_does_not_probe_the_server(server_watch, t2_service_env, tmp_path: Path) -> None:
    d = tmp_path / "batch"
    d.mkdir()
    _pdf(d, "one.pdf")
    # pdf_stack for the mineru extractor may stop the run before the probe on a host without the stack;
    # the pin is that no resolution or probe happened either way.
    CliRunner().invoke(main, ["index", "pdf", "--dir", str(d), "--corpus", "rfvyy-ext", "--extractor", "mineru", "--sensitive"])
    assert server_watch["resolve"] == [] and server_watch["probe"] == [] and server_watch["http"] == []


# --------------------------------------------------------------------------- #
# Q3: the bge sibling, for every prefix.
# --------------------------------------------------------------------------- #

_VOYAGE = {
    "knowledge": "knowledge__rfvyy-ext__voyage-context-3__v1",
    "docs": "docs__rfvyy-ext__voyage-context-3__v1",
    "rdr": "rdr__rfvyy-ext__voyage-context-3__v1",
    "code": "code__rfvyy-ext__voyage-code-3__v1",
}


@pytest.mark.parametrize("prefix", sorted(_VOYAGE))
def test_the_bge_sibling_of_a_voyage_collection_has_the_same_prefix_and_subject(prefix: str) -> None:
    assert sw.bge_sibling(_VOYAGE[prefix]) == f"{prefix}__rfvyy-ext__{BGE}__v1"


@pytest.mark.parametrize("prefix", sorted(_VOYAGE))
def test_local_mode_routes_a_sensitive_write_without_the_ack_to_the_sibling(prefix: str) -> None:
    assert sw.resolve_sensitive_collection(_VOYAGE[prefix], ack=False, what="t") == f"{prefix}__rfvyy-ext__{BGE}__v1"


@pytest.mark.parametrize("prefix", sorted(_VOYAGE))
def test_the_ack_keeps_a_sensitive_write_on_voyage(prefix: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert sw.resolve_sensitive_collection(_VOYAGE[prefix], ack=True, what="t") == _VOYAGE[prefix]
    assert "sent to Voyage AI" in capsys.readouterr().err


@pytest.mark.parametrize("name", [f"knowledge__rfvyy-ext__{BGE}__v1", "docs__rfvyy-ext__minilm-l6-v2-384__v1"])
def test_a_local_embedder_collection_is_left_alone(name: str) -> None:
    assert sw.resolve_sensitive_collection(name, ack=False, what="t") == name


def test_an_unknown_embedder_reads_as_external_and_has_no_sibling() -> None:
    assert sw.is_external_embedder("knowledge__legacy") is True
    with pytest.raises(sw.ExternalEmbedderRefusedError, match="no bge sibling"):
        sw.resolve_sensitive_collection("knowledge__legacy", ack=False, what="t")


def test_a_voyage_posture_install_has_no_bge_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.local_embed_profile", lambda: "voyage")
    with pytest.raises(sw.ExternalEmbedderRefusedError, match="no local bge collection"):
        sw.resolve_sensitive_collection(_VOYAGE["knowledge"], ack=False, what="t")
    assert sw.resolve_sensitive_collection(_VOYAGE["knowledge"], ack=True, what="t") == _VOYAGE["knowledge"]


@pytest.mark.parametrize("prefix", sorted(_VOYAGE))
def test_store_put_lands_a_sensitive_note_in_the_bge_sibling(t2_service_env, prefix: str) -> None:
    out = CliRunner().invoke(
        main, ["store", "put", "-", "--title", f"rfvyy-route-{prefix}", "-c", _VOYAGE[prefix], "--sensitive"], input=_CONTENT)
    assert out.exit_code == 0, out.output
    sibling = f"{prefix}__rfvyy-ext__{BGE}__v1"
    assert f"→  {sibling}" in out.output
    (entry,) = _reader().find_by_title_exact(f"rfvyy-route-{prefix}")
    assert entry.sensitive is True and entry.physical_collection == sibling


def test_store_put_with_the_ack_keeps_the_voyage_collection(t2_service_env) -> None:
    seen: list[str] = []

    def fake_put(**kw):
        seen.append(kw["collection"])
        raise Stop

    with patch("nexus.catalog.note_write.put_note", fake_put):
        out = CliRunner().invoke(
            main, ["store", "put", "-", "--title", "rfvyy-ack", "-c", _VOYAGE["knowledge"], "--sensitive",
                   "--ack-external-embedder"], input=_CONTENT)
    assert isinstance(out.exception, Stop) and seen == [_VOYAGE["knowledge"]]


def test_index_md_routes_and_acknowledges_like_store_put(t2_service_env, tmp_path: Path) -> None:
    seen: list[str | None] = []

    def fake_index(path, corpus, *a, **kw):
        seen.append(kw.get("collection_name"))
        raise Stop

    md = _md(tmp_path)
    with patch("nexus.doc_indexer.index_markdown", fake_index):
        CliRunner().invoke(main, ["index", "md", str(md), "--corpus", "rfvyy-ext", "--collection", _VOYAGE["docs"], "--sensitive"])
        CliRunner().invoke(main, ["index", "md", str(md), "--corpus", "rfvyy-ext", "--collection", _VOYAGE["docs"], "--sensitive", "--ack-external-embedder"])
        CliRunner().invoke(main, ["index", "md", str(md), "--corpus", "rfvyy-ext", "--sensitive"])
    assert seen == [f"docs__rfvyy-ext__{BGE}__v1", _VOYAGE["docs"], f"docs__rfvyy-ext__{BGE}__v1"]


def test_index_pdf_routes_and_acknowledges_like_store_put(t2_service_env, tmp_path: Path) -> None:
    seen: list[str | None] = []

    def fake_index(path, corpus, *a, **kw):
        seen.append(kw.get("collection_name"))
        raise Stop

    pdf = _pdf(tmp_path)
    with patch("nexus.doc_indexer.index_pdf", fake_index):
        for extra in ([], ["--ack-external-embedder"]):
            CliRunner().invoke(
                main, ["index", "pdf", str(pdf), "--corpus", "rfvyy-ext", "--extractor", "pymupdf",
                       "--collection", _VOYAGE["knowledge"], "--sensitive", *extra])
    assert seen == [f"knowledge__rfvyy-ext__{BGE}__v1", _VOYAGE["knowledge"]]


def test_dt_index_routes_and_acknowledges_like_store_put(t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.commands import dt as dtmod

    md = _md(tmp_path, "dt.md")
    seen: list[str] = []
    monkeypatch.setattr(dtmod, "_gather_records", lambda **kw: [("U1", str(md))])

    def fake_record(uuid, path, **kw):
        seen.append(kw["collection"])
        return True, 1

    monkeypatch.setattr(dtmod, "_index_record", fake_record)
    for extra in ([], ["--ack-external-embedder"]):
        out = CliRunner().invoke(
            main, ["dt", "index", "--uuid", "U1", "--collection", _VOYAGE["docs"], "--sensitive", *extra])
        assert out.exit_code == 0, out.output
    assert seen == [f"docs__rfvyy-ext__{BGE}__v1", _VOYAGE["docs"]]


def test_ack_without_sensitive_is_a_usage_error(t2_service_env) -> None:
    out = CliRunner().invoke(main, ["store", "put", "-", "--title", "t", "-c", "s", "--ack-external-embedder"], input="x")
    assert out.exit_code == 2 and "only means something with --sensitive" in out.output


def test_the_ack_help_names_every_sibling_and_the_cloud_rule() -> None:
    text = sw.ack_help()
    assert f"__{BGE}__v1" in text
    for prefix in ("knowledge__", "docs__", "rdr__", "code__"):
        assert prefix in text
    assert "cloud mode" in text and "refuses" in text


# --------------------------------------------------------------------------- #
# Cloud mode: refuses without the ack on every verb, discloses with it (row 22).
# --------------------------------------------------------------------------- #

def _cloud_verbs(tmp_path: Path):
    md = _md(tmp_path, "cloud.md")
    pdf = _pdf(tmp_path, "cloud.pdf")
    return {
        "store put": ["store", "put", "-", "--title", "rfvyy-cloud-note", "-c", "rfvyy-cloud", "--sensitive"],
        "index md": ["index", "md", str(md), "--corpus", "rfvyy-cloud", "--sensitive"],
        "index pdf": ["index", "pdf", str(pdf), "--corpus", "rfvyy-cloud", "--extractor", "pymupdf", "--sensitive"],
        "dt index": ["dt", "index", "--uuid", "U-CLOUD", "--corpus", "rfvyy-cloud", "--sensitive"],
        "dt capture": ["dt", "capture", "--file", str(md), "--corpus", "rfvyy-cloud", "--sensitive"],
    }


@pytest.mark.parametrize("verb", ["store put", "index md", "index pdf", "dt index", "dt capture"])
def test_cloud_mode_refuses_a_sensitive_write_without_the_ack(
    t2_service_env, cloud_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.mcp_client import devonthink as dt_client

    touched: list[str] = []
    monkeypatch.setattr(dt_client, "available", lambda *a, **k: touched.append("dt") or True)
    monkeypatch.setattr(dt_client, "dt_import_file", lambda p: touched.append("dt_import") or "U-CLOUD")
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)
    registers: list[object] = []
    real_register = HttpCatalogClient.register
    monkeypatch.setattr(HttpCatalogClient, "register", lambda self, *a, **k: registers.append(k) or real_register(self, *a, **k))
    out = CliRunner().invoke(main, _cloud_verbs(tmp_path)[verb], input=_CONTENT)
    assert out.exit_code != 0, out.output
    assert "cloud mode" in out.output and "Voyage" in out.output and "--ack-external-embedder" in out.output, out.output
    assert registers == [] and touched == [], f"{verb} wrote or touched DEVONthink before refusing: {registers} {touched}"
    assert _reader().find_by_title_exact("rfvyy-cloud-note") == []


@pytest.mark.parametrize("verb", ["store put", "index md", "index pdf", "dt index", "dt capture"])
def test_cloud_mode_with_the_ack_prints_the_tenant_disclosure(
    t2_service_env, cloud_mode, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verb: str,
) -> None:
    """Row 22: the one-line disclosure is shown, on the verb, before the write begins. The write is stopped
    at the first seam after the front door, so the line is the whole observable."""
    from nexus.mcp_client import devonthink as dt_client

    monkeypatch.setattr(dt_client, "available", lambda *a, **k: False)  # dt capture stops here, after the front door
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [])
    monkeypatch.setattr("nexus.commands.store._t3", lambda **k: (_ for _ in ()).throw(Stop()))
    monkeypatch.setattr("nexus.doc_indexer.index_markdown", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    monkeypatch.setattr("nexus.doc_indexer.index_pdf", lambda *a, **k: (_ for _ in ()).throw(Stop()))
    out = CliRunner().invoke(main, [*_cloud_verbs(tmp_path)[verb], "--ack-external-embedder"], input=_CONTENT)
    assert "managed cloud tenant" in out.output and "Voyage AI" in out.output, out.output
    assert "refused: in cloud mode" not in out.output


def test_mcp_store_put_refuses_in_cloud_mode_with_the_reason(t2_service_env, cloud_mode) -> None:
    from nexus.mcp import core

    puts: list[object] = []
    with patch("nexus.catalog.note_write.put_note", lambda **kw: puts.append(kw)):
        out = str(core.store_put(content=_CONTENT, collection="rfvyy-cloud", title="rfvyy-mcp-cloud", sensitive=True))
    assert "cloud mode" in out and "Voyage" in out and "Nothing was written" in out, out
    assert puts == []


def test_mcp_store_put_in_local_mode_stores_into_the_bge_sibling(t2_service_env) -> None:
    from nexus.mcp import core

    out = str(core.store_put(
        content=_CONTENT, collection=_VOYAGE["knowledge"], title="rfvyy-mcp-routed", sensitive=True))
    sibling = f"knowledge__rfvyy-ext__{BGE}__v1"
    assert out.startswith("Stored:") and sibling in out and "embeds with Voyage AI" in out, out
    (entry,) = _reader().find_by_title_exact("rfvyy-mcp-routed")
    assert entry.sensitive is True and entry.physical_collection == sibling
