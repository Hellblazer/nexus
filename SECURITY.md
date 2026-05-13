# Security Policy

This document summarises nexus's v1 trust boundary. The authoritative
design record is [RDR-113](docs/rdr/rdr-113-host-trust-model.md); if
the two ever disagree, the RDR wins. SECURITY.md is the user-facing
synopsis for packagers, deployers, and reviewers who shouldn't have
to read the full design document.

## Trust Boundary (v1)

nexus is designed for **single-user host trust**. The daemon owner and
every connecting client must share a UID. There is no cross-user
isolation, no token authentication, and no transport-level encryption
in v1; those are explicit non-goals.

The mechanism is straightforward:

1. The Unix-domain socket is created with `chmod 0600` immediately
   after bind, before `listen()` is called. No peer can connect during
   the bind-to-chmod window: `connect()` against a bound-but-not-listening
   UDS returns `ConnectionRefusedError` (verified by RDR-113 spike A1),
   so the actual gate is `listen()`, not `bind()`.
2. The parent socket directory is `0o700` as a defence-in-depth tier.
3. The TCP listener (used by container deployments) is hard-coded to
   `127.0.0.1`. There is no `--bind-host` flag. The TCP port number is
   chosen dynamically at startup.
4. On every UDS `accept()`, the daemon reads peer credentials via
   `SO_PEERCRED` (Linux) or `LOCAL_PEERCRED` (macOS) and rejects the
   connection if the peer's effective UID does not match the daemon's
   UID. TCP connections skip the UID check because loopback already
   gates them to the local user namespace.

## Threats Addressed

- **A second user on the same host attaches to my UDS.** Blocked by
  the `0o600` mode plus the peer-UID check.
- **A second user crafts a TCP connection to my daemon.** The
  hard-coded loopback bind prevents the listener from being reachable
  from the network. Reaching loopback as a different local user is
  taken to require OS-level access the v1 model assumes is not
  available; RDR-113 §Key Discoveries lists this as an explicit
  assumption rather than an enforced mechanism.
- **The TCP listener escapes to the network.** Blocked by the same
  hard-coded loopback bind. There is no configuration knob to expose
  the listener externally.

## Threats NOT Addressed (Accepted Risk)

- **Same-user malicious processes.** A process running as the daemon's
  UID owns the socket and the SQLite file directly. Defending against
  this is outside nexus's trust boundary; the assumption is that the
  user trusts the code running as their own UID.
- **Compromised parent processes injecting environment variables.** If
  something with the daemon's UID can set `NX_T2_ADDR` (or its
  equivalent), it can redirect the client; the daemon does not validate
  client-supplied addresses against a registry.
- **Cross-host attackers.** There is no network transport in v1, and
  cross-host deployments are explicitly out of scope for RDR-112.

## Forward Reference

Token-based authentication, mTLS, group-based UDS ACLs, and similar
mechanisms are not part of v1. They land in a future RDR if and when
the threat model expands: multi-user shared dev VMs, federated
deployments, or cross-host operation. Until that RDR exists, the
single-user host-local v1 path is the supported deployment.

A future RDR will own its own threat model covering multi-user,
federated, or cross-host deployments; expect the v1 contract here to
grow rather than be replaced.

## Reporting a Vulnerability

Please report security issues privately, **not** in a public GitHub
issue:

- Open a [GitHub security advisory](https://github.com/Hellblazer/nexus/security/advisories/new), or
- Email `hal.hildebrand@me.com` with `[nexus-security]` in the subject.

We aim to acknowledge reports promptly and coordinate disclosure on
a timeline that matches the severity. nexus is a solo-maintainer
project, so response cadence is best-effort and varies with
availability. If your report is
about a deployment scenario that is already documented as an accepted
risk above, we'll still respond, but expect the answer to point back
to this document.
