#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Wrapper that picks a Python interpreter satisfying the plugin's >=3.12
# requirement and execs the given hook script under it.
#
# Probes higher versions first so a system that has python3.13 on PATH
# uses it even when unqualified `python3` resolves to something older
# (the common macOS case where /Library/Frameworks/Python.framework
# wins PATH precedence over /opt/homebrew/bin). Falls back to plain
# `python3` last — if that happens to be too old, the hook script's own
# `sys.version_info < (3, 12)` guard surfaces a clean actionable error
# rather than the worse parser-failure modes we'd see otherwise.
#
# Usage:
#   bash _run_python_hook.sh /abs/path/to/hook_script.py [args...]

set -u

# Order matches conexus's supported Python range (>=3.12,<3.14 in
# pyproject.toml). If conexus widens that range, add new versions here.
for py in python3.13 python3.12; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" "$@"
  fi
done

# Last resort: plain python3. Hook script's own version guard handles
# the too-old case loudly. If python3 happens to be 3.14+, the hook
# itself runs fine (hooks are stdlib-only) but downstream `nx ...`
# subprocess calls will fail since conexus isn't installable there.
exec python3 "$@"
