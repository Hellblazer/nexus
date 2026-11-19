#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source this; it defines functions and runs nothing.
#
# A release-candidate sandbox: a scratch HOME and NEXUS_CONFIG_DIR, the candidate
# wheel installed into it with `uv tool install`, and ONE runner that starts every
# command in it under an `env -i` whitelist, so the operator's HOME, credentials and
# NX_* variables cannot leak in. Shared by tests/e2e/cloud-leg.sh (the pre-tag cloud
# leg) and marginalia's RDR-253 seat harness, which sources it and launches claude
# through the runner. Seat-launch specifics (the claude credentials wrapper, per-launch
# cwd, tmux socket, /mcp check) belong to that harness, not here.
#
# VARIABLES a sourcing script may read after cand_sandbox_create <dir> (or after
# cand_sandbox_use <dir> to switch between sandboxes; the state lives in <dir>/env.sh):
#   CAND_SBX_ROOT         the sandbox directory itself
#   CAND_SBX_HOME         its HOME (<root>/home)
#   CAND_SBX_CONFIG_DIR   its NEXUS_CONFIG_DIR (<root>/home/.config/nexus)
#   CAND_SBX_TOOL_DIR     UV_TOOL_DIR (<root>/home/.local/share/uv/tools)
#   CAND_SBX_TOOL_BIN     UV_TOOL_BIN_DIR, where `nx` lands (<root>/home/.local/bin)
#   CAND_SBX_TMP          its TMPDIR
#   CAND_SBX_RUN          the runner: `"$CAND_SBX_RUN" [NAME=value ...] <command> [args]`
#                         execs the command under the whitelist below. A leading NAME=value
#                         is passed through (env's own syntax); give it paths and flags,
#                         never a secret, because argv is readable by `ps`.
#   CAND_SBX_NX           the installed nx
#   CAND_SBX_PY           the candidate tool venv's python
# and, after cand_engine_start:
#   CAND_PG_ROOT          the fresh short /tmp path NX_TEST_PG_ROOT names for the engine
#   CAND_ENGINE_JSON      engine.json: base_url, pid, tenants [{tenant, token}]
#   CAND_ENGINE_PID       the holder's pid
#
# THE WHITELIST (the runner's whole environment): HOME, USER, LANG, TERM,
# NEXUS_CONFIG_DIR, CLAUDE_CONFIG_DIR, NX_NO_TELEMETRY=1, TMPDIR, UV_TOOL_DIR,
# UV_TOOL_BIN_DIR, PATH=<tool bin>:/usr/bin:/bin:/usr/sbin:/sbin, plus the client
# credentials of the sandbox's mode. Credentials are NOT on any command line: they sit
# in <root>/secrets/<VAR> (mode 0600, one file per variable) and an inner shell exports
# them after the `env -i`, so the runner's argv carries none.
#
# CLIENT MODES (cand_sandbox_config):
#   local  NX_SERVICE_URL + NX_SERVICE_TOKEN + NX_LOCAL=1: a loopback engine and a tenant it minted (NX_LOCAL
#          makes the client write the local engine's bge embedding model, not voyage).
#   cloud  NX_SERVICE_URL + NX_MINT_TOKEN + NX_MINT_TENANT, NX_SERVICE_TOKEN absent. The
#          cloud test tenant's credential is mint-locked, so the client self-mints its
#          short-lived data tokens; this is the shape of a managed install's data credential.
#
# THE ONE MAPPING from 1Password item fields to client environment variables is
# CAND_CLOUD_FIELD_MAP below. A field is `item-field:ENV_VAR`; a trailing `?` on the
# field makes it optional. Change the cloud credential's shape here and nowhere else.
#   nx config set mint_token / mint_tenant / service_url are the persistent spellings of
#   the same three keys; the sandbox uses the env form so nothing persists in config.yml.

CAND_CLOUD_FIELD_MAP="${CAND_CLOUD_FIELD_MAP:-service_url:NX_SERVICE_URL token:NX_MINT_TOKEN mint_tenant:NX_MINT_TENANT}"

