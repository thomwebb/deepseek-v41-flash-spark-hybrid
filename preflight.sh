#!/bin/bash
# CPU-only preparation; the original service stays up throughout.
set -euo pipefail
root=$HOME/spark-hybrid-20261031
old=$HOME/spark-ab-20261031
worker=${SPARK_WORKER:-10.100.40.1}  # no user: ssh uses whoever runs the recipe
cd "$root/recipe"
"$old/dsv41-token-map" --tokenizer "$root/model/tokenizer.json" --config "$root/model/config.json" --output "$root/assets/engram-token-map.bin"
rsync -a "$root/assets/engram-token-map.bin" "$worker:$root/assets/engram-token-map.bin"
# Transfer/checksum work is independent of the shape-only AOT enumeration.
(
  rsync -a --partial --no-owner --no-group "$root/model/" "$worker:$root/model/"
  ssh -o BatchMode=yes "$worker" "cd '$root/model' && sha256sum -c SHA256SUMS" > "$root/logs/worker-checksums.log" 2>&1
  echo MODEL_TRANSFER_VERIFIED > "$root/results/model-transfer-ready.txt"
) > "$root/logs/model-transfer.log" 2>&1 &
transfer=$!
(cd "$root/model" && sha256sum -c SHA256SUMS) > "$root/logs/head-checksums.log" 2>&1 &
checksum=$!
(ssh -o BatchMode=yes "$worker" "cd '$root/recipe'; scripts/run-zig.sh 1 --check-aot; echo \$? > '$root/state/aot-enumerate.exit'") > "$root/logs/aot-r1.log" 2>&1 &
worker_enum=$!
set +e
scripts/run-zig.sh 0 --check-aot > "$root/logs/aot-r0.log" 2>&1
head_status=$?
wait "$worker_enum"
worker_status=$?
set -e
echo "Enumeration command status head=$head_status worker=$worker_status"
test -s "$root/state/aot-needs-r0.json"
scp "$worker:$root/state/aot-needs-r1.json" "$root/state/aot-needs-r1.json"
for rank in 0 1; do
  if ! python3 scripts/check_native_assets.py --needs "$root/state/aot-needs-r$rank.json" --aot "$root/assets/aot"; then
    docker run --rm --name "spark-hybrid-aot-r$rank" --runtime=runc --memory=1536m --memory-swap=1536m --cpus=2 \
      -e NVIDIA_VISIBLE_DEVICES=void -e PY_SOURCE=/stage/recipe/build/reference/src \
      -e PYTHONPATH=/stage/recipe/build/reference/src -e OMP_NUM_THREADS=2 \
      -e TRITON_PTXAS_PATH=/usr/local/cuda-13.3/bin/ptxas \
      -e TRITON_PTXAS_BLACKWELL_PATH=/usr/local/cuda-13.3/bin/ptxas \
      -e TRITON_CACHE_DIR=/stage/triton-cache -v "$root:/stage" \
      -w /stage/recipe/engine/zig tensorfold-dsv41:rollback-20261031 \
      python3 tools/zig/triton_fill.py compile --py /stage/recipe/build/reference/src \
      --needs "/stage/state/aot-needs-r$rank.json" --options tools/zig/dsv41_triton_options.json \
      --arch 121 --have /stage/assets/aot --out /stage/assets/aot
  fi
  python3 scripts/check_native_assets.py --needs "$root/state/aot-needs-r$rank.json" --aot "$root/assets/aot"
done
rsync -a "$root/assets/" "$worker:$root/assets/"
wait "$transfer"
wait "$checksum"
ssh -o BatchMode=yes "$worker" "cd '$root/recipe'; python3 scripts/check_native_assets.py --needs '$root/state/aot-needs-r1.json' --aot '$root/assets/aot'"
head_identity=$(scripts/run-zig.sh 0 --identity)
worker_identity=$(ssh -o BatchMode=yes "$worker" "cd '$root/recipe'; scripts/run-zig.sh 1 --identity")
printf '%s\n' "head=$head_identity" "worker=$worker_identity" > "$root/results/identities.txt"
[[ "$head_identity" == "$worker_identity" ]]
printf 'HYBRID_STAGED_AND_VERIFIED\n' | tee "$root/results/staging-ready.txt"
