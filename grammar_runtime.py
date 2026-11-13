"""Lifecycle shared by the durable grammar supervisor and maintenance tooling."""
import json
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
import docker_clone as dc
from runtime import ROOT, WORKER, health, remote, run

HEAD = "spark-hybrid-grammar-r0"
PEER = "spark-hybrid-grammar-r1"
ORIGINAL_IMAGE = "tensorfold-dsv41:rollback-20261031"


def worker_action(action, *arguments):
    return remote(shlex.join(["python3", str(ROOT / "docker_clone.py"), action, PEER, *map(str, arguments)]))


def assert_original_stopped():
    old = dc.docker("GET", "/containers/dsv41-1017-exl3-tf/json", missing_ok=True)
    if old and old["State"]["Running"]:
        raise RuntimeError("Original head is still running")
    script = ('import docker_clone as d; s=d.docker("GET","/containers/dsv41-1017-exl3-tf/json",missing_ok=True); '
              'print("running" if s and s["State"]["Running"] else "stopped")')
    result = remote(f"cd {shlex.quote(str(ROOT))}; python3 -c {shlex.quote(script)}")
    if result.stdout.strip() != "stopped":
        raise RuntimeError("Original worker is still running")


def start_pair(manifest, logdir):
    assert_original_stopped()
    info = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Id}}", manifest["image"]]).stdout)
    if info != manifest["image_id"]:
        raise RuntimeError("Head image differs from its qualification manifest")
    dc.stop(HEAD)
    dc.remove(HEAD)
    worker_action("stop")
    worker_action("remove")
    worker_action("start", "--snapshot", ROOT / "rollback/worker-container.json", "--image", ORIGINAL_IMAGE)
    dc.start(ROOT / "rollback/head-container.json", HEAD, manifest["image"], True)
    logdir.mkdir(parents=True, exist_ok=True)
    remote(f"umask 077; mkdir -p {shlex.quote(str(logdir))}")
    run(["bash", Path.home() / "DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh", "start", HEAD,
         "3.5", logdir / "memguard-head.log", ROOT / "grammar-memguard.pid", "2", "2.5"])
    remote(f"bash ~/.cache/dsv41-tensorfold/memguard.sh start {PEER} 3.5 '{logdir}/memguard-worker.log' '{ROOT}/grammar-memguard.pid' 2 2.5")
    return {"head_image_id": dc.inspect(HEAD)["Image"], "source_commit": manifest["provenance"]["source_commit"]}


def ready(seconds=600):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        peer = worker_action("status")
        peer_state = json.loads(peer.stdout)
        if not peer_state or not peer_state.get("state", {}).get("Running"):
            raise RuntimeError("Hybrid worker exited during startup")
        state = dc.inspect(HEAD)
        if not state or not state["State"]["Running"]:
            raise RuntimeError("Hybrid head exited during startup")
        try:
            current = health()
            if current.get("status") == "ok" and not current.get("warming"):
                return {"startup_s": time.monotonic() - started, "health": current}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(3)
    raise RuntimeError("Hybrid startup exceeded its deadline")


def stop_pair():
    errors = []
    actions = [lambda: dc.stop(HEAD), lambda: worker_action("stop"),
               lambda: run(["bash", Path.home() / "DeepSeek-v4.1-Flash-1017-2x-DGX-Sparks/scripts/memguard.sh",
                            "stop", ROOT / "grammar-memguard.pid"]),
               lambda: remote(f"bash ~/.cache/dsv41-tensorfold/memguard.sh stop '{ROOT}/grammar-memguard.pid'")]
    for action in actions:
        try:
            action()
        except Exception as error:
            errors.append(type(error).__name__)
    if errors:
        raise RuntimeError("Hybrid cleanup needs attention: " + ", ".join(errors))


def follow_logs():
    commands = [["docker", "logs", "-f", HEAD],
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", WORKER, "docker logs -f " + PEER]]
    return [subprocess.Popen(command, start_new_session=True) for command in commands]


def stop_followers(followers):
    for process in followers:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
