"""A stand-in for `nx` that reports the T2 service as unavailable (PROSE_EDIT_NX points at it).

memory.py reads the sentence below as a connection failure and exits 3; brief.py passes that
code through. The failure canary uses it to check that the skill stops instead of repairing.
"""
import sys

sys.stderr.write("T2 storage service unavailable: connection refused (failure canary)\n")
sys.exit(1)
