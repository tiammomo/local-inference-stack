"""Tests for atomic, privacy-preserving acceptance evidence."""

from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "acceptance_evidence", ROOT_DIR / "scripts" / "acceptance-evidence.py"
)
assert SPEC and SPEC.loader
ACCEPTANCE_EVIDENCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE_EVIDENCE)
MODEL_MANAGER_SPEC = importlib.util.spec_from_file_location(
    "model_manager_for_evidence", ROOT_DIR / "scripts" / "model-manager.py"
)
assert MODEL_MANAGER_SPEC and MODEL_MANAGER_SPEC.loader
MODEL_MANAGER = importlib.util.module_from_spec(MODEL_MANAGER_SPEC)
MODEL_MANAGER_SPEC.loader.exec_module(MODEL_MANAGER)


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_atomic_writer_uses_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "evidence.json"
            payload = {
                "schemaVersion": 2,
                "status": "passed",
                "privacy": "synthetic metadata only",
            }
            ACCEPTANCE_EVIDENCE.write_atomic(target, payload)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_gpu_inventory_parser_does_not_persist_host_identity(self) -> None:
        original = ACCEPTANCE_EVIDENCE.command_output
        try:
            ACCEPTANCE_EVIDENCE.command_output = lambda *_args, **_kwargs: (
                "0, NVIDIA Test GPU, 16384, 999.1"
            )
            self.assertEqual(
                ACCEPTANCE_EVIDENCE.gpu_inventory(),
                [
                    {
                        "index": 0,
                        "name": "NVIDIA Test GPU",
                        "vramGiB": 16.0,
                        "driver": "999.1",
                    }
                ],
            )
        finally:
            ACCEPTANCE_EVIDENCE.command_output = original

    def test_host_fingerprint_is_stable_and_does_not_expose_machine_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            machine_id = "0123456789abcdef0123456789abcdef"
            source = Path(directory) / "machine-id"
            source.write_text(machine_id + "\n", encoding="utf-8")
            fingerprint = ACCEPTANCE_EVIDENCE.host_fingerprint((source,))
            self.assertEqual(len(fingerprint or ""), 64)
            self.assertNotIn(machine_id, fingerprint or "")
            self.assertEqual(
                fingerprint,
                MODEL_MANAGER.host_fingerprint((source,)),
            )

    def test_writer_and_reader_hash_the_same_acceptance_dependencies(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        self.assertEqual(
            ACCEPTANCE_EVIDENCE.acceptance_configuration(model),
            MODEL_MANAGER.acceptance_configuration(model),
        )

    def test_payload_digest_detects_changes(self) -> None:
        payload = {
            "schemaVersion": 3,
            "status": "passed",
            "durationSeconds": 12,
        }
        payload["selfSha256"] = ACCEPTANCE_EVIDENCE.payload_sha256(payload)
        self.assertEqual(
            payload["selfSha256"],
            MODEL_MANAGER.payload_sha256(payload),
        )
        payload["durationSeconds"] = 13
        self.assertNotEqual(
            payload["selfSha256"],
            MODEL_MANAGER.payload_sha256(payload),
        )

    def test_secure_reader_rejects_wide_permissions_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "evidence.json"
            ACCEPTANCE_EVIDENCE.write_atomic(target, {"schemaVersion": 3})
            self.assertEqual(
                MODEL_MANAGER.read_secure_evidence(target),
                {"schemaVersion": 3},
            )
            target.chmod(0o644)
            self.assertIsNone(MODEL_MANAGER.read_secure_evidence(target))
            target.chmod(0o600)
            linked = root / "linked.json"
            linked.symlink_to(target)
            self.assertIsNone(MODEL_MANAGER.read_secure_evidence(linked))


if __name__ == "__main__":
    unittest.main()
