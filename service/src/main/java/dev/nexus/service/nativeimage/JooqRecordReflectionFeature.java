package dev.nexus.service.nativeimage;

import dev.nexus.service.jooq.nexus.Nexus;
import dev.nexus.service.jooq.t1.T1;
import java.util.ArrayList;
import java.util.List;
import org.graalvm.nativeimage.hosted.Feature;
import org.graalvm.nativeimage.hosted.RuntimeReflection;
import org.jooq.Table;

/**
 * GraalVM native-image {@link Feature} that registers the constructors of every
 * jOOQ generated record type for reflection.
 *
 * <p>Why this is necessary: jOOQ's {@code INSERT … RETURNING} path
 * ({@code org.jooq.impl.Tools.recordFactory} → {@code TableImpl.getRecordConstructor}
 * → {@code Class.getDeclaredConstructor()}) reflectively instantiates the generated
 * record class to materialise the returned row. Community reachability metadata
 * covers jOOQ's own library classes but cannot know about an application's generated
 * records, so in the native image every {@code RETURNING} write — and, per
 * nexus-opr9m, every plain {@code SELECT} materialising rows via
 * {@code selectFrom(...).fetch()/fetchOne()} — throws
 * {@code org.graalvm.nativeimage.MissingReflectionRegistrationError} → HTTP 500.
 * This was first discovered by RDR-173 P7 (nexus-i9o37): {@code POST /v1/aspects/upsert}
 * 500'd on {@code DocumentAspectsRecord.<init>()}, leaving {@code document_aspects}
 * empty even though the aspect worker extracted correctly.
 *
 * <p>Registration is driven by jOOQ's own schema model, but the schema model is
 * PER GENERATED SCHEMA — {@link Nexus#NEXUS} covers the {@code nexus} schema (T2
 * stores) only. The {@code t1} schema (T1 scratch, nexus-gmiaf.13) is a SEPARATE
 * generated schema ({@link T1#T1}) and was never enumerated here, so
 * {@code ScratchRecord}'s constructor was unreachable in the native image — every
 * cloud-deployed {@code get}/{@code search}/{@code list} against T1 scratch 500'd
 * (nexus-opr9m: 63/63 failures over 48h, 100% reproducible; writes were unaffected
 * because a plain {@code INSERT...execute()} with no {@code RETURNING} never hits
 * the record-materialization path). Every generated schema's tables must be
 * enumerated here — this is the second time a schema was added without updating
 * this Feature; there is no compile-time signal that a new generated schema needs
 * wiring in, so watch for this class of gap whenever a new Liquibase schema (not
 * just a new table) is introduced.
 *
 * <p>Only constructors are registered — that is the exact failing mechanism;
 * jOOQ sets the returned column values through its internal {@code TableField} model,
 * not by reflecting on record members, so registering methods/fields would be
 * unevidenced scope (RDR-173 nexus-i9o37).
 *
 * <p>Wired via {@code --features=dev.nexus.service.nativeimage.JooqRecordReflectionFeature}
 * in the {@code native} profile of {@code service/pom.xml}.
 */
public final class JooqRecordReflectionFeature implements Feature {

    /**
     * The record types this Feature registers, derived from jOOQ's schema model
     * across every generated schema ({@code nexus} and {@code t1}).
     *
     * <p>This is the SAME discovery path {@link #beforeAnalysis} uses; it is exposed
     * (package-visible) so the structural guard test exercises the Feature's real
     * enumeration rather than re-deriving the set independently.
     */
    static List<Class<?>> recordTypes() {
        List<Class<?>> records = new ArrayList<>();
        for (Table<?> table : Nexus.NEXUS.getTables()) {
            records.add(table.getRecordType());
        }
        for (Table<?> table : T1.T1.getTables()) {
            records.add(table.getRecordType());
        }
        return records;
    }

    @Override
    public void beforeAnalysis(BeforeAnalysisAccess access) {
        for (Class<?> recordType : recordTypes()) {
            RuntimeReflection.register(recordType);
            RuntimeReflection.register(recordType.getDeclaredConstructors());
        }
    }
}
