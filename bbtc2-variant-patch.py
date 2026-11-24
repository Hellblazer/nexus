# measurement-only patch (nexus-bbtc2 remeasure); never committed. Reads BBTC2_VARIANT in the engine env.
import sys
F = "service/src/main/java/dev/nexus/service/vectors/PgVectorRepository.java"
s = open(F).read()
old1 = """                    Result<? extends Record> all = ctx.selectFrom(probeFn).fetch();
                    if (all.size() <= selectiveGateMax) {"""
new1 = """                    final String bbtc2Variant = System.getenv().getOrDefault("BBTC2_VARIANT", "");
                    Result<? extends Record> all = "direct".equals(bbtc2Variant) ? null : ctx.selectFrom(probeFn).fetch();
                    if (all != null && all.size() <= selectiveGateMax) {"""
old2 = """                    PgSession.setSearchStatementTimeout(ctx, bound.getAsInt());
                    Result<? extends Record> per = ctx.selectFrom("""
new2 = """                    if ("fallback".equals(bbtc2Variant)) {
                        return () -> { };
                    }
                    PgSession.setSearchStatementTimeout(ctx, bound.getAsInt());
                    Result<? extends Record> per = ctx.selectFrom("""
assert s.count(old1) == 1 and s.count(old2) == 1
open(F, "w").write(s.replace(old1, new1).replace(old2, new2))
print("variant patch applied")
