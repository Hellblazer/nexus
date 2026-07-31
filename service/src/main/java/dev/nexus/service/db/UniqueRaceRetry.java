// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.function.Supplier;

/**
 * Transaction-boundary retry for a unique violation on a NON-arbitrated key
 * (nexus-0ehwe arbiter class; sibling of {@link DeadlockRetry}).
 *
 * <p><strong>The gap this closes.</strong> The primary fix for the arbiter class is to
 * RESOLVE the table's other identity keys BEFORE writing, so the statement cannot reach
 * them — the same prevent-rather-than-catch stance {@code claimNextSeq} takes for the
 * tumbler PK. Under {@code READ COMMITTED} that check is a TOCTOU: two transactions can
 * both resolve "absent" and both proceed, and the loser gets a {@code 23505} on a key
 * its {@code ON CONFLICT} does not name. That is not hypothetical — it is precisely what
 * {@code idx_catalog_owners_repo_hash}'s own DDL comment calls a "TOCTOU guard", and it
 * is how nexus-jq53b was first observed (a CI flake on PR #1423).
 *
 * <p><strong>Why retrying the whole transaction, and not catching in place.</strong>
 * {@code TenantScope.withTenant} runs its unit of work in ONE transaction, so a failed
 * statement puts the session in {@code 25P02} (current transaction is aborted) and every
 * follow-up query inside that lambda fails too. Recovering in place would need
 * savepoints; retrying at the transaction boundary needs none, because PostgreSQL has
 * already rolled the failed attempt back before the exception surfaces.
 *
 * <p><strong>Why bounded at two attempts, and why that terminates.</strong> The retry is
 * not a hope — it changes the input to the resolve-first step. On attempt 2 the racer's
 * row is COMMITTED and therefore visible, so resolution is now deterministic: it either
 * converges on that row or refuses with a
 * {@link CatalogIdentityConflictException}. A third attempt could not learn anything a
 * second did not, so exhausting the budget rethrows the original violation unchanged
 * rather than spinning.
 *
 * <p>The wrapped unit MUST be only the DB transaction — never anything with external
 * side effects, or a retry would repeat them.
 */
public final class UniqueRaceRetry {

    private static final Logger log = LoggerFactory.getLogger(UniqueRaceRetry.class);

    /** Total attempts (initial try + one informed retry). See the class javadoc. */
    static final int MAX_ATTEMPTS = 2;

    private UniqueRaceRetry() {
    }

    /**
     * Run {@code writeTxn}, retrying ONCE if it fails with a {@code 23505} naming one of
     * {@code constraints}.
     *
     * @param context     short label for the retry log line (e.g. {@code "upsertOwner"})
     * @param constraints the NON-arbitrated unique keys whose violation is a losable race;
     *                    a violation of any OTHER key propagates immediately and unchanged
     * @param writeTxn    the transaction to run; MUST be free of external side effects
     */
    public static <T> T run(String context, String[] constraints, Supplier<T> writeTxn) {
        int attempt = 0;
        while (true) {
            try {
                return writeTxn.get();
            } catch (RuntimeException ex) {
                if (SqlConstraints.violatesAny(ex, constraints) && ++attempt < MAX_ATTEMPTS) {
                    log.warn("event=unique_race_retry context={} constraint={} attempt={} maxAttempts={}",
                            context, SqlConstraints.violated(ex), attempt, MAX_ATTEMPTS);
                    continue;
                }
                throw ex;
            }
        }
    }

    /** {@code void} variant of {@link #run(String, String[], Supplier)}. */
    public static void run(String context, String[] constraints, Runnable writeTxn) {
        run(context, constraints, () -> {
            writeTxn.run();
            return null;
        });
    }
}
