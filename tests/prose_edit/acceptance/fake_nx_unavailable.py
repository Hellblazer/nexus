"""A stand-in for `nx` that reports the T2 service as unavailable (PROSE_EDIT_NX points at it).

It prints what a real nx prints when no service endpoint is published, remedy sentence
included, so the failure canary sees the text a real failure hands memory.py.
test_acceptance_tools.py ties the sentence to src/nexus/db/service_endpoint.py.
memory.py reads it as a connection failure and exits 3 in its own words; brief.py passes
that code through. The canary checks that the skill stops instead of repairing.
"""
import sys

sys.stderr.write(
    "Error: T2 storage service unavailable: nexus-service endpoint is not resolvable "
    "(NX_STORAGE_BACKEND=service): start the supervisor with 'nx daemon service start' "
    "(publishes the endpoint lease this client auto-discovers), or export NX_SERVICE_PORT / "
    "NX_SERVICE_TOKEN (and optionally NX_SERVICE_HOST) explicitly.\n"
)
sys.exit(1)
