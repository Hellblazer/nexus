# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Point an E2E gate at the CANDIDATE engine, and prove which engine it ran
against (nexus-0kmat).

WHY. fresh-install-mvv.sh and data-token-cli-gate.sh launch ``nx`` under
``env -i`` with an allowlist that drops every ``NX_*`` and ``NEXUS_SERVICE_*``
variable, and ``nx init`` then downloads the engine at REQUIRED_ENGINE_VERSION:
the PUBLISHED, pinned tag. release-sandbox.sh (battery legs smoke and
shakedown) takes whatever binary is installed. A gate that provisions the
pinned engine proves nothing about an engine change that is not yet tagged, so
the RDR-223 ownerless-write refusal passed every one of them vacuously: the
engine they ran has no ownership check. The pin only moves after the tag is
immutable, so a missed writer costs a re-cut.

THE ONE INPUT. ``NX_CANDIDATE_ENGINE=<path>`` names the candidate: a ``*.jar``
(launched through ``NEXUS_SERVICE_JAR``; the JVM-jar path
``scripts/build-gate-jar.sh`` produces, far cheaper than a native build and the
shim AGENTS.md's release-workflow shape-check section sanctions) or an
executable native binary (``NEXUS_SERVICE_BIN``). Each gate reads it here and
puts the resulting variables INSIDE its own ``env -i`` allowlist, so the
scrub no longer drops it. ``NX_CUT_MODE=1`` says "this run gates a cut": there,
no candidate is a refusal, never a quiet fall-back to the pinned engine.

SUBCOMMANDS (all stdlib, no nexus import: the gates run this under the system
python3 with a scrubbed environment):

``env [--stage DIR]``
    Resolve the candidate from the environment and print the variables a gate
    adds to its ``env -i`` allowlist, one ``NAME=VALUE`` per line. With
    ``--stage DIR`` the candidate is first COPIED into DIR and the variables
    name the copy: the supervisor finds its own engine processes by argv
    (``-jar <path>``), so two gates launching the same jar path at once
    stop and mis-adopt each other's engines (measured 2026-10-01: a concurrent
    gate's teardown SIGTERMed the other's freshly spawned engine, exit 143).
    One private copy per gate removes the collision, which the battery's
    parallel gate group would otherwise hit on every cut. Exit 0 with
    no output when no candidate is set and cut mode is off. Exit 2 (message on
    stderr) when cut mode is on and no candidate is set, when the candidate is
    set but missing / not a file / not executable, or when an ambient
    ``NEXUS_SERVICE_JAR`` / ``NEXUS_SERVICE_BIN`` names a different artifact
    (two launch artifacts is a contradiction, not a precedence rule).

``identity <config-dir> [--label L]``
    After the gate provisioned its engine, read the live lease under
    ``<config-dir>`` and print ONE line naming the engine that is actually
    serving: ``ENGINE IDENTITY [L]: candidate=yes|no kind=... artifact=...
    sha256=... release_version=... build_ref=... ownerless_write_mode=...``.
    In cut mode, exit 1 unless the lease's artifact IS the candidate, judged
    by sha256 of the bytes (a staged copy is the candidate; the pinned
    published engine is not, whatever its path) -- a leg that ran against the
    pinned published engine is a FAILURE -- and ``/v1/status`` is reachable
    and reports the expected ``ownerless_write_mode`` (default ``enforce``;
    ``NX_CANDIDATE_EXPECT_OWNERLESS_MODE=none`` drops ONLY that mode assert,
    for a candidate run in a mode other than enforce: the engine must still
    be reachable and still carry the counters).

``refusals <config-dir> [--label L]``
    End of journey. Re-check that the engine STILL serving is the candidate
    (a swap mid-journey is caught here), print the ownerless-write counters,
    and count ``ownerless_chunk_write_refused`` / ``..._would_refuse`` lines in
    the engine log the lease's launch kind names (``storage_service_jar.log``
    for a jar, ``storage_service_native.log`` for a native binary, plus
    rotations). In cut mode exit 1 on any refusal or would-refuse (a red gate
    IS the oracle: a writer this journey exercises wrote a chunk with no
    manifest owner; fix the writer before tagging), on an unreachable
    ``/v1/status``, on both counters reading absent, and on a missing engine
    log (the engine ran, so its log exists).

