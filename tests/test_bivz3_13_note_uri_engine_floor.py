# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 (nexus-bivz3.13): a note write is refused when the serving engine is below
``NOTE_URI_MIN_ENGINE_VERSION``.

A client that mints ``nx-note://`` URIs must not write a note to an engine that cannot hold one
(critique Significant 4). The check is :func:`nexus.db.serving_engine_version.require_note_uri_engine`;
it runs before the writer's first write and raises :class:`~nexus.errors.NoteUriEngineTooOldError`
to the caller. Two layers:

* the guard itself: refusal, the process cache (only a result at or above the floor is kept), the
  "no version" branch (never refuses, one warning per process), the unset floor (inert, no probe);
* every note writer: MCP ``store_put``, ``nx store put``, ``nx memory promote``, the recovery-bundle
  import and the exporter's ``import_collection`` refuse below the floor, and nothing is registered
  or written.

The probe is the test seam: no test here talks to a network.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import serving_engine_version as sev
from nexus.errors import NoteUriEngineTooOldError

_FLOOR = (0, 1, 200)
_BELOW = (0, 1, 199)


class _Served:
    """The serving engine the probe reports; ``probes`` counts how often the guard asked."""

    def __init__(self) -> None:
        self.version: tuple[int, int, int] | None = _BELOW
        self.probes = 0


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    """A floor of 0.1.200 and a probe that answers from ``served.version``."""
    from nexus import engine_version as ev

    monkeypatch.setattr(ev, "NOTE_URI_MIN_ENGINE_VERSION", _FLOOR)
    monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", _FLOOR)  # the release shape: pin at the floor
    sev.reset_note_uri_guard_for_tests()
    state = _Served()

    def _probe() -> tuple[int, int, int] | None:
        state.probes += 1
        return state.version

    monkeypatch.setattr(sev, "probe_serving_engine_version", _probe)
    yield state
    sev.reset_note_uri_guard_for_tests()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every register and every T3/manifest write the note writer makes, by name."""
    from nexus.catalog import note_write
    from nexus.catalog import store_hook as sh

    calls: list[str] = []
    real_register, real_write = sh.catalog_store_hook_tracked, note_write.write_note

    def _register(*a, **k):
        calls.append("register")
        return real_register(*a, **k)

    def _write(*a, **k):
        calls.append("write")
        return real_write(*a, **k)

    monkeypatch.setattr(sh, "catalog_store_hook_tracked", _register)
    monkeypatch.setattr(note_write, "write_note", _write)
    return calls


class TestGuard:
    def test_below_the_floor_is_refused_with_the_local_remedy(self, served):
        with patch("nexus.config.is_local_mode", return_value=True), pytest.raises(NoteUriEngineTooOldError) as err:
            sev.require_note_uri_engine()
        text = str(err.value)
        assert "nx daemon restart-stale" in text
        assert "0.1.199" in text and "0.1.200" in text

    def test_below_the_floor_on_the_managed_engine_says_it_is_not_upgraded_yet(self, served):
        with patch("nexus.config.is_local_mode", return_value=False), pytest.raises(NoteUriEngineTooOldError) as err:
            sev.require_note_uri_engine()
        text = str(err.value)
        assert "has not been upgraded yet" in text
        assert "restart-stale" not in text

    @pytest.mark.parametrize("local", [True, False])
    def test_a_pin_below_the_floor_does_not_send_the_user_round_the_restart_loop(self, served, monkeypatch, local):
        """restart-stale converges to REQUIRED_ENGINE_VERSION; when that is below the floor it can never help."""
        from nexus import engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", _BELOW)
        with patch("nexus.config.is_local_mode", return_value=local), pytest.raises(NoteUriEngineTooOldError) as err:
            sev.require_note_uri_engine()
        text = str(err.value)
        assert "restart-stale" not in text and "has not been upgraded yet" not in text
        assert "ahead of its pinned engine" in text
        assert "0.1.199" in text and "0.1.200" in text  # the pin and the floor are both named

    def test_a_pin_at_the_floor_keeps_the_restart_advice(self, served, monkeypatch):
        from nexus import engine_version as ev

        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", _FLOOR)
        with patch("nexus.config.is_local_mode", return_value=True), pytest.raises(NoteUriEngineTooOldError) as err:
            sev.require_note_uri_engine()
        assert "nx daemon restart-stale" in str(err.value) and "ahead of its pinned" not in str(err.value)

    def test_below_then_at_the_floor_in_one_process_the_second_write_passes(self, served):
        with pytest.raises(NoteUriEngineTooOldError):
            sev.require_note_uri_engine()
        assert served.probes == 1
        served.version = _FLOOR  # the daemon was restarted onto the new engine
        sev.require_note_uri_engine()
        assert served.probes == 2, "a below-floor result is never cached: the second write probed again"
        sev.require_note_uri_engine()
        sev.require_note_uri_engine()
        assert served.probes == 2, "a result at the floor is cached for the process: no more probes"

    def test_above_the_floor_is_cached_too(self, served):
        served.version = (0, 1, 250)
        sev.require_note_uri_engine()
        sev.require_note_uri_engine()
        assert served.probes == 1

    def test_no_version_does_not_refuse_and_warns_once_per_process(self, served):
        served.version = None
        with structlog.testing.capture_logs() as logs:
            sev.require_note_uri_engine()
            sev.require_note_uri_engine()
        assert served.probes == 2, "no version is not cached: every write asks again"
        warnings = [e for e in logs if e["log_level"] == "warning" and e["event"] == "note_uri_engine_version_unknown"]
        assert len(warnings) == 1, logs

    def test_no_version_then_a_version_below_the_floor_refuses(self, served):
        served.version = None
        sev.require_note_uri_engine()
        served.version = _BELOW
        with pytest.raises(NoteUriEngineTooOldError):
            sev.require_note_uri_engine()

    def test_an_unset_floor_is_inert_and_never_probes(self, served, monkeypatch):
        from nexus import engine_version as ev

        monkeypatch.setattr(ev, "NOTE_URI_MIN_ENGINE_VERSION", (0, 0, 0))
        sev.require_note_uri_engine()
        assert served.probes == 0


@pytest.mark.xfail(strict=True, reason=(
    "nexus-bivz3.20 sets NOTE_URI_MIN_ENGINE_VERSION at the engine-tag push to develop. When it does, this "
    "XPASSes and fails strict: delete this test in the same commit. Until then the guard is inert and every "
    "note writer in the suite runs without the version check."))
def test_the_note_uri_floor_is_set():
    from nexus import engine_version as ev

    assert ev.NOTE_URI_MIN_ENGINE_VERSION != (0, 0, 0)


@pytest.mark.real_engine_probe
class TestTheProbe:
    """``probe_serving_engine_version`` is the public form of the channel waiter's probe."""

    def test_a_blank_release_version_is_no_version(self):
        class _Resp:
            def raise_for_status(self) -> None: ...
            def json(self): return {"release_version": ""}

        with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
                   return_value=("http://127.0.0.1:1", "t")), \
             patch.object(sev.httpx, "get", return_value=_Resp()):
            assert sev.probe_serving_engine_version() is None

    def test_a_release_version_parses(self):
        class _Resp:
            def raise_for_status(self) -> None: ...
            def json(self): return {"release_version": "0.1.201"}

        with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
                   return_value=("http://127.0.0.1:1", "t")), \
             patch.object(sev.httpx, "get", return_value=_Resp()):
            assert sev.probe_serving_engine_version() == (0, 1, 201)

    def test_an_unresolvable_endpoint_is_no_version_never_a_raise(self):
        with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
                   side_effect=RuntimeError("no lease")):
            assert sev.probe_serving_engine_version() is None

    def test_a_transport_error_is_no_version_never_a_raise(self):
        with patch("nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
                   return_value=("http://127.0.0.1:1", "t")), \
             patch.object(sev.httpx, "get", side_effect=sev.httpx.ConnectError("refused")):
            assert sev.probe_serving_engine_version() is None


