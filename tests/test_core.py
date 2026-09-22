import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from nomiarch.core.http import ApiError
from nomiarch.core.server import application
from nomiarch.core.store import Store
from nomiarch.runtime.broker import application as broker_application
from nomiarch.runtime.broker import evaluate


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.tokens = {k: k * 40 for k in ("operator", "worker", "broker")}
        self.app = application(self.store, self.tokens, "development-deterministic")

    def auth(self, role):
        return {"Authorization": "Bearer " + self.tokens[role]}

    def test_worker_cannot_read_operator_evidence_or_submit_tasks(self):
        for method, path, body in [("GET", "/v1/evidence", {}), ("POST", "/v1/tasks", {"config": {}})]:
            with self.assertRaises(ApiError) as e:
                self.app(method, path, self.auth("worker"), body)
            self.assertEqual(e.exception.code, 401)

    def test_operator_cannot_impersonate_worker(self):
        with self.assertRaises(ApiError):
            self.app("POST", "/internal/worker/claim", self.auth("operator"), {})

    def test_idempotency_rejects_different_input(self):
        first = self.store.submit({"https_only": True}, "request-1")
        self.assertEqual(first, self.store.submit({"https_only": True}, "request-1"))
        with self.assertRaises(ValueError):
            self.store.submit({"https_only": False}, "request-1")

    def test_expired_worker_cannot_overwrite_retried_task(self):
        task = self.store.submit({})
        old = self.store.claim()
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET until=0 WHERE id=?", (task,))
        new = self.store.claim()
        with self.assertRaises(ValueError):
            self.store.complete(task, old["lease"], {}, True)
        self.store.complete(task, new["lease"], {"ok": True}, True)
        self.assertEqual(self.store.task(task)["status"], "completed")

    def test_queue_and_evidence_survive_store_restart(self):
        task = self.store.submit({"public_access": True})
        restarted = Store(self.temp.name)
        self.assertEqual(restarted.claim()["id"], task)
        previous = "0" * 64
        for event in restarted.evidence()["events"]:
            self.assertEqual(event["previous"], previous)
            self.assertEqual(event["hash"], hashlib.sha256(previous.encode() + event["payload"].encode()).hexdigest())
            previous = event["hash"]

    def test_configuration_values_are_not_coerced_to_safe_booleans(self):
        result = evaluate({"public_access": "false", "https_only": 1})
        self.assertEqual([f["status"] for f in result["findings"]], ["fail", "fail", "unknown"])

    def test_gateway_fails_closed_if_opa_or_evidence_is_down(self):
        settings = {"core": "http://core:8787", "worker_token": "worker", "broker_token": "broker",
                    "policy_mode": "opa", "model_mode": "development", "opa": "http://policy:8181"}
        app = broker_application(settings)
        for response in [OSError("OPA down"), {"result": True}]:
            with patch("nomiarch.runtime.broker.request", side_effect=[response, OSError("audit down")]), \
                 patch("nomiarch.runtime.broker.evaluate") as tool:
                with self.assertRaises(OSError):
                    app("POST", "/v1/tools", {"Authorization": "Bearer worker"}, {"tool": "config.evaluate", "input": {}})
                tool.assert_not_called()

    def test_undefined_policy_result_denies_and_records_denial(self):
        settings = {"core": "http://core:8787", "worker_token": "worker", "broker_token": "broker",
                    "policy_mode": "opa", "model_mode": "development", "opa": "http://policy:8181"}
        with patch("nomiarch.runtime.broker.request", side_effect=[{}, {"recorded": True}]) as calls:
            with self.assertRaises(ApiError) as e:
                broker_application(settings)("POST", "/v1/tools", {"Authorization": "Bearer worker"}, {"tool": "config.evaluate", "input": {}})
            self.assertEqual(e.exception.code, 403)
            self.assertEqual(calls.call_args.kwargs["data"]["kind"], "tool.denied")


if __name__ == "__main__":
    unittest.main()
