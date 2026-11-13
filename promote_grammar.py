"""Promote only fully qualified artifacts; verify durable supervision and automatic pair recovery."""
import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
import docker_clone as dc
from grammar_runtime import HEAD, worker_action
from runtime import ROOT, benchmark, compare, health, run, write_json
from verify_grammar import STAGE, load_qualified


def interrupted(signum, frame):
    raise RuntimeError(f"Promotion interrupted by signal {signum}")


def unit(field):
    return run(["systemctl", "--user", "show", "dsv41-hybrid.service", "--property", field, "--value"]).stdout.strip()


def require_linger():
    """An enabled user unit does not run at boot unless linger is on."""
    user = run(["id", "-un"]).stdout.strip()
    state = run(["loginctl", "show-user", user, "--property=Linger", "--value"]).stdout.strip()
    if state != "yes":
        raise RuntimeError(f"User linger is '{state}' for {user}; run install-supervisor.sh first, "
                           "otherwise the promoted unit never starts after a reboot")
    return {"user": user, "linger": state}


def wait_supervised(manifest, after, previous_pid=None, seconds=600):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            pid = int(unit("MainPID"))
            state = json.loads((ROOT / "results/grammar-service-state.json").read_text())
            current = dc.inspect(HEAD)
            snapshot = health()
            if (pid and pid != previous_pid and state.get("pid") == pid and state.get("started", 0) >= after
                    and current and current["State"]["Running"] and current["Image"] == manifest["image_id"]
                    and snapshot.get("status") == "ok" and not snapshot.get("warming")):
                return {"pid": pid, "container_id": current["Id"], "health": snapshot}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(3)
    raise RuntimeError("Supervised hybrid did not become healthy before its deadline")


