#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Exercise the nx CLI command groups against the sandbox.
# Requires: lib.sh sourced, SANDBOX exported.

source "$(dirname "$0")/lib.sh"

print_cli() { uv run nx "$@"; }

step "nx --version"
run "nx --version"                           print_cli --version
run "nx --help"                              print_cli --help

step "nx doctor"
run "nx doctor --check-schema"               print_cli doctor --check-schema

step "nx config"
run "nx config list"                         print_cli config list

step "nx collection list"
run "nx collection list"                     print_cli collection list

step "nx catalog subcommands"
run "nx catalog stats"                       print_cli catalog stats
run "nx catalog coverage"                    print_cli catalog coverage
run "nx catalog orphans"                     print_cli catalog orphans --no-links
run "nx catalog session-summary"             print_cli catalog session-summary

step "nx memory"
run "nx memory put/list/delete cycle"        bash -c "
    uv run nx memory put --project validate --title cli-test --tags validate 'hello world' &&
    uv run nx memory list --project validate &&
    uv run nx memory delete --project validate --title cli-test --yes
"

step "nx scratch"
run "nx scratch put/list cycle"              bash -c "
    uv run nx scratch put 'cli-scratch-test' --tags validate &&
    uv run nx scratch list
"

step "nx store"
run "nx store list"                          print_cli store list

step "nx search (empty corpus)"
run "nx search 'nothing'"                    print_cli search "nothing"

step "nx hooks"
run "nx hooks status"                        print_cli hooks status

step "nx enrich --help"
run "nx enrich --help"                       print_cli enrich --help

step "nx taxonomy --help"
run "nx taxonomy --help"                     print_cli taxonomy --help

step "nx upgrade --dry-run"
run "nx upgrade --dry-run"                   print_cli upgrade --dry-run

summary "cli"
[[ $FAIL -eq 0 ]] || exit 1