@pytest.mark.t2_tenant
class TestEveryWriterRefuses:
    """Each note writer, below the floor: refused with the remedy, nothing registered, nothing written."""

    def test_put_note_raises_before_the_register_and_the_write(self, served, writes):
        from nexus.catalog.note_write import put_note

        with pytest.raises(NoteUriEngineTooOldError):
            put_note(content="bivz3.13 body", collection="knowledge__bivz3-13__bge-base-en-v15-768__v1",
                     title="bivz3-13-put-note")
        assert writes == []
        assert served.probes == 1

    def test_the_check_runs_after_pure_validation(self, served, writes):
        """An empty note is a ValueError, not a probe: validation precedes the check."""
        from nexus.catalog.note_write import put_note

        with pytest.raises(ValueError):
            put_note(content="", collection="knowledge__bivz3-13__bge-base-en-v15-768__v1")
        assert served.probes == 0

    def test_mcp_store_put(self, served, writes):
        from nexus.mcp.core import store_put

        out = store_put(content="bivz3.13 mcp body", collection="bivz3-13-subject", title="bivz3-13-mcp")
        assert out.startswith("Error: store_put:"), out
        assert "nx daemon restart-stale" in out or "has not been upgraded yet" in out
        assert writes == []

    def test_nx_store_put(self, served, writes):
        result = CliRunner().invoke(
            main, ["store", "put", "-", "--title", "bivz3-13-cli", "-c", "bivz3-13-subject"],
            input="bivz3.13 cli body")
        assert result.exit_code != 0, result.output
        assert "restart-stale" in result.output or "has not been upgraded yet" in result.output
        assert writes == []

    def test_nx_memory_promote_keeps_the_t2_entry(self, served, writes, tmp_path: Path):
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction
        from nexus.db.t2 import T2Database
        from nexus.db.t3 import T3Database
        from tests.conftest import make_vector_test_client

        from nexus.mcp_infra import inject_t3

        t3 = T3Database(_client=make_vector_test_client(), _ef_override=MiniLMDirectEmbeddingFunction())
        inject_t3(t3)  # as the sibling promote tests do: the collection probe resolves against it
        path = tmp_path / "t2.db"
        with T2Database(path) as db:
            row = db.put(project="proj", title="bivz3-13-promote", content="bivz3.13 promote body", ttl=None)
        try:
            with patch("nexus.commands.memory.t2_handle", side_effect=lambda: T2Database(path)), \
                 patch("nexus.db.make_t3", return_value=t3):
                result = CliRunner().invoke(
                    main, ["memory", "promote", str(row), "--collection", "bivz3-13-subject", "--remove"])
        finally:
            inject_t3(None)
        assert result.exit_code != 0, result.output
        assert "restart-stale" in result.output or "has not been upgraded yet" in result.output
        assert "T2 entry is unchanged" in result.output
        with T2Database(path) as db:
            assert db.get(project="proj", title="bivz3-13-promote") is not None, "--remove did not delete it"
        assert writes == []

    def test_recovery_bundle_import_of_a_note(self, served, writes, monkeypatch):
        from nexus.catalog import recovery_bundle as rb

        monkeypatch.setattr(rb, "target_collection_for",
                            lambda recorded, t3: "knowledge__bivz3-13__bge-base-en-v15-768__v1")
        rec = {"record": "knowledge_doc", "collection": "knowledge__bivz3-13", "title": "bivz3-13-recovery",
               "content": "bivz3.13 recovery body", "tags": "", "category": "", "ttl_days": None}
        with pytest.raises(NoteUriEngineTooOldError):
            rb._default_import_doc(object(), rec)
        assert writes == []

    def test_the_bundle_import_aborts_on_the_first_refusal(self, served, writes, monkeypatch, tmp_path: Path):
        """One refusal, not N per-record failures: the first NoteUriEngineTooOldError ends the import, so the
        engine is asked once and not once per note."""
        from nexus.catalog import recovery_bundle as rb

        monkeypatch.setattr(rb, "target_collection_for",
                            lambda recorded, t3: "knowledge__bivz3-13__bge-base-en-v15-768__v1")
        monkeypatch.setattr(rb, "read_bundle", lambda p: ({}, [
            {"record": "knowledge_doc", "collection": "c", "title": f"t{i}", "content": f"body {i}", "tags": "",
             "category": "", "ttl_days": None} for i in range(3)]))
        with pytest.raises(NoteUriEngineTooOldError):
            rb.import_bundle(reader=object(), writer=object(), t3=object(), path=tmp_path / "b.jsonl")
        assert served.probes == 1, "three notes in the bundle, one probe: the import stopped at the first"
        assert writes == []

    def test_the_recovery_import_command_reports_one_clear_error(self, served, monkeypatch, tmp_path: Path):
        from nexus.catalog import recovery_bundle as rb

        monkeypatch.setattr(rb, "target_collection_for",
                            lambda recorded, t3: "knowledge__bivz3-13__bge-base-en-v15-768__v1")
        monkeypatch.setattr(rb, "read_bundle", lambda p: ({}, [
            {"record": "knowledge_doc", "collection": "c", "title": f"t{i}", "content": f"body {i}", "tags": "",
             "category": "", "ttl_days": None} for i in range(3)]))
        bundle = tmp_path / "b.jsonl"
        bundle.write_text("{}\n")
        with patch("nexus.commands.catalog._get_catalog", return_value=object()), \
             patch("nexus.commands.catalog._get_catalog_writer", return_value=object()), \
             patch("nexus.db.make_t3", return_value=object()):
            result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
        assert result.exit_code != 0, result.output
        assert result.output.count("aborted at the first note") == 1 and "0.1.199" in result.output
        assert "DOC FAILURES" not in result.output
        assert served.probes == 1

    def test_the_spy_sees_a_write_when_the_engine_is_at_the_floor(self, served, writes):
        """Positive control for every ``writes == []`` above: the same spy records the register and the
        write once the guard passes, so an empty list means the guard stopped them, not that the spy is blind."""
        from nexus.catalog.note_write import put_note

        served.version = _FLOOR
        put_note(content="bivz3.13 control body", collection="knowledge__bivz3-13__bge-base-en-v15-768__v1",
                 title="bivz3-13-control")
        assert "register" in writes and "write" in writes, writes

    @staticmethod
    def _header_only(tmp_path: Path, collection: str) -> Path:
        """An export file whose header names *collection* and omits ``embedding_model``: a note import
        passes the guard and then dies on the missing key, with nothing written, which orders the two."""
        import json

        path = tmp_path / "h.nxexp"
        path.write_text(json.dumps({"format_version": 1, "collection_name": collection}) + "\n")
        return path

    def test_exporter_import_collection_refuses_a_note_target_before_reading_any_record(self, served, writes, tmp_path: Path):
        from nexus.exporter import import_collection

        with pytest.raises(NoteUriEngineTooOldError):
            import_collection(object(), self._header_only(tmp_path, "knowledge__bivz3-13__bge-base-en-v15-768__v1"))
        assert served.probes == 1
        assert writes == []

    @pytest.mark.parametrize("target", ["docs__bivz3-13", "code__bivz3-13", "rdr__bivz3-13"])
    def test_exporter_import_collection_does_not_probe_for_a_file_routed_target(self, served, writes, tmp_path: Path, target):
        """docs__, code__ and rdr__ imports mint file:// URIs, no note URI: the guard is not theirs to apply."""
        from nexus.errors import FormatVersionError
        from nexus.exporter import import_collection

        with pytest.raises(FormatVersionError, match="embedding_model"):
            import_collection(object(), self._header_only(tmp_path, target))
        assert served.probes == 0

    def test_exporter_import_collection_judges_the_target_not_the_source_name(self, served, writes, tmp_path: Path):
        from nexus.errors import FormatVersionError
        from nexus.exporter import import_collection

        note_file = self._header_only(tmp_path, "knowledge__bivz3-13__bge-base-en-v15-768__v1")
        with pytest.raises(FormatVersionError):
            import_collection(object(), note_file, target_collection="docs__bivz3-13")
        assert served.probes == 0
        with pytest.raises(NoteUriEngineTooOldError):
            import_collection(object(), self._header_only(tmp_path, "docs__bivz3-13"),
                              target_collection="knowledge__bivz3-13__bge-base-en-v15-768__v1")




