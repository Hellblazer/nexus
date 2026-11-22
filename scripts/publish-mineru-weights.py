#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage, verify and publish MinerU's pinned pipeline weights (F35, nexus-7t3w1).

The pin is ``nexus.mineru_weights.MANIFEST`` (upstream ``opendatalab/PDF-Extract-Kit-1.0`` at the revision
named there). This script reads it from the module, so the client, the gate and the release cannot disagree.

    publish-mineru-weights.py --stage-dir DIR            stage the files from the Hugging Face cache (or a
                                                         pinned-revision download), check each against the
                                                         manifest, write the license, notice and manifest files
    publish-mineru-weights.py --stage-dir DIR --verify-only
                                                         check an already staged DIR, source nothing
    publish-mineru-weights.py --stage-dir DIR --upload   after staging, upload to the release tag
                                                         ``mineru-pipeline-ed6b654c`` ONLY where it is absent

DIR then holds one file per asset: ``<path under models/ with "/" as "__">`` for each of the 20 manifest files,
``LICENSE-AGPL-3.0.txt``, ``LICENSE-Apache-2.0.txt``, ``THIRD-PARTY-NOTICES.md`` and ``MANIFEST.tsv``. The
candidate gate serves it over http (``NX_MINERU_WEIGHTS_BASE_URL``); the release skill runs ``--upload`` at the
cut, under the release's go. ``--upload`` creates the release when it is absent, uploads only the assets that are
missing, never overwrites, and refuses (uploading nothing) when an existing asset's sha256 differs from the
manifest. A staged file that does not match the manifest is refused, never repaired: delete it and re-stage.

Exit 0 on success, 1 when a file or asset does not verify, 2 on a usage error."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))  # the checkout's own manifest, whatever else is installed

from nexus import mineru_weights as mw  # noqa: E402 — after the sys.path line
from nexus.db.service_bge_model import file_sha256  # noqa: E402

DEFAULT_REPO = "Hellblazer/nexus"
NOTICE_DIR = REPO / "third_party" / "mineru-pipeline-weights"
#: asset name -> the file in this checkout it is copied from. The AGPL text is the repo's own LICENSE
#: (``LICENSE`` is the GNU AGPL v3, checked by tests/test_f35_mineru_self_host_weights.py).
STATIC_ASSETS: dict[str, Path] = {
    "LICENSE-AGPL-3.0.txt": REPO / "LICENSE",
    "LICENSE-Apache-2.0.txt": NOTICE_DIR / "LICENSE-Apache-2.0.txt",
    "THIRD-PARTY-NOTICES.md": NOTICE_DIR / "THIRD-PARTY-NOTICES.md",
}
MANIFEST_ASSET = "MANIFEST.tsv"


def manifest_tsv() -> str:
    """``MANIFEST.tsv``: path under the root, bytes, sha256, one tab-separated line per pinned file."""
    return "".join(f"{f.path}\t{f.size}\t{f.sha256}\n" for f in mw.MANIFEST)


def staged_names() -> list[str]:
    return [f.asset for f in mw.MANIFEST] + [*STATIC_ASSETS, MANIFEST_ASSET]


def _check(path: Path, entry: mw.WeightFile) -> str | None:
    """None when *path* is *entry*, else why it is not."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"{entry.asset}: cannot be read ({exc})"
    if size != entry.size:
        return f"{entry.asset}: {size} bytes, the manifest says {entry.size}"
    digest = file_sha256(path)
    if digest != entry.sha256:
        return f"{entry.asset}: sha256 {digest}, the manifest says {entry.sha256}"
    return None


def source_file(entry: mw.WeightFile) -> Path:
    """The upstream bytes for *entry*: the pinned snapshot in the local Hugging Face cache when it has the
    file, else a download at the pinned revision (the only network use of this script)."""
    snapshot = mw._hf_snapshot_dir()
    if snapshot is not None:
        cached = snapshot / entry.path
        if cached.is_file():
            return cached
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — only a cold cache needs it

    return Path(hf_hub_download(mw.WEIGHTS_REPO, entry.path, revision=mw.WEIGHTS_REVISION))


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_static(stage_dir: Path) -> None:
    for name, src in STATIC_ASSETS.items():
        _write_atomic(stage_dir / name, src.read_bytes())
    _write_atomic(stage_dir / MANIFEST_ASSET, manifest_tsv().encode("utf-8"))


def stage(stage_dir: Path, *, say: Callable[[str], None] = print) -> list[str]:
    """Stage every manifest file into *stage_dir*; return the problems (empty when staged and verified).

    A file already there must match the manifest (it is kept) or it is a problem, left untouched: nothing
    mismatching is ever overwritten or published by accident."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    for entry in mw.MANIFEST:
        dest = stage_dir / entry.asset
        if dest.exists():
            why = _check(dest, entry)
            if why:
                problems.append(f"staged file does not match the manifest, delete it to re-stage: {why}")
            continue
        src = source_file(entry)
        why = _check(src, entry)
        if why:
            problems.append(f"upstream file {src} does not match the manifest: {why}")
            continue
        tmp = dest.with_name(dest.name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        say(f"staged {entry.asset} ({entry.size} bytes)")
    if not problems:
        write_static(stage_dir)
    return problems


def verify_stage(stage_dir: Path) -> list[str]:
    """Check *stage_dir* against the manifest: every asset present with the pinned size and sha256, and the
    license, notice and manifest files present (``MANIFEST.tsv`` equal to the module's)."""
    problems: list[str] = []
    for entry in mw.MANIFEST:
        path = stage_dir / entry.asset
        if not path.is_file():
            problems.append(f"{entry.asset}: missing")
            continue
        why = _check(path, entry)
        if why:
            problems.append(why)
    for name in STATIC_ASSETS:
        if not (stage_dir / name).is_file():
            problems.append(f"{name}: missing")
    try:
        if (stage_dir / MANIFEST_ASSET).read_text(encoding="utf-8") != manifest_tsv():
            problems.append(f"{MANIFEST_ASSET}: differs from the manifest in nexus.mineru_weights")
    except OSError:
        problems.append(f"{MANIFEST_ASSET}: missing")
    return problems


# ── upload (the release cut, under the release's go) ────────────────────────────────────────────────


def staged_digests(stage_dir: Path) -> dict[str, str]:
    """asset name -> sha256 of what would be uploaded."""
    return {name: file_sha256(stage_dir / name) for name in staged_names()}


def plan_upload(
    staged: dict[str, str],
    existing: dict[str, str | None] | None,
    remote_sha: Callable[[str], str],
) -> tuple[list[str], list[str]]:
    """``(to_upload, conflicts)``. *existing* maps the release's asset names to their sha256 (None when the
    host did not report one: then *remote_sha* fetches it), or is None when the release is absent. An asset
    that exists with the staged sha256 is left alone; one that exists with another is a conflict; the rest
    are uploaded."""
    if existing is None:
        return list(staged), []
    to_upload: list[str] = []
    conflicts: list[str] = []
    for name, digest in staged.items():
        if name not in existing:
            to_upload.append(name)
            continue
        actual = existing[name] or remote_sha(name)
        if actual != digest:
            conflicts.append(f"{name}: the release has sha256 {actual}, the staged file has {digest}")
    return to_upload, conflicts


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)  # noqa: S603, S607 — gh is the release tool


