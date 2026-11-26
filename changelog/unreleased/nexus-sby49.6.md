### Fixed

- **`nx hook auto-approve` answers only `PreToolUse`.** It no longer answers `PermissionRequest` (or an event with no name), so a user's own `ask` rule for a conexus tool prompts again instead of being overridden by the plugin's allowlist.