def main():
    os.umask(0o077)
    lock = (ROOT / "promotion.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # The qualification controller must have completed and released ownership before promotion.
    controller = (ROOT / "controller.lock").open("a+")
    fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(controller, fcntl.LOCK_UN)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    manifest, qualification, quality = load_qualified()
    started = time.time()
    directory = ROOT / "results" / f"grammar-promotion-{int(started)}"
    directory.mkdir(parents=True)
    done = directory / "promotion-complete.txt"
    report = {"started": started, "promoted": False, "image": manifest["image"],
              "image_id": manifest["image_id"], "quality_bench_id": quality["candidate_id"]}
    guardian, maintenance = None, False
    try:
        run(["sha256sum", "-c", ROOT / "rollback/config-sha256.txt"])
        if health().get("live", {}).get("connections", 0):
            raise RuntimeError("Live traffic detected; refusing deployment maintenance")
        report["linger"] = require_linger()
        installed = Path.home() / ".config/systemd/user/dsv41-hybrid.service"
        installed.parent.mkdir(parents=True, exist_ok=True)
        staged = ROOT / "dsv41-hybrid.service"
        if installed.exists() and installed.read_bytes() != staged.read_bytes():
            shutil.copy2(installed, directory / "previous-hybrid.service")
        shutil.copy2(staged, installed)
        run(["systemd-analyze", "--user", "verify", installed])
        run(["systemctl", "--user", "daemon-reload"])
        guardian = subprocess.Popen(["sh", "-c", f"sleep 3600; test -f '{done}' || sh '{ROOT}/grammar_rollback.sh'"],
                                    stdout=(directory / "guardian.log").open("a"), stderr=subprocess.STDOUT,
                                    start_new_session=True)
        maintenance = True
        run(["systemctl", "--user", "disable", "--now", "dsv41.service"], 180)
        run(["systemctl", "--user", "reset-failed", "dsv41.service"], check=False)
        run(["systemctl", "--user", "enable", "--now", "dsv41-hybrid.service"], 180)
        report["initial_ready"] = wait_supervised(manifest, started)
        run(["python3", ROOT / "extended_contract.py", directory / "extended-contract.json"], 300,
            log=directory / "extended-contract.log")
        report["extended_contract"] = json.loads((directory / "extended-contract.json").read_text())
        baseline = json.loads((STAGE / "results/baseline.json").read_text())
        deployed = benchmark(directory, "supervised")
        report["initial_performance"] = compare(baseline, deployed)
        if report["initial_performance"]["reasons"]:
            raise RuntimeError("Supervised performance gate failed")
        run(["python3", ROOT / "mixed_soak.py", directory / "mixed-soak.json"], 900,
            log=directory / "mixed-soak.log")
        report["mixed_soak"] = json.loads((directory / "mixed-soak.json").read_text())
        run(["python3", ROOT / "session_probe.py", directory / "session", "--phase", "before"], 300,
            log=directory / "session-before.log")
        # Exercise an unexpected head exit, not merely systemctl restart. Restart=always must rebuild both ranks.
        before = report["initial_ready"]
        worker_before = json.loads(worker_action("status").stdout)
        restart_started = time.time()
        run(["docker", "stop", "-t", "20", HEAD], 60)
        after = wait_supervised(manifest, restart_started, previous_pid=before["pid"])
        if before["container_id"] == after["container_id"]:
            raise RuntimeError("Supervisor did not replace the exited head container")
        report["automatic_recovery"] = {"seconds": time.time() - restart_started,
                                        "old_pid": before["pid"], "new_pid": after["pid"],
                                        "new_container_id": after["container_id"]}
        worker_after = json.loads(worker_action("status").stdout)
        if not worker_after["state"]["Running"] or worker_before["container_id"] == worker_after["container_id"]:
            raise RuntimeError("Worker was not replaced and recovered with the head")
        if worker_after["image_id"] != manifest["provenance"]["base_image_id"]:
            raise RuntimeError("Recovered worker uses an unqualified image")
        report["worker_image_id"] = worker_after["image_id"]
        run(["python3", ROOT / "session_probe.py", directory / "session", "--phase", "after"], 300,
            log=directory / "session-after.log")
        post = benchmark(directory, "after-recovery")
        report["final_performance"] = compare(baseline, post)
        if report["final_performance"]["reasons"]:
            raise RuntimeError("Post-recovery performance gate failed")
        report["final_health"] = health()
        report["new_unit"] = {"active": run(["systemctl", "--user", "is-active", "dsv41-hybrid.service"]).stdout.strip(),
                              "enabled": run(["systemctl", "--user", "is-enabled", "dsv41-hybrid.service"]).stdout.strip()}
        report["original_unit"] = {"active": run(["systemctl", "--user", "is-active", "dsv41.service"], check=False).stdout.strip(),
                                   "enabled": run(["systemctl", "--user", "is-enabled", "dsv41.service"], check=False).stdout.strip()}
        if report["new_unit"] != {"active": "active", "enabled": "enabled"}:
            raise RuntimeError("New supervision is not durable")
        if report["original_unit"] != {"active": "inactive", "enabled": "disabled"}:
            raise RuntimeError("Original and hybrid supervisors could conflict on reboot")
        run(["sha256sum", "-c", ROOT / "rollback/config-sha256.txt"])
        report["promoted"] = True
        done.write_text("QUALIFIED_HYBRID_SERVING_UNDER_SYSTEMD\n")
    except Exception as error:
        report["error"] = str(error)
        if maintenance:
            recovered = run(["sh", ROOT / "grammar_rollback.sh"], 600, False, directory / "rollback.log")
            report["restored_original"] = recovered.returncode == 0
        print("PROMOTION_FAILED", str(error), flush=True)
    finally:
        if guardian and (report["promoted"] or report.get("restored_original") or not maintenance):
            try:
                os.killpg(guardian.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        report["finished"] = time.time()
        write_json(directory / "promotion.json", report)
        write_json(ROOT / "results/grammar-deployment.json", report)
        print(json.dumps({k: v for k, v in report.items() if k not in ("mixed_soak", "final_health")}), flush=True)


if __name__ == "__main__":
    main()
