// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import org.jooq.DSLContext;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Tips that a {@code board/ci/<base>--done} row carries a verdict for and no {@code --push} record names
 * ({@code PushChain.Node#recorded()} false; RDR-252 post-close audit S3). The push script writes the record after the
 * push and reports a failed write on stderr only, and a tip without one has no pusher: as the earliest suspect of a red
 * it hands the red to the leader as {@code no_record}, and inside a red it joins the suspects with nobody behind it.
 * This is the count that makes the failure visible: the stamper logs each such tip when its {@code --done} row lands,
 * and the group read ({@code GET /v1/group/<name>}) carries the live total as {@code unrecorded_tips}.
 *
 * <p>The total is derived from the live rows, so it needs no state of its own and survives an engine restart; it
 * counts the tips whose rows both subspaces still keep (three days).
 */
public final class UnrecordedTips {

    private UnrecordedTips() {
    }

    /** The distinct tips with a counting verdict in {@code dones} and no push record in {@code pushes}, oldest first. */
    static List<String> missing(List<BoardRow> pushes, List<BoardRow> dones) {
        Set<String> recorded = new HashSet<>();
        for (BoardRow p : pushes) {
            if (p.sha() != null && !p.sha().isBlank()) {
                recorded.add(p.sha());
            }
        }
        Set<String> out = new LinkedHashSet<>();
        for (BoardRow d : dones) {
            DoneVerdict.of(d).filter(DoneVerdict.Verdict::counts).map(DoneVerdict.Verdict::sha)
                .filter(sha -> !recorded.contains(sha)).ifPresent(out::add);
        }
        return new ArrayList<>(out);
    }

    /** The total over the topic families {@code bases} of one tenant, read in the caller's transaction. */
    public static int count(DSLContext ctx, String tenant, Collection<String> bases) {
        int n = 0;
        for (String base : bases) {
            n += missing(BoardReads.rows(ctx, tenant, base, "push"), BoardReads.rows(ctx, tenant, base, "done")).size();
        }
        return n;
    }
}
