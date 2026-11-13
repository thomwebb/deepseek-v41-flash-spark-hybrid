"""Shared, bounded lifecycle for the two-node hybrid experiments."""
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

# Defaults reproduce the reference deployment; override for another pair.
ROOT = Path(os.environ.get("SPARK_HYBRID_ROOT", str(Path.home() / "spark-hybrid-20261031")))
RECIPE = ROOT / "recipe"
# No user in the default: ssh logs in as whoever runs the recipe. Set user@host to override.
WORKER = os.environ.get("SPARK_WORKER", "10.100.40.1")
PREFIX = "spark-hybrid-native-mia"
BASE = "http://127.0.0.1:8888"


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def run(args, timeout=120, check=True, log=None):
    print("RUN", " ".join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), timeout=timeout, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log:
        Path(log).write_text(result.stdout)
    if result.returncode and check:
        print(result.stdout[-10000:], flush=True)
        result.check_returncode()
    return result


def remote(script, timeout=120, check=True):
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", WORKER, script], timeout, check)


def health():
    with urllib.request.urlopen(BASE + "/health", timeout=5) as response:
        return json.load(response)


def wait_ready(seconds=1200):
    start = time.monotonic()
    while time.monotonic()-start < seconds:
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", PREFIX+"-r0"],
                               capture_output=True, text=True, timeout=10)
        if state.returncode or state.stdout.strip() != "true":
            raise RuntimeError("Hybrid head exited; inspect saved container log")
        try:
            snapshot = health()
            if snapshot.get("status") == "ok" and not snapshot.get("warming"):
                return {"startup_s": time.monotonic()-start, "health": snapshot}
        except Exception:
            pass
        time.sleep(3)
    raise RuntimeError("Hybrid readiness exceeded its bounded deadline")


def stop_candidate():
    run(["docker", "stop", "-t", "35", PREFIX+"-r0"], 60, False)
    remote("docker stop -t 35 " + PREFIX+"-r1", 60, False)
    run(["bash", Path.home()/"DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh",
         "stop", ROOT/"candidate-memguard.pid"], check=False)
    remote(f"bash ~/.cache/dsv41-tensorfold/memguard.sh stop '{ROOT}/candidate-memguard.pid'", check=False)


def start_candidate(name):
    log_dir = ROOT / "logs" / name
    log_dir.mkdir(exist_ok=True)
    remote(f"cd '{RECIPE}'; CONFIG=config/prod-zig.env scripts/run-zig.sh 1 --detach", 150)
    run(["bash", "scripts/run-zig.sh", "0", "--detach"], 150)
    followers = []
    for rank in (0, 1):
        output = (log_dir / f"rank{rank}.log").open("w")
        command = ["docker", "logs", "-f", PREFIX+f"-r{rank}"]
        if rank:
            command = ["ssh", "-o", "BatchMode=yes", WORKER, " ".join(command)]
        followers.append(subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT))
        output.close()
    run(["bash", Path.home()/"DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh",
         "start", PREFIX+"-r0", "3.5", log_dir/"memguard.log", ROOT/"candidate-memguard.pid", "2", "2.5"])
    remote(f"bash ~/.cache/dsv41-tensorfold/memguard.sh start {PREFIX}-r1 3.5 "
           f"'{ROOT}/logs/candidate-memguard.log' '{ROOT}/candidate-memguard.pid' 2 2.5")
    return followers


def configure(name, changes):
    changes = {**changes, "TF_DSV41_CALIB_DIR": "/state/calib-" + name}
    text = (RECIPE / "config/hybrid-fast.env").read_text()
    keys = set(changes)
    lines = [line for line in text.splitlines()
             if line.split("=", 1)[0].removeprefix("export ") not in keys]
    lines += ["", "# Controlled experimental overrides: " + name]
    lines += [f"{key}={value}" for key, value in changes.items()]
    # Fresh calibration per policy; prepared weight images remain pack-specific and reusable.
    destination = RECIPE / "config/prod-zig.env"
    destination.write_text("\n".join(lines)+"\n")
    worker_text = destination.read_text().replace("/engram-r0", "/engram-r1")
    temporary = ROOT / "worker-profile.env"
    temporary.write_text(worker_text)
    run(["scp", temporary, WORKER+":"+str(destination)])
    directory = ROOT / "results" / name
    directory.mkdir(exist_ok=True)
    (directory / "effective-profile.env").write_text(destination.read_text())


def benchmark(directory, name):
    path = directory / (name+".json")
    result = run(["python3", ROOT/"ab_benchmark.py", BASE, path], 310, False,
                 directory / (name+".log"))
    if not path.exists():
        raise RuntimeError("Benchmark failed without producing results: " + result.stdout[-2000:])
    return json.loads(path.read_text())


def compare(baseline, candidate, fraction=0.95):
    reasons, ratios = [], {}
    if baseline["manifest"] != candidate["manifest"]:
        reasons.append("Different benchmark manifests")
    after = {row["label"]: row for row in candidate["results"]}
    for before in baseline["results"]:
        if before["ok"] and not after.get(before["label"], {}).get("ok"):
            reasons.append("Previously passing case failed: " + before["label"])
    for kind in ("code", "prose"):
        key = "median_decode_tokens_per_second_approx"
        old, new = baseline["summary"][kind][key], candidate["summary"][kind][key]
        ratios[kind] = new/old if old and new else 0
    old = baseline["mixed_batch"].get("completion_tokens_per_second")
    new = candidate.get("mixed_batch", {}).get("completion_tokens_per_second")
    ratios["mixed"] = new/old if old and new else 0
    for kind, value in ratios.items():
        if value < fraction:
            reasons.append(f"{kind} below {fraction:.0%} baseline: {value:.3f}")
    old = baseline["summary"]["prefill"].get("median_ttft_s")
    new = candidate["summary"]["prefill"].get("median_ttft_s")
    if not old or not new or new > old*1.2:
        reasons.append("Prefill TTFT missing or >20% slower")
    if not candidate.get("health_after", {}).get("ok"):
        reasons.append("Benchmark ended unhealthy")
    return {"reasons": reasons, "ratios": ratios}
