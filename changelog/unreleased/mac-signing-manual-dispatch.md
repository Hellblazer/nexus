### Changed

- **The macOS signing rehearsal runs only by manual dispatch** until the Apple Developer ID secrets are set in the `apple-signing` environment, so it no longer fails on develop whenever its workflow file changes (Sam 2026-11-13). Run it with `gh workflow run mac-signing-rehearsal.yml --ref develop`.
