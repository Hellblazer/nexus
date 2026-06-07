package dev.nexus.service.db;

import dev.nexus.service.jooq.tables.Memory;
import dev.nexus.service.jooq.tables.records.MemoryRecord;
import org.jooq.DSLContext;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;

import static dev.nexus.service.jooq.Tables.MEMORY;

/**
 * RDR-152 bead nexus-gmiaf.6 — jOOQ-based memory entry repository.
 *
 * <p>All methods are called within a {@link TenantScope#withTenant} transaction, which:
 * <ol>
 *   <li>stamps the {@code nexus.tenant} GUC on the connection, and</li>
 *   <li>commits on return (or rolls back on exception).</li>
 * </ol>
 *
 * <p>RLS: the {@code tenant_isolation} Postgres policy enforces
 * {@code tenant_id = current_setting('nexus.tenant', true)} on every row access.
 * Callers that pass the wrong tenant see zero rows (not an error), exactly as intended.
 *
 * <p>The generated {@link Memory} table class and {@link MemoryRecord} are the sole
 * references to table/column names. No string-literal SQL for column names is used here.
 *
 * <p>Usage:
 * <pre>{@code
 *   MemoryRepository repo = new MemoryRepository(tenantScope);
 *
 *   // insert
 *   long id = repo.upsert("my-tenant", "my-project", "my-title", "content", null, null, 30);
 *
 *   // query
 *   List<MemoryRecord> rows = repo.findByProject("my-tenant", "my-project");
 * }</pre>
 */
public final class MemoryRepository {

    private static final Logger log = LoggerFactory.getLogger(MemoryRepository.class);

    private final TenantScope tenantScope;

    public MemoryRepository(TenantScope tenantScope) {
        this.tenantScope = tenantScope;
    }

    /**
     * Insert or update a memory entry.
     *
     * <p>ON CONFLICT key is {@code (tenant_id, project, title)} per the changelog
     * comment (cross-tenant key invariant: never use {@code (project, title)} alone).
     *
     * <p>{@code session} is the Python-side session identifier carried on every T2 write
     * and required by the .8 ETL to preserve provenance. Pass {@code null} if not known.
     *
     * @return the generated {@code id} of the inserted/updated row
     */
    public long upsert(String tenant,
                       String project,
                       String title,
                       String content,
                       String tags,
                       String session,
                       String agent,
                       Integer ttlDays) {
        return tenantScope.withTenant(tenant, ctx -> doUpsert(
                ctx, tenant, project, title, content, tags, session, agent, ttlDays));
    }

    /**
     * Find all memory entries in the given project visible to {@code tenant}.
     * RLS prevents cross-tenant leakage: the policy filters at the DB level.
     */
    public List<MemoryRecord> findByProject(String tenant, String project) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project))
                          .orderBy(MEMORY.TIMESTAMP.desc())
                          .fetch());
    }

    /**
     * Find a single memory entry by its unique (project, title) within the tenant.
     * Returns empty if no row matches (RLS filtered or genuinely absent).
     */
    public Optional<MemoryRecord> findByTitle(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.selectFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project)
                              .and(MEMORY.TITLE.eq(title)))
                          .fetchOptional());
    }

    /**
     * Delete a memory entry by (project, title). Returns true if a row was deleted.
     * RLS ensures the row is only deleted if it belongs to {@code tenant}.
     */
    public boolean delete(String tenant, String project, String title) {
        return tenantScope.withTenant(tenant,
                ctx -> ctx.deleteFrom(MEMORY)
                          .where(MEMORY.PROJECT.eq(project)
                              .and(MEMORY.TITLE.eq(title)))
                          .execute() > 0);
    }

    // ── Private helpers ────────────────────────────────────────────────────────

    private long doUpsert(DSLContext ctx,
                           String tenant,
                           String project,
                           String title,
                           String content,
                           String tags,
                           String session,
                           String agent,
                           Integer ttlDays) {
        OffsetDateTime now = OffsetDateTime.now();

        /*
         * ON CONFLICT (tenant_id, project, title): the correct upsert key per
         * the .5 changelog comment.  Using (project, title) alone would bypass
         * tenant_id and allow cross-tenant row collisions.
         */
        var result = ctx.insertInto(MEMORY)
                        .set(MEMORY.TENANT_ID,    tenant)
                        .set(MEMORY.PROJECT,      project)
                        .set(MEMORY.TITLE,        title)
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         tags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    now)
                        .set(MEMORY.TTL,          ttlDays)
                        .set(MEMORY.ACCESS_COUNT, 0)
                        .onConflict(MEMORY.TENANT_ID, MEMORY.PROJECT, MEMORY.TITLE)
                        .doUpdate()
                        .set(MEMORY.CONTENT,      content)
                        .set(MEMORY.TAGS,         tags)
                        .set(MEMORY.SESSION,      session)
                        .set(MEMORY.AGENT,        agent)
                        .set(MEMORY.TIMESTAMP,    now)
                        .set(MEMORY.TTL,          ttlDays)
                        .returning(MEMORY.ID)
                        .fetchOne();

        long id = result != null ? result.getId() : -1L;
        log.debug("event=memory_upsert tenant={} project={} title={} id={}", tenant, project, title, id);
        return id;
    }
}