# The uv cache the install reads. A fresh cache would re-download every dependency;
# the ambient cache holds downloads only, not configuration. Override with
# CAND_UV_CACHE_DIR for a fully private one.
CAND_UV_BIN="${CAND_UV_BIN:-uv}"

_cand_die() { echo "candidate_sandbox: $*" >&2; return 1; }

# cand_sandbox_create <dir>  : make the sandbox skeleton, write <dir>/env.sh and <dir>/run.
cand_sandbox_create() {
    local root="${1:?cand_sandbox_create <dir>}"
    mkdir -p "$root" || return 1
    root="$(cd "$root" && pwd -P)" || return 1
    case "$root" in /|"$HOME"|"$HOME"/) _cand_die "refusing to use $root as a sandbox"; return 1 ;; esac
    umask 077
    mkdir -p "$root/home/.config/nexus" "$root/home/.local/bin" "$root/home/.local/share/uv/tools" \
             "$root/tmp" "$root/secrets" "$root/home/.claude" || return 1
    chmod 700 "$root" "$root/secrets" || return 1
    local uvcache="${CAND_UV_CACHE_DIR:-}"
    if [ -z "$uvcache" ]; then
        uvcache="$("$CAND_UV_BIN" cache dir 2>/dev/null)" || uvcache=""
    fi
    {
        printf 'export CAND_SBX_ROOT=%q\n' "$root"
        printf 'export CAND_SBX_HOME=%q\n' "$root/home"
        printf 'export CAND_SBX_CONFIG_DIR=%q\n' "$root/home/.config/nexus"
        printf 'export CAND_SBX_TOOL_DIR=%q\n' "$root/home/.local/share/uv/tools"
        printf 'export CAND_SBX_TOOL_BIN=%q\n' "$root/home/.local/bin"
        printf 'export CAND_SBX_TMP=%q\n' "$root/tmp"
        printf 'export CAND_SBX_RUN=%q\n' "$root/run"
        printf 'export CAND_SBX_NX=%q\n' "$root/home/.local/bin/nx"
        printf 'export CAND_SBX_PY=%q\n' "$root/home/.local/share/uv/tools/conexus/bin/python"
        printf 'export CAND_SBX_UV_CACHE=%q\n' "$uvcache"
    } >"$root/env.sh"
    # The runner. Its own env is the whitelist; secrets are read from files by the INNER
    # shell, so no argv ever carries one.
    cat >"$root/run" <<'RUNNER'
