#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/e2e/constraints_resolve_gate_test.sh -- offline self-test for
# tests/e2e/constraints-resolve-gate.sh (nexus-2k66t.4.4).
#
# The gate needs the network and a real uv. This test needs neither: it runs the
# real gate script against a STUB uv on PATH (NX_RESOLVE_UV) that records its
# argv and behaves as each case says. It proves the gate fails when it should
# (fail-first), and does not pass vacuously (the nexus-moht0 doctrine):
#
#   1. a stub that resolves everything: PASSED, 22 examined, 22 resolved, and the
#      argv carries the shipped overrides and constraints, every platform, both
#      Python versions, the cpu, cu130 and cu126 torch backends (cu126 and cu130
#      only with the [pdf] extra), MACOSX_DEPLOYMENT_TARGET=14.0 for macOS, and a
#      requirements file free of uv sources;
#   2. one platform unresolvable: FAILED, the target named, the others still run;
#   3. a compile that exits 0 with an empty result, or only half the pins: FAILED;
#   4. a torch backend that does not take effect (cu130/cu126 resolve +cpu): FAILED;
#   5. a [pdf] compile without docling-slim or without mineru: FAILED;
#   6. the macOS 13 control resolving (its own "floor moved" wording), failing for
#      another reason, or failing mentioning onnxruntime without uv's platform-tag
#      text: FAILED;
#   7. uv absent: FAILED, nothing examined; uv too old for a torch backend: FAILED
#      up front, naming the backend and the uv version;
#   8. a gate with no targets, or one row fewer than the literal expected count:
#      FAILED.
#
# Run with: bash tests/e2e/constraints_resolve_gate_test.sh
set -u -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# NX_RESOLVE_GATE_UNDER_TEST points the suite at a mutated copy (fail-first runs).
GATE="${NX_RESOLVE_GATE_UNDER_TEST:-$HERE/constraints-resolve-gate.sh}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/constraints_resolve_gate_test.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
PASS=0; FAIL=0
ok()  { echo "  [ok] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
expect_has()   { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1: missing [$3]" ;; esac; }
expect_lacks() { case "$2" in *"$3"*) bad "$1: unexpected [$3]" ;; *) ok "$1" ;; esac; }
expect_eq()    { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: want [$2] got [$3]"; fi; }

[ -f "$GATE" ] || { bad "tests/e2e/constraints-resolve-gate.sh does not exist"; echo "$PASS passed, $FAIL failed"; exit 1; }

SHIMS="$WORKDIR/shims"; mkdir -p "$SHIMS"
ARGV_LOG="$WORKDIR/argv.log"
REQ_COPY="$WORKDIR/req-copies"; mkdir -p "$REQ_COPY"
cat >"$SHIMS/uv" <<'STUB'
#!/usr/bin/env bash
# Stub uv: records argv, behaves per STUB_* variables.
if [ "${1:-}" = "--version" ]; then echo "uv 0.0.0-stub"; exit 0; fi
out=""; tb=""; plat=""; req="$3"
prev=""
for a in "$@"; do
  case "$prev" in
    --output-file) out="$a" ;;
    --torch-backend) tb="$a" ;;
    --python-platform) plat="$a" ;;
  esac
  prev="$a"
done
printf 'MAC=%s PLAT=%s TB=%s ARGS=%s\n' "${MACOSX_DEPLOYMENT_TARGET:-unset}" "$plat" "$tb" "$*" >>"$STUB_ARGV_LOG"
cp "$req" "$STUB_REQ_COPY/$(basename "$req").$$.$RANDOM" 2>/dev/null
if [ -n "${STUB_REJECT_BACKEND:-}" ] && [ "$tb" = "$STUB_REJECT_BACKEND" ]; then
  echo "error: invalid value '$tb' for '--torch-backend <TORCH_BACKEND>'" >&2; exit 2
