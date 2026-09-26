#!/usr/bin/env bash
# tests/e2e/lib/claude_mcp_grant.sh -- the shared launcher a harness sources
# to run `claude` with the automation token granted to nx-mcp ALONE, never
# set directly under the protected name on any argv or in any file
# (RDR-219 amendment, "The nx-mcp dispatch grant", Phase 3b Step 2,
# nexus-wauo1.39).
#
# WHY THIS EXISTS. nx-mcp is always Claude's own child, and Claude Code
# deletes CLAUDE_CODE_OAUTH_TOKEN from its own process.env immediately
# after reading it, so nothing nx-mcp starts -- including its own `claude
# -p` subprocesses for the operator tools, nx_answer's inline planner and
# plan runner, nx_tidy, nx_enrich_beads, nx_plan_audit, and aspect
# extraction -- inherits the operator's credential by ordinary environment
# inheritance. A harness that exercises any of those grants nx-mcp its
# OWN, separate copy of the token under an UNPROTECTED name,
# NX_HARNESS_CLAUDE_OAUTH_TOKEN, carried in the nexus MCP server's own
# `env` block inside a `--strict-mcp-config`/`--mcp-config` payload.
# `src/nexus/claude_child_env.py` is the one place that maps the harness
# name back into the protected one, and only in a CHILD's own environment
# -- never in nx-mcp's `os.environ`, so the harness name never reaches a
# Bash-tool child, a nested MCP server, or any other process nx-mcp did
# not build that specific env dict for.
#
# DELIVERY MECHANISM. The config is built entirely in-memory and handed to
# `claude` through a PIPE, `--mcp-config <(builtin printf ...)` -- never a
# file on disk. The token value is never a positional argument to any
# EXTERNAL command: it is escaped with plain bash parameter substitution
# (no subprocess at all) and interpolated into a `builtin printf` call,
# which runs inside this shell (or the forked-but-never-exec'd subshell a
# process substitution uses to run its command list) rather than
# execve'ing a separate `/usr/bin/printf` process -- so the value never
# becomes an argv element any `ps` snapshot can see. `claude` reads the
# pipe once at startup and keeps the parsed config in memory (a `/mcp`
# reconnect restarts the server with the value still present -- T3
# analysis-deep-rdr219-devfd-mcp-config-2026-09-25).
#
# A HARNESS THAT LOADS THE CONEXUS PLUGIN (--plugin-dir) is refused UNLESS
# the server entry is named EXACTLY `plugin:conexus:nexus`, set via
# `CLAUDE_MCP_GRANT_SERVER_NAME` before calling. That is the one proven
# shape (nexus-wauo1.37): the default entry name, plain "nexus", is still
# refused together with --plugin-dir, because that combination is the one
# hook-surface-shakeout measured as BROKEN -- a manually configured server
# named "nexus" leaves every `mcp_tool` hook (which all address
# `plugin:conexus:nexus` literally) pointed at a name that does not exist
# ("Stop hook error: MCP server 'plugin:conexus:nexus' not connected", the
# session continuing anyway). Naming the override entry
# `plugin:conexus:nexus` instead -- the same name Claude Code gives the
# plugin's OWN `.mcp.json` key `nexus` -- was measured two ways to work:
# the model's tool calls route to the override (T3
# analysis-deep-rdr219-devfd-mcp-config-2026-09-25 Q5: the plugin's own
# copy is suppressed as a duplicate), and separately (nexus-wauo1.37,
# tests/e2e/hook-surface-shakeout, real Claude Code 2.1.277, one subagent-
# dispatch turn in each of two container runs against the identical image)
# every one of the six `mcp_tool` hooks that turn provokes --
# hook_agent_dispatch_expect, hook_subagent_start, hook_subagent_start_
# stamp, hook_subagent_start_tuple, hook_subagent_stop_tuple, hook_stop_
# verification -- fired identically with and without the override, per
# `hook_census.py` itself reading the tee'd JSON-RPC stream into nx-mcp
# (the hook's own effect, not the absence of a "not connected" error).
# T2 `nexus_rdr/219-plugin-override-proof` carries the artifact paths.
#
# USAGE (source this file, then call the function):
#
#   source tests/e2e/lib/claude_mcp_grant.sh
#   claude_mcp_grant nx-mcp -- -p "prompt" --allowedTools mcp__nexus__search
#
#   # a harness that ALSO loads the conexus plugin (--plugin-dir):
#   CLAUDE_MCP_GRANT_SERVER_NAME=plugin:conexus:nexus \
#       claude_mcp_grant nx-mcp -- -p "prompt" --plugin-dir /path/to/conexus
#
# `claude_mcp_grant NEXUS_CMD [NEXUS_ARG...] [-- CLAUDE_ARG...]` execs
# `claude --strict-mcp-config --mcp-config <(...) CLAUDE_ARG...`, where the
# piped config declares exactly one MCP server, named by
# `CLAUDE_MCP_GRANT_SERVER_NAME` (default "nexus"), running
# `NEXUS_CMD [NEXUS_ARG...]` with `NX_HARNESS_CLAUDE_OAUTH_TOKEN` set to
# THIS SHELL's own `CLAUDE_CODE_OAUTH_TOKEN` -- set ahead of this call by
# `claude_credentials.py run -- ...` (this function reads that variable;
# it never fetches a token itself). A caller that needs additional MCP
# servers besides the named one sets `CLAUDE_MCP_GRANT_EXTRA_SERVERS_JSON`
# to a raw JSON fragment of additional `"name": {...}` entries (no leading
# or trailing comma) before calling; it is spliced into `mcpServers`
# verbatim, alongside the named entry.
#
# FAILS LOUDLY, before ever invoking `claude`, when `CLAUDE_CODE_OAUTH_TOKEN`
# is unset or empty in the calling shell -- this function never fetches a
# token itself, so an absent one is a caller-ordering bug, not a
# recoverable condition here.
#
# NEVER prints the token, and NEVER enables xtrace (`set -x`) itself --
# doing so here would put the token in this shell's own trace output. A
# caller that already has `set -x` active before sourcing this file is
# responsible for its own trace hygiene. Sourcing this file changes no
# shell option in the caller (no `set -u` either); every expansion below
# carries its own default.
#
# The function EXECs `claude` on success, so a caller's EXIT trap and any
# cleanup after the call do not run in this shell: run it as the last
# command of a subshell or a dedicated script.

