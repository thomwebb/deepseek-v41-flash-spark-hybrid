#!/bin/sh
# Stop only this prototype, cancel only its own dashboard run, and restore the untouched unit.
set -u
umask 077
ROOT="$HOME/spark-hybrid-20261031"
WORKER="${SPARK_WORKER:-10.100.40.1}"  # no user: ssh uses whoever runs the recipe
mkdir -p "$ROOT/logs"
# Also works after promotion: prevent the new supervisor from racing the old one on reboot.
systemctl --user disable --now dsv41-hybrid.service >/dev/null 2>&1 || true
systemctl --user enable dsv41.service >/dev/null 2>&1 || true
python3 - "$ROOT" <<'PY'
import json,os,sys,urllib.request
from pathlib import Path
path=Path(sys.argv[1])/"grammar-qualification/results/sparkdash-candidate-start.json"
base=os.environ.get("SPARKDASH_QUALITY_URL","").rstrip("/")+"/"
if path.exists() and base!="/":
    try:
        job_id=json.loads(path.read_text())["benchId"]
        with urllib.request.urlopen(base+job_id,timeout=10) as response:
            job=json.load(response)
        if job.get("status")=="running":
            request=urllib.request.Request(base+job_id,method="DELETE")
            with urllib.request.urlopen(request,timeout=10) as response:
                json.load(response)
            print("Cancelled prototype quality run",job_id)
    except Exception as error:
        print("Quality cancellation unavailable:",type(error).__name__)
PY
ARCHIVE="$ROOT/logs/grammar-rollback-$(date +%s)"
mkdir -p "$ARCHIVE"
docker logs spark-hybrid-grammar-r0 > "$ARCHIVE/rank0.log" 2>&1 || true
timeout 40 ssh -o BatchMode=yes -o ConnectTimeout=10 "$WORKER" \
    'docker logs spark-hybrid-grammar-r1' > "$ARCHIVE/rank1.log" 2>&1 || true
docker stop -t 25 spark-hybrid-grammar-r0 >/dev/null 2>&1 || true
timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=10 "$WORKER" \
    'docker stop -t 25 spark-hybrid-grammar-r1 >/dev/null 2>&1 || true' || true
bash "$HOME/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh" stop "$ROOT/grammar-memguard.pid" || true
timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=10 "$WORKER" \
    "bash ~/.cache/dsv41-tensorfold/memguard.sh stop '$ROOT/grammar-memguard.pid'" || true
sh "$ROOT/rollback.sh"