def release_assets(repo: str, tag: str) -> dict[str, str | None] | None:
    """The release's ``{asset name: sha256 or None}``, or None when it does not exist."""
    proc = _gh("release", "view", tag, "--repo", repo, "--json", "assets", check=False)
    if proc.returncode != 0:
        if "not found" in proc.stderr.lower():
            return None
        raise RuntimeError(f"gh release view {tag} failed: {proc.stderr.strip()}")
    assets = json.loads(proc.stdout).get("assets", [])
    return {a["name"]: ((a.get("digest") or "").removeprefix("sha256:") or None) for a in assets}


def _remote_sha(repo: str, tag: str) -> Callable[[str], str]:
    def fetch(name: str) -> str:
        with tempfile.TemporaryDirectory(prefix="mineru-asset-") as tmp:
            _gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", tmp)
            return file_sha256(Path(tmp) / name)

    return fetch


def upload(stage_dir: Path, repo: str, *, dry_run: bool, say: Callable[[str], None] = print) -> int:
    tag = mw.WEIGHTS_ASSET_TAG
    staged = staged_digests(stage_dir)
    existing = release_assets(repo, tag)
    to_upload, conflicts = plan_upload(staged, existing, _remote_sha(repo, tag))
    if conflicts:
        for line in conflicts:
            say(f"REFUSED: {line}")
        say(f"{tag} is immutable: nothing was uploaded.")
        return 1
    if not to_upload:
        say(f"{tag} already carries all {len(staged)} assets with the staged digests; nothing to upload.")
        return 0
    files = [str(stage_dir / name) for name in to_upload]
    verb = "would upload" if dry_run else "uploading"
    say(f"{verb} {len(to_upload)} asset(s) to {repo} {tag} ({'create the release' if existing is None else 'release exists'})")
    if dry_run:
        return 0
    if existing is None:
        notes = (
            f"MinerU pipeline weights, an unmodified copy of {mw.WEIGHTS_REPO} at {mw.WEIGHTS_REVISION}. "
            "Licenses and per-model notices are in the assets; MANIFEST.tsv lists path, size and sha256."
        )
        _gh(
            "release", "create", tag, *files, "--repo", repo, "--title", f"MinerU pipeline weights ({tag})",
            "--notes", notes, "--latest=false",
        )
    else:
        _gh("release", "upload", tag, *files, "--repo", repo)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage-dir", required=True, type=Path)
    ap.add_argument("--verify-only", action="store_true", help="check an already staged dir; source nothing")
    ap.add_argument("--upload", action="store_true", help="upload to the release tag where absent (the cut, under the go)")
    ap.add_argument("--dry-run", action="store_true", help="with --upload: print the plan, upload nothing")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    args = ap.parse_args(argv)
    if args.dry_run and not args.upload:
        ap.error("--dry-run needs --upload")

    problems = verify_stage(args.stage_dir) if args.verify_only else stage(args.stage_dir)
    if not problems:
        problems = verify_stage(args.stage_dir)
    if problems:
        for line in problems:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    total = sum(f.size for f in mw.MANIFEST)
    print(f"OK: {len(mw.MANIFEST)} files ({total} bytes) verified against the manifest in {args.stage_dir}")
    if args.upload:
        return upload(args.stage_dir, args.repo, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
