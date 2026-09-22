import copy
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

from nomiarch.bootstrap import bundle, recovery
from nomiarch.bootstrap.manifests import render
from nomiarch.common import NomiarchError, Runner, digest, write_json
from nomiarch.core.store import Store


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.key = Path(cls.shared.name) / "key.pem"
        cls.public = Path(cls.shared.name) / "public.pem"
        bundle.keygen(cls.key, cls.public)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        for name in bundle.REQUIRED:
            (self.inputs / name).write_bytes(b"synthetic admission-test data")
        self.output = self.root / "release.tar"
        bundle.seal(self.inputs, self.output, self.key, {"architecture": "amd64", "k3s_version": "test",
                    "core_image": "nomiarch/core:" + "a" * 64, "model_image": "nomiarch/model:" + "b" * 64})

    def rewrite(self, operation):
        entries = []
        with tarfile.open(self.output) as archive:
            for entry in archive:
                entries.append((entry, archive.extractfile(entry).read()))
        with tarfile.open(self.output, "w") as archive:
            for entry, content in operation(entries):
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))

    def test_signed_complete_bundle_extracts(self):
        manifest = bundle.inspect(self.output, self.public, self.root / "verified")
        self.assertEqual(manifest["architecture"], "amd64")
        self.assertEqual((self.root / "verified/model.gguf").read_bytes(), b"synthetic admission-test data")

    def test_tampered_payload_rejected_before_any_extraction(self):
        self.rewrite(lambda entries: [(e, b"tampered" if e.name == "model.gguf" else c) for e, c in entries])
        with self.assertRaises(NomiarchError):
            bundle.inspect(self.output, self.public, self.root / "verified")
        self.assertFalse((self.root / "verified").exists())

    def test_missing_artifact_rejected(self):
        self.rewrite(lambda entries: [(e, c) for e, c in entries if e.name != "workload-images.tar"])
        with self.assertRaises(NomiarchError):
            bundle.inspect(self.output, self.public)

    def test_traversal_and_duplicate_entries_are_rejected(self):
        self.rewrite(lambda entries: entries + [(tarfile.TarInfo("../outside"), b"payload")])
        with self.assertRaises(NomiarchError):
            bundle.inspect(self.output, self.public)

    def test_untrusted_signer_is_rejected(self):
        other = self.root / "other.pem"
        public = self.root / "other.pub"
        bundle.keygen(other, public)
        with self.assertRaises(NomiarchError):
            bundle.inspect(self.output, public)


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age tools required for encrypted recovery integration tests")
class RecoveryTests(unittest.TestCase):
    def test_encrypted_snapshot_restores_queue_and_identities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            write_json(source / "tokens.json", {"operator": "synthetic-operator-token"})
            store = Store(source / "data")
            task = store.submit({"public_access": False})
            identity = root / "identity.txt"
            Runner().run(["age-keygen", "-o", identity])
            recipient = Runner().run(["age-keygen", "-y", identity]).strip()
            archive = root / "backup.zip.age"
            receipt = recovery.export(source, archive, recipient)
            self.assertNotIn(b"synthetic-operator-token", archive.read_bytes())
            restored = root / "restored"
            recovery.restore(archive, restored, identity, receipt["sha256"])
            self.assertEqual(Store(restored / "data").claim()["id"], task)
            self.assertEqual(json.loads((restored / "tokens.json").read_text())["operator"], "synthetic-operator-token")
            with self.assertRaises(NomiarchError):
                recovery.restore(archive, root / "bad", identity, "0" * 64)
            with self.assertRaises(NomiarchError):
                recovery.restore(archive, restored, identity, receipt["sha256"])


class IsolationManifestTests(unittest.TestCase):
    def test_services_use_no_registry_fallback_or_bootstrap_identity(self):
        objects = render({"core_image": "nomiarch/core:" + "a" * 64, "model_image": "nomiarch/model:" + "b" * 64,
                          "policy": "package test"}, {k: k for k in ("operator", "worker", "broker")})["items"]
        deployments = [o for o in objects if o["kind"] == "Deployment"]
        self.assertEqual({o["metadata"]["name"] for o in deployments}, {"core", "worker", "broker", "model"})
        for deployment in deployments:
            spec = deployment["spec"]["template"]["spec"]
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertNotIn("hostNetwork", spec)
            for container in spec["containers"]:
                self.assertEqual(container["imagePullPolicy"], "Never")
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                if deployment["metadata"]["name"] != "core":
                    self.assertNotIn("NOMIARCH_OPERATOR_TOKEN", {e["name"] for e in container.get("env", [])})
        deny = next(o for o in objects if o["kind"] == "NetworkPolicy" and o["metadata"]["name"] == "default-deny")
        self.assertEqual(deny["spec"]["policyTypes"], ["Ingress", "Egress"])
        self.assertNotIn("egress", deny["spec"])


if __name__ == "__main__":
    unittest.main()
