// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import dev.nexus.service.db.IdentityException;
import dev.nexus.service.db.IdentityFenceReads;
import dev.nexus.service.db.IdentityFenceReads.MemoryRow;
import dev.nexus.service.db.IdentityLine;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * RDR-253 Step 2: the fences. To fence a write is to refuse it because it comes from a process that no longer
 * holds the seat. The handlers of the four families call one method here at each fenced operation and add no logic
 * of their own: the mailbox claim operations ({@code TupleHandler}), the membership routes
 * ({@code IdentityHandler}), the threads entry ({@code MemoryHandler}) and the identity scratch scope
 * ({@code ScratchHandler}).
 *
 * <p>Two checks, in this order. <b>Ownership</b>: the request's identity token names the family's owner (403
 * {@code not_owner}). <b>Generation</b>: the token's generation is the owner's current one; a token minted for a
 * generation that a later process has taken is {@code 409 generation_superseded}, a code no other 409 of the
 * engine uses (the client's stop-writing signal; an expired token is a 401 from {@code AuthFilter}, which a client
 * answers by re-reading its token file). A token whose identity has let go of the seat
 * (state {@code unheld} or {@code ended}, or {@code lapsed} with its holder recorded gone) is
 * {@code 409 identity_not_held}, since the generation is unchanged by an {@code end}, a {@code release} or a purge;
 * a {@code lapsed} seat is still held and its token still writes. A write with no token is refused ({@code 403 identity_token_required}),
 * except a write to a family whose owner's current binding is {@code unleased} (a Desktop seat, whose MCP server
 * holds no token): that is accepted, labelled with the response header {@link #UNATTESTED_HEADER}, and logged.
 * The exception is keyed on the target and is for a write with no token; a token that names another identity is
 * still refused, since a caller that can prove who it is has no claim on a fence it cannot satisfy. A send to a
 * mailbox is not a fenced operation at all. Reads are never checked.
 *
 * <p>The checks read the identity with {@link IdentityFenceReads} (no lock, no write) in their own statement
 * before the write: a process that takes the seat between the check and the write is refused on its next one,
 * as for every handler-layer check of the engine.
 */
public final class IdentityFence {

    private static final Logger log = LoggerFactory.getLogger(IdentityFence.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** Set on the response of a write accepted without a token to an {@code unleased} seat's family. */
    public static final String UNATTESTED_HEADER = "X-Nexus-Unattested";

    /** The identity scope of T1 scratch: a request's {@code session_id} of {@code identity:<id>}. */
    public static final String SCRATCH_PREFIX = "identity:";

    private static final Pattern ID = Pattern.compile("^i[0-9]+$");
    private static final String MAILBOX = "mailbox/";

    /** One stored or to-be-written T2 entry, as the batch import hands them over. */
    public record Entry(String project, String title, String content) {
    }

    private final IdentityFenceReads reads;

    public IdentityFence(IdentityFenceReads reads) {
        this.reads = reads;
    }

    // ── the two checks ───────────────────────────────────────────────────────

    /** Throws unless the request may write the state of {@code ownerId}; see the class javadoc. */
    public void requireOwner(HttpExchange ex, String tenant, String ownerId) {
        owner(ex, tenant, ownerId, new HashSet<>());
    }

    private void owner(HttpExchange ex, String tenant, String ownerId, Set<String> checked) {
        if (!checked.add(ownerId)) {
            return;
        }
        String tokenId = RequestContext.identity();
        Optional<IdentityFenceReads.State> state = reads.state(tenant, ownerId);
        if (state.isEmpty()) {
            // Nobody owns it: no token can satisfy the fence, and a refusal that says so is the honest one.
            throw new IdentityException(404, "unknown_identity", "no identity " + ownerId, Map.of("identity", ownerId));
        }
        if (tokenId == null) {
            if ("unleased".equals(state.get().lease())) {
                // A parked in is fenced again when it wakes: the label is set once.
                if (!ex.getResponseHeaders().containsKey(UNATTESTED_HEADER)) {
                    ex.getResponseHeaders().add(UNATTESTED_HEADER, "true");
                }
                log.info("event=identity_fence_unattested tenant={} owner={} path={}", tenant, ownerId,
                    ex.getRequestURI().getPath());
                return;
            }
            throw new IdentityException(403, "identity_token_required",
                "a write to the state of " + ownerId + " needs an identity token", Map.of("identity", ownerId));
        }
        if (!tokenId.equals(ownerId)) {
            throw new IdentityException(403, "not_owner",
                "the identity token names " + tokenId + ", not the owner " + ownerId,
                Map.of("identity", ownerId, "token_identity", tokenId));
        }
        IdentityFenceReads.State st = state.get();
        Integer tokenGeneration = RequestContext.identityGeneration();
        if (tokenGeneration == null || tokenGeneration != st.generation()) {
            throw new IdentityException(409, "generation_superseded",
                ownerId + " is at generation " + st.generation() + "; this token is for " + tokenGeneration,
                refusalDetails(ownerId, tokenGeneration, st));
        }
        // A token outlives the seat it was minted for: after end, release or a purge the generation is unchanged,
        // so the generation alone would let a process that has let go of the seat keep writing. A lapsed seat is
        // still held (a sleeping host wakes and renews); only one whose holder a client recorded gone is not.
        if ("unheld".equals(st.state()) || "ended".equals(st.state())
            || ("lapsed".equals(st.state()) && st.holderGone())) {
            throw new IdentityException(409, "identity_not_held",
                ownerId + " is " + st.state() + (st.holderGone() ? " with its holder gone" : "")
                    + ": no process holds it, so this token fences nothing",
                refusalDetails(ownerId, tokenGeneration, st));
        }
    }

    private static Map<String, Object> refusalDetails(String ownerId, Integer tokenGeneration,
                                                      IdentityFenceReads.State st) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("identity", ownerId);
        d.put("token_generation", tokenGeneration);
        d.put("current_generation", st.generation());
        d.put("state", st.state());
        if (st.holderHost() != null || st.holderPid() != null) {
            Map<String, Object> holder = new LinkedHashMap<>();
            holder.put("host", st.holderHost());
            holder.put("pid", st.holderPid());
            d.put("holder", holder);
        }
        return d;
    }

    /** Writes a refusal as the JSON the identity routes use: {@code error} is the code, then the message and details. */
    public static void refuse(HttpExchange ex, IdentityException e) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", e.code());
        body.put("message", e.getMessage());
        body.putAll(e.details());
        HttpUtil.send(ex, e.status(), MAPPER.writeValueAsString(body));
    }

    // ── mailbox ──────────────────────────────────────────────────────────────

    /** The identity that owns {@code subspace} ({@code mailbox/i<digits>}), or null for any other subspace. */
    static String mailboxOwner(String subspace) {
        if (subspace == null || !subspace.startsWith(MAILBOX)) {
            return null;
        }
        String address = subspace.substring(MAILBOX.length());
        return ID.matcher(address).matches() ? address : null;
    }

    /** {@code in} and {@code inp}: the owner's claim of mail in {@code subspace}. */
    public void mailboxClaim(HttpExchange ex, String tenant, String subspace) {
        String owner = mailboxOwner(subspace);
        if (owner != null) {
            requireOwner(ex, tenant, owner);
        }
    }

    /**
     * {@code ack}, {@code nack}, {@code renew} and {@code release} name only a claim id, so the claim's subspace is
     * looked up; a claim that is no longer live has none, and the operation answers what it always has.
     */
    public void mailboxClaimById(HttpExchange ex, String tenant, String claimId) {
        reads.claimSubspace(tenant, claimId).ifPresent(subspace -> mailboxClaim(ex, tenant, subspace));
    }

    /**
     * The {@code sender_session} stamp of a tuple written with this request (JDR-002). With an identity token it is
     * the session id of the identity's newest binding row under the token's generation, so a write after
     * {@code /clear} is stamped with the new session id and never with one frozen into the token; otherwise the
     * minted session's, as before; otherwise null.
     */
    public String senderSession(String tenant) {
        String identity = RequestContext.identity();
        Integer generation = RequestContext.identityGeneration();
        if (identity != null && generation != null) {
            Optional<String> current = reads.currentSession(tenant, identity, generation);
            if (current.isPresent()) {
                return current.get();
            }
        }
        return RequestContext.isMintedSession() ? RequestContext.session() : null;
    }

    // ── threads entries (T2 memory, project threads) ─────────────────────────

    /** True for the project whose entries carrying an {@code identity:} line are fenced. */
    public static boolean threads(String project) {
        return IdentityLine.PROJECT.equals(project);
    }

    /**
     * {@code put}, {@code put_or_merge} and {@code import}: one write of {@code content} at {@code (project, title)},
     * and, for a {@code put} with {@code replaces}, the removal from view of the entry titled {@code replaces}.
     */
    public void threadsWrite(HttpExchange ex, String tenant, String project, String title, String content,
                             String replaces) {
        if (!threads(project)) {
            return;
        }
        Set<String> checked = new HashSet<>();
        guard(ex, tenant, reads.memoryByTitle(tenant, project, title).orElse(null), content, checked);
        if (replaces != null) {
            guard(ex, tenant, reads.memoryByTitle(tenant, project, replaces).orElse(null), null, checked);
        }
    }

    /** {@code import_batch}: every row of the batch that is in project threads, checked against its stored entry. */
    public void threadsBatch(HttpExchange ex, String tenant, Collection<Entry> rows) {
        List<Entry> mine = rows.stream().filter(r -> threads(r.project())).toList();
        if (mine.isEmpty()) {
            return;
        }
        Map<String, MemoryRow> stored = new LinkedHashMap<>();
        reads.memoryByTitles(tenant, IdentityLine.PROJECT, mine.stream().map(Entry::title).distinct().toList())
            .forEach(r -> stored.put(r.title(), r));
        Set<String> checked = new HashSet<>();
        for (Entry e : mine) {
            guard(ex, tenant, stored.get(e.title()), e.content(), checked);
        }
    }

    /**
     * Entries named by id: {@code delete} by id, {@code restore}, {@code merge} (the kept row takes
     * {@code keepContent}; the others are removed) and the sources of a rollup summary. Rows outside project threads
     * and rows with no {@code identity:} line are not fenced.
     */
    public void threadsRows(HttpExchange ex, String tenant, Collection<Long> ids, Long keepId, String keepContent) {
        Set<String> checked = new HashSet<>();
        for (MemoryRow row : reads.memoryByIds(tenant, ids)) {
            if (threads(row.project())) {
                guard(ex, tenant, row, keepId != null && keepId == row.id() ? keepContent : null, checked);
            }
        }
    }

    /**
     * One entry. {@code stored} is the row as it is (null: none yet) and {@code content} what the write would leave
     * ({@code null}: the entry is removed from view or its content is not rewritten). A stored entry with an
     * identity line needs its owner; the line may not be removed or changed. An entry with no line may be given
     * one only by the identity it names. Reading the stored entry and not the request is what stops a writer from
     * claiming an entry by dropping the line from the write.
     */
    private void guard(HttpExchange ex, String tenant, MemoryRow stored, String content, Set<String> checked) {
        Set<String> storedIds = stored == null ? Set.of() : IdentityLine.ids(stored.content());
        Set<String> newIds = content == null ? Set.of() : IdentityLine.ids(content);
        for (String id : storedIds) {
            owner(ex, tenant, id, checked);
        }
        if (!storedIds.isEmpty()) {
            if (content != null && !storedIds.equals(newIds)) {
                throw new IdentityException(403, "identity_line_immutable",
                    "an entry that names " + storedIds + " may not drop or change its identity: line",
                    Map.of("identity", String.join(",", storedIds)));
            }
            return;
        }
        for (String id : newIds) {
            owner(ex, tenant, id, checked);
        }
    }

    /** Stamps a T2 row with the identity of the request's token; a request with none stamps nothing. */
    public void stamp(String tenant, long memoryId) {
        String identity = RequestContext.identity();
        if (identity != null) {
            reads.stampMemory(tenant, memoryId, identity);
        }
    }

    // ── identity scratch (T1 identity scope) ─────────────────────────────────

    /** True when {@code sessionId} names the identity scope ({@code identity:<id>}) rather than a session. */
    public static boolean isIdentityScope(String sessionId) {
        return sessionId != null && sessionId.startsWith(SCRATCH_PREFIX);
    }

    /** The identity an identity scope belongs to; 400 when what follows the prefix is not an identity id. */
    static String scopeOwner(String scope) {
        String id = scope.substring(SCRATCH_PREFIX.length());
        if (!ID.matcher(id).matches()) {
            throw new IdentityException(400, "bad_scope", "an identity scope is identity:<id>, with an id like i7");
        }
        return id;
    }

    /** {@code put}, {@code flag}, {@code unflag}, {@code delete}: a write to the identity scope needs its owner. */
    public void scratchWrite(HttpExchange ex, String tenant, String sessionId) {
        if (isIdentityScope(sessionId)) {
            requireOwner(ex, tenant, scopeOwner(sessionId));
        }
    }

    /**
     * Whether a session-scope request whose body names {@code sessionId} is allowed for the request's identity
     * token: the session index maps that session to the token's identity and generation. This is what keeps a write
     * after {@code /clear} (a new session id under the same generation) from being refused.
     */
    public boolean sessionAccepted(String tenant, String sessionId) {
        String identity = RequestContext.identity();
        Integer generation = RequestContext.identityGeneration();
        return identity != null && generation != null && reads.sessionMapsTo(tenant, sessionId, identity, generation);
    }
}
