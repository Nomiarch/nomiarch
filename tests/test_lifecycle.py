import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nomiarch.bootstrap.config import validate
from nomiarch.bootstrap.controller import apply, verify_worker, reap
from nomiarch.bootstrap.providers import Azure, Local, Existing
from nomiarch.common import NomiarchError, Runner, read_json, write_json


AZURE = {"api_version": "nomiarch.io/v1alpha1", "name": "test-lab", "target": "azure", "architecture": "amd64",
         "capacity": {"cpus": 2, "memory_gib": 8, "disk_gib": 40},
         "lifecycle": {"destroy_after": True, "max_runtime_minutes": 30},
         "azure": {"subscription_id": "00000000-0000-0000-0000-000000000000", "location": "canadacentral",
                   "vm_size": "Standard_B2ms", "ssh_user": "nomiarch", "ssh_key": "/private/key", "ssh_public_key": "/private/key.pub",
                   "admin_cidr": "10.0.0.0/24", "image": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "24.04.202609010"}}}


class FakeProvider:
    def __init__(self, *args):
        self.run = args[1]
        self.destroyed = False
        self.failure = None

    def prepare(self):
        pass

    def provision(self):
        if self.failure:
            raise self.failure
        return {"host": "10.88.1.4"}

    def verify(self):
        return {"vm": "running"}

    def destroy(self):
        self.destroyed = True
        return {"status": "deleted"}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def execute(self, failure=None, destroy_failure=False, retain=False):
        calls = []
        def factory(*args):
            provider = FakeProvider(*args)
            provider.failure = failure
            if destroy_failure:
                def failed_destroy():
                    raise NomiarchError("resource lock")
                provider.destroy = failed_destroy
            calls.append(provider)
            return provider
        config = copy.deepcopy(AZURE)
        config["lifecycle"]["destroy_after"] = not retain
        with patch("nomiarch.bootstrap.controller.files_preflight"):
            result = apply(config, self.temp.name, "foundation", factory=factory)
        return result, calls

    def test_destroy_runs_after_success(self):
        result, calls = self.execute()
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(result["cleanup"]["status"], "deleted")
        self.assertTrue(calls[-1].destroyed)
        self.assertTrue((Path(result["run_directory"]) / "report.json").exists())

    def test_failed_partial_apply_and_cancellation_still_cleanup(self):
        for failure in (RuntimeError("partial create"), KeyboardInterrupt(), NomiarchError("deadline expired")):
            with self.subTest(failure=type(failure).__name__):
                result, calls = self.execute(failure)
                self.assertEqual(result["validation"]["status"], "failed")
                self.assertEqual(result["cleanup"]["status"], "deleted")
                self.assertTrue(calls[-1].destroyed)

    def test_cleanup_failure_does_not_disguise_validated_result(self):
        result, _ = self.execute(destroy_failure=True)
        self.assertEqual(result["validation"]["status"], "passed")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertIn("retry", result["cleanup"])

    def test_retained_install_is_not_deleted(self):
        result, calls = self.execute(retain=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].destroyed)
        self.assertEqual(result["cleanup"]["status"], "not-requested")

    def test_unknown_foreign_resource_blocks_whole_group_delete(self):
        run = {"id": "a" * 32, "prefix": "nomiarch-" + "a" * 16}
        provider = Azure(copy.deepcopy(AZURE), run, self.temp.name, Runner(), ".")
        group = {"id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/" + run["prefix"] + "-rg",
                 "tags": {"nomiarch-run": run["id"]}}
        with self.assertRaises(NomiarchError):
            provider.assert_cleanup_scope(group, [{"id": group["id"] + "/providers/Microsoft.Storage/storageAccounts/foreign", "tags": {"nomiarch-run": run["id"]}}])

    def test_matching_name_in_wrong_subscription_is_not_owned(self):
        run = {"id": "a" * 32, "prefix": "nomiarch-" + "a" * 16}
        provider = Azure(copy.deepcopy(AZURE), run, self.temp.name, Runner(), ".")
        with self.assertRaises(NomiarchError):
            provider.assert_cleanup_scope({"id": "/subscriptions/WRONG/resourceGroups/" + run["prefix"] + "-rg", "tags": {"nomiarch-run": run["id"]}}, [])

    def test_existing_host_destroy_after_is_rejected(self):
        config = {k: v for k, v in copy.deepcopy(AZURE).items() if k != "azure"}
        config["target"] = "existing"
        config["existing"] = {"host": "host.example", "ssh_user": "ubuntu", "ssh_key": "/private/key", "known_hosts": "/known-hosts"}
        with self.assertRaises(NomiarchError):
            validate(config)

    def test_same_machine_or_stale_cleanup_worker_is_rejected(self):
        config = copy.deepcopy(AZURE)
        config["lifecycle"]["cleanup_worker_id"] = "worker"
        import socket, time
        for host, timestamp in [(socket.gethostname(), time.time()), ("other-host", 1)]:
            write_json(Path(self.temp.name) / "workers/worker.json", {"host": host, "time": timestamp})
            with self.assertRaises(NomiarchError):
                verify_worker(self.temp.name, config)

    def test_reaper_recovers_expired_controller_run(self):
        result, _ = self.execute(retain=True)
        path = Path(result["run_directory"]) / "run.json"
        run = read_json(path)
        run["destroy_after"], run["expires_at"] = True, 0
        write_json(path, run)
        with patch("nomiarch.bootstrap.controller.cleanup") as cleanup:
            cleanup.side_effect = lambda directory, run, repo: dict(run, cleanup={"status": "deleted"})
            reap(self.temp.name, "test-worker", False, ".")
            cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
