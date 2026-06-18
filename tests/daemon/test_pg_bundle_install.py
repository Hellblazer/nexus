# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 P2: PG-bundle acquisition through the shared verified install seam.

Isolated: `_download` stubbed (no network), signature checker injected, tmp
config_dir. The published-then-downloaded round-trip against a REAL signed
bundle is the separate integration bead (nexus-3dfjq); here we cover the
consumer's fail-closed wiring and placement.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from nexus.daemon import binary_install as b

_TAG = "engine-service-v0.1.3"
_CONTENT = b"a-relocatable-pg-bundle-tarball\n"


class _OkChecker:
    def check(self, **_kw) -> None:  # signature verifies
        pass


def _good_download(url, dest, *, timeout=0):
    """Stub: materialise a self-consistent asset + sha256 + bundle."""
    if url.endswith(".sha256"):
        digest = hashlib.sha256(_CONTENT).hexdigest()
        dest.write_text(f"{digest}  {b.pg_bundle_asset_name()}\n")
    elif url.endswith(".sigstore.json"):
        dest.write_text('{"protobuf":"bundle"}')
    else:
        dest.write_bytes(_CONTENT)


def test_install_pg_bundle_places_and_writes_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "_download", _good_download)
    dest, prov = b.install_pg_bundle(
        _TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path,
    )
    assert dest == b.pg_bundle_dest(tmp_path)
    assert dest.is_file() and dest.read_bytes() == _CONTENT
    # placed where the RF-161-3-fixed _select_bundled_pg looks
    assert dest.parent == tmp_path / "service"
    sidecar = tmp_path / "service" / "nexus-pg.meta.json"
    assert sidecar.is_file()
    meta = json.loads(sidecar.read_text())
    assert meta["version"] == "0.1.3"
    assert meta["tag"] == _TAG
    assert meta["asset"] == b.pg_bundle_asset_name()
    assert meta["sha256"] == hashlib.sha256(_CONTENT).hexdigest()


def test_install_pg_bundle_rejects_non_namespace_tag(tmp_path):
    with pytest.raises(b.BinaryVerificationError):
        b.install_pg_bundle("v0.1.3", tmp_path)
    assert not b.pg_bundle_dest(tmp_path).exists()


def test_install_pg_bundle_fails_closed_on_download_error(tmp_path, monkeypatch):
    def _boom(url, dest, *, timeout=0):
        raise b.BinaryVerificationError("network down")

    monkeypatch.setattr(b, "_download", _boom)
    with pytest.raises(b.BinaryVerificationError):
        b.install_pg_bundle(_TAG, tmp_path, download_dir=tmp_path)
    assert not b.pg_bundle_dest(tmp_path).exists()


def test_install_pg_bundle_fails_closed_on_sha256_mismatch(tmp_path, monkeypatch):
    def _bad_sha(url, dest, *, timeout=0):
        if url.endswith(".sha256"):
            dest.write_text(f"{'0' * 64}  {b.pg_bundle_asset_name()}\n")
        elif url.endswith(".sigstore.json"):
            dest.write_text("{}")
        else:
            dest.write_bytes(_CONTENT)

    monkeypatch.setattr(b, "_download", _bad_sha)
    with pytest.raises(b.BinaryVerificationError):
        b.install_pg_bundle(_TAG, tmp_path, checker=_OkChecker(), download_dir=tmp_path)
    assert not b.pg_bundle_dest(tmp_path).exists()


def test_install_pg_bundle_fails_closed_when_signature_rejected(tmp_path, monkeypatch):
    class _RejectChecker:
        def check(self, **_kw):
            raise ValueError("signature does not verify")

    monkeypatch.setattr(b, "_download", _good_download)
    with pytest.raises(b.BinaryVerificationError):
        b.install_pg_bundle(_TAG, tmp_path, checker=_RejectChecker(), download_dir=tmp_path)
    assert not b.pg_bundle_dest(tmp_path).exists()


# ── published -> downloaded -> placed -> located -> extracted round-trip ────
# (nexus-3dfjq) The pre-existing relocation tests only exercised a LOCALLY-BUILT
# bundle; this closes the RF-161-3 gap by driving the full acquire-then-select
# chain — proving the P2 consumer (install_pg_bundle) and the P2 bugfix
# (_select_bundled_pg default search dir) connect.