# The harness-side name the RDR-219 amendment maps into
# CLAUDE_CODE_OAUTH_TOKEN, mirroring src/nexus/claude_child_env.py's
# HARNESS_OAUTH_TOKEN_ENV_VAR -- kept as a literal here (this file is
# sourced standalone by bash harnesses with no nexus import), not
# re-derived from the Python module.
_CLAUDE_MCP_GRANT_ENV_VAR="NX_HARNESS_CLAUDE_OAUTH_TOKEN"

# The ONE server name --plugin-dir is allowed alongside (nexus-wauo1.37):
# the plugin's own namespaced name for its ".mcp.json" key "nexus", proven
# by a real container run to keep every `mcp_tool` hook (all of which
# address this literal string) resolving correctly when this function's
# own entry replaces the plugin's. Any other name paired with --plugin-dir
# stays refused -- see the refusal message below for why.
_CLAUDE_MCP_GRANT_PLUGIN_SERVER_NAME="plugin:conexus:nexus"

# JSON-escapes a command name or CLI argument: backslash first, then
# double-quote, newline, carriage return and tab. Any other control
# character is refused (return 1), so the piped config always parses.
_claude_mcp_grant_json_escape() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\n'/\\n}
    s=${s//$'\r'/\\r}
    s=${s//$'\t'/\\t}
    if [[ $s == *[[:cntrl:]]* ]]; then
        return 1
    fi
    builtin printf '%s' "$s"
}

# `["a","b",...]` for a (possibly empty) argument list.
_claude_mcp_grant_json_array() {
    local out="[" first=1 item
    for item in "$@"; do
        if [ "$first" -eq 1 ]; then
            first=0
        else
            out+=","
        fi
        local escaped
        escaped=$(_claude_mcp_grant_json_escape "$item") || return 1
        out+="\"$escaped\""
    done
    out+="]"
    printf '%s' "$out"
}

claude_mcp_grant() {
    local -a nexus_argv=()
    local -a claude_args=()
    local seen_sep=0
    local a

    for a in "$@"; do
        if [ "$seen_sep" -eq 0 ] && [ "$a" = "--" ]; then
            seen_sep=1
            continue
        fi
        if [ "$seen_sep" -eq 0 ]; then
            nexus_argv+=("$a")
        else
            claude_args+=("$a")
        fi
    done

    if [ "${#nexus_argv[@]}" -eq 0 ]; then
        echo "claude_mcp_grant: usage: claude_mcp_grant NEXUS_CMD [NEXUS_ARG...] [-- CLAUDE_ARG...]" >&2
        return 2
    fi
    local server_name="${CLAUDE_MCP_GRANT_SERVER_NAME:-nexus}"
    for a in "${claude_args[@]+"${claude_args[@]}"}"; do
        case $a in
            --plugin-dir|--plugin-dir=*)
                if [ "$server_name" != "$_CLAUDE_MCP_GRANT_PLUGIN_SERVER_NAME" ]; then
                    echo "claude_mcp_grant: refusing --plugin-dir with server name" \
                         "'$server_name': only CLAUDE_MCP_GRANT_SERVER_NAME=" \
                         "$_CLAUDE_MCP_GRANT_PLUGIN_SERVER_NAME is proven safe" \
                         "alongside --plugin-dir (RDR-219, nexus-wauo1.37) -- the" \
                         "default name 'nexus' left every mcp_tool hook pointed at" \
                         "a server that did not exist" >&2
                    return 2
                fi
                ;;
        esac
    done
    if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        echo "claude_mcp_grant: CLAUDE_CODE_OAUTH_TOKEN is not set in this shell -- run this" \
             "harness through 'claude_credentials.py run -- ...' first (RDR-219)" >&2
        return 1
    fi

    local nexus_cmd="${nexus_argv[0]}"
    local -a nexus_rest=()
    if [ "${#nexus_argv[@]}" -gt 1 ]; then
        nexus_rest=("${nexus_argv[@]:1}")
    fi

    local nexus_cmd_json nexus_args_json extra_servers extra_suffix
    if ! nexus_cmd_json="\"$(_claude_mcp_grant_json_escape "$nexus_cmd")\"" \
        || ! nexus_args_json="$(_claude_mcp_grant_json_array "${nexus_rest[@]+"${nexus_rest[@]}"}")"; then
        echo "claude_mcp_grant: a nexus command or argument contains a control character" >&2
        return 2
    fi
    extra_servers="${CLAUDE_MCP_GRANT_EXTRA_SERVERS_JSON:-}"
    extra_suffix=""
    if [ -n "$extra_servers" ]; then
        extra_suffix=",$extra_servers"
    fi

    # The token itself is escaped with pure bash parameter substitution --
    # no subprocess, no command substitution -- so it never transits
    # anything that could show up as a separate process's argv.
    local raw_token="$CLAUDE_CODE_OAUTH_TOKEN"
    local token="$raw_token"
    if [[ $token == *[[:cntrl:]]* ]]; then
        echo "claude_mcp_grant: CLAUDE_CODE_OAUTH_TOKEN contains a control character;" \
             "refusing to build an unparseable config" >&2
        return 1
    fi
    token=${token//\\/\\\\}
    token=${token//\"/\\\"}

    local server_name_json
    if ! server_name_json="$(_claude_mcp_grant_json_escape "$server_name")"; then
        echo "claude_mcp_grant: CLAUDE_MCP_GRANT_SERVER_NAME contains a control character" >&2
        return 2
    fi

    # `builtin printf` (never a bare `printf`, in case something upstream
    # shadowed the name with a function) writes the config; the pipe is a
    # process substitution, never a file. Nothing here ever writes the
    # token to disk or passes it as an argv element to any exec'd program.
    #
    # Claude's own login rides fd 3 (CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR),
    # not its environment, so the token is not in Claude's exec-time
    # environment either (nexus-wauo1.36; see claude_fd_exec.sh).
    unset CLAUDE_CODE_OAUTH_TOKEN
    export CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=3
    exec claude --strict-mcp-config --mcp-config <(builtin printf \
        '{"mcpServers":{"%s":{"command":%s,"args":%s,"env":{"%s":"%s"}}%s}}' \
        "$server_name_json" "$nexus_cmd_json" "$nexus_args_json" \
        "$_CLAUDE_MCP_GRANT_ENV_VAR" "$token" "$extra_suffix") \
        "${claude_args[@]+"${claude_args[@]}"}" 3< <(builtin printf '%s' "$raw_token")
}
