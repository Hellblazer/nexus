# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 3b (nexus-rfvyy.6.9): ``--sensitive`` on the five write verbs and MCP ``store_put``.

Every test runs a real verb against the real engine substrate and reads the catalog row back. The
flag must be on the row BEFORE the post-store chains fire, a re-put or re-index of a document that
already exists must raise it too (the engine's ``registerDocument`` writes nothing on a match for a
file path, and the note re-put branches never register), and an engine that cannot hold the flag
must stop the write before the register call.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main

_CONTENT = "RDR-243 write-verb pin: the lease renewal memo for the harbour office. " * 12


def _reader():
    from nexus.catalog.factory import make_catalog_reader

    return make_catalog_reader()


def _note_entry(title: str):
    rows = _reader().find_by_title_exact(title)
    assert len(rows) == 1, f"expected one live document titled {title!r}, got {rows}"
    return rows[0]


def _file_entry(path: Path):
    rows = _reader().find_all_by_file_path(str(path.resolve()))
    assert len(rows) == 1, f"expected one live document for {path}, got {rows}"
    return rows[0]


def _md(tmp_path: Path, name: str = "memo.md", body: str = _CONTENT) -> Path:
    p = tmp_path / name
    p.write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    return p


def _pdf(tmp_path: Path, name: str = "memo.pdf") -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 760), _CONTENT, fontsize=11)
    p = tmp_path / name
    doc.save(str(p))
    doc.close()
    return p


@pytest.fixture
def env(t2_service_env, monkeypatch: pytest.MonkeyPatch):
    # The CLI verbs ask for the probe through the shared client; the real engine answers true.
    return t2_service_env


# --------------------------------------------------------------------------- #
# The flag lands on the row: a fresh document and an existing one, per verb.
# --------------------------------------------------------------------------- #

def _store_put(title: str, extra: list[str] | None = None, subject: str = "rfvyy-writeverbs"):
    return CliRunner().invoke(
        main, ["store", "put", "-", "--title", title, "-c", subject, *(extra or [])], input=_CONTENT)


def test_store_put_marks_a_fresh_note_sensitive(env) -> None:
    out = _store_put("rfvyy-note-fresh", ["--sensitive"])
    assert out.exit_code == 0, out.output
    assert _note_entry("rfvyy-note-fresh").sensitive is True


def test_store_put_without_the_flag_leaves_the_note_plain(env) -> None:
    out = _store_put("rfvyy-note-plain")
    assert out.exit_code == 0, out.output
    assert _note_entry("rfvyy-note-plain").sensitive is False


def test_store_put_raises_the_flag_on_an_existing_note(env) -> None:
    """The re-put branch reconciles onto the live row and never registers; the flag still has to move."""
    assert _store_put("rfvyy-note-reput").exit_code == 0
    assert _note_entry("rfvyy-note-reput").sensitive is False
    out = _store_put("rfvyy-note-reput", ["--sensitive"])
    assert out.exit_code == 0, out.output
    assert _note_entry("rfvyy-note-reput").sensitive is True


def test_a_later_plain_put_does_not_lower_the_flag(env) -> None:
    assert _store_put("rfvyy-note-sticky", ["--sensitive"]).exit_code == 0
    assert _store_put("rfvyy-note-sticky").exit_code == 0
    assert _note_entry("rfvyy-note-sticky").sensitive is True


