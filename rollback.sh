#!/bin/sh
# Restore the original pinned image/config through its existing systemd supervisor.
set -eu
stage=$HOME/spark-hybrid-20261031
worker=${SPARK_WORKER:-10.100.40.1}  # no user: ssh uses whoever runs the recipe
candidate=spark-hybrid-native-mia
original=dsv41-1017-exl3-tf
mkdir -p "$stage/logs"
# Cancel only this trial's own SparkDash job before restoring the old endpoint.
python3 - <<'PY'
import json
import os
import urllib.request
from pathlib import Path
path=Path.home()/'spark-hybrid-20261031/results/sparkdash-candidate-start.json'
base=os.environ.get('SPARKDASH_QUALITY_URL','').rstrip('/')
if path.exists() and base:
    try:
        job=json.loads(path.read_text())
        url=base+'/'+job['benchId']
        with urllib.request.urlopen(url,timeout=10) as response:
            current=json.load(response)
        if current.get('status')=='running':
            with urllib.request.urlopen(urllib.request.Request(url,method='DELETE'),timeout=10) as response:
                json.load(response)
            print('Cancelled this trial SparkDash job')
    except Exception as exc:
        print('SparkDash cancel check:',str(exc))
PY
# New containers are --rm; preserve logs before stopping them.
if docker inspect "$candidate-r0" >/dev/null 2>&1; then
    docker logs "$candidate-r0" > "$stage/logs/candidate-r0.log" 2>&1 || true
    docker stop -t 25 "$candidate-r0" || true
fi
ssh -o BatchMode=yes -o ConnectTimeout=10 "$worker" '
    stage=$HOME/spark-hybrid-20261031
    if docker inspect spark-hybrid-native-mia-r1 >/dev/null 2>&1; then
        docker logs spark-hybrid-native-mia-r1 > "$stage/logs/candidate-r1.log" 2>&1 || true
        docker stop -t 25 spark-hybrid-native-mia-r1 || true
    fi
'
bash "$HOME/DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh" stop "$stage/candidate-memguard.pid" || true
ssh -o BatchMode=yes "$worker" "bash ~/.cache/dsv41-tensorfold/memguard.sh stop '$stage/candidate-memguard.pid'" || true
systemctl --user reset-failed dsv41.service || true
systemctl --user start dsv41.service
python3 - <<'PY'
import json
import time
import urllib.request
for attempt in range(180):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=4) as response:
            health = json.load(response)
        if health.get('status') == 'ok' and not health.get('warming'):
            print('ROLLBACK_HEALTH_OK', flush=True)
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit('Rollback did not become healthy within six minutes')
PY
