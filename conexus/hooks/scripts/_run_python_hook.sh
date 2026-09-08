#!/usr/bin/env bash
# Run a Python hook script under an interpreter that can serve it.
#
# Order:
#   1. The installed conexus generation's own python
#      (<tools>/current/bin/python; <tools> is $NX_TOOLS_DIR or
#      ~/.local/share/nexus/tools). It is the only interpreter on a box that
#      is guaranteed to import `nexus` and its dependencies, which the hooks
#      that ask the catalog or T3 a question (rdr_hook.py,
#      phase_review_close_requires_gate.py) need. Measured 2026-09-08
#      (nexus-owna8's follow-up): under a bare Homebrew python3.13 those
#      imports fail, the failure cannot even be logged (structlog is missing
#      too), and the rdr hook reported a fully indexed tree as NOT indexed on
#      every session start while its own tests passed in the dev venv.
#   2. python3.13, then python3.12 by name, so a macOS framework python3
#      (3.10) that wins PATH precedence does not run the hook.
#   3. Plain python3, so the hook's own version guard can print its error.
set -u
# ${HOME:-} so a hook launched with no HOME (a scrubbed env) falls through
# instead of dying on `set -u`; the run check so a partially reaped or
# wrong-arch generation python falls through instead of exec failing.
tools="${NX_TOOLS_DIR:-${HOME:-}/.local/share/nexus/tools}"
gen_py="$tools/current/bin/python"
if [ -x "$gen_py" ] && "$gen_py" -c '' >/dev/null 2>&1; then
  exec "$gen_py" "$@"
fi
for py in python3.13 python3.12; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" "$@"
  fi
done
exec python3 "$@"
