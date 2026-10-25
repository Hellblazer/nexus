#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Constraints resolve gate (nexus-2k66t.4.4): the shipped dependency lock must
# RESOLVE on every supported target, off-host.
#
#   tests/e2e/constraints-resolve-gate.sh [--list]
#
# WHY. tests/test_install_constraints_pin.py proves src/nexus/_install/
# constraints.txt equals uv.lock. It cannot prove the lock INSTALLS anywhere: a
# universal lock can name a version whose wheels a target cannot take (the
# onnxruntime wheel set starts at macOS 14). The install path hands uv the
# shipped overrides.txt and constraints.txt (install_generation.sh); this gate
# runs the same resolution, `uv pip compile` with the same two files, for every
# platform the product claims, without installing anything.
#
# CELLS (one compile each; the table below is the list, EXPECTED is its size):
#   default install (no extra), torch backend cpu on Linux (core deps carry no
#   torch, so a GPU backend there would only repeat cpu):
#     linux x86_64 and aarch64 x python 3.12 and 3.13                        4
#     windows x64 x python 3.12 and 3.13                                     2
#     macOS arm64 at macOS 14 (the declared floor) x python 3.12 and 3.13    2
#   the [pdf] extra (every ordinary install path adds it since nexus-2k66t.4.9):
#     linux x86_64 and aarch64 x python 3.12 and 3.13 x backend cpu          4
#     linux x86_64 and aarch64 x python 3.12 and 3.13 x backend cu130 (CUDA 13.x)  4
#     linux x86_64 and aarch64 x python 3.12 x backend cu126 (CUDA 12.6-12.8)  2
#     windows x64 x python 3.12 and 3.13                                     2
#     macOS arm64 at macOS 14 x python 3.12 and 3.13                         2
# 22 compiles, plus the macOS 13 control. The GPU backends are the NX_TORCH_BACKEND
# opt-in; the docs name cu130 and cu126 for the two driver generations. macOS 14
# is expressed through MACOSX_DEPLOYMENT_TARGET, which uv reads for
# aarch64-apple-darwin (its default is 13.0, where the onnxruntime wheels end:
# that is the failure platform_failure_hint in src/nexus/commands/self_cmd.py
# names).
#
# The package under test is the CHECKOUT's dependency set (nothing of this tree
# is on the index before the tag). It is read out of pyproject.toml as plain
# requirements (project.dependencies plus the chosen extra), NOT handed to uv as
# a project: uv would apply [tool.uv.sources], which pins torch to the CPU index
# on Linux, and every GPU target would silently resolve CPU torch (measured
# 2026-10-24). The installed wheel carries no sources, so neither does this.
# --no-config keeps the repo's [tool.uv] table out of it for the same reason.
#
# Checks that keep a green from being vacuous. (1) Every [pdf] compile must pin
# docling-slim and mineru, the packages the extra exists for, and reach a pin
# count near the real lock's (a half-resolve is a failure). (2) Each Linux [pdf]
# cell must resolve torch to its backend's local version (+cpu, +cu130, +cu126).
# (3) A NEGATIVE CONTROL: macOS 13 must NOT resolve, and must fail with uv's
# platform-tag error for onnxruntime (the wording platform_failure_hint matches);
# if it resolves, the macOS floor moved and the docs and this control move with it.
# (4) A probe up front: this uv must accept every --torch-backend the cells use.
#
# NON-VACUITY (the nexus-moht0 doctrine). Every declared compile must run
# (EXPECTED is a literal, not derived from the table); the verdict prints
# examined, resolved and failed; a compile that exits 0 with fewer pins than its
# extra set's floor counts as a failure; zero examined is a failure; a missing
# uv or input file is a failure, never a skip. There is no path that exits 0
# without the PASSED line, and no skip vocabulary.
#
# Needs the network (the package index metadata, the torch indexes). Not in the
# default suite; a release-battery leg (`resolve`) and runnable by hand. The
# offline self-test is tests/e2e/constraints_resolve_gate_test.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
UV="${NX_RESOLVE_UV:-uv}"
# shellcheck source=lib/python.sh disable=SC1091
source "$HERE/lib/python.sh"
PYPROJECT="${NX_RESOLVE_PYPROJECT:-$REPO_ROOT/pyproject.toml}"
OVERRIDES="${NX_RESOLVE_OVERRIDES:-$REPO_ROOT/src/nexus/_install/overrides.txt}"
CONSTRAINTS="${NX_RESOLVE_CONSTRAINTS:-$REPO_ROOT/src/nexus/_install/constraints.txt}"
# Pin-count floors, 80-85% of the real lock's compile on each extra set
# (measured live 2026-10-24: default 135-136 pins, [pdf] 212-231 depending on target).
MIN_PINS_DEFAULT="${NX_RESOLVE_MIN_PINS_DEFAULT:-110}"
MIN_PINS_PDF="${NX_RESOLVE_MIN_PINS_PDF:-180}"
# Every [pdf] compile must pin these (anchored regexes over the compiled output).
PDF_REQUIRED=("^docling-slim==" "^mineru==")
# The literal size of CELLS. Hard-coded on purpose: deleting a row must fail.
EXPECTED=22

