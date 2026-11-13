"""Mutation-safety tests; no Docker daemon, GPU, or real credentials are used."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import docker_clone as dc


class CloneSafety(unittest.TestCase):
    def test_original_name_is_never_a_mutation_target(self):
        with self.assertRaises(ValueError):
            dc.owned_name("dsv41-1017-exl3-tf")
        self.assertEqual(dc.owned_name("spark-hybrid-grammar-r0"), "spark-hybrid-grammar-r0")

    def test_stopping_requires_our_ownership_label(self):
        current = {"Config": {"Labels": {"owner": "somebody-else"}}}
        with patch.object(dc, "inspect", return_value=current), patch.object(dc, "docker") as docker:
            with self.assertRaises(RuntimeError):
                dc.stop("spark-hybrid-grammar-r0")
            docker.assert_not_called()

    def test_removal_never_forces_a_running_container(self):
        current = {"Config": {"Labels": {"owner": dc.OWNER}}, "State": {"Running": True}}
        with patch.object(dc, "inspect", return_value=current), patch.object(dc, "docker") as docker:
            with self.assertRaises(RuntimeError):
                dc.remove("spark-hybrid-grammar-r0")
            docker.assert_not_called()

    def test_clone_preserves_private_settings_without_logging_them(self):
        baseline = {"Config": {"Image": "original", "Env": ["HF_TOKEN=synthetic-test-secret", "UNCHANGED=7"],
                               "Cmd": ["tensorfold-native", "serve", "checkpoint"], "Hostname": "old-id", "Domainname": "",
                               "Labels": None},
                    "HostConfig": {"Binds": ["existing-cache:/hf:ro"], "NetworkMode": "host", "IpcMode": "host",
                                   "RestartPolicy": {"Name": "always"}, "AutoRemove": True, "ContainerIDFile": "old.cid"}}
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(json.dumps([baseline]))
            with patch.object(dc, "inspect", return_value=None), patch.object(dc, "docker", side_effect=[{"Id": "new-id"}, {}]) as docker:
                self.assertEqual(dc.start(snapshot, "spark-hybrid-grammar-r0", "new-image", True), "new-id")
                payload = docker.call_args_list[0].args[2]
        self.assertEqual(payload["Env"], baseline["Config"]["Env"] + ["TF_HYBRID_GRAMMAR=1"])
        self.assertEqual(payload["Cmd"], baseline["Config"]["Cmd"])
        self.assertEqual(payload["HostConfig"]["Binds"], baseline["HostConfig"]["Binds"])
        self.assertEqual(payload["HostConfig"]["RestartPolicy"]["Name"], "no")
        self.assertFalse(payload["HostConfig"]["AutoRemove"])
        self.assertNotIn("Hostname", payload)
        self.assertNotIn("ContainerIDFile", payload["HostConfig"])
        self.assertEqual(payload["Labels"]["owner"], dc.OWNER)
        self.assertEqual(baseline["HostConfig"]["RestartPolicy"]["Name"], "always")

    def test_gid_selection_tracks_live_slots_and_rejects_missing_roce(self):
        for valid in (True, False):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                port = root / "ib/hca0/ports/1"
                (port / "gids").mkdir(parents=True)
                (port / "gid_attrs/types").mkdir(parents=True)
                (port / "gids/3").write_text("0000:0000:0000:0000:0000:0000:0000:0000")
                (port / "gid_attrs/types/3").write_text("RoCE v2")
                if valid:
                    (port / "gids/4").write_text("0000:0000:0000:0000:0000:ffff:0a64:2802")
                    (port / "gid_attrs/types/4").write_text("RoCE v2")
                snapshot = root / "snapshot.json"
                snapshot.write_text(json.dumps({"Config": {"Env": ["NCCL_IB_HCA=hca0", "NCCL_IB_GID_INDEX=3", "TF_RDMA_GID_INDEX=3"]}, "HostConfig": {}}))
                def mapped_path(value):
                    return root / "ib" if str(value) == "/sys/class/infiniband" else Path(value)
                with patch.object(dc, "Path", side_effect=mapped_path), patch.object(dc, "inspect", return_value=None), patch.object(dc, "docker", side_effect=[{"Id": "new"}, {}]) as docker:
                    if valid:
                        dc.start(snapshot, "spark-hybrid-grammar-r0", "image", True)
                        env = docker.call_args_list[0].args[2]["Env"]
                        self.assertIn("NCCL_IB_GID_INDEX=4", env)
                        self.assertIn("TF_RDMA_GID_INDEX=4", env)
                    else:
                        with self.assertRaises(RuntimeError):
                            dc.start(snapshot, "spark-hybrid-grammar-r0", "image", True)
                        docker.assert_not_called()

    def test_daemon_errors_do_not_echo_private_request_details(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 400
        response.read.return_value = b'{"message":"synthetic-test-secret"}'
        with patch.object(dc, "UnixHTTP", return_value=connection):
            with self.assertRaises(RuntimeError) as caught:
                dc.docker("POST", "/containers/create", {"Env": ["HF_TOKEN=synthetic-test-secret"]})
        self.assertNotIn("synthetic-test-secret", str(caught.exception))
        connection.close.assert_called_once()

    def test_already_stopped_is_a_successful_idempotent_stop(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 304
        response.read.return_value = b""
        with patch.object(dc, "UnixHTTP", return_value=connection):
            self.assertEqual(dc.docker("POST", "/containers/spark-hybrid-grammar-r0/stop?t=25"), {})


if __name__ == "__main__":
    unittest.main()
