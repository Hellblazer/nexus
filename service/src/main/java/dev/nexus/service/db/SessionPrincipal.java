package dev.nexus.service.db;

/**
 * RDR-152 bead nexus-gmiaf.32.2 — a verified per-session principal.
 *
 * <p>Resolved by {@link TokenStore#resolveSession(String)} from a minted
 * {@code session_tokens} row: a presented session token hash maps to exactly one
 * {@code (tenantId, sessionId)} pair. {@link AuthFilter} stamps {@code sessionId}
 * into the exchange so handlers use the SERVER-RESOLVED session, never a
 * client-supplied one (Decision 2: cross-session denial within a tenant).
 *
 * @param tenantId  the tenant the session belongs to (must match the bearer tenant)
 * @param sessionId the logical session id the token authorises
 */
public record SessionPrincipal(String tenantId, String sessionId) {
}