#!/usr/bin/env bash
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec /usr/bin/env -i \
    HOME="$here/home" USER="${USER:-sandbox}" LANG="${LANG:-en_US.UTF-8}" TERM="${TERM:-xterm-256color}" \
    NEXUS_CONFIG_DIR="$here/home/.config/nexus" CLAUDE_CONFIG_DIR="$here/home/.claude" NX_NO_TELEMETRY=1 \
    TMPDIR="$here/tmp" UV_TOOL_DIR="$here/home/.local/share/uv/tools" UV_TOOL_BIN_DIR="$here/home/.local/bin" \
    PATH="$here/home/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    SBX_SECRETS="$here/secrets" \
    /bin/bash -c '
        for f in "$SBX_SECRETS"/*; do
            [ -f "$f" ] || continue
            IFS= read -r v <"$f" || true
            export "$(basename "$f")=$v"
        done
        unset SBX_SECRETS v f
        exec /usr/bin/env "$@"' _ "$@"
RUNNER
    chmod 700 "$root/run"
    # shellcheck disable=SC1091
    . "$root/env.sh"
}

# cand_sandbox_use <dir>  : switch the CAND_SBX_* variables to an existing sandbox.
cand_sandbox_use() {
    [ -f "${1:?cand_sandbox_use <dir>}/env.sh" ] || { _cand_die "no sandbox at $1"; return 1; }
    # shellcheck disable=SC1091
    . "$1/env.sh"
}

# cand_sandbox_install <wheel> : uv tool install the candidate wheel (python 3.12) into the sandbox.
# The command line carries no credential; UV_TOOL_DIR and UV_TOOL_BIN_DIR sit inside the sandbox.
cand_sandbox_install() {
    local wheel="${1:?cand_sandbox_install <wheel>}"
    [ -f "$wheel" ] || { _cand_die "no wheel at $wheel"; return 1; }
    local uvbin
    uvbin="$(command -v "$CAND_UV_BIN")" || { _cand_die "uv is not on PATH"; return 1; }
    wheel="$(cd "$(dirname "$wheel")" && pwd -P)/$(basename "$wheel")"
    /usr/bin/env -i HOME="$CAND_SBX_HOME" USER="${USER:-sandbox}" LANG="${LANG:-en_US.UTF-8}" \
        TMPDIR="$CAND_SBX_TMP" UV_TOOL_DIR="$CAND_SBX_TOOL_DIR" UV_TOOL_BIN_DIR="$CAND_SBX_TOOL_BIN" \
        UV_CACHE_DIR="$CAND_SBX_UV_CACHE" \
        PATH="$(dirname "$uvbin"):/usr/bin:/bin:/usr/sbin:/sbin" \
        "$uvbin" tool install --python 3.12 "$wheel" >"$CAND_SBX_ROOT/install.log" 2>&1 \
        || { tail -n 20 "$CAND_SBX_ROOT/install.log" >&2; _cand_die "uv tool install failed (log: $CAND_SBX_ROOT/install.log)"; return 1; }
    [ -x "$CAND_SBX_NX" ] || { _cand_die "install produced no $CAND_SBX_NX"; return 1; }
}

# _cand_secret <name> <value-file-or-"-value"> : never takes the value as an argument.
# cand_secret_from_file <var> <file> : copy a file's first line to secrets/<var> (0600).
cand_secret_from_file() {
    local var="${1:?}" src="${2:?}" v
    IFS= read -r v <"$src" || true
    [ -n "$v" ] || { _cand_die "empty value for $var"; return 1; }
    ( umask 077; printf '%s\n' "$v" >"$CAND_SBX_ROOT/secrets/$var" ) || return 1
    chmod 600 "$CAND_SBX_ROOT/secrets/$var"
}

# cand_cloud_fields : print the item field names of CAND_CLOUD_FIELD_MAP, one per line, "?" kept for optional.
cand_cloud_fields() {
    local pair
    for pair in $CAND_CLOUD_FIELD_MAP; do printf '%s\n' "${pair%%:*}"; done
}

# cand_sandbox_config cloud <cred-dir> : <cred-dir>/<field> files (one value each, 0600) become the
#   cloud-mode secrets through CAND_CLOUD_FIELD_MAP; NX_SERVICE_TOKEN stays unset.
# cand_sandbox_config local <engine.json> <index> : the loopback engine's URL and tenant <index>'s token.
cand_sandbox_config() {
    local mode="${1:?}"
    rm -f "$CAND_SBX_ROOT"/secrets/* 2>/dev/null
    case "$mode" in
      cloud)
        local credd="${2:?cred dir}" pair field var opt
        for pair in $CAND_CLOUD_FIELD_MAP; do
            field="${pair%%:*}"; var="${pair#*:}"; opt=0
            case "$field" in *\?) opt=1; field="${field%\?}" ;; esac
            if [ -s "$credd/$field" ]; then
                cand_secret_from_file "$var" "$credd/$field" || return 1
            elif [ "$opt" = 0 ]; then
                _cand_die "cloud credential field '$field' is missing (it maps to $var)"; return 1
            fi
        done
        [ ! -e "$CAND_SBX_ROOT/secrets/NX_SERVICE_TOKEN" ] || { _cand_die "NX_SERVICE_TOKEN must stay unset in cloud mode"; return 1; }
        ;;
      local)
        local ej="${2:?engine.json}" idx="${3:-0}"
        # The token goes engine.json -> secrets file inside python; it is never an argument or stdout.
        "${E2E_PYTHON:?run e2e_python_resolve first}" - "$ej" "$idx" "$CAND_SBX_ROOT/secrets" <<'PY' || return 1
import json, os, sys
doc = json.load(open(sys.argv[1])); i = int(sys.argv[2]); out = sys.argv[3]
url = doc["base_url"]
if not url.startswith("http://127.0.0.1:"):
    sys.exit("refusing a non-loopback local engine: " + url.split("//", 1)[-1].split("/")[0].rsplit(":", 1)[0])
tenants = doc.get("tenants") or [{"tenant": doc["tenant"], "token": doc["token"]}]
for name, value in (("NX_SERVICE_URL", url), ("NX_SERVICE_TOKEN", tenants[i]["token"]), ("NX_LOCAL", "1")):
    fd = os.open(os.path.join(out, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
PY
        ;;
      *) _cand_die "unknown client mode '$mode' (local|cloud)"; return 1 ;;
    esac
}

# cand_sandbox_run <dir> <cmd...> : run in a sandbox without switching the current one.
cand_sandbox_run() { local d="${1:?}"; shift; "$d/run" "$@"; }

# cand_engine_start <repo-root> <out-dir> [tenant-count] : boot a loopback engine (the repo's gate jar;
# run scripts/build-gate-jar.sh first), mint tenant-count tenants (default 1), write <out-dir>/engine.json,
# and hold it in the background. NX_TEST_PG_ROOT is a FRESH short /tmp path (macOS caps a socket path at
# 103 bytes), removed by cand_engine_stop.
cand_engine_start() {
    local repo="${1:?repo root}" out="${2:?out dir}" count="${3:-1}"
    mkdir -p "$out" || return 1
    CAND_PG_ROOT="$(mktemp -d /tmp/nxcs-XXXXXX)" || return 1
    export CAND_PG_ROOT
    CAND_ENGINE_JSON="$out/engine.json"; export CAND_ENGINE_JSON
    rm -f "$CAND_ENGINE_JSON"
    (   cd "$repo" || exit 1
        NX_TEST_PG_ROOT="$CAND_PG_ROOT" exec uv run python tests/e2e/lib/engine_holder.py "$CAND_ENGINE_JSON" "$count"
    ) >"$out/holder.log" 2>&1 &
    CAND_ENGINE_PID=$!; export CAND_ENGINE_PID
    local _i
    for _i in $(seq 1 360); do
        [ -s "$CAND_ENGINE_JSON" ] && return 0
        kill -0 "$CAND_ENGINE_PID" 2>/dev/null || { tail -n 20 "$out/holder.log" >&2; _cand_die "engine holder exited before it was ready"; return 1; }
        sleep 0.5
    done
    tail -n 20 "$out/holder.log" >&2; _cand_die "engine did not come up in 180 s"; return 1
}

# cand_engine_stop : stop the holder (it tears the engine down on SIGTERM) and delete its PG root.
cand_engine_stop() {
    if [ -n "${CAND_ENGINE_PID:-}" ] && kill -0 "$CAND_ENGINE_PID" 2>/dev/null; then
        kill -TERM "$CAND_ENGINE_PID" 2>/dev/null
        local _i
        for _i in $(seq 1 60); do kill -0 "$CAND_ENGINE_PID" 2>/dev/null || break; sleep 0.5; done
        kill -0 "$CAND_ENGINE_PID" 2>/dev/null && kill -KILL "$CAND_ENGINE_PID" 2>/dev/null
        wait "$CAND_ENGINE_PID" 2>/dev/null || true
    fi
    CAND_ENGINE_PID=""
    case "${CAND_PG_ROOT:-}" in /tmp/nxcs-*) rm -rf "$CAND_PG_ROOT" ;; esac
    CAND_PG_ROOT=""
}

# cand_sandbox_destroy <dir> : delete a sandbox (secrets included). Refuses a path outside what create made.
cand_sandbox_destroy() {
    local d="${1:?}"
    [ -f "$d/env.sh" ] && [ -x "$d/run" ] || { _cand_die "$d is not a candidate sandbox"; return 1; }
    rm -rf "$d"
}