``cut-assert-log <logfile> <label> [--candidate PATH]``
    Battery side. In cut mode, exit 1 unless the leg's log carries BOTH an
    ``ENGINE IDENTITY`` line and an ``ENGINE OWNERLESS REFUSALS`` line, every
    one naming the candidate by sha256 (``--candidate`` overrides
    ``NX_CANDIDATE_ENGINE`` for a leg whose engine is not that file, e.g. the
    native binary a container leg runs), the refusals lines carrying numeric
    zero counters and zero log hits and the mode the run expects. A leg whose
    own checks went green but which never reported an engine, never read the
    refusal counters, or reported the pinned engine, is not a pass.

``manifest-artifact <artifacts-dir> jar|native`` / ``candidate-in-manifest <artifacts-dir> <path>``
    Battery side. Print the manifest's path for an artifact; say which
    manifest artifact (if any) a candidate file is, by sha256.

Runs under any python3 >= 3.9: the gates call it with the system python
before any uv environment exists.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

CANDIDATE_ENV = "NX_CANDIDATE_ENGINE"
CUT_MODE_ENV = "NX_CUT_MODE"
EXPECT_MODE_ENV = "NX_CANDIDATE_EXPECT_OWNERLESS_MODE"
DEFAULT_EXPECT_MODE = "enforce"
#: Both events: enforce logs ``..._refused``, log-only logs ``..._would_refuse``.
REFUSAL_LOG_RE = re.compile(r"ownerless_chunk_write_(?:refused|would_refuse)")
IDENTITY_PREFIX = "ENGINE IDENTITY"
REFUSALS_PREFIX = "ENGINE OWNERLESS REFUSALS"
_LAUNCH_VARS = ("NEXUS_SERVICE_JAR", "NEXUS_SERVICE_BIN")


class CandidateError(Exception):
    """A refusal the caller prints and turns into a nonzero exit."""


