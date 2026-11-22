#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stage, verify and publish MinerU's pinned pipeline weights (F35, nexus-7t3w1).

The pin is ``nexus.mineru_weights.MANIFEST`` (upstream ``opendatalab/PDF-Extract-Kit-1.0`` at the revision
named there). This script reads it from the module, so the client, the gate and the release cannot disagree.

    publish-mineru-weights.py --stage-dir DIR            stage the files from the Hugging Face cache (any
                                                         revision whose bytes verify; else a pinned-revision
                                                         download), check each against the manifest, write the
                                                         license, notice and manifest files
    publish-mineru-weights.py --stage-dir DIR --verify-only
                                                         check an already staged DIR, source nothing
    publish-mineru-weights.py --stage-dir DIR --upload   after staging, upload to the release tag
                                                         ``mineru-pipeline-ed6b654c`` ONLY where it is absent

DIR then holds one file per asset: ``<path under models/ with "/" as "__">`` for each of the 20 manifest files,
``LICENSE-AGPL-3.0.txt``, ``LICENSE-Apache-2.0.txt``, ``THIRD-PARTY-NOTICES.md`` and ``MANIFEST.tsv``. The
candidate gate serves it over http (``NX_MINERU_WEIGHTS_BASE_URL``); the release skill runs ``--upload`` at the
cut, under the release's go.

What is refused, with nothing uploaded: a staged weight file or ``MANIFEST.tsv`` that does not match the manifest
(never repaired: delete it and re-stage), an existing release asset whose sha256 differs for a weight file or
``MANIFEST.tsv``, and a release that exists only as a draft. The license and notice assets are different: the
copy already published stays (a wording fix in the notices must not make every later cut fail), the script
warns that the repo's text differs, and uploads only the ones the release lacks. ``--upload`` creates the
release when it is absent (at ``--target``, default ``main``; not marked latest), uploads only the missing assets
and never overwrites.

``--verify-only --upload`` also refuses when a staged license or notice differs from its source in this checkout
(stage again first). A ``--stage-dir`` containing ``#`` is a usage error.

Exit 0 on success, 1 when a file or asset does not verify or the release cannot be read, 2 on a usage error."""

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
from typing import NamedTuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))  # the checkout's own manifest, whatever else is installed

from nexus import mineru_weights as mw  # noqa: E402 — after the sys.path line
from nexus.db.service_bge_model import file_sha256  # noqa: E402

DEFAULT_REPO = "Hellblazer/nexus"
DEFAULT_TARGET = "main"
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


def source_file(entry: mw.WeightFile) -> tuple[Path, bool]:
    """``(path, verified)``: the upstream bytes for *entry*. A copy in the Hugging Face cache, any revision,
    whose size and sha256 match is returned verified; otherwise a download at the pinned revision (the only
    network use of this script), not yet verified."""
    copy = mw._find_verified_copy(entry, None)
    if copy is not None:
        return copy, True
    from huggingface_hub import hf_hub_download  # noqa: PLC0415 — only a cold or mismatching cache needs it

    return Path(hf_hub_download(mw.WEIGHTS_REPO, entry.path, revision=mw.WEIGHTS_REVISION)), False


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)  # published files are world-readable; mkstemp makes them 0600
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
        src, verified = source_file(entry)
        why = None if verified else _check(src, entry)
        if why:
            problems.append(f"upstream file {src} does not match the manifest: {why}")
            continue
        fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part", dir=stage_dir)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        say(f"staged {entry.asset} ({entry.size} bytes)")
    if not problems:
        write_static(stage_dir)
    return problems


def verify_stage(stage_dir: Path) -> list[str]:
    """Check *stage_dir* against the manifest: every weight asset present with the pinned size and sha256, the
    license and notice files present, and ``MANIFEST.tsv`` equal to the module's. These refuse everything."""
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


def static_warnings(stage_dir: Path) -> list[str]:
    """License and notice assets whose staged bytes differ from the repo's source file (a stale stage dir):
    a warning, not a refusal; staging again rewrites them."""
    out: list[str] = []
    for name, src in STATIC_ASSETS.items():
        staged = stage_dir / name
        if staged.is_file() and src.is_file() and file_sha256(staged) != file_sha256(src):
            out.append(f"{name}: the staged copy differs from {src.relative_to(REPO)}; stage again to refresh it")
    return out


# ── upload (the release cut, under the release's go) ────────────────────────────────────────────────


def staged_digests(stage_dir: Path) -> dict[str, str]:
    """asset name -> sha256 of what would be uploaded."""
    return {name: file_sha256(stage_dir / name) for name in staged_names()}


class Plan(NamedTuple):
    to_upload: list[str]
    conflicts: list[str]  # a weight file or MANIFEST.tsv that differs: refuses everything
    kept: list[str]  # a license or notice asset that differs: the published copy stays


def plan_upload(
    staged: dict[str, str],
    existing: dict[str, str | None] | None,
    remote_sha: Callable[[str], str],
    static: frozenset[str] = frozenset(STATIC_ASSETS),
) -> Plan:
    """What an upload does. *existing* maps the release's asset names to their sha256 (None when the host did
    not report a sha256: then *remote_sha* fetches and hashes it), or is None when the release is absent. An
    asset that exists with the staged sha256 is left alone; a weight file or ``MANIFEST.tsv`` that exists with
    another is a conflict; a name in *static* that exists with another stays as published (*kept*); the rest
    are uploaded."""
    if existing is None:
        return Plan(list(staged), [], [])
    to_upload: list[str] = []
    conflicts: list[str] = []
    kept: list[str] = []
    for name, digest in staged.items():
        if name not in existing:
            to_upload.append(name)
            continue
        actual = existing[name] or remote_sha(name)
        if actual == digest:
            continue
        line = f"{name}: the release has sha256 {actual}, the staged file has {digest}"
        (kept if name in static else conflicts).append(line)
    return Plan(to_upload, conflicts, kept)


