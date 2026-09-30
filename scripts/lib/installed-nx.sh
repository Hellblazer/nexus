#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/lib/installed-nx.sh: resolve the INSTALLED `nx` generation and label
# the tuple-space endpoint it talks to. Sourced, never executed.
#
# Shared by scripts/git-push-develop.sh (push lock + develop-freeze read) and
# scripts/develop-freeze.sh (freeze set/clear/status). The bodies moved here
# verbatim from git-push-develop.sh (nexus-agctp review findings 1 and 2) so the
# two scripts resolve the same `nx` against the same tuple space by construction.
# A freeze set through one `nx` and read through another would be the same
# split-brain the lock's scope label exists to expose.

# Installed nx only. `uv run` and an activated venv both prepend a checkout's
# OWN `.venv/bin` to PATH ahead of the installed generation, so a bare `nx`
# there is a DEV-CHECKOUT editable install, which the nexus-a2qhz
# production-write guard refuses on every real tuple-space write (reading
# identically to an unreachable tuple space). So walk every `nx` on PATH by hand
# (`command -v -a` is a zsh-ism; bash has no `-a`) and take the first one that
# is neither under a `.venv/` directory nor under this checkout's own toplevel.
#
# Prints the chosen path and returns 0, or prints nothing and returns 1.
installed_nx_resolve() {
  local repo_toplevel path_ifs dir candidate cand_dir resolved
  local -a path_dirs=()
  repo_toplevel="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  path_ifs="$IFS"
  IFS=':' read -r -a path_dirs <<< "$PATH"
  IFS="$path_ifs"
  for dir in "${path_dirs[@]+"${path_dirs[@]}"}"; do
    [[ -z "$dir" ]] && continue
    candidate="$dir/nx"
    [[ -x "$candidate" ]] || continue
    cand_dir="$(cd -- "$dir" 2>/dev/null && pwd -P)" || continue
    resolved="$cand_dir/nx"
    case "$resolved" in
      */.venv/*) continue ;;
    esac
    if [[ -n "$repo_toplevel" && "$resolved" == "$repo_toplevel"/* ]]; then
      continue
    fi
    printf '%s' "$candidate"
    return 0
  done
  return 1
}

# Endpoint + tenant this invocation resolved. Reads ONLY through `nx` itself
# (never a bash-side re-parse of config.yml or a lease file), in the same
# priority nexus.db.service_endpoint.resolve_service_endpoint documents: an
# explicit NX_SERVICE_URL/HOST/PORT override already in the environment, else
# the persisted config.yml `service_url`, else a local supervisor's live lease.
# Every read is individually guarded so a resolution failure degrades to
# "(unresolvable)"/"(unknown)" and NEVER aborts the caller under `set -e`: this
# is a diagnostic label, not a gate.
installed_nx_describe_scope() {
  local nxbin="$1" endpoint="" tenant="" cfg status host port
  if [[ -n "${NX_SERVICE_URL:-}" ]]; then
    endpoint="$NX_SERVICE_URL"
  else
    cfg="$("$nxbin" config get service_url --show 2>/dev/null || true)"
    if [[ -n "$cfg" && "$cfg" != "service_url: not set" ]]; then
      endpoint="$cfg"
    elif [[ -n "${NX_SERVICE_HOST:-}" && -n "${NX_SERVICE_PORT:-}" ]]; then
      endpoint="${NX_SERVICE_HOST}:${NX_SERVICE_PORT}"
    else
      status="$("$nxbin" daemon service status --json 2>/dev/null || true)"
      host=""
      port=""
      if [[ -n "$status" ]]; then
        host="$(printf '%s' "$status" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("host") or "")' 2>/dev/null || true)"
        port="$(printf '%s' "$status" | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d.get("port") or "")' 2>/dev/null || true)"
      fi
      if [[ -n "$host" && -n "$port" ]]; then
        endpoint="${host}:${port}"
      fi
    fi
  fi
  [[ -z "$endpoint" ]] && endpoint="(unresolvable)"

  tenant="$("$nxbin" config get mint_tenant --show 2>/dev/null || true)"
  if [[ -z "$tenant" || "$tenant" == "mint_tenant: not set" ]]; then
    tenant="(unknown)"
  fi

  printf 'endpoint=%s tenant=%s' "$endpoint" "$tenant"
}