# tag|extra ("-" = the default install)|uv --python-platform|python|torch backend ("-" = none)|MACOSX_DEPLOYMENT_TARGET ("-" = none)|torch pin regex ("-" = none)
CELLS=(
  "linux-x86_64-py3.12-cpu/default|-|x86_64-unknown-linux-gnu|3.12|cpu|-|-"
  "linux-x86_64-py3.13-cpu/default|-|x86_64-unknown-linux-gnu|3.13|cpu|-|-"
  "linux-aarch64-py3.12-cpu/default|-|aarch64-unknown-linux-gnu|3.12|cpu|-|-"
  "linux-aarch64-py3.13-cpu/default|-|aarch64-unknown-linux-gnu|3.13|cpu|-|-"
  "windows-x64-py3.12/default|-|x86_64-pc-windows-msvc|3.12|-|-|-"
  "windows-x64-py3.13/default|-|x86_64-pc-windows-msvc|3.13|-|-|-"
  "macos14-arm64-py3.12/default|-|aarch64-apple-darwin|3.12|-|14.0|-"
  "macos14-arm64-py3.13/default|-|aarch64-apple-darwin|3.13|-|14.0|-"
  "linux-x86_64-py3.12-cpu/pdf|pdf|x86_64-unknown-linux-gnu|3.12|cpu|-|^torch==[0-9.]+[+]cpu"
  "linux-x86_64-py3.13-cpu/pdf|pdf|x86_64-unknown-linux-gnu|3.13|cpu|-|^torch==[0-9.]+[+]cpu"
  "linux-x86_64-py3.12-cu130/pdf|pdf|x86_64-unknown-linux-gnu|3.12|cu130|-|^torch==[0-9.]+[+]cu130"
  "linux-x86_64-py3.13-cu130/pdf|pdf|x86_64-unknown-linux-gnu|3.13|cu130|-|^torch==[0-9.]+[+]cu130"
  "linux-x86_64-py3.12-cu126/pdf|pdf|x86_64-unknown-linux-gnu|3.12|cu126|-|^torch==[0-9.]+[+]cu126"
  "linux-aarch64-py3.12-cpu/pdf|pdf|aarch64-unknown-linux-gnu|3.12|cpu|-|^torch==[0-9.]+[+]cpu"
  "linux-aarch64-py3.13-cpu/pdf|pdf|aarch64-unknown-linux-gnu|3.13|cpu|-|^torch==[0-9.]+[+]cpu"
  "linux-aarch64-py3.12-cu130/pdf|pdf|aarch64-unknown-linux-gnu|3.12|cu130|-|^torch==[0-9.]+[+]cu130"
  "linux-aarch64-py3.13-cu130/pdf|pdf|aarch64-unknown-linux-gnu|3.13|cu130|-|^torch==[0-9.]+[+]cu130"
  "linux-aarch64-py3.12-cu126/pdf|pdf|aarch64-unknown-linux-gnu|3.12|cu126|-|^torch==[0-9.]+[+]cu126"
  "windows-x64-py3.12/pdf|pdf|x86_64-pc-windows-msvc|3.12|-|-|-"
  "windows-x64-py3.13/pdf|pdf|x86_64-pc-windows-msvc|3.13|-|-|-"
  "macos14-arm64-py3.12/pdf|pdf|aarch64-apple-darwin|3.12|-|14.0|-"
  "macos14-arm64-py3.13/pdf|pdf|aarch64-apple-darwin|3.13|-|14.0|-"
)

if [ "${1:-}" = "--list" ]; then
  for c in "${CELLS[@]}"; do printf '%s\n' "${c%%|*}"; done
  echo "control-macos13/default"
  exit 0
