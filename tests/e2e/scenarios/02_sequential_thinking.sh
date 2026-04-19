#!/usr/bin/env bash
# Scenario 02: Sequential thinking — OBSOLETE SKIP
#
# Before 2026-02-26 this scenario exercised `nx thought add/show` — a
# T2-SQLite-backed thought chain whose defining property was survival
# across Claude `/compact`. That CLI subcommand + its T2 persistence
# layer were removed on 2026-02-26 (commit 44017ed) in favour of the
# `mcp__sequential-thinking__sequentialthinking` MCP tool, which is
# session-local to Claude and does NOT survive `/compact`.
#
# The scenario's central assertion ("chain persists after /compact") is
# therefore no longer a property of the system and cannot be resurrected
# without re-introducing the removed `nx thought` surface. Skip with
# clear context rather than delete, so future maintainers reading the
# directory know this gap is intentional.

scenario "02 sequential-thinking: OBSOLETE after nx thought removal (2026-02-26)"
skip "nx thought removed 2026-02-26; sequential-thinking now an MCP tool with no T2 persistence"
scenario_end