def cut_mode(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(CUT_MODE_ENV, "").strip() == "1"


def candidate_path(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(CANDIDATE_ENV, "").strip()


def expected_mode(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(EXPECT_MODE_ENV, DEFAULT_EXPECT_MODE).strip() or DEFAULT_EXPECT_MODE


def _real(path: str) -> str:
    return os.path.realpath(path)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _java_home(environ: dict[str, str]) -> str:
    """JAVA_HOME for a jar launch. The gates' ``env -i`` PATH is
    ``<venv>/bin:/usr/bin:/bin``, where macOS's ``/usr/bin/java`` is a stub, so
    a JVM the operator reached through ``PATH`` must travel by name."""
    configured = environ.get("JAVA_HOME", "").strip()
    if configured:
        return configured
    found = shutil.which("java", path=environ.get("PATH"))
    if not found:
        return ""
    return os.path.dirname(os.path.dirname(_real(found)))


def _stage(raw: str, stage_dir: str) -> str:
    """Copy the candidate into *stage_dir* and return the copy's path."""
    os.makedirs(stage_dir, exist_ok=True)
    dest = os.path.join(stage_dir, os.path.basename(raw))
    if not (os.path.isfile(dest) and _sha256(dest) == _sha256(raw)):
        shutil.copy2(raw, dest)
    return dest


def resolve_env(environ: dict[str, str] | None = None, stage_dir: str = "") -> list[str]:
    """The ``NAME=VALUE`` lines a gate adds to its ``env -i`` allowlist."""
    env = dict(os.environ if environ is None else environ)
    raw = candidate_path(env)
    if not raw:
        if cut_mode(env):
            raise CandidateError(
                f"CUT MODE ({CUT_MODE_ENV}=1) but {CANDIDATE_ENV} is unset: this gate "
                "would provision the PINNED PUBLISHED engine, which has no ownership "
                "check, and pass vacuously for an unreleased engine change. Build the "
                "candidate (scripts/build-gate-jar.sh, or the battery's artifacts leg) "
                f"and set {CANDIDATE_ENV}=<jar or native binary>."
            )
        return []
    if not os.path.isfile(raw):
        raise CandidateError(
            f"{CANDIDATE_ENV}={raw!r} is not a file. Refusing to fall back to the "
            "pinned published engine: a gate asked for a candidate it cannot find "
            "has proved nothing about it."
        )
    real = _real(raw)
    is_jar = raw.endswith(".jar")
    if not is_jar and not os.access(raw, os.X_OK):
        raise CandidateError(
            f"{CANDIDATE_ENV}={raw!r} is not a *.jar and is not executable; a native "
            f"candidate needs its execute bit (chmod +x {raw})."
        )
    mine, other = ("NEXUS_SERVICE_JAR", "NEXUS_SERVICE_BIN") if is_jar else (
        "NEXUS_SERVICE_BIN", "NEXUS_SERVICE_JAR")
    for var in _LAUNCH_VARS:
        ambient = env.get(var, "").strip()
        if ambient and (var == other or _real(ambient) != real):
            raise CandidateError(
                f"{CANDIDATE_ENV}={raw!r} and ambient {var}={ambient!r} name different "
                "launch artifacts; set one (the supervisor would honour whichever it "
                "reads first, and the gate would report the wrong engine)."
            )
    if stage_dir:
        real = _real(_stage(raw, stage_dir))
    lines = [f"{mine}={real}"]
    if is_jar:
        home = _java_home(env)
        if not home:
            raise CandidateError(
                f"{CANDIDATE_ENV}={raw!r} is a jar but no JVM is reachable: set JAVA_HOME "
                "or put java on PATH."
            )
        lines.append(f"JAVA_HOME={home}")
    return lines


def _read_lease(config_dir: str) -> dict:
    leases = sorted(glob.glob(os.path.join(config_dir, "storage_service_addr.*")))
    if not leases:
        raise CandidateError(
            f"no storage_service_addr.* lease under {config_dir}: the gate has no live "
            "engine to name, so it cannot say which engine it ran against."
        )
    with open(leases[0], encoding="utf-8") as fh:
        record = json.load(fh)
    return record.get("endpoint", record)


def _get_json(host: str, port: int, path: str) -> dict | None:
    """GET a loopback engine route, or None. An EMPTY proxy map on purpose: an
    ambient HTTP(S)_PROXY / ALL_PROXY makes urllib route 127.0.0.1 through the
    proxy and the probe fails, which a gate would then read as 'no counters'."""
    url = f"http://{host}:{port}{path}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as resp:  # noqa: S310 - loopback engine
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _engine_probe(config_dir: str) -> tuple[dict, dict | None, dict | None]:
    """``(lease endpoint, /version or None, /v1/status or None)``; None means unreachable."""
    ep = _read_lease(config_dir)
    host, port = ep.get("host", "127.0.0.1"), int(ep["port"])
    return ep, _get_json(host, port, "/version"), _get_json(host, port, "/v1/status")


def _fmt(value: object) -> str:
    return "none" if value in (None, "") else str(value)


def _artifact_facts(ep: dict, environ: dict[str, str]) -> tuple[bool, str, str, object]:
    """``(is_candidate, kind, sha256, artifact)`` for the artifact the lease names."""
    artifact = ep.get("artifact")
    cand = candidate_path(environ)
    exists = bool(artifact) and os.path.isfile(str(artifact))
    kind, sha = "none", "none"
    if exists:
        kind = "jar" if str(artifact).endswith(".jar") else "native"
        sha = _sha256(str(artifact))
    is_candidate = bool(exists and cand and os.path.isfile(cand) and sha == _sha256(cand))
    return is_candidate, kind, sha, artifact


def _not_candidate_failure(artifact: object, sha: str, cand: str, when: str = "") -> str:
    return (
        f"cut mode: this leg {when}ran against {_fmt(artifact)} (sha256 {sha}), not the "
        f"candidate {cand}. "
        "A leg that ran against the pinned published engine is a failure, whatever "
        "its own checks said."
    )


def identity(config_dir: str, label: str, environ: dict[str, str] | None = None) -> tuple[str, str | None]:
    """Return ``(identity_line, failure_or_None)``."""
    env = os.environ if environ is None else environ
    ep, version, status = _engine_probe(config_dir)
    is_candidate, kind, sha, artifact = _artifact_facts(ep, env)
    mode = (status or {}).get("ownerless_write_mode")
    line = (
        f"{IDENTITY_PREFIX} [{label}]: candidate={'yes' if is_candidate else 'no'} "
        f"kind={kind} artifact={_fmt(artifact)} sha256={sha} "
        f"release_version={_fmt((version or {}).get('release_version'))} "
        f"build_ref={_fmt((version or {}).get('build_ref'))} "
        f"ownerless_write_mode={_fmt(mode)}"
    )
    if not cut_mode(env):
        return line, None
    cand = candidate_path(env)
    if not cand:
        return line, f"cut mode without {CANDIDATE_ENV}"
    if not is_candidate:
        return line, _not_candidate_failure(artifact, sha, cand)
    if status is None or version is None:
        gone = "/v1/status" if status is None else "/version"
        return line, (
            f"cut mode: the engine's {gone} is unreachable (no proxy is used for the loopback "
            "probe), so this leg cannot say which mode the engine runs or what it counted."
        )
    expect = expected_mode(env)
    if expect == "none":
        sys.stderr.write(
            f"WARNING: {EXPECT_MODE_ENV}=none, so this run does not assert the "
            "engine's ownerless_write_mode (everything else is still asserted).\n"
        )
        return line, None
    if mode != expect:
        return line, (
            f"cut mode: /v1/status reports ownerless_write_mode={_fmt(mode)}, expected "
            f"{expect}. The candidate has no ownerless-write check (or runs it in the "
            "wrong mode), so this leg cannot catch an ownerless writer."
        )
    return line, None


def _engine_logs(config_dir: str, ep: dict) -> list[str]:
    """The engine log(s) the lease's launch kind names, rotations included. A jar launch
    writes ``storage_service_jar.log``, a native launch ``storage_service_native.log``
    (storage_service_daemon.py ``_svc_log_name``)."""
    kind = ep.get("launch_kind")
    if kind not in ("jar", "native"):
        kind = "jar" if str(ep.get("artifact", "")).endswith(".jar") else "native"
    pattern = os.path.join(config_dir, "logs", f"storage_service_{kind}.log*")
    return sorted(p for p in glob.glob(pattern) if os.path.isfile(p))


def refusals(config_dir: str, label: str, environ: dict[str, str] | None = None) -> tuple[str, str | None]:
    env = os.environ if environ is None else environ
    ep, _version, status = _engine_probe(config_dir)
    is_candidate, _kind, sha, artifact = _artifact_facts(ep, env)
    status_d = status or {}
    refused = status_d.get("ownerless_writes_refused_total")
    would = status_d.get("ownerless_writes_would_refuse_total")
    logs = _engine_logs(config_dir, ep)
    log_hits = 0
    for path in logs:
        with open(path, encoding="utf-8", errors="replace") as fh:
            log_hits += sum(1 for ln in fh if REFUSAL_LOG_RE.search(ln))
    log_cell = "none" if not logs else ",".join(os.path.basename(p) for p in logs)
    line = (
        f"{REFUSALS_PREFIX} [{label}]: candidate={'yes' if is_candidate else 'no'} sha256={sha} "
        f"refused_total={_fmt(refused)} would_refuse_total={_fmt(would)} "
        f"log_lines={log_hits if logs else 'none'} log={log_cell} "
        f"mode={_fmt(status_d.get('ownerless_write_mode'))}"
    )
    if not cut_mode(env):
        return line, None
    cand = candidate_path(env)
    if not cand:
        return line, f"cut mode without {CANDIDATE_ENV}"
    if not is_candidate:
        return line, _not_candidate_failure(artifact, sha, cand, "ended its journey: it ")
    if status is None:
        return line, (
            "cut mode: /v1/status is unreachable at the end of the journey, so a refusal "
            "could not have been seen"
        )
    if refused is None and would is None:
        return line, (
            "cut mode: /v1/status carries no ownerless-write counters, so a refusal "
            "could not have been seen"
        )
    if (refused or 0) > 0 or (would or 0) > 0 or log_hits > 0:
        return line, (
            "cut mode: the engine refused (or would refuse) an ownerless chunk write "
            "during this journey. A writer this gate exercises writes a chunk with no "
            "manifest owner: fix the writer before tagging "
            f"(engine log: {', '.join(logs) or 'none'})."
        )
    if not logs:
        return line, (
            f"cut mode: no engine log under {os.path.join(config_dir, 'logs')} for the lease's "
            "launch kind, so the log half of the refusal oracle read nothing (the engine ran, "
            "so its log exists; the counters alone do not survive an engine restart)"
        )
    return line, None


_IDENTITY_RE = re.compile(
    rf"^\s*{IDENTITY_PREFIX} \[[^\]]*\]: candidate=(yes|no) .*artifact=(\S+) sha256=(\w+)", re.M
)
_REFUSALS_RE = re.compile(
    rf"^\s*{REFUSALS_PREFIX} \[[^\]]*\]: candidate=(yes|no) sha256=(\w+) "
    r"refused_total=(\S+) would_refuse_total=(\S+) log_lines=(\S+) log=\S+ mode=(\S+)", re.M
)


def cut_assert_log(
    logfile: str, label: str, environ: dict[str, str] | None = None, candidate: str = "",
) -> str | None:
    env = os.environ if environ is None else environ
    if not cut_mode(env):
        return None
    cand = candidate or candidate_path(env)
    if not cand:
        return f"{label}: cut mode without {CANDIDATE_ENV}"
    try:
        with open(logfile, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return f"{label}: cannot read leg log {logfile}: {exc}"
    try:
        want = _sha256(cand)
    except OSError as exc:
        return f"{label}: cannot read the candidate {cand}: {exc}"
    ids = _IDENTITY_RE.findall(text)
    if not ids:
        return (
            f"{label}: cut mode, but the leg log carries no '{IDENTITY_PREFIX}' line, so "
            "the leg never said which engine it ran against (pinned published engine?)"
        )
    bad = [art for flag, art, sha in ids if flag != "yes" or sha != want]
    if bad:
        return f"{label}: cut mode, but the leg ran against {bad[0]}, not the candidate {cand}"
    refs = _REFUSALS_RE.findall(text)
    if not refs:
        return (
            f"{label}: cut mode, but the leg log carries no '{REFUSALS_PREFIX}' line: the leg "
            "never read the engine's ownerless-write counters and log at the end of its journey"
        )
    expect = expected_mode(env)
    for flag, sha, refused, would, log_lines, mode in refs:
        if flag != "yes" or sha != want:
            return (
                f"{label}: cut mode, but the engine at the END of the journey was not the "
                f"candidate {cand} (swapped mid-journey?)"
            )
        if not (refused.isdigit() and would.isdigit() and log_lines.isdigit()):
            return (
                f"{label}: cut mode, but the refusals line has no usable reading "
                f"(refused_total={refused} would_refuse_total={would} log_lines={log_lines}): "
                "a refusal could not have been seen"
            )
        if int(refused) or int(would) or int(log_lines):
            return (
                f"{label}: cut mode, but the engine refused (or would refuse) an ownerless "
                f"chunk write (refused_total={refused} would_refuse_total={would} "
                f"log_lines={log_lines})"
            )
        if expect != "none" and mode != expect:
            return f"{label}: cut mode, but the engine reported ownerless_write_mode={mode}, expected {expect}"
    return None


def _manifest(artifacts_dir: str) -> dict:
    path = os.path.join(artifacts_dir, "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise CandidateError(f"cannot read {path}: {exc}") from exc


def manifest_artifact(artifacts_dir: str, name: str) -> str:
    entry = _manifest(artifacts_dir).get("artifacts", {}).get(name)
    if not entry:
        raise CandidateError(f"{artifacts_dir}/manifest.json has no '{name}' artifact")
    return os.path.join(artifacts_dir, entry["path"])


def candidate_in_manifest(artifacts_dir: str, candidate: str) -> str:
    """``jar`` / ``native`` when *candidate* is byte-identical to that manifest artifact, else ``none``."""
    arts = _manifest(artifacts_dir).get("artifacts", {})
    sha = _sha256(candidate)
    for name in ("jar", "native"):
        if name in arts and arts[name].get("sha256") == sha:
            return name
    return "none"


def _label(rest: list[str]) -> str:
    return rest[1] if rest[:1] == ["--label"] and len(rest) > 1 else "gate"


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "env" and args == []:
            for line in resolve_env():
                print(line)
            return 0
        if cmd == "env" and len(args) == 2 and args[0] == "--stage":
            for line in resolve_env(stage_dir=args[1]):
                print(line)
            return 0
        if cmd in ("identity", "refusals") and args:
            config_dir, label = args[0], _label(args[1:])
            line, failure = (identity if cmd == "identity" else refusals)(config_dir, label)
        elif cmd == "cut-assert-log" and len(args) in (2, 4) and (len(args) == 2 or args[2] == "--candidate"):
            failure = cut_assert_log(args[0], args[1], candidate=args[3] if len(args) == 4 else "")
            if failure:
                print(f"CANDIDATE ENGINE CHECK FAILED: {failure}", file=sys.stderr)
                return 1
            return 0
        elif cmd == "manifest-artifact" and len(args) == 2 and args[1] in ("jar", "native"):
            print(manifest_artifact(args[0], args[1]))
            return 0
        elif cmd == "candidate-in-manifest" and len(args) == 2:
            which = candidate_in_manifest(args[0], args[1])
            print(which)
            return 0 if which != "none" else 1
        else:
            sys.stderr.write(__doc__ or "")
            return 2
    except CandidateError as exc:
        print(f"CANDIDATE ENGINE REFUSED: {exc}", file=sys.stderr)
        return 2
    print(line)
    if failure:
        print(f"CANDIDATE ENGINE CHECK FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