fi
[ $# -eq 0 ] || { echo "usage: constraints-resolve-gate.sh [--list]" >&2; exit 2; }

fail_closed() {
  echo "constraints-resolve-gate: $1" >&2
  echo "CONSTRAINTS RESOLVE GATE FAILED — $1"
  exit 1
}

command -v "$UV" >/dev/null 2>&1 || fail_closed "uv is not on PATH ($UV); this gate cannot resolve anything without it"
for f in "$PYPROJECT" "$OVERRIDES" "$CONSTRAINTS"; do
  [ -f "$f" ] || fail_closed "input file is missing: $f"
done
# 3.11 for tomllib.
e2e_python_resolve 11 || fail_closed "no Python >= 3.11 to read pyproject.toml"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nx-resolve-gate.XXXXXX")" || fail_closed "cannot create a scratch directory"
trap 'rm -rf "$WORK"' EXIT

# The package's requirements as wheel metadata would state them: the runtime
# dependencies plus one extra, no [tool.uv.sources] (see the header).
write_requirements() {  # write_requirements <extra-or-"-"> <out-file>
  "$E2E_PYTHON" - "$PYPROJECT" "$1" "$2" <<'PYEOF' || return 1
import sys, tomllib
src, extra, out = sys.argv[1:4]
with open(src, "rb") as fh:
    project = tomllib.load(fh)["project"]
reqs = list(project["dependencies"])
if extra != "-":
    reqs += project["optional-dependencies"][extra]
if not reqs:
    sys.exit("no requirements read from " + src)
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(reqs) + "\n")
PYEOF
}

EXAMINED=0
RESOLVED=0
FAILED=()

# compile_target <requirements-file> <platform> <python> <torch-backend|-> <macos|-> <out> <log>
# Runs the compile exactly as the installer's resolution does; returns uv's exit code.
compile_target() {
  local req="$1" plat="$2" pyv="$3" tb="$4" mac="$5" out="$6" log="$7"
  local args=(pip compile "$req" --no-config --no-header
    --python-platform "$plat" --python-version "$pyv"
    --overrides "$OVERRIDES" --constraints "$CONSTRAINTS" --output-file "$out")
  [ "$tb" = "-" ] || args+=(--torch-backend "$tb")
  if [ "$mac" = "-" ]; then
    env -u MACOSX_DEPLOYMENT_TARGET "$UV" "${args[@]}" >"$log" 2>&1
  else
    env MACOSX_DEPLOYMENT_TARGET="$mac" "$UV" "${args[@]}" >"$log" 2>&1
  fi
}

resolve_one() {  # resolve_one <cell>
  local spec="$1"
  local tag extra plat pyv tb mac want rest label floor
  tag="${spec%%|*}"; rest="${spec#*|}"
  extra="${rest%%|*}"; rest="${rest#*|}"
  plat="${rest%%|*}"; rest="${rest#*|}"
  pyv="${rest%%|*}"; rest="${rest#*|}"
  tb="${rest%%|*}"; rest="${rest#*|}"
  mac="${rest%%|*}"; want="${rest#*|}"
  label="default"; floor="$MIN_PINS_DEFAULT"
  if [ "$extra" != "-" ]; then label="$extra"; floor="$MIN_PINS_PDF"; fi
  local file="${tag//\//.}"
  local out="$WORK/$file.txt" log="$WORK/$file.log" req="$WORK/$label.in"
  EXAMINED=$((EXAMINED + 1))
  if ! write_requirements "$extra" "$req"; then
    FAILED+=("$tag")
    echo "  FAIL $tag (could not read the requirements from $PYPROJECT)"
    return 0
  fi
  local rc pins
  compile_target "$req" "$plat" "$pyv" "$tb" "$mac" "$out" "$log"; rc=$?
  if [ "$rc" -ne 0 ]; then
    FAILED+=("$tag")
    echo "  FAIL $tag (uv exit $rc)"
    sed -e 's/^/       /' "$log" | tail -n 12
    return 0
  fi
  pins=0
  if [ -f "$out" ]; then
    pins="$(grep -c -E '^[A-Za-z0-9_.-]+==' "$out" || true)"
  fi
  if [ "${pins:-0}" -lt "$floor" ]; then
    FAILED+=("$tag")
    echo "  FAIL $tag (uv exit 0 but only ${pins:-0} pinned lines resolved, need >= $floor: a vacuous or half resolve)"
    return 0
  fi
  # The [pdf] extra exists for these packages; a resolve without them proved nothing about it.
  if [ "$extra" = "pdf" ]; then
    local req_re
    for req_re in "${PDF_REQUIRED[@]}"; do
      if ! grep -q -E "$req_re" "$out"; then
        FAILED+=("$tag")
        echo "  FAIL $tag (resolved $pins pins, but none matches /$req_re/: the [pdf] stack is missing)"
        return 0
      fi
    done
  fi
  # torch is in the [pdf] extra only; its backend must be the one asked for.
  if [ "$extra" != "-" ] && [ "$want" != "-" ]; then
    if ! grep -q -E "$want" "$out"; then
      FAILED+=("$tag")
      echo "  FAIL $tag (resolved, but no line matches /$want/: the torch backend did not take effect)"
      return 0
    fi
  fi
  RESOLVED=$((RESOLVED + 1))
  echo "  ok   $tag ($pins pins)"
}