fi
if [ "${MACOSX_DEPLOYMENT_TARGET:-}" = "13.0" ]; then
  if [ -n "${STUB_MAC13_OK:-}" ]; then :;
  elif [ -n "${STUB_MAC13_OTHER:-}" ]; then echo "error: network is down" >&2; exit 2
  elif [ -n "${STUB_MAC13_ONNX_ONLY:-}" ]; then echo "error: failed to download onnxruntime==9.9.9: connection reset" >&2; exit 1
  else echo "error: No solution found when resolving dependencies
  cause: Because onnxruntime==9.9.9 has no wheels with a matching platform tag (e.g., \`macosx_13_0_arm64\`) and you require onnxruntime==9.9.9, we can conclude that your requirements are unsatisfiable." >&2; exit 1; fi
fi
if [ -n "${STUB_FAIL_PLATFORM:-}" ] && [ "$plat" = "$STUB_FAIL_PLATFORM" ]; then
  echo "error: stub: no solution on $plat" >&2; exit 1
fi
if [ -n "${STUB_EMPTY:-}" ]; then : >"$out"; exit 0; fi
{
  # A real lock compiles to ~130 pins (default) and ~210 ([pdf]).
  total="${STUB_PINS:-230}"
  if grep -q docling-slim "$req"; then
    echo "docling-slim==2.0"
    [ -n "${STUB_NO_MINERU:-}" ] || echo "mineru==3.0"
  fi
  i=0; while [ $i -lt "$total" ]; do echo "pkg$i==1.0"; i=$((i + 1)); done
  if [ -n "$tb" ]; then
    if [ -n "${STUB_CUDA_AS_CPU:-}" ]; then echo "torch==2.8.0+cpu"; else echo "torch==2.8.0+$tb"; fi
  fi
} >"$out"
if [ -n "${STUB_NO_DOCLING:-}" ]; then grep -v '^docling-slim==' "$out" >"$out.tmp"; mv "$out.tmp" "$out"; fi
exit 0
STUB
chmod +x "$SHIMS/uv"

run_gate() {  # run_gate <gate-path> [VAR=value...]  -> prints output, then rc= line
  local gate="$1"; shift
  : >"$ARGV_LOG"; rm -f "$REQ_COPY"/*
  local out rc
  out="$(env "$@" PATH="$SHIMS:$PATH" NX_RESOLVE_UV="$SHIMS/uv" STUB_ARGV_LOG="$ARGV_LOG" STUB_REQ_COPY="$REQ_COPY" bash "$gate" 2>&1)"; rc=$?
  printf '%s\nrc=%s\n' "$out" "$rc"
}

# ── 1. everything resolves ───────────────────────────────────────────────────
echo "Test 1: a uv that resolves everything passes, with the right argv"
out="$(run_gate "$GATE" X=1)"
expect_has "PASSED line with counts" "$out" "CONSTRAINTS RESOLVE GATE PASSED — 22/22 targets resolved (examined 22, failed 0, control held)"
expect_has "exit 0" "$out" "rc=0"
argv="$(cat "$ARGV_LOG")"
expect_eq "23 compiles (22 targets + the control)" "23" "$(grep -v 'probe.in' "$ARGV_LOG" | grep -c .)"
expect_eq "3 probes (cpu, cu130, cu126)" "3" "$(grep -c 'probe.in' "$ARGV_LOG")"
for needle in "src/nexus/_install/overrides.txt" "src/nexus/_install/constraints.txt" \
  "--python-platform x86_64-unknown-linux-gnu" "--python-platform aarch64-unknown-linux-gnu" \
  "--python-platform x86_64-pc-windows-msvc" "--python-platform aarch64-apple-darwin" \
  "--python-version 3.12" "--python-version 3.13" "--torch-backend cpu" "--torch-backend cu130" \
  "--torch-backend cu126" "MAC=14.0" "--no-config"; do
  expect_has "argv carries [$needle]" "$argv" "$needle"
done
real="$(grep -v 'probe.in' "$ARGV_LOG")"
expect_eq "cu130 compiles: 4 (pdf only)" "4" "$(printf '%s\n' "$real" | grep -c -e '--torch-backend cu130')"
expect_eq "cu126 compiles: 2 (pdf only, py3.12)" "2" "$(printf '%s\n' "$real" | grep -e '--torch-backend cu126' | grep -c -e '--python-version 3.12')"
expect_eq "cu126 compiles total: 2" "2" "$(printf '%s\n' "$real" | grep -c -e '--torch-backend cu126')"
defaults="$(grep -F 'default.in' "$ARGV_LOG")"
expect_eq "8 default-set compiles" "8" "$(printf '%s\n' "$defaults" | grep -c .)"
expect_lacks "no GPU backend on a default-set compile" "$defaults" "--torch-backend cu"
expect_lacks "no torch backend on windows/macOS" "$(grep -E 'PLAT=(x86_64-pc-windows-msvc|aarch64-apple-darwin)' "$ARGV_LOG")" "--torch-backend"
reqs="$(cat "$REQ_COPY"/* 2>/dev/null)"
expect_has "requirements include a runtime dependency" "$reqs" "onnxruntime"
expect_has "the pdf variant includes the PDF stack" "$reqs" "docling-slim"
expect_lacks "requirements carry no uv index or sources" "$reqs" "download.pytorch.org"
expect_lacks "requirements carry no tool.uv table" "$reqs" "tool.uv"
# The default install must not carry the pdf stack, or "default" would equal "pdf".
default_in=""
for f in "$REQ_COPY"/default.in*; do [ -e "$f" ] && { default_in="${f##*/}"; break; }; done
expect_lacks "the default requirements omit the PDF stack" "$(cat "$REQ_COPY/$default_in")" "docling-slim"

# ── 2. one platform unresolvable ─────────────────────────────────────────────
echo "Test 2: an unresolvable platform fails the gate and is named"
out="$(run_gate "$GATE" STUB_FAIL_PLATFORM=x86_64-pc-windows-msvc)"
expect_has "FAILED line names the windows targets" "$out" "windows-x64-py3.12/default"
expect_has "names the pdf variant too" "$out" "windows-x64-py3.13/pdf"
expect_has "counts 4 unresolvable of 22" "$out" "CONSTRAINTS RESOLVE GATE FAILED — 4 of 22 unresolvable"
expect_has "exit 1" "$out" "rc=1"
expect_lacks "never prints PASSED" "$out" "GATE PASSED"
expect_has "the other targets still ran" "$out" "ok   linux-aarch64-py3.13-cu130/pdf"

# ── 3. exit 0 with nothing, or half, resolved ────────────────────────────────
echo "Test 3: a compile that exits 0 with an empty or half result is a failure"
out="$(run_gate "$GATE" STUB_EMPTY=1)"
expect_has "FAILED, 22 of 22" "$out" "CONSTRAINTS RESOLVE GATE FAILED — 22 of 22 unresolvable"
expect_has "says vacuous" "$out" "a vacuous or half resolve"
expect_has "exit 1" "$out" "rc=1"
# 150 pins clears the default floor (110) and misses the pdf floor (180): only the
# 14 pdf cells fail. The old single floor of 40 passed all of them.
out="$(run_gate "$GATE" STUB_PINS=150)"
expect_has "half a pdf resolve fails 14 cells" "$out" "CONSTRAINTS RESOLVE GATE FAILED — 14 of 22 unresolvable"
expect_has "the pdf cell is named" "$out" "linux-x86_64-py3.12-cpu/pdf"
expect_lacks "the default cells are not blamed" "$out" "FAIL linux-x86_64-py3.12-cpu/default"
expect_has "exit 1" "$out" "rc=1"
out="$(run_gate "$GATE" STUB_PINS=100)"
expect_has "below the default floor too: 22 of 22" "$out" "CONSTRAINTS RESOLVE GATE FAILED — 22 of 22 unresolvable"

# ── 4. the torch backend does not take effect ────────────────────────────────
echo "Test 4: a cu130 or cu126 target that resolves CPU torch fails"
out="$(run_gate "$GATE" STUB_CUDA_AS_CPU=1)"
expect_has "FAILED names a cu130 pdf target" "$out" "linux-x86_64-py3.12-cu130/pdf"
expect_has "FAILED names a cu126 pdf target" "$out" "linux-aarch64-py3.12-cu126/pdf"
expect_has "says the backend did not take effect" "$out" "the torch backend did not take effect"
expect_lacks "the cpu targets are not blamed" "$out" "FAIL linux-x86_64-py3.12-cpu/pdf"
expect_has "exit 1" "$out" "rc=1"

# ── 5. the pdf stack must be in every pdf compile ────────────────────────────
echo "Test 5: a pdf compile without mineru or without docling-slim fails"
out="$(run_gate "$GATE" STUB_NO_MINERU=1)"
expect_has "missing mineru fails the pdf cells" "$out" "CONSTRAINTS RESOLVE GATE FAILED — 14 of 22 unresolvable"
expect_has "says the pdf stack is missing" "$out" "the [pdf] stack is missing"
expect_has "names the mineru pattern" "$out" "/^mineru==/"
expect_lacks "the default cells are not blamed" "$out" "FAIL windows-x64-py3.12/default"
out="$(run_gate "$GATE" STUB_NO_DOCLING=1)"
expect_has "missing docling-slim fails the pdf cells" "$out" "none matches /^docling-slim==/"
expect_has "exit 1" "$out" "rc=1"

# ── 6. the negative control ──────────────────────────────────────────────────
echo "Test 6: the macOS 13 control must fail, with uv's platform-tag error on onnxruntime"
out="$(run_gate "$GATE" STUB_MAC13_OK=1)"
expect_has "control that resolves says the floor moved" "$out" "CONSTRAINTS RESOLVE GATE FAILED — macOS floor moved: macOS 13 now RESOLVES"
expect_has "names docs/getting-started.md" "$out" "docs/getting-started.md"
expect_has "names platform_failure_hint" "$out" "platform_failure_hint"
expect_lacks "it is not the generic control wording" "$out" "negative control:"
expect_has "exit 1" "$out" "rc=1"
out="$(run_gate "$GATE" STUB_MAC13_OTHER=1)"
expect_has "control failing for another reason fails the gate" "$out" "negative control: macOS 13 failed (uv exit 2), but not with uv's platform-tag error"
expect_has "exit 1" "$out" "rc=1"
out="$(run_gate "$GATE" STUB_MAC13_ONNX_ONLY=1)"
expect_has "exit 1 naming onnxruntime without platform-tag text does not satisfy the control" "$out" "negative control: macOS 13 failed (uv exit 1), but not with uv's platform-tag error"
expect_lacks "never prints PASSED" "$out" "GATE PASSED"
expect_has "exit 1" "$out" "rc=1"

# ── 7. no uv, and a uv too old for a torch backend ───────────────────────────
echo "Test 7: with no uv the gate fails closed; a uv that rejects a backend fails up front"
out="$(env PATH="/usr/bin:/bin" NX_RESOLVE_UV="$WORKDIR/no-such-uv" bash "$GATE" 2>&1)"; rc=$?
expect_eq "exit 1" "1" "$rc"
expect_has "FAILED line" "$out" "CONSTRAINTS RESOLVE GATE FAILED — uv is not on PATH"
expect_lacks "never prints PASSED" "$out" "GATE PASSED"
out="$(run_gate "$GATE" STUB_REJECT_BACKEND=cu126)"
expect_has "names the backend and the uv version" "$out" "CONSTRAINTS RESOLVE GATE FAILED — uv too old for cu126: uv 0.0.0-stub does not accept --torch-backend cu126"
expect_eq "one red, not one per cell" "1" "$(printf '%s\n' "$out" | grep -c 'FAILED')"
expect_lacks "no cell ran" "$(grep -v 'probe.in' "$ARGV_LOG")" "overrides.txt"
expect_has "exit 1" "$out" "rc=1"
out="$(run_gate "$GATE" STUB_REJECT_BACKEND=cu130)"
expect_has "cu130 is probed too" "$out" "uv too old for cu130"

# ── 8. no targets, one target short ──────────────────────────────────────────
echo "Test 8: a gate with no target examined, or one row short, fails"
EMPTY_GATE="$WORKDIR/constraints-resolve-gate.sh"
# Same script, target table emptied; HERE must still find lib/python.sh and the repo.
grep -v -E '^  "(linux|windows|macos)' "$GATE" | sed -e "s|^HERE=.*|HERE=\"$HERE\"|" >"$EMPTY_GATE"
[ "$(grep -c -E '^  "(linux|windows|macos)' "$EMPTY_GATE" || true)" = "0" ] && ok "the emptied copy has no targets" || bad "the emptied copy still has targets"
out="$(run_gate "$EMPTY_GATE" X=1)"
expect_has "FAILED, nothing examined" "$out" "no target was examined (examined 0 of 22)"
expect_has "exit 1" "$out" "rc=1"
expect_lacks "never prints PASSED" "$out" "GATE PASSED"
SHORT_GATE="$WORKDIR/short-gate.sh"
grep -v -F '"windows-x64-py3.13/pdf|' "$GATE" | sed -e "s|^HERE=.*|HERE=\"$HERE\"|" >"$SHORT_GATE"
[ "$(grep -c -E '^  "(linux|windows|macos)' "$SHORT_GATE" || true)" = "21" ] && ok "the short copy has 21 rows" || bad "the short copy does not have 21 rows"
out="$(run_gate "$SHORT_GATE" X=1)"
expect_has "FAILED, one row short" "$out" "CONSTRAINTS RESOLVE GATE FAILED — examined 21 of 22 declared compiles"
expect_has "exit 1" "$out" "rc=1"
expect_lacks "never prints PASSED" "$out" "GATE PASSED"

# ── 9. static: no skip path ──────────────────────────────────────────────────
echo "Test 9: the gate has no skip path"
body="$(grep -v '^#' "$GATE")"
expect_lacks "no skip vocabulary" "$(printf '%s' "$body" | tr 'A-Z' 'a-z')" "skipped"
expect_eq "exit 0 only after the PASSED line" "1" "$(printf '%s\n' "$body" | grep -c '^exit 0$')"
expect_eq "EXPECTED is a literal" "1" "$(printf '%s\n' "$body" | grep -c '^EXPECTED=[0-9][0-9]*$')"

echo
echo "constraints_resolve_gate_test.sh: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
