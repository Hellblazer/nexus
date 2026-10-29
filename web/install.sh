#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Nexus installer for macOS and Linux (RDR-228 Phase 3, nexus-sv2wx.26).
#
#   curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh
#   curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh -s -- --local
#   curl -LsSf https://hellblazer.github.io/nexus/install.sh | sh -s -- --slim
#
# Everything is inside the one function main, and main is called on the last
# line. A download that is cut short is then a syntax error or a file with no
# call in it, and runs nothing.
#
# The script reads its answers from the terminal (/dev/tty), not from standard
# input: when it is piped from curl, standard input is the script itself.

main() {
    set -eu

    # The PATH the user's shell has. uv's directory is added to this script's own PATH
    # below, which says nothing about the shell the user types in afterwards.
    ORIGINAL_PATH=$PATH
    NX_INSTALL_NAME="install.sh"
    NX_MARKETPLACE="Hellblazer/nexus"
    NX_PLUGIN="conexus@nexus-plugins"
    # The uv project's own installer. NX_INSTALL_UV_URL replaces it (a test seam;
    # file: works). The script says the override is in use, by name, and never prints
    # the value; only https: and file: are accepted.
    UV_INSTALLER_URL="${NX_INSTALL_UV_URL:-https://astral.sh/uv/install.sh}"
    UV_URL_OVERRIDDEN=no
    if [ -n "${NX_INSTALL_UV_URL:-}" ]; then UV_URL_OVERRIDDEN=yes; fi

    say() { printf '%s\n' "$*"; }
    warn() { printf '%s\n' "$*" >&2; }
    die() { warn "$NX_INSTALL_NAME: $*"; exit 1; }
    bad_usage() { warn "$NX_INSTALL_NAME: $*"; warn "Try: $NX_INSTALL_NAME --help"; exit 2; }

    usage() {
        cat <<-'EOF'
	Install the Nexus command-line tool (nx). Adds the conexus plugin to Claude Code
	when claude is on the PATH, and sets up the storage service if you ask for it.

	Usage: install.sh [--local | --cloud | --cli-only] [--autostart | --no-autostart] [--slim]

	  --local          Run the storage service on this machine. Downloads about 0.8 GB
	                   in all on macOS and 1.0 GB on Linux, PDF support included. Runs
	                   `nx init`, in local mode even when a service address is already
	                   configured.
	  --cloud          Use a managed service. Runs `nx config init`, which asks for the
	                   service address and your token. With no terminal attached it
	                   needs NX_SERVICE_URL and NX_SERVICE_TOKEN (or NX_MINT_TOKEN) in
	                   the environment, and stops before downloading anything without
	                   them.
	  --cli-only       Install the command-line tool and nothing else.
	  --autostart      With --local: start the service at login. Passed to `nx init`
	                   as --yes.
	  --no-autostart   With --local: do not start the service at login. Passed to
	                   `nx init` as --no-autostart.
	  --slim           Leave PDF support out: the download falls to about 0.25 GB for the
	                   command-line tool (about 0.55 GB with --local). The first PDF you
	                   index then fetches it (`nx self install --extras pdf` does it
	                   ahead of time).
	  -h, --help       Show this text.

	Typing this command is your consent to install the command-line tool, so the
	script does not ask about that. It installs uv first when uv is missing, using
	the uv project's own installer. When nx is already installed it skips the build
	and says so. By default the build installs everything, PDF support included
	(MinerU and torch): it downloads about 0.5 GB on macOS and 0.6 GB on Linux
	for the command-line tool, and the service adds the rest of the --local figure. The
	sizes were measured on 7.89.0 on macOS and Linux (2026-10-29); Windows was not measured.
	Disk use is about 1.9 to 2.2 GB for the command-line tool and 2.6 to 2.9 GB with --local.
	None of them is a promise.

	Platforms: Apple Silicon Mac on macOS 14 or later, Linux on x86-64 or arm64. An Intel
	Mac or a Mac on macOS 13 cannot run it: the pinned onnxruntime ships macOS 14 wheels
	only, so the build stops.

	On Linux it asks for the CPU build of torch, so it does not download the CUDA libraries,
	about 4.5 GB more (set NX_TORCH_BACKEND to choose another build).

	With no service flag, the script asks one question on the terminal, before it
	downloads anything. With no terminal attached and no flag, it installs the
	command-line tool only and says so.

	Start at login: with --local and neither autostart flag, the script asks when a
	terminal is attached, and the answer is no unless you type y. With no terminal it
	passes --no-autostart. A login unit is never registered without an explicit yes.
	EOF
    }

    # ---- the terminal ---------------------------------------------------------------
    # "A terminal is attached" means /dev/tty can be opened. Standard input does
    # not count: under `curl | sh` it is the script.
    have_tty() { ( : </dev/tty ) 2>/dev/null; }

    ask() { # ask PROMPT: the reply is in ANSWER; returns 1 when the terminal is gone
        printf '%s' "$1" >/dev/tty
        ANSWER=""
        IFS= read -r ANSWER </dev/tty || return 1
    }

    choose_mode() {
        {
            printf '\n%s\n' "How should Nexus run its storage service?"
            printf '  1) local     run it on this machine (downloads about 0.8 GB, 1.0 GB on Linux)\n'
            printf '  2) cloud     use a managed service (you type its address and token)\n'
            printf '  3) CLI only  install the command-line tool and nothing else\n'
        } >/dev/tty
        tries=0
        while [ "$tries" -lt 3 ]; do
            ask "Choose 1, 2 or 3 [3]: " || break
            case $ANSWER in
                1|local) MODE=local; return 0 ;;
                2|cloud) MODE=cloud; return 0 ;;
                3|cli-only|"") MODE=cli-only; return 0 ;;
            esac
            printf 'Please answer 1, 2 or 3.\n' >/dev/tty
            tries=$((tries + 1))
        done
        MODE=cli-only
    }

    choose_autostart() {
        ask "Start the service at login? [y/N]: " || { AUTOSTART=no; return 0; }
        case $ANSWER in
            y|Y|yes|Yes|YES) AUTOSTART=yes ;;
            *) AUTOSTART=no ;;
        esac
    }

    # ---- arguments ------------------------------------------------------------------
    MODE=""
    AUTOSTART=""
    AUTOSTART_ARG=""
    SLIM=no
    for arg in "$@"; do
        case $arg in
            -h|--help) usage; return 0 ;;
            --local|--cloud|--cli-only)
                wanted=${arg#--}
                if [ -n "$MODE" ] && [ "$MODE" != "$wanted" ]; then
                    bad_usage "--$MODE and $arg cannot be used together"
                fi
                MODE=$wanted ;;
            --autostart)
                if [ "$AUTOSTART" = no ]; then bad_usage "--autostart and --no-autostart cannot be used together"; fi
                AUTOSTART=yes; AUTOSTART_ARG=$arg ;;
            --no-autostart)
                if [ "$AUTOSTART" = yes ]; then bad_usage "--autostart and --no-autostart cannot be used together"; fi
                AUTOSTART=no; AUTOSTART_ARG=$arg ;;
            --slim) SLIM=yes ;;
            *) bad_usage "unknown option: $arg" ;;
        esac
    done

    # ---- decide everything that needs the user, before any download -----------------
    TTY=no
    if have_tty; then TTY=yes; fi

    if [ -z "$MODE" ]; then
        if [ "$TTY" = yes ]; then
            choose_mode
        else
            MODE=cli-only
            say "No terminal is attached and no service flag was given, so this installs the"
            say "command-line tool only. For the service, run \`nx init\` (local) or"
            say "\`nx config init\` (managed) afterwards, or run this script again with --local"
            say "or --cloud."
        fi
    fi
    if [ "$MODE" != local ] && [ -n "$AUTOSTART" ]; then
        say "Note: $AUTOSTART_ARG only applies to --local; ignored."
    fi
    if [ "$MODE" = local ] && [ -z "$AUTOSTART" ]; then
        if [ "$TTY" = yes ]; then
            choose_autostart
        else
            AUTOSTART=no
            say "No terminal is attached, so the service will not start at login (--no-autostart)."
            say "Pass --autostart to change that."
        fi
    fi

    # --cloud with no terminal: the wizard can only skip its prompts for what the
    # environment already holds. Stop now, not after the build. Presence only; the
    # values are never read or printed.
    if [ "$MODE" = cloud ] && [ "$TTY" = no ]; then
        if [ -z "${NX_SERVICE_URL:-}" ] || { [ -z "${NX_SERVICE_TOKEN:-}" ] && [ -z "${NX_MINT_TOKEN:-}" ]; }; then
            bad_usage "--cloud needs a terminal to ask for the service address and token. Run it in a terminal, or set NX_SERVICE_URL and NX_SERVICE_TOKEN (or NX_MINT_TOKEN) first. Nothing was changed."
        fi
    fi

    # ---- where things are -------------------------------------------------------------
    expand() { # a directory setting: ~ and ~/ expanded, a relative path refused
        case $1 in
            "") return 1 ;;
            \~) printf '%s' "$HOME" ;;
            \~/*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
            /*) printf '%s' "$1" ;;
            *) return 1 ;;
        esac
    }
    TOOLS_DIR=$(expand "${NX_TOOLS_DIR:-}") || TOOLS_DIR="$HOME/.local/share/nexus/tools"
    BIN_DIR=$(expand "${NX_BIN_DIR:-}") || BIN_DIR="$HOME/.local/bin"

    # ---- uv ---------------------------------------------------------------------------
    uv_dir_on_path() { # put the first directory that holds uv on the PATH
        for d in "${XDG_BIN_HOME:-$HOME/.local/bin}" "${UV_INSTALL_DIR:-$HOME/.local/bin}" "$HOME/.local/bin" "$HOME/.cargo/bin"; do
            if [ -x "$d/uv" ]; then PATH="$d:$PATH"; export PATH; return 0; fi
        done
        return 1
    }
    ensure_uv() {
        if command -v uv >/dev/null 2>&1; then return 0; fi
        if uv_dir_on_path; then return 0; fi
        say "uv is not installed; installing it with the uv project's own installer."
        if [ "$UV_URL_OVERRIDDEN" = yes ]; then
            say "Using the uv installer named by NX_INSTALL_UV_URL."
            case $UV_INSTALLER_URL in
                https:*|file:*) ;;
                *) die "NX_INSTALL_UV_URL must be an https: or file: URL. Nothing was changed." ;;
            esac
        fi
        # Fetch to a file, then run it: in a pipe, a failed download would be hidden
        # by the shell that runs the empty result. The file goes when the script ends,
        # however it ends.
        fetched=""
        trap 'rm -f "$fetched"' EXIT
        trap 'exit 1' INT TERM
        fetched=$(mktemp "${TMPDIR:-/tmp}/nx-install-uv.XXXXXX") || die "could not create a temporary file."
        fetch_failed="could not download the uv installer. Install uv (https://docs.astral.sh/uv/), then run this again."
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf "$UV_INSTALLER_URL" -o "$fetched" || die "$fetch_failed"
        elif command -v wget >/dev/null 2>&1; then
            wget -q -O "$fetched" "$UV_INSTALLER_URL" || die "$fetch_failed"
        else
            die "curl or wget is needed to install uv. Install one of them, or install uv yourself (https://docs.astral.sh/uv/), then run this again."
        fi
        sh "$fetched" || die "the uv installer failed. Install uv (https://docs.astral.sh/uv/), then run this again."
        rm -f "$fetched"
        uv_dir_on_path || true
        command -v uv >/dev/null 2>&1 || die "uv was installed but is not on the PATH. Open a new terminal and run this again."
    }

    # ---- the CLI ----------------------------------------------------------------------
    # A program counts as the CLI when it answers --version the way nx does. Another
    # program named nx (a build tool) does not, and an nx that cannot start does not
    # either: the build below repairs that one.
    is_our_nx() {
        [ -x "$1" ] || return 1
        out=$("$1" --version 2>/dev/null) || return 1
        case $out in "nx, version "*) return 0 ;; esac
        return 1
    }
    find_cli() { # sets NX to the CLI's path: the generation layout first, then the bin directory, then PATH
        for c in "$TOOLS_DIR/current/bin/nx" "$BIN_DIR/nx" "$(command -v nx 2>/dev/null || true)"; do
            if [ -n "$c" ] && is_our_nx "$c"; then NX=$c; return 0; fi
        done
        return 1
    }

    ensure_uv

    if find_cli; then
        say "The Nexus CLI is already installed ($NX); skipping the build."
    else
        say "Building the Nexus CLI (this downloads the conexus package)."
        # conexus@latest, not bare conexus: uv reuses the environment it cached for a bare
        # requirement, which can be a conexus from before --bootstrap existed.
        # The default build installs everything: --extras pdf goes to the GENERATION (the
        # throwaway environment that runs --bootstrap stays slim, and uv's cache means the
        # shared part is fetched once). --slim leaves PDF support out; the first PDF index
        # fetches it then (nexus-2k66t.4.9).
        # On Linux the build gets the CPU build of torch, as the plugin's setup tool gives
        # it to the same build (its build_env function): without it uv installs the CUDA torch
        # and its libraries, about 4.5 GB. NX_TORCH_BACKEND alone overrides, and an
        # ambient UV_TORCH_BACKEND is replaced, so the throwaway environment and the
        # generation get the same torch. A uv too old to know the variable ignores it.
        build_cli() {
            if [ "$SLIM" = yes ]; then
                uv tool run --python 3.12 --from conexus@latest nx self install --bootstrap
            else
                uv tool run --python 3.12 --from conexus@latest nx self install --bootstrap --extras pdf
            fi
        }
        if [ "$(uname -s 2>/dev/null || true)" = Linux ]; then
            ( UV_TORCH_BACKEND="${NX_TORCH_BACKEND:-cpu}"; export UV_TORCH_BACKEND; build_cli ) \
                || die "the build failed (see the lines above). Nothing else was done."
        else
            build_cli || die "the build failed (see the lines above). Nothing else was done."
        fi
        find_cli || die "the build finished but nx was not found in $TOOLS_DIR/current/bin. Open a new terminal and run this again."
        if [ "$SLIM" = yes ]; then
            say "PDF support was left out (--slim). The first PDF you index installs it; to add it now, run: nx self install --extras pdf"
        fi
    fi

    # ---- the CLI-only record ----------------------------------------------------------
    # Claude Code's session-start hook asks about a missing service at every session unless
    # the plugin's setup record says CLI only was chosen. The plugin's setup tool writes that
    # record when it is asked for CLI only; this writes the same file, the same shape, so a
    # CLI-only install by script and by plugin look alike to the hook and to `nx doctor`.
    # The directory is the one the plugin reads: NEXUS_CONFIG_DIR, else NX_CONFIG_DIR, else
    # ~/.config/nexus (a relative value is read against the current directory). The record
    # holds no secret. A run of the plugin's setup that is live keeps its record, and a failed
    # write is a note, not a failure: the install is done either way.
    record_cli_only() {
        cfg=${NEXUS_CONFIG_DIR:-${NX_CONFIG_DIR:-$HOME/.config/nexus}}
        case $cfg in /*) ;; *) cfg=$PWD/$cfg ;; esac
        sdir=$cfg/setup
        if [ -d "$sdir/lock" ]; then return 0; fi
        now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
        if ( umask 077 \
            && mkdir -p "$sdir" \
            && printf '{"state": "done", "stage": "build", "mode": "cli-only", "autostart": false, "started_at": "%s", "updated_at": "%s", "message": "The Nexus CLI is installed. No service is set up.", "steps_done": ["build"], "next": "Reconnect the nexus server or start a new session. To set up a service, ask the user first; if they agree, call setup with mode local."}\n' "$now" "$now" > "$sdir/status.json.$$.tmp" \
            && mv -f "$sdir/status.json.$$.tmp" "$sdir/status.json" \
            && rm -f "$sdir/not-now" ) 2>/dev/null; then
            :
        else
            rm -f "$sdir/status.json.$$.tmp" 2>/dev/null || true
            warn "Note: could not record that no service is set up, so Claude Code may ask about one."
        fi
    }

    # ---- the plugin -------------------------------------------------------------------
    if command -v claude >/dev/null 2>&1; then
        say "Adding the conexus plugin to Claude Code."
        if claude plugin marketplace add "$NX_MARKETPLACE" && claude plugin install "$NX_PLUGIN"; then
            :
        else
            warn "Claude Code did not take the plugin. In Claude Code run:"
            warn "  /plugin marketplace add $NX_MARKETPLACE"
            warn "  /plugin install $NX_PLUGIN"
        fi
    else
        say "Claude Code (claude) is not on the PATH, so the plugin was not added. In Claude Code run:"
        say "  /plugin marketplace add $NX_MARKETPLACE"
        say "  /plugin install $NX_PLUGIN"
    fi

    # ---- the service ------------------------------------------------------------------
    case $MODE in
        local)
            if [ "$AUTOSTART" = yes ]; then flag=--yes; else flag=--no-autostart; fi
            say "Setting up the local service: nx init $flag --no-beads-prime"
            # NX_LOCAL=1 on this one call: nx init picks the managed arm when a service
            # address is configured, and --local asked for the local service.
            NX_LOCAL=1 "$NX" init "$flag" --no-beads-prime </dev/null \
                || die "nx init did not finish. The CLI is installed; fix the cause above and run \`nx init\` again."
            ;;
        cloud)
            say "Configuring the managed service: nx config init"
            if [ "$TTY" = yes ]; then
                "$NX" config init </dev/tty \
                    || die "nx config init did not finish. The CLI is installed; run \`nx config init\` in a terminal."
            else
                "$NX" config init </dev/null \
                    || die "nx config init did not finish. The CLI is installed; run \`nx config init\` in a terminal."
            fi
            ;;
        *)
            record_cli_only
            ;;
    esac

    # ---- done -------------------------------------------------------------------------
    case ":$ORIGINAL_PATH:" in
        *":$BIN_DIR:"*) ;;
        *) say "Note: $BIN_DIR is not on the PATH of this shell. Open a new terminal, or add it to your PATH, so that nx and uv are found." ;;
    esac
    say "Done. If Claude Code is open, reconnect the nexus server (/mcp) or start a new session."
}

main "$@"