# Probe: every --torch-backend a cell uses must be one this uv accepts. An old uv
# rejects a newer backend name at argument parsing; without this the gate would
# print one cryptic red per cell using it. Fails closed.
probe_torch_backends() {
  local seen=" " c rest tb plat uvv
  : >"$WORK/probe.in"
  uvv="$("$UV" --version 2>&1)"; uvv="${uvv%%$'\n'*}"
  for c in "${CELLS[@]}"; do
    rest="${c#*|}"; rest="${rest#*|}"; plat="${rest%%|*}"; rest="${rest#*|}"; rest="${rest#*|}"
    tb="${rest%%|*}"
    [ "$tb" = "-" ] && continue
    case "$seen" in *" $tb "*) continue ;; esac
    seen="$seen$tb "
    if ! "$UV" pip compile "$WORK/probe.in" --no-config --no-header --python-platform "$plat" \
        --torch-backend "$tb" --output-file "$WORK/probe.out" >"$WORK/probe.$tb.log" 2>&1; then
      fail_closed "uv too old for $tb: $uvv does not accept --torch-backend $tb (upgrade uv; $(tail -n 2 "$WORK/probe.$tb.log" | tr '\n' ' ' | cut -c1-200))"
    fi
  done
}

# Negative control: the macOS 13 floor failure must still be a failure, and the
# failure must be uv's platform-tag error for onnxruntime (the wording
# _PLATFORM_TAG_FAILURE_RE in src/nexus/commands/self_cmd.py matches), not a
# network error that happens to mention onnxruntime. Live wording, uv 0.12:
#   onnxruntime==X has no wheels with a matching platform tag (e.g., `macosx_13_0_arm64`)
# CONTROL_KIND: held | resolved | other
CONTROL="not run"
CONTROL_KIND="other"
run_control() {
  local req="$WORK/control.in" out="$WORK/control.txt" log="$WORK/control.log" rc
  write_requirements "-" "$req" || { CONTROL="could not read the requirements"; return 1; }
  compile_target "$req" aarch64-apple-darwin 3.12 - 13.0 "$out" "$log"; rc=$?
  if [ "$rc" -eq 0 ]; then
    CONTROL_KIND="resolved"
    CONTROL="macOS 13 now RESOLVES. Update docs/getting-started.md, the control here and platform_failure_hint in src/nexus/commands/self_cmd.py, then change this control to the new floor"
    return 1
  fi
  if ! grep -q -i -E 'onnxruntime.*matching platform tag.{0,40}macosx_13_' "$log"; then
    CONTROL="macOS 13 failed (uv exit $rc), but not with uv's platform-tag error for onnxruntime: $(tail -n 3 "$log" | tr '\n' ' ' | cut -c1-240)"
    return 1
  fi
  CONTROL_KIND="held"
  CONTROL="macOS 13 unresolvable on onnxruntime's platform tag, as the floor says"
  return 0
}

uv_version="$("$UV" --version 2>&1)"; uv_version="${uv_version%%$'\n'*}"
pin_lines=0
while IFS= read -r line; do [[ "$line" == *==* ]] && pin_lines=$((pin_lines + 1)); done <"$CONSTRAINTS"
echo "CONSTRAINTS RESOLVE GATE: ${#CELLS[@]} compiles ($EXPECTED expected); $uv_version; constraints=$pin_lines lines"
probe_torch_backends
for c in "${CELLS[@]}"; do
  resolve_one "$c"
done
CONTROL_OK=1
run_control || CONTROL_OK=0
echo "  control: $CONTROL"

echo
if [ "$EXAMINED" -eq 0 ]; then
  echo "CONSTRAINTS RESOLVE GATE FAILED — no target was examined (examined 0 of $EXPECTED)"
  exit 1
fi
if [ "$EXAMINED" -ne "$EXPECTED" ]; then
  echo "CONSTRAINTS RESOLVE GATE FAILED — examined $EXAMINED of $EXPECTED declared compiles"
  exit 1
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "CONSTRAINTS RESOLVE GATE FAILED — ${#FAILED[@]} of $EXAMINED unresolvable: ${FAILED[*]}"
  exit 1
fi
if [ "$CONTROL_OK" -ne 1 ]; then
  if [ "$CONTROL_KIND" = "resolved" ]; then
    echo "CONSTRAINTS RESOLVE GATE FAILED — macOS floor moved: $CONTROL"
  else
    echo "CONSTRAINTS RESOLVE GATE FAILED — negative control: $CONTROL"
  fi
  exit 1
fi
echo "CONSTRAINTS RESOLVE GATE PASSED — $RESOLVED/$EXAMINED targets resolved (examined $EXAMINED, failed 0, control held)"
exit 0