# --- the gate-jar stamp at the sites outside build-gate-jar.sh (that script has its own shell test) ----------

_REPO = Path(__file__).resolve().parent.parent
_ENGINE_VERSION_SRC = (_REPO / "src" / "nexus" / "engine_version.py").read_text()
_FLOOR_LINE = re.compile(r"^NOTE_URI_MIN_ENGINE_VERSION[^=\n]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)[^\n]*$", re.M)
_PIN_LINE = re.compile(r"^REQUIRED_ENGINE_VERSION[^=\n]*=\s*\((\d+),\s*(\d+),\s*(\d+)\)", re.M)


def _version_in(pattern: re.Pattern[str]) -> tuple[int, int, int]:
    found = pattern.search(_ENGINE_VERSION_SRC)
    assert found, f"engine_version.py no longer matches {pattern.pattern!r}: update this pin's regex"
    return tuple(int(part) for part in found.groups())  # type: ignore[return-value]


def _with_floor(version: tuple[int, int, int] | None) -> str:
    """The real engine_version.py with its floor line rewritten (``None`` removes it)."""
    line = "" if version is None else f"NOTE_URI_MIN_ENGINE_VERSION: tuple[int, int, int] = {version!r}"
    return _FLOOR_LINE.sub(line, _ENGINE_VERSION_SRC, count=1)


