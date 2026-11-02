#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-89lp5: did the engine log a native tokenizer used after it was freed?
#
# usage: tokenizer_freed_in_log.sh <svc.log>
#   exit 1, offending lines on stdout: the log shows an encode against a freed tokenizer
#   exit 0, no output:                 clean
#   exit 2:                            the log could not be read (a check that read nothing is a failure)
#
# The strings are the two ways a tokenizer freed under an encode shows: DJL's
# IllegalStateException on the good side of the race, a Rust panic / runtime abort on the bad one.
# native-smoke.sh's SIGTERM leg calls this; tests/test_native_smoke_tokenizer_probe.py feeds it a
# log with those strings (must exit 1) and a clean log (must exit 0), so the pattern is known to
# fail when the bug is present.
set -u
log="${1:?usage: tokenizer_freed_in_log.sh <svc.log>}"
[ -r "$log" ] || { echo "tokenizer_freed_in_log: cannot read $log" >&2; exit 2; }
grep -nE 'Native resource has been released already|panicked at|fatal runtime error' "$log"
rc=$?
case "$rc" in
  0) exit 1 ;;
  1) exit 0 ;;
  *) exit 2 ;;
esac
