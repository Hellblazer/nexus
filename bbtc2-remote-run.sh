#!/usr/bin/env bash
# run from service/; outputs under ../bbtc2-out/
set -u
O=../bbtc2-out; rm -rf $O; mkdir -p $O
SUB="HnswServingGucParityTest,TenantKeyedStateGateTest,SharedLexicalProbeTest,TextGate*,LexicalSharedProbe*,PgVectorSearchPerCollection*,Hybrid*,Vectors05*,Vectors06*,SchemaRollbackRoundTrip*,JooqRecordReflectionFeatureTest,RawSqlGateTest,SqlStringLiteralGateTest,VectorSensitivityRouteParityTest,PgVectorRepositoryContractTest,Rdr192*"
../scripts/mvnw-leased.sh -B test -Dtest="$SUB" -Dsurefire.failIfNoSpecifiedTests=false > $O/subset.log 2>&1
echo "subset exit=$?" >> $O/results.txt
mkdir -p $O/subset-reports; cp target/surefire-reports/*.txt $O/subset-reports/ 2>/dev/null
P=src/main/java/dev/nexus/service/vectors/SharedLexicalProbe.java
cp $P $O/SharedLexicalProbe.java.orig
sed -i 's/: new RankChashes(byCollection.getOrDefault(collection, new byte\[0\]\[\]));/: new RankChashes(java.util.Arrays.copyOf(byCollection.getOrDefault(collection, new byte[0][]), Math.min(1, byCollection.getOrDefault(collection, new byte[0][]).length)));/' $P
echo "falsA applied=$(grep -c 'Math.min(1, byCollection' $P)" >> $O/results.txt
../scripts/mvnw-leased.sh -B test -Dtest=LexicalSharedProbeIdentityIntegrationTest -Dsurefire.failIfNoSpecifiedTests=false > $O/falsA.log 2>&1
echo "falsA exit=$?" >> $O/results.txt
cp $O/SharedLexicalProbe.java.orig $P
sed -i 's/? new RankDense()/? new RankChashes(new byte[0][])/' $P
echo "falsB applied=$(grep -c 'new RankChashes(new byte\[0\]\[\])$' $P)" >> $O/results.txt
../scripts/mvnw-leased.sh -B test -Dtest=LexicalSharedProbeIdentityIntegrationTest -Dsurefire.failIfNoSpecifiedTests=false > $O/falsB.log 2>&1
echo "falsB exit=$?" >> $O/results.txt
cp $O/SharedLexicalProbe.java.orig $P
echo "restored diff=$(cmp -s $O/SharedLexicalProbe.java.orig $P && echo same || echo DIFFERENT)" >> $O/results.txt