def _ci_stamp_commands() -> list[str]:
    text = (_REPO / ".github" / "workflows" / "ci.yml").read_text()
    return re.findall(r"ver=\$\((python3? -c \"import re,sys.*?)\)\n", text)


def _local_gate_stamp_program() -> str:
    text = (_REPO / "tests" / "e2e" / "local-service-gate.sh").read_text()
    head = 'GATE_STAMP="$("$E2E_PYTHON" -c \''
    start = text.index(head) + len(head)
    return text[start:text.index("')\"", start)]


def _run_stamp(tmp_path: Path, argv: list[str], engine_version_text: str) -> tuple[int, str, str]:
    import subprocess
    import sys

    (tmp_path / "src" / "nexus").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "nexus" / "engine_version.py").write_text(engine_version_text)
    run = subprocess.run([sys.executable if a == "@PY" else a for a in argv], cwd=tmp_path, capture_output=True, text=True)
    return run.returncode, run.stdout.strip(), run.stderr.strip()


def _stamp_cases(tmp_path: Path, argv: list[str]) -> None:
    """Value-agnostic: the real constants are read, never the placeholder, so setting the floor at the landing bead needs no edit here."""
    pin, floor = _version_in(_PIN_LINE), _version_in(_FLOOR_LINE)
    expect = ".".join(map(str, max(pin, floor)))
    rc, out, err = _run_stamp(tmp_path, argv, _ENGINE_VERSION_SRC)
    assert (rc, out) == (0, expect), "the stamp is max(pin, floor) of the tree as it is: " + err
    rc, out, err = _run_stamp(tmp_path, argv, _with_floor((0, 1, 9999)))
    assert (rc, out) == (0, "0.1.9999"), "a floor above the pin must be what the gate jar is stamped with: " + err
    rc, out, err = _run_stamp(tmp_path, argv, _with_floor((0, 0, 1)))
    assert (rc, out) == (0, ".".join(map(str, pin))), "a floor below the pin leaves the pin"
    rc, out, err = _run_stamp(tmp_path, argv, _with_floor(None))
    assert rc != 0 or out == "", "a missing floor constant must not stamp"