def test_index_md_marks_a_fresh_document_and_an_indexed_one(env, tmp_path: Path) -> None:
    fresh = _md(tmp_path, "fresh.md")
    out = CliRunner().invoke(main, ["index", "md", str(fresh), "--corpus", "rfvyy", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(fresh).sensitive is True

    old = _md(tmp_path, "old.md", body=_CONTENT + " first draft")
    assert CliRunner().invoke(main, ["index", "md", str(old), "--corpus", "rfvyy"]).exit_code == 0
    assert _file_entry(old).sensitive is False
    old.write_text(old.read_text() + "\nsecond draft\n", encoding="utf-8")
    out = CliRunner().invoke(main, ["index", "md", str(old), "--corpus", "rfvyy", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(old).sensitive is True


def test_index_pdf_marks_a_fresh_document_and_an_indexed_one(env, tmp_path: Path) -> None:
    fresh = _pdf(tmp_path, "fresh.pdf")
    out = CliRunner().invoke(
        main, ["index", "pdf", str(fresh), "--corpus", "rfvyy", "--extractor", "pymupdf", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(fresh).sensitive is True

    old = _pdf(tmp_path, "old.pdf")
    assert CliRunner().invoke(
        main, ["index", "pdf", str(old), "--corpus", "rfvyy", "--extractor", "pymupdf"]).exit_code == 0
    assert _file_entry(old).sensitive is False
    out = CliRunner().invoke(
        main, ["index", "pdf", str(old), "--corpus", "rfvyy", "--extractor", "pymupdf", "--force", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(old).sensitive is True


def test_index_pdf_dir_marks_every_document(env, tmp_path: Path) -> None:
    d = tmp_path / "batch"
    d.mkdir()
    a, b = _pdf(d, "a.pdf"), _pdf(d, "b.pdf")
    out = CliRunner().invoke(
        main, ["index", "pdf", "--dir", str(d), "--corpus", "rfvyy", "--extractor", "pymupdf", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(a).sensitive is True and _file_entry(b).sensitive is True


def _dt_patches(monkeypatch: pytest.MonkeyPatch, records: list[tuple[str, str]]):
    """The DEVONthink seams: record enumeration and the record facts and stamp, which need the app."""
    from nexus.commands import dt as dtmod

    monkeypatch.setattr(dtmod, "_gather_records", lambda **kw: records)
    monkeypatch.setattr(dtmod, "_dt_record_facts", lambda uuid: {"name": f"dt-{uuid}", "page_count": 0, "url": "", "year": 0})
    monkeypatch.setattr(dtmod, "_stamp_dt_uri_on_entry", lambda *a, **k: True)
    monkeypatch.setattr(dtmod, "_dt_uri_is_catalogued", lambda uuid: False)
    monkeypatch.setattr(dtmod, "_is_darwin", lambda: True)


def test_dt_index_marks_a_file_backed_record(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = _md(tmp_path, "dt-record.md")
    pdf = _pdf(tmp_path, "dt-record.pdf")
    _dt_patches(monkeypatch, [("U-MD", str(md)), ("U-PDF", str(pdf))])
    out = CliRunner().invoke(
        main, ["dt", "index", "--uuid", "U-MD", "--uuid", "U-PDF", "--corpus", "rfvyy", "--extractor", "pymupdf",
               "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(md).sensitive is True
    assert _file_entry(pdf).sensitive is True


def test_dt_capture_hands_the_flag_to_the_index_step(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = _md(tmp_path, "dt-captured.md")
    _dt_patches(monkeypatch, [("U-CAP", str(md))])
    from nexus.mcp_client import devonthink as dt_client

    monkeypatch.setattr(dt_client, "available", lambda *a, **k: True)
    monkeypatch.setattr(dt_client, "dt_import_file", lambda path: "U-CAP")
    out = CliRunner().invoke(main, ["dt", "capture", "--file", str(md), "--corpus", "rfvyy", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert _file_entry(md).sensitive is True


# --------------------------------------------------------------------------- #
# MCP store_put: a parameter that sets the flag, none that clears it.
# --------------------------------------------------------------------------- #

def test_mcp_store_put_exposes_sensitive_and_no_way_to_clear_it() -> None:
    from nexus.mcp import core

    params = inspect.signature(core.store_put).parameters
    assert "sensitive" in params and params["sensitive"].default is False
    assert not [p for p in params if "clear" in p or "unsensitive" in p or "public" in p]
    # catalog `update` refuses the column too: the audited route is the only way down.
    from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS

    assert "set_sensitivity" in CATALOG_WRITE_OPS


def test_mcp_store_put_marks_the_note(env) -> None:
    from nexus.mcp import core

    out = core.store_put(content=_CONTENT, collection="rfvyy-mcp", title="rfvyy-mcp-note", sensitive=True)
    assert str(out).startswith("Stored:"), out
    assert _note_entry("rfvyy-mcp-note").sensitive is True
    out = core.store_put(content=_CONTENT + " v2", collection="rfvyy-mcp", title="rfvyy-mcp-plain")
    assert str(out).startswith("Stored:"), out
    assert _note_entry("rfvyy-mcp-plain").sensitive is False


# --------------------------------------------------------------------------- #
# An engine that cannot hold the flag: the write stops before the register call.
# --------------------------------------------------------------------------- #

_PROBES = {
    "absent": lambda self: (False, ""),
    "false": lambda self: (False, "0.1.100"),
    "raises": lambda self: (_ for _ in ()).throw(RuntimeError("probe broke")),
}


def _verbs(tmp_path: Path):
    md = _md(tmp_path, "refused.md")
    pdf = _pdf(tmp_path, "refused.pdf")
    return {
        "store put": (["store", "put", "-", "--title", "rfvyy-refused-note", "-c", "rfvyy-refused", "--sensitive"], _CONTENT),
        "index md": (["index", "md", str(md), "--corpus", "rfvyy-refused", "--sensitive"], None),
        "index pdf": (["index", "pdf", str(pdf), "--corpus", "rfvyy-refused", "--extractor", "pymupdf", "--sensitive"], None),
        "index pdf --dir": (["index", "pdf", "--dir", str(pdf.parent), "--corpus", "rfvyy-refused", "--extractor", "pymupdf", "--sensitive"], None),
        "dt index": (["dt", "index", "--uuid", "U-REF", "--corpus", "rfvyy-refused", "--sensitive"], None),
        "dt capture": (["dt", "capture", "--file", str(md), "--corpus", "rfvyy-refused", "--sensitive"], None),
    }, md, pdf


@pytest.mark.parametrize("probe", sorted(_PROBES))
@pytest.mark.parametrize("verb", ["store put", "index md", "index pdf", "index pdf --dir", "dt index", "dt capture"])
def test_a_verb_refuses_before_register_when_the_engine_cannot_hold_the_flag(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, probe: str,
) -> None:
    verbs, md, pdf = _verbs(tmp_path)
    argv, stdin = verbs[verb]
    _dt_patches(monkeypatch, [("U-REF", str(md))])
    from nexus.mcp_client import devonthink as dt_client

    captured: list[str] = []
    monkeypatch.setattr(dt_client, "available", lambda *a, **k: True)
    monkeypatch.setattr(dt_client, "dt_import_file", lambda path: captured.append(path) or "U-REF")
    registers: list[object] = []
    real_register = HttpCatalogClient.register

    def spy(self, *a, **k):
        registers.append((a, k))
        return real_register(self, *a, **k)

    with patch.object(HttpCatalogClient, "catalog_sensitivity_probe", _PROBES[probe]), \
            patch.object(HttpCatalogClient, "register", spy):
        out = CliRunner().invoke(main, argv, input=stdin)
    assert out.exit_code != 0, out.output
    assert "sensitiv" in out.output.lower() and "Nothing was written" in out.output, out.output
    assert registers == [], f"{verb} reached the register call: {registers}"
    assert captured == [], f"{verb} touched DEVONthink before refusing"
    assert _reader().find_by_title_exact("rfvyy-refused-note") == []
    assert _reader().find_all_by_file_path(str(md.resolve())) == []
    assert _reader().find_all_by_file_path(str(pdf.resolve())) == []


def test_mcp_store_put_refuses_before_register_when_the_engine_cannot_hold_the_flag(env) -> None:
    from nexus.mcp import core

    registers: list[object] = []
    real_register = HttpCatalogClient.register

    def spy(self, *a, **k):
        registers.append(k)
        return real_register(self, *a, **k)

    with patch.object(HttpCatalogClient, "catalog_sensitivity_probe", _PROBES["false"]), \
            patch.object(HttpCatalogClient, "register", spy):
        out = core.store_put(content=_CONTENT, collection="rfvyy-mcp", title="rfvyy-mcp-refused", sensitive=True)
    assert "sensitiv" in str(out).lower() and "Nothing was written" in str(out), out
    assert registers == []
    assert _reader().find_by_title_exact("rfvyy-mcp-refused") == []


# --------------------------------------------------------------------------- #
# The negative control for the refusal pin: with the refusal call removed the refuse tests go red.
# --------------------------------------------------------------------------- #

def test_the_refusal_pin_is_not_vacuous(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With require_catalog_sensitivity neutralised, the same refused engine lets ``store put`` write."""
    import nexus.commands.store as store_mod
    import nexus.sensitive_write as sw

    monkeypatch.setattr(sw, "require_catalog_sensitivity", lambda *a, **k: None)
    assert hasattr(sw, "require_catalog_sensitivity")  # the gate every verb calls goes through this name
    with patch.object(HttpCatalogClient, "catalog_sensitivity_probe", _PROBES["false"]):
        out = _store_put("rfvyy-control-note", ["--sensitive"])
    assert out.exit_code == 0, "with the gate removed the write must go through; if it does not, the refuse tests pin nothing"
    del store_mod


# --------------------------------------------------------------------------- #
# The generated help.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(("group", "cmd"), [("index", "pdf"), ("index", "md"), ("dt", "index"), ("dt", "capture"), ("store", "put")])
def test_the_flag_is_there_with_the_generated_help(group: str, cmd: str) -> None:
    from nexus.tables import llm_surfaces as ls

    sub = main.commands[group].commands[cmd]
    opt = next((p for p in sub.params if isinstance(p, click.Option) and "--sensitive" in p.opts), None)
    assert opt is not None and opt.is_flag
    assert opt.help == ls.render_sensitive_help(ls.RELEASE_ENFORCED_PHASE)
