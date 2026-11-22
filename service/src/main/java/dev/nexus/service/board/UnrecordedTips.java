// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.jooq.DSLContext;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static dev.nexus.service.jooq.nexus.Tables.IDENTITY_SESSIONS;

/**
 * Two figures for the push records of the tips a {@code board/ci/<base>--done} row carries a verdict for (RDR-252
 * post-close audit S3). The push script writes the record after the push and reports a failed write on stderr only; a
 * tip without a record has no pusher, so as the earliest suspect of a red it hands the red to the leader as
 * {@code no_record}, and inside a red it joins the suspects with nobody behind it.
 * <ul>
 *   <li><b>unrecorded</b>: no {@code --push} row names the tip ({@code PushChain.Node#recorded()} false);</li>
 *   <li><b>unattributed</b>: a row names it, but its {@code sender_session} is null or maps to no identity (the
 *       minimal record the push script falls back to when the stamped write fails), so the tip has a record and
 *       still no pusher.</li>
 * </ul>
 *
 * <p>The window is the {@code --done} rows still alive and not near their end: a row counts only while more than
 * {@link #EXPIRY_MARGIN} of its own life is left. The live board keeps a {@code --done} row about 24 hours (the
 * adapter sets the row's ttl; the template's 3 days is only its ceiling), and the {@code --push} row of a tip is made
 * earlier than its {@code --done} row, so it expires earlier; counting a {@code --done} row in its last two hours
 * would report tips whose push record merely expired first. A hand push or a back-merge with no record by design is
 * counted too: the figure says "no pusher known", not "the script failed".
 *
 * <p>The figures are derived from the live rows each time, so they need no state and survive a restart. What does not
 * survive the window is the count itself: the durable trace of an unrecorded tip is the stamper's WARN line
 * ({@code event=board_done_tip_unrecorded sha=...}), logged once per sha when its first red {@code --done} row lands.
 */
public final class UnrecordedTips {

    /** A {@code --done} row in its last this-long of life is left out (its tip's push row may have expired first). */
    static final Duration EXPIRY_MARGIN = Duration.ofHours(2);

    /** The two figures. */
    public record Counts(int unrecorded, int unattributed) {
    }

    private UnrecordedTips() {
    }

    /** The distinct tips with a counting verdict in a live {@code --done} row and no push record, oldest first. */
    static List<String> missing(List<BoardRow> pushes, List<BoardRow> dones, Instant now) {
        Set<String> recorded = new HashSet<>();
        for (BoardRow p : pushes) {
            if (p.sha() != null && !p.sha().isBlank()) {
                recorded.add(p.sha());
            }
        }
        List<String> out = new ArrayList<>();
        for (String sha : tips(dones, now)) {
            if (!recorded.contains(sha)) {
                out.add(sha);
            }
        }
        return out;
    }

    /**
     * The distinct tips, among those in the window, whose first push record has a null or blank sender session or one
     * not in {@code mappedStamps} (the sessions the session index maps to an identity).
     */
    static List<String> unattributed(List<BoardRow> pushes, List<BoardRow> dones, Instant now, Set<String> mappedStamps) {
        Map<String, BoardRow> first = new HashMap<>();
        for (BoardRow p : pushes.stream().sorted(java.util.Comparator.comparing(BoardRow::createdAt)
            .thenComparing(BoardRow::id)).toList()) {
            if (p.sha() != null && !p.sha().isBlank()) {
                first.putIfAbsent(p.sha(), p);
            }
        }
        List<String> out = new ArrayList<>();
        for (String sha : tips(dones, now)) {
            BoardRow p = first.get(sha);
            if (p != null && (p.senderSession() == null || p.senderSession().isBlank() || !mappedStamps.contains(p.senderSession()))) {
                out.add(sha);
            }
        }
        return out;
    }

    /** The distinct tips of the counting {@code --done} rows that are alive and not in their last {@link #EXPIRY_MARGIN}. */
    private static Set<String> tips(List<BoardRow> dones, Instant now) {
        Set<String> out = new LinkedHashSet<>();
        for (BoardRow d : dones) {
            if (d.expiresAt() != null && !d.expiresAt().minus(EXPIRY_MARGIN).isAfter(now)) {
                continue;
            }
            DoneVerdict.of(d).filter(DoneVerdict.Verdict::counts).map(DoneVerdict.Verdict::sha).ifPresent(out::add);
        }
        return out;
    }

    /**
     * The two figures over the topic families {@code bases} of one tenant, read in the caller's transaction. Cost: per
     * base, two reads of at most 5000 rows ({@code --push}, {@code --done}), and one session index read; nothing is
     * cached, because the group read is rare (the aura load gate reads it once at load).
     */
    public static Counts count(DSLContext ctx, String tenant, Collection<String> bases, Instant now) {
        int unrecorded = 0;
        int unattributed = 0;
        for (String base : bases) {
            List<BoardRow> pushes = BoardReads.rows(ctx, tenant, base, "push");
            List<BoardRow> dones = BoardReads.rows(ctx, tenant, base, "done");
            unrecorded += missing(pushes, dones, now).size();
            Set<String> stamps = new HashSet<>();
            pushes.forEach(p -> {
                if (p.senderSession() != null && !p.senderSession().isBlank()) {
                    stamps.add(p.senderSession());
                }
            });
            Set<String> mapped = stamps.isEmpty() ? Set.of() : new HashSet<>(ctx.select(IDENTITY_SESSIONS.SESSION_ID)
                .from(IDENTITY_SESSIONS).where(IDENTITY_SESSIONS.TENANT_ID.eq(tenant))
                .and(IDENTITY_SESSIONS.SESSION_ID.in(stamps)).fetch(IDENTITY_SESSIONS.SESSION_ID));
            unattributed += unattributed(pushes, dones, now, mapped).size();
        }
        return new Counts(unrecorded, unattributed);
    }
}