class TestTheGateJarStampSites:
    def test_both_ci_stamp_steps_exist(self):
        assert len(_ci_stamp_commands()) == 2, "ci.yml has two gate-jar stamp steps (the shard-matrix job copy and the seam gate)"

    @pytest.mark.parametrize("which", [0, 1])
    def test_ci_stamp_is_the_max_of_the_pin_and_the_floor(self, which, tmp_path: Path):
        command = _ci_stamp_commands()[which].replace("python -c", "python3 -c", 1)
        _stamp_cases(tmp_path, ["/bin/sh", "-c", command])

    def test_local_service_gate_stamp_is_the_max_of_the_pin_and_the_floor(self, tmp_path: Path):
        _stamp_cases(tmp_path, ["@PY", "-c", _local_gate_stamp_program()])


class TestExplicitUriRegisterRefuses:
    """The two register paths that take a caller's source_uri (critique of fdcbce198): a note URI is refused
    below the floor before the register; any other URI registers without a probe."""

    _NOTE = "nx-note://knowledge__bivz3-13__bge-base-en-v15-768__v1/bivz3-13-explicit"

    def _writer(self, monkeypatch: pytest.MonkeyPatch, module: str):
        from unittest.mock import MagicMock

        writer = MagicMock()
        writer.register.return_value = "1.1.1"
        monkeypatch.setattr(f"{module}._get_catalog_writer", lambda: writer)
        monkeypatch.setattr(f"{module}._get_catalog", lambda: MagicMock(), raising=False)
        return writer

    @pytest.mark.parametrize("uri", [_NOTE, "chroma://knowledge__bivz3-13__bge-base-en-v15-768__v1/legacy"])
    def test_nx_catalog_register_refuses_a_note_uri(self, served, monkeypatch, uri):
        writer = self._writer(monkeypatch, "nexus.commands.catalog")
        result = CliRunner().invoke(main, ["catalog", "register", "--title", "t", "--owner", "1.1",
                                           "--source-uri", uri])
        assert result.exit_code != 0, result.output
        assert "restart-stale" in result.output or "has not been upgraded yet" in result.output
        writer.register.assert_not_called()
        assert served.probes == 1

    def test_nx_catalog_register_of_a_non_note_uri_does_not_probe(self, served, monkeypatch):
        writer = self._writer(monkeypatch, "nexus.commands.catalog")
        result = CliRunner().invoke(main, ["catalog", "register", "--title", "t", "--owner", "1.1",
                                           "--source-uri", "x-devonthink-item://ABC"])
        assert result.exit_code == 0, result.output
        writer.register.assert_called_once()
        assert served.probes == 0

    def test_mcp_catalog_register_refuses_a_note_uri(self, served, monkeypatch):
        from nexus.mcp.catalog import catalog_register

        writer = self._writer(monkeypatch, "nexus.mcp.catalog")
        out = catalog_register(title="t", owner="1.1", source_uri=self._NOTE)
        assert "error" in str(out), out
        assert "restart-stale" in str(out) or "has not been upgraded yet" in str(out)
        writer.register.assert_not_called()