class ReleaseInfo(NamedTuple):
    assets: dict[str, str | None]
    is_draft: bool


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)  # noqa: S603, S607 — gh is the release tool


def asset_sha256(asset: dict) -> str | None:
    """The sha256 the host reports for *asset*, or None. Only a ``sha256:`` digest is read as one; any other
    algorithm (or none) is unknown, and the caller downloads and hashes the asset instead."""
    digest = asset.get("digest") or ""
    return digest[len("sha256:"):] if digest.startswith("sha256:") else None


def release_assets(repo: str, tag: str) -> ReleaseInfo | None:
    """The release's assets (name -> sha256 or None) and draft flag, or None when it does not exist.
    Raises RuntimeError with a one-line cause when ``gh`` cannot read it."""
    try:
        proc = _gh("release", "view", tag, "--repo", repo, "--json", "assets,isDraft", check=False)
    except OSError as exc:
        raise RuntimeError(f"cannot run gh: {exc}") from exc
    if proc.returncode != 0:
        if "not found" in proc.stderr.lower():
            return None
        raise RuntimeError(f"gh release view {tag} failed: {proc.stderr.strip() or proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"gh release view {tag} returned unreadable JSON: {exc}") from exc
    return ReleaseInfo({a["name"]: asset_sha256(a) for a in data.get("assets", [])}, bool(data.get("isDraft")))


def _remote_sha(repo: str, tag: str) -> Callable[[str], str]:
    def fetch(name: str) -> str:
        with tempfile.TemporaryDirectory(prefix="mineru-asset-") as tmp:
            _gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", tmp)
            return file_sha256(Path(tmp) / name)

    return fetch


def upload(
    stage_dir: Path, repo: str, *, dry_run: bool, target: str = DEFAULT_TARGET, say: Callable[[str], None] = print
) -> int:
    tag = mw.WEIGHTS_ASSET_TAG
    staged = staged_digests(stage_dir)
    try:
        info = release_assets(repo, tag)
        if info is not None and info.is_draft:
            say(
                f"REFUSED: {repo} has {tag} only as a draft release. Publish it or delete it by hand, then run "
                "this again: a draft's assets are not the published tag the client downloads from."
            )
            return 1
        plan = plan_upload(staged, None if info is None else info.assets, _remote_sha(repo, tag))
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        say(f"ERROR: could not read the release {tag} on {repo}: {exc}")
        return 1
    for line in plan.kept:
        say(f"WARNING: keeping the published copy: {line}")
    if plan.conflicts:
        for line in plan.conflicts:
            say(f"REFUSED: {line}")
        say(f"{tag} is immutable: nothing was uploaded.")
        return 1
    if not plan.to_upload:
        say(f"{tag} already carries all {len(staged)} assets (differing license or notice text kept as published); nothing to upload.")
        return 0
    files = [str(stage_dir / name) for name in plan.to_upload]
    state = f"create the release, tag at {target}" if info is None else "release exists"
    say(f"{'would upload' if dry_run else 'uploading'} {len(plan.to_upload)} asset(s) to {repo} {tag} ({state})")
    if dry_run:
        return 0
    try:
        if info is None:
            notes = (
                f"MinerU pipeline weights, an unmodified copy of {mw.WEIGHTS_REPO} at {mw.WEIGHTS_REVISION}. "
                "Licenses and per-model notices are in the assets; MANIFEST.tsv lists path, size and sha256."
            )
            _gh(
                "release", "create", tag, *files, "--repo", repo, "--title", f"MinerU pipeline weights ({tag})",
                "--notes", notes, "--target", target, "--latest=false",
            )
        else:
            _gh("release", "upload", tag, *files, "--repo", repo)
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        say(f"ERROR: the upload to {repo} {tag} failed: {str(detail).strip()}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stage-dir", required=True, type=Path)
    ap.add_argument("--verify-only", action="store_true", help="check an already staged dir; source nothing")
    ap.add_argument("--upload", action="store_true", help="upload to the release tag where absent (the cut, under the go)")
    ap.add_argument("--dry-run", action="store_true", help="with --upload: print the plan, upload nothing")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--target", default=DEFAULT_TARGET, help="the branch a newly created release's tag points at")
    args = ap.parse_args(argv)
    if args.dry_run and not args.upload:
        ap.error("--dry-run needs --upload")
    if "#" in str(args.stage_dir):
        ap.error("--stage-dir must not contain '#': it breaks the asset URLs the gate serves and the paths gh is given")

    problems = verify_stage(args.stage_dir) if args.verify_only else stage(args.stage_dir)
    if not problems:
        problems = verify_stage(args.stage_dir)
    if problems:
        for line in problems:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    stale = static_warnings(args.stage_dir)
    for line in stale:
        print(f"WARNING: {line}", file=sys.stderr)
    if stale and args.upload:
        # --verify-only does not re-stage, so the stale license or notice is exactly what would go up.
        for line in stale:
            print(f"FAIL: refusing to upload a stale static asset: {line}", file=sys.stderr)
        return 1
    total = sum(f.size for f in mw.MANIFEST)
    print(f"OK: {len(mw.MANIFEST)} files ({total} bytes) verified against the manifest in {args.stage_dir}")
    if args.upload:
        return upload(args.stage_dir, args.repo, dry_run=args.dry_run, target=args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