def test_pg_bundle_publish_download_select_roundtrip(tmp_path, monkeypatch, make_pg_bundle_txz):
    import os

    from nexus.commands.init import _select_bundled_pg
    from nexus.db import pg_bundle

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.delenv(pg_bundle.BUNDLE_ENV, raising=False)

    # "publish": a real, extractable bundle .txz (the release asset).
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    published = make_pg_bundle_txz(release_dir, b.pg_bundle_asset_name())
    payload = published.read_bytes()

    def _serve(url, dest, *, timeout=0):
        if url.endswith(".sha256"):
            dest.write_text(f"{hashlib.sha256(payload).hexdigest()}  {b.pg_bundle_asset_name()}\n")
        elif url.endswith(".sigstore.json"):
            dest.write_text('{"protobuf":"bundle"}')
        else:
            dest.write_bytes(payload)

    monkeypatch.setattr(b, "_download", _serve)

    config_dir = tmp_path / "cfg"
    # Acquire+verify+place via the seam. Signature crypto is exercised separately
    # (P1 deferred @integration real-verify); inject an ok checker since no real
    # signed release artifact exists yet (freeze window).
    dest, _prov = b.install_pg_bundle(
        _TAG, config_dir, checker=_OkChecker(), download_dir=tmp_path,
    )
    assert dest == b.pg_bundle_dest(config_dir)
    assert dest.is_file()

    # The bugfix half: with no env override and no injected search_dirs,
    # _select_bundled_pg finds the just-placed bundle under <config_dir>/service/
    # and extracts it, returning the bin/ dir.
    bin_dir = _select_bundled_pg(config_dir)
    assert bin_dir is not None, "acquired bundle must be discovered by _select_bundled_pg"
    assert os.environ["NEXUS_PG_BIN"] == str(bin_dir)
    assert str(bin_dir).startswith(str(config_dir))


# ── CLI --pg-bundle / --no-pg-bundle toggle ─────────────────────────────────


def _stub_binary(tag, config_dir, *, installed_by=""):
    return config_dir / "service" / "nexus-service", {
        "asset": "nexus-service-x", "version": "0.1.3",
        "sha256": "a" * 64, "source_url": "u",
    }


def test_cli_acquires_pg_bundle_by_default(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nexus.commands.daemon import service_install_binary_cmd

    calls = []
    monkeypatch.setattr(b, "install_binary", _stub_binary)

    def _stub_pg(tag, config_dir, *, installed_by=""):
        calls.append(tag)
        return config_dir / "service" / "nexus-pg.txz", {
            "asset": "nexus-pg-x", "version": "0.1.3", "sha256": "b" * 64, "source_url": "u",
        }

    monkeypatch.setattr(b, "install_pg_bundle", _stub_pg)
    r = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--config-dir", str(tmp_path)],
    )
    assert r.exit_code == 0, r.output
    assert calls == [_TAG]


def test_cli_no_pg_bundle_skips_bundle(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from nexus.commands.daemon import service_install_binary_cmd

    monkeypatch.setattr(b, "install_binary", _stub_binary)

    def _must_not_run(*a, **k):
        raise AssertionError("install_pg_bundle called under --no-pg-bundle")

    monkeypatch.setattr(b, "install_pg_bundle", _must_not_run)
    r = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--no-pg-bundle", "--config-dir", str(tmp_path)],
    )
    assert r.exit_code == 0, r.output


def test_cli_binary_ok_pg_bundle_fail_exits_2_with_clear_message(tmp_path, monkeypatch):
    """CRE H1/H2: binary installs, PG bundle fails -> exit 2 AND the message
    makes clear the binary succeeded (no misleading 'restart' hint)."""
    from click.testing import CliRunner

    from nexus.commands.daemon import service_install_binary_cmd

    monkeypatch.setattr(b, "install_binary", _stub_binary)

    def _pg_fail(tag, config_dir, *, installed_by=""):
        raise b.BinaryVerificationError("bundle sha256 mismatch")

    monkeypatch.setattr(b, "install_pg_bundle", _pg_fail)
    r = CliRunner().invoke(
        service_install_binary_cmd, [_TAG, "--config-dir", str(tmp_path)],
    )
    assert r.exit_code == 2
    out = r.output.lower()
    assert "binary installed ok" in out  # binary success surfaced
    assert "bundle" in out
    assert "restart the service" not in out  # misleading hint suppressed
