# Changelog

All notable changes to the dt plugin are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.19.2] - 2026-04-29

Initial release. Plugin version aligned with conexus 4.19.2 (the release that hardened the `nx dt` CLI surface this plugin wraps).

- `/dt:index-selection` slash command wrapping `nx dt index --selection`. Forwards `--collection` and `--corpus` to the underlying CLI.
- `/dt:open-result <tumbler-or-uuid>` slash command wrapping `nx dt open`. Accepts catalog tumblers and raw DEVONthink UUIDs.
- macOS-only. Requires `nx` CLI on `PATH` and DEVONthink running for live selectors.
