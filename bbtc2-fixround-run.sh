#!/usr/bin/env bash
# run from service/ ; $1 = subset | falsify ; outputs under ../bbtc2-out-fr/
set -u
O=../bbtc2-out-fr; mkdir -p $O
SUB="HnswServingGucParityTest,TenantKeyedStateGateTest,SharedLexicalProbeTest,LexicalGateProbeTest,TextGate*,LexicalSharedProbe*,PgVectorSearchPerCollection*,Hybrid*,Vectors05*,Vectors06*,SchemaRollbackRoundTrip*,JooqRecordReflectionFeatureTest,RawSqlGateTest,SqlStringLiteralGateTest,VectorSensitivityRouteParityTest,PgVectorRepositoryContractTest,Rdr192*"
mvn_() { ../scripts/mvnw-leased.sh -B test -Dtest="$1" -Dsurefire.failIfNoSpecifiedTests=false; }
if [ "$1" = subset ]; then
  rm -rf $O/subset*; 
  mvn_ "$SUB" > $O/subset.log 2>&1
  echo "subset exit=$?" >> $O/results.txt
  mkdir -p $O/subset-reports; cp target/surefire-reports/*.txt $O/subset-reports/ 2>/dev/null
  exit 0
fi
BYC=TextGateProbeByCollectionIntegrationTest
IDN=LexicalSharedProbeIdentityIntegrationTest
UNIT=LexicalGateProbeTest
run_f() { # name tests
  python3 ../bbtc2-fixround-falsify.py apply $1 $O >> $O/results.txt
  mvn_ "$2" > $O/$1.log 2>&1
  echo "$1 exit=$? $(grep -E '^\[(ERROR|WARNING)\] Tests run:.*Fail' $O/$1.log | tail -1)" >> $O/results.txt
  grep -E "^\[ERROR\]   [A-Za-z]" $O/$1.log | sed 's/^/    /' | head -12 >> $O/results.txt
  python3 ../bbtc2-fixround-falsify.py restore $1 $O >> $O/results.txt
}
if [ "$1" = f1 ]; then run_f F1 HnswServingGucParityTest; echo "falsify done" >> $O/results.txt; exit 0; fi
run_f F1 HnswServingGucParityTest
run_f F7 $UNIT
run_f F8 $UNIT
run_f F3 "$UNIT,$IDN"
run_f F2 $BYC
run_f F4 "$BYC,$IDN"
run_f F5 "$BYC,TextGateProbeSingleRoleGuardIntegrationTest#migratedAsTheServiceRole*"
run_f F6 $BYC
run_f F9 $BYC
echo "falsify done" >> $O/results.txt
