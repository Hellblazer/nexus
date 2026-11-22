# SPDX-License-Identifier: AGPL-3.0-or-later
"""F35 (nexus-7t3w1): scripts/publish-mineru-weights.py stages, verifies and uploads the pinned weights; ``gh`` is always stubbed."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nexus import mineru_weights as mw
from tests._f35_weights_bed import (  # noqa: F401 — env is a fixture
    _CONTENT, _complete_root, _root, env,
)

#: tests/conftest.py stubs the fetch and the child env for every other module; this one drives them.
REAL_MINERU_WEIGHTS_FETCH = True

REPO_ROOT = Path(__file__).resolve().parent.parent

from nexus.db.service_bge_model import file_sha256


# ── the publish script ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def publish():
    spec = importlib.util.spec_from_file_location("publish_mineru_weights", REPO_ROOT / "scripts" / "publish-mineru-weights.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_repo_license_is_the_agpl_the_script_publishes(publish):
    assert publish.STATIC_ASSETS["LICENSE-AGPL-3.0.txt"].read_text().lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    apache = publish.STATIC_ASSETS["LICENSE-Apache-2.0.txt"].read_text()
    assert "Apache License" in apache and "Version 2.0, January 2004" in apache and len(apache) > 11000
    notices = publish.STATIC_ASSETS["THIRD-PARTY-NOTICES.md"].read_text()
    assert mw.WEIGHTS_REVISION in notices and "agpl-3.0" in notices
    for model in ("PP-DocLayoutV2", "UniMERNet", "PP-OCRv", "PP-LCNet_x1_0_doc_ori", "PP-LCNet_x1_0_table_cls", "SLANet_plus", "unet.onnx"):
        assert model in notices, model


def test_staging_from_the_hf_cache_reproduces_the_manifest_and_verifies(env, publish, tmp_path):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 0
    for entry in mw.MANIFEST:
        assert file_sha256(stage / entry.asset) == entry.sha256
    assert (stage / "MANIFEST.tsv").read_text() == "".join(f"{f.path}\t{f.size}\t{f.sha256}\n" for f in mw.MANIFEST)
    for name in ("LICENSE-AGPL-3.0.txt", "LICENSE-Apache-2.0.txt", "THIRD-PARTY-NOTICES.md"):
        assert (stage / name).is_file()
    assert publish.verify_stage(stage) == []
    assert publish.main(["--stage-dir", str(stage), "--verify-only"]) == 0
    assert publish.main(["--stage-dir", str(stage)]) == 0  # idempotent on a staged dir


def test_the_publish_script_refuses_a_mismatching_staged_file(env, publish, tmp_path, capsys):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 0
    victim = stage / mw.asset_name("models/Layout/PP-DocLayoutV2/model.safetensors")
    victim.write_bytes(b"Q" * len(_CONTENT["models/Layout/PP-DocLayoutV2/model.safetensors"]))  # right size, wrong bytes

    assert publish.main(["--stage-dir", str(stage)]) == 1
    assert publish.main(["--stage-dir", str(stage), "--verify-only"]) == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and "sha256" in err and "Layout__PP-DocLayoutV2__model.safetensors" in err
    assert victim.read_bytes().startswith(b"Q"), "a mismatching staged file is reported, never silently repaired"


def test_the_publish_script_refuses_a_mismatching_upstream_file_and_stages_nothing_for_it(env, publish, tmp_path, capsys, monkeypatch):
    bad = "models/MFR/unimernet_hf_small_2503/README.md"
    import huggingface_hub  # noqa: PLC0415

    env.hub.fill({**_CONTENT, bad: b"X" * len(_CONTENT[bad])})
    # no cached copy verifies, so the script downloads at the pinned revision: here that yields the same wrong bytes
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", lambda repo, filename, revision: str(env.hub.snapshot / filename))
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 1
    assert not (stage / mw.asset_name(bad)).exists()
    assert not (stage / "MANIFEST.tsv").exists(), "nothing is published as a set when a file fails"
    assert "does not match the manifest" in capsys.readouterr().err


def test_a_missing_staged_asset_fails_verification(env, publish, tmp_path):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    (stage / mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")).unlink()
    assert any("missing" in p for p in publish.verify_stage(stage))


def test_the_upload_plan_is_create_all_when_absent_missing_only_when_partial_and_refuses_a_different_asset(publish):
    staged = {"a": "1", "b": "2", "c": "3"}

    def remote(name: str) -> str:
        return {"b": "2"}[name]

    none = frozenset()
    assert publish.plan_upload(staged, None, remote, none) == (["a", "b", "c"], [], [])
    assert publish.plan_upload(staged, {"a": "1", "b": None}, remote, none) == (["c"], [], [])  # b's digest is fetched
    assert publish.plan_upload(staged, {"a": "1", "b": "2", "c": "3"}, remote, none) == ([], [], [])
    plan = publish.plan_upload(staged, {"a": "9"}, remote, none)
    assert plan.to_upload == ["b", "c"] and len(plan.conflicts) == 1 and "a:" in plan.conflicts[0] and plan.kept == []


def test_a_differing_license_or_notice_asset_keeps_the_published_copy_while_a_weight_or_manifest_still_conflicts(publish):
    staged = {"w": "1", "THIRD-PARTY-NOTICES.md": "2", "MANIFEST.tsv": "3"}
    existing = {"w": "1", "THIRD-PARTY-NOTICES.md": "9", "MANIFEST.tsv": "3"}
    plan = publish.plan_upload(staged, existing, lambda n: "", frozenset({"THIRD-PARTY-NOTICES.md"}))
    assert plan.to_upload == [] and plan.conflicts == [] and len(plan.kept) == 1 and "THIRD-PARTY-NOTICES.md" in plan.kept[0]
    plan = publish.plan_upload(staged, {**existing, "MANIFEST.tsv": "9"}, lambda n: "", frozenset({"THIRD-PARTY-NOTICES.md"}))
    assert len(plan.conflicts) == 1 and "MANIFEST.tsv" in plan.conflicts[0]


def test_only_a_sha256_digest_is_read_as_one(publish):
    assert publish.asset_sha256({"digest": "sha256:" + "a" * 64}) == "a" * 64
    assert publish.asset_sha256({"digest": "sha512:" + "a" * 128}) is None, "another algorithm is unknown, never stripped to look like a sha256"
    assert publish.asset_sha256({"digest": "md5:abc"}) is None
    assert publish.asset_sha256({}) is None and publish.asset_sha256({"digest": None}) is None


def _info(publish, assets, draft=False):
    return publish.ReleaseInfo(assets, draft)


def test_upload_refuses_a_conflict_and_uploads_nothing(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    name = mw.asset_name("models/Layout/PP-DocLayoutV2/config.json")
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: _info(publish, {name: "0" * 64}))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lines.append) == 1
    assert calls == [] and any("REFUSED" in ln and name in ln for ln in lines)


def test_upload_refuses_a_draft_release_with_a_clear_message(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: _info(publish, {}, draft=True))
    calls: list = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lines.append) == 1
    assert calls == [] and any("only as a draft" in ln for ln in lines)


def test_upload_turns_a_failed_release_read_into_one_error_line(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])

    def boom(repo, tag):
        raise RuntimeError("gh release view failed: HTTP 502")

    monkeypatch.setattr(publish, "release_assets", boom)
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lines.append) == 1
    assert len(lines) == 1 and lines[0].startswith("ERROR:") and "HTTP 502" in lines[0]


def test_release_assets_requests_the_draft_flag_and_reads_digests(publish, monkeypatch):
    seen: list[tuple[str, ...]] = []

    def fake_gh(*args, check=True):
        seen.append(args)
        payload = {"isDraft": True, "assets": [{"name": "a", "digest": "sha256:" + "b" * 64}, {"name": "c", "digest": "sha1:x"}]}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(publish, "_gh", fake_gh)
    info = publish.release_assets("owner/repo", "tag")
    assert "assets,isDraft" in seen[0]
    assert info.is_draft is True and info.assets == {"a": "b" * 64, "c": None}


def test_upload_creates_an_absent_release_at_an_explicit_target_not_latest_and_uploads_only_missing_assets_otherwise(
    env, publish, tmp_path, monkeypatch
):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))

    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: None)
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert calls[0][:3] == ("release", "create", mw.WEIGHTS_ASSET_TAG) and "--latest=false" in calls[0]
    assert calls[0][calls[0].index("--target") + 1] == "main"
    assert len([a for a in calls[0] if a.startswith(str(stage))]) == len(_CONTENT) + 4

    calls.clear()
    have = publish.staged_digests(stage)
    held_back = mw.asset_name("models/TabRec/SlanetPlus/slanet-plus.onnx")
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: _info(publish, {n: d for n, d in have.items() if n != held_back}))
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert len(calls) == 1 and calls[0][:3] == ("release", "upload", mw.WEIGHTS_ASSET_TAG)
    assert [a for a in calls[0] if a.startswith(str(stage))] == [str(stage / held_back)]
    assert "--clobber" not in calls[0]

    calls.clear()
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: _info(publish, dict(have)))
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lambda _l: None) == 0
    assert calls == []


def test_upload_warns_and_keeps_a_published_notice_that_differs_without_failing_the_cut(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    have = publish.staged_digests(stage)
    have["THIRD-PARTY-NOTICES.md"] = "0" * 64  # the published notice is an older wording
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: _info(publish, have))
    calls: list = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=False, say=lines.append) == 0
    assert calls == [] and any("WARNING" in ln and "THIRD-PARTY-NOTICES.md" in ln for ln in lines)


def test_dry_run_uploads_nothing_and_names_the_target(env, publish, tmp_path, monkeypatch):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    publish.main(["--stage-dir", str(stage)])
    monkeypatch.setattr(publish, "release_assets", lambda repo, tag: None)
    calls: list = []
    monkeypatch.setattr(publish, "_gh", lambda *a, **k: calls.append(a))
    lines: list[str] = []
    assert publish.upload(stage, "owner/repo", dry_run=True, target="main", say=lines.append) == 0
    assert calls == [] and any("tag at main" in ln for ln in lines)


def test_a_staged_notice_that_differs_from_the_repo_is_a_warning_not_a_refusal(env, publish, tmp_path, capsys):
    env.hub.fill(_CONTENT)
    stage = tmp_path / "stage"
    assert publish.main(["--stage-dir", str(stage)]) == 0
    (stage / "THIRD-PARTY-NOTICES.md").write_text("stale")
    assert publish.verify_stage(stage) == []
    assert any("THIRD-PARTY-NOTICES.md" in w for w in publish.static_warnings(stage))
    assert publish.main(["--stage-dir", str(stage), "--verify-only"]) == 0
    assert "WARNING" in capsys.readouterr().err
