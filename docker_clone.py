"""Clone private baseline Docker snapshots without exposing environment secrets in argv or logs."""
import copy
import http.client
import json
import re
import socket
from pathlib import Path
from urllib.parse import quote

OWNER = "code-puppy-7290b8"


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=120)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker(method, path, body=None, missing_ok=False):
    connection = UnixHTTP()
    try:
        data = None if body is None else json.dumps(body).encode()
        connection.request(method, path, body=data,
                           headers={} if data is None else {"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        if response.status == 404 and missing_ok:
            return None
        if response.status == 304 and method == "POST" and "/stop?" in path:
            return {}
        if not 200 <= response.status < 300:
            # Docker errors can contain request details. Never dump the private config or its environment.
            raise RuntimeError(f"Docker {method} {path.split('?')[0]} returned HTTP {response.status}")
        return json.loads(raw) if raw else {}
    finally:
        connection.close()


def owned_name(name):
    if not re.fullmatch(r"spark-hybrid-grammar-r[01]", name):
        raise ValueError("Refusing to manage a non-hybrid container")
    return quote(name, safe="")


def inspect(name):
    return docker("GET", f"/containers/{owned_name(name)}/json", missing_ok=True)


def assert_owned(current):
    if (current.get("Config", {}).get("Labels") or {}).get("owner") != OWNER:
        raise RuntimeError("Refusing to mutate a container without our ownership label")


def stop(name):
    current = inspect(name)
    if current is None:
        return None
    assert_owned(current)
    return docker("POST", f"/containers/{owned_name(name)}/stop?t=25", missing_ok=True)


def remove(name):
    # No force and no volume deletion. Only stopped, reproducible experiment containers may be removed.
    current = inspect(name)
    if current is None:
        return
    assert_owned(current)
    if current["State"]["Running"]:
        raise RuntimeError("Refusing to remove a running hybrid container")
    docker("DELETE", f"/containers/{owned_name(name)}")


def start(snapshot, name, image, grammar):
    encoded = owned_name(name)
    if inspect(name) is not None:
        raise RuntimeError("Hybrid container already exists; archive its logs before replacement")
    baseline = json.loads(Path(snapshot).read_text())
    if isinstance(baseline, list):
        baseline = baseline[0]
    config = copy.deepcopy(baseline["Config"])
    for key in ("Hostname", "Domainname"):
        config.pop(key, None)  # Let host networking inherit this host's identity, not an explicit hostname.
    config["Image"] = image
    # GID slots can move across network reconfiguration/reboots. Never reuse a
    # stale numeric slot from a saved container: require live IPv4 RoCE-v2 GIDs.
    env = dict(entry.split("=", 1) for entry in config.get("Env", []) if "=" in entry)
    hcas = env.get("NCCL_IB_HCA", "").lstrip("=").split(",")
    candidates = None
    for hca in filter(None, hcas):
        port = Path("/sys/class/infiniband") / hca.split(":")[0] / "ports/1"
        valid = set()
        for gid in (port / "gids").glob("*"):
            try:
                value = gid.read_text().strip()
                kind = (port / "gid_attrs/types" / gid.name).read_text().strip()
                if kind == "RoCE v2" and ":ffff:" in value and not value.endswith(":0000:0000"):
                    valid.add(int(gid.name))
            except OSError:
                continue
        candidates = valid if candidates is None else candidates & valid
    if hcas and hcas != [""]:
        if not candidates:
            raise RuntimeError("No common live IPv4 RoCE-v2 GID slot across configured HCAs")
        env["NCCL_IB_GID_INDEX"] = str(min(candidates))
        env["TF_RDMA_GID_INDEX"] = str(min(candidates))
    config["Env"] = [key + "=" + value for key, value in env.items()]
    config["Env"] = [entry for entry in config.get("Env", [])
                     if not entry.startswith("TF_HYBRID_GRAMMAR=")]
    config["Env"].append("TF_HYBRID_GRAMMAR=" + str(int(grammar)))
    config["Labels"] = dict(config.get("Labels") or {})
    config["Labels"].update({"owner": OWNER, "hybrid.variant": "grammar"})
    host = copy.deepcopy(baseline["HostConfig"])
    host["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    host["AutoRemove"] = False
    host.pop("ContainerIDFile", None)
    config["HostConfig"] = host
    created = docker("POST", "/containers/create?name=" + encoded, config)
    docker("POST", "/containers/" + created["Id"] + "/start")
    return created["Id"]


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "remove", "status"])
    parser.add_argument("name")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--grammar", action="store_true")
    args = parser.parse_args()
    if args.action == "start":
        if not args.snapshot or not args.image:
            parser.error("start requires --snapshot and --image")
        print(start(args.snapshot, args.name, args.image, args.grammar))
    elif args.action == "stop":
        stop(args.name)
    elif args.action == "remove":
        remove(args.name)
    else:
        state = inspect(args.name)
        print(json.dumps(None if state is None else {"container_id": state["Id"], "image_id": state["Image"], "state": state["State"]}))


if __name__ == "__main__":
    main()
