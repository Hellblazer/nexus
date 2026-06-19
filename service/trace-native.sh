#!/usr/bin/env bash
# nexus-lp2qo spike: native-image tracing-agent run. Boots the shaded fat jar on
# GraalVM 25.0.3 under native-image-agent, drives a broad workload across every
# T2/T1 repo (migration + jOOQ CRUD + reads) plus embed init, then SIGTERMs so the
# agent flushes traced metadata into META-INF/native-image/traced/.
set -u
cd "$(dirname "$0")"
JH="$HOME/.sdkman/candidates/java/25.0.3-graal"
JAR=target/nexus-service-1.0-SNAPSHOT.jar
TRACE_DIR=src/main/resources/META-INF/native-image/traced
mkdir -p "$TRACE_DIR"

PGPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
SVCPORT=$(python3 -c "import socket;s=socket.socket();s.bind(('',0));print(s.getsockname()[1]);s.close()")
echo "PGPORT=$PGPORT SVCPORT=$SVCPORT TRACE_DIR=$TRACE_DIR"
docker rm -f lp2qo-pg >/dev/null 2>&1 || true
docker run -d --name lp2qo-pg -e POSTGRES_DB=nexus -e POSTGRES_USER=nexus -e POSTGRES_PASSWORD=nexus -p ${PGPORT}:5432 pgvector/pgvector:pg17 >/dev/null
until docker exec lp2qo-pg pg_isready -U nexus >/dev/null 2>&1; do sleep 1; done; sleep 2; echo "PG ready"

export NX_DB_URL="jdbc:postgresql://localhost:${PGPORT}/nexus"
export NX_DB_USER=nexus NX_DB_PASS=nexus
export NX_SERVICE_PORT=$SVCPORT NX_SERVICE_TOKEN=spiketoken NX_EMBED_MODE=onnx

"$JH/bin/java" \
  -agentlib:native-image-agent=config-merge-dir="$TRACE_DIR",experimental-class-define-support \
  --enable-preview --enable-native-access=ALL-UNNAMED \
  -jar "$JAR" > /tmp/lp2qo-trace-svc.log 2>&1 &
SVCPID=$!
echo "svc pid $SVCPID (agent tracing)"

UP=0
for i in $(seq 1 90); do
  if ! kill -0 $SVCPID 2>/dev/null; then echo "SERVICE EXITED EARLY at ${i}s"; tail -30 /tmp/lp2qo-trace-svc.log; break; fi
  if curl -fsS "http://localhost:${SVCPORT}/health" >/dev/null 2>&1; then echo "HEALTH OK after ${i}s"; UP=1; break; fi
  sleep 1
done

if [ "$UP" = "1" ]; then
  B=(-s -o /dev/null -w "%{http_code} ")
  AUTH=(-H "Authorization: Bearer spiketoken")
  J=(-H "Content-Type: application/json")
  U="http://localhost:${SVCPORT}"
  echo "--- driving workload ---"
  echo -n "health/version: "; curl "${B[@]}" "$U/health"; curl "${B[@]}" "${AUTH[@]}" "$U/version"; echo
  echo -n "memory: "
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST -d '{"project":"spike","title":"t1","content":"native jooq","tags":"a,b","ttl":30}' "$U/v1/memory/put"
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST -d '{"project":"spike","title":"t2","content":"second","tags":"c","ttl":30}' "$U/v1/memory/put"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/get?project=spike&title=t1"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/search?query=native&project=spike"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/list?project=spike"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/all?project=spike"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/projects"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/memory/search_by_tag?tag=a&project=spike"
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST -d '{"keepId":1,"deleteIds":[],"mergedContent":"m"}' "$U/v1/memory/merge"
  curl "${B[@]}" "${AUTH[@]}" -X DELETE "$U/v1/memory/delete?project=spike&title=t2"; echo
  echo -n "scratch(t1): "
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST -d '{"content":"scratch entry","tags":"x"}' "$U/v1/t1/put"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/t1/list"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/t1/search?query=scratch"; echo
  echo -n "plans: "
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST -d '{"query":"q","planJson":"{}","project":"spike","outcome":"success","tags":"t"}' "$U/v1/plans/save"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/plans/list?project=spike"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/plans/list_active?project=spike"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/plans/search?query=q&project=spike"; echo
  echo -n "taxonomy: "
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/taxonomy/topics?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/taxonomy/top_topics?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/taxonomy/projection_counts?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/taxonomy/hubs?collection=knowledge__x"; echo
  echo -n "aspects: "
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/aspects/list_by_collection?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/aspects/get_by_doc_id?doc_id=d1&collection=knowledge__x"; echo
  echo -n "chash: "
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/chash/distinct_collections"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/chash/is_empty?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/chash/count_for_collection?collection=knowledge__x"
  curl "${B[@]}" "${AUTH[@]}" "$U/v1/chash/lookup?collection=knowledge__x&chash=abc"; echo
  # LOCAL bge-768 EMBED (nexus-pqatt): the encode path drives the DJL HuggingFace
  # tokenizers JNI (libtokenizers.so -> FindClass+NewObject on CharSpan) and the
  # onnxruntime session run (OnnxTensor/TensorInfo JNI ctors). Without this call
  # the agent never observes those jniAccessible registrations, the native image
  # omits them, and the first embed SIGABRTs at lib.rs:475 (Result::unwrap on a
  # JavaException). This boots in onnx-local mode (no NX_VOYAGE_API_KEY), so the
  # injected Bge768Embedder serves /v1/vectors/embed.
  echo -n "embed(bge-768): "
  curl "${B[@]}" "${AUTH[@]}" "${J[@]}" -X POST \
    -d '{"collection":"knowledge__x","texts":["native-image embed trace","second sentence for a multi-row batch"]}' \
    "$U/v1/vectors/embed"; echo
  echo "--- workload done ---"
else
  echo "SERVICE NEVER CAME UP — agent config may be incomplete"
fi

echo "--- SIGTERM for agent flush ---"
kill -TERM $SVCPID 2>/dev/null
for i in $(seq 1 20); do kill -0 $SVCPID 2>/dev/null || { echo "exited after ${i}s"; break; }; sleep 1; done
kill -9 $SVCPID 2>/dev/null || true
docker rm -f lp2qo-pg >/dev/null 2>&1 || true
echo "=== traced config files ==="
ls -la "$TRACE_DIR" 2>/dev/null
echo "DONE"
