# lib/require_container.sh — hard refusal for a rehearse*.sh that assumes it
# runs inside its rehearsal container. Sourced, never executed directly; the
# check fires as a side effect of sourcing so a caller cannot forget the
# follow-up call.
#
# Every rehearse*.sh header says "Runs INSIDE the container" as a comment.
# Nothing enforced it: run on a host, these scripts provision a REAL Postgres
# under $HOME/.config/nexus, install the native service binary there, source
# pg_credentials, and (pre-nexus-oqh4s) rewrote the host's git identity via
# `git config --global`. A host run damages the operator's actual local
# nexus install. This makes "inside the container" a checked fact.
#
# Detection, any one sufficient:
#   NX_REHEARSAL_IN_CONTAINER=1   explicit marker, set by every rehearsal
#                                  Dockerfile in this directory (ENV)
#   /.dockerenv                   Docker's own container marker
#   /run/.containerenv             Podman's own container marker
if [[ "${NX_REHEARSAL_IN_CONTAINER:-}" != "1" && ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
    echo "REFUSING: $(basename "${BASH_SOURCE[1]:-$0}") runs only inside its rehearsal container." >&2
    echo "  No NX_REHEARSAL_IN_CONTAINER=1, /.dockerenv, or /run/.containerenv found." >&2
    echo "  Run via run.sh (which builds and docker-runs the matching image) — never directly on a host: it provisions a real Postgres and service binary under \$HOME/.config/nexus." >&2
    exit 2
fi
