from __future__ import annotations

import hashlib
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import attestation, bundle, cli, configuration, reference, storage
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.result import CommandResult, IntegrityError, RecoveryError
from local_inference_stack.runner import RunResult
from local_inference_stack.transactions import TransactionStore


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_profiles_are_deterministic_derivatives(self) -> None:
        self.assertEqual(configuration.check(ProjectPaths(ROOT)), [])

    def test_profile_validation_rejects_candidate_production_port(self) -> None:
        document = json.loads((ROOT / "config/runtime-profiles.json").read_text())
        document["profiles"]["candidate"]["environment"]["QWEN_PUBLISH_PORT"] = "18080"
        with self.assertRaisesRegex(Exception, "production port"):
            configuration.validate(document)


class CommandContractTests(unittest.TestCase):
    def test_generated_reference_covers_the_argparse_command_tree(self) -> None:
        def leaves(parser: object, prefix: tuple[str, ...] = ()) -> set[str]:
            children = None
            for action in parser._actions:
                if action.__class__.__name__ == "_SubParsersAction":
                    children = action.choices
                    break
            if children is None:
                return {" ".join(prefix)}
            result: set[str] = set()
            for name, child in children.items():
                result.update(leaves(child, (*prefix, name)))
            return result

        self.assertEqual(leaves(cli.parser()), set(reference.command_paths()))
        self.assertEqual(
            set(reference.COMMANDS),
            {path.split()[0] for path in reference.command_paths()},
        )

    def test_command_result_has_stable_top_level_shape(self) -> None:
        result = CommandResult("doctor", "ok", "healthy").as_dict()
        self.assertEqual(
            set(result),
            {
                "schemaVersion",
                "command",
                "status",
                "code",
                "summary",
                "facts",
                "nextActions",
            },
        )

    def test_plan_adapter_preserves_the_legacy_payload(self) -> None:
        payload = {
            "mode": "read-only-plan",
            "recommendation": {"id": "reviewed-model"},
            "evidenceStatus": "estimated-on-this-host",
            "readyToDeploy": False,
            "nextCommands": [],
        }
        with patch.object(
            cli,
            "run",
            return_value=RunResult(("python3",), 0, json.dumps(payload), ""),
        ):
            result = cli._plan_result(
                ProjectPaths(ROOT),
                Namespace(model=None, vram_gib=None, ram_gib=None),
            )
        self.assertEqual(result.facts["plan"], payload)
        self.assertEqual(result.code, 0)

    def test_mutation_without_yes_is_structured_usage_error(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = cli.main(["deploy", "--json"])
        document = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(document["code"], 2)
        self.assertEqual(document["status"], "error")


class TransactionTests(unittest.TestCase):
    def test_interrupted_transaction_is_durable_and_blocks_a_second_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            first = TransactionStore(paths)
            document = first.begin("release", "quick", {"profile": "latency"})
            first.transition("production_stopping")
            after_restart = TransactionStore(paths)
            plan = after_restart.reconciliation_plan()
            self.assertTrue(plan["required"])
            self.assertEqual(plan["transaction"]["id"], document["id"])
            with self.assertRaises(RecoveryError):
                after_restart.begin("profile", "throughput", {})
            after_restart.transition("failed", detail="injected SIGKILL boundary")
            after_restart.transition("production_restoring")
            completed = after_restart.transition("completed")
            self.assertFalse(TransactionStore(paths).reconciliation_plan()["required"])
            self.assertEqual(completed["state"], "completed")
            self.assertEqual(paths.transaction_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_transition_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TransactionStore(ProjectPaths(Path(directory)))
            store.begin("release", "quick", {})
            with self.assertRaises(RecoveryError):
                store.transition("completed")

    def test_every_release_failure_boundary_is_recoverable_after_restart(self) -> None:
        paths_to_boundary = (
            (),
            ("production_stopping",),
            ("production_stopping", "candidate_starting"),
            ("production_stopping", "candidate_starting", "accepting"),
            (
                "production_stopping",
                "candidate_starting",
                "accepting",
                "production_restoring",
            ),
        )
        for transitions in paths_to_boundary:
            with self.subTest(boundary=transitions[-1] if transitions else "planned"):
                with tempfile.TemporaryDirectory() as directory:
                    paths = ProjectPaths(Path(directory))
                    store = TransactionStore(paths)
                    store.begin("release", "quick", {"profile": "latency"})
                    for state in transitions:
                        store.transition(state)
                    restarted = TransactionStore(paths)
                    self.assertTrue(restarted.reconciliation_plan()["required"])
                    restarted.transition("failed", detail="fault injected")
                    restarted.transition("production_restoring")
                    restarted.transition("completed")
                    self.assertFalse(restarted.reconciliation_plan()["required"])


class AttestationTests(unittest.TestCase):
    def test_hash_and_clean_tree_are_required(self) -> None:
        payload = {
            "schemaVersion": 1,
            "kind": "local-inference-stack/reusable-validation",
            "project": {"revision": "a" * 40, "dirty": False},
        }
        document = {
            "schemaVersion": 1,
            "payload": payload,
            "payloadSha256": hashlib.sha256(attestation.canonical_bytes(payload)).hexdigest(),
            "signature": None,
        }
        verified = attestation.verify_document(document)
        self.assertTrue(verified["cleanTree"])
        document["payloadSha256"] = "0" * 64
        with self.assertRaises(IntegrityError):
            attestation.verify_document(document)

    def test_signature_policy_is_separate_from_self_hash(self) -> None:
        payload = {"project": {"dirty": False}}
        document = {
            "schemaVersion": 1,
            "payload": payload,
            "payloadSha256": hashlib.sha256(attestation.canonical_bytes(payload)).hexdigest(),
            "signature": None,
        }
        with self.assertRaisesRegex(IntegrityError, "signature"):
            attestation.verify_document(document, require_signature=True)


class BundleTests(unittest.TestCase):
    def _project(self, root: Path) -> ProjectPaths:
        (root / "catalog").mkdir()
        (root / "config").mkdir()
        (root / "models" / "tiny").mkdir(parents=True)
        data = b"reviewed model bytes"
        artifact = root / "models" / "tiny" / "tiny.gguf"
        artifact.write_bytes(data)
        catalog = {
            "schemaVersion": 1,
            "updatedAt": "2026-07-31",
            "artifactPolicy": {},
            "defaultModel": "tiny-model",
            "models": [
                {
                    "id": "tiny-model",
                    "modelDirectory": "tiny",
                    "artifactRepository": "example/repository",
                    "artifactRevision": "a" * 40,
                    "license": {"spdx": "Apache-2.0", "reviewRequired": True},
                    "artifacts": [
                        {
                            "filename": "tiny.gguf",
                            "required": True,
                            "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    ],
                }
            ],
        }
        (root / "catalog" / "models.json").write_text(json.dumps(catalog))
        (root / "config" / "runtime-profiles.json").write_text("{}")
        (root / "compose.yaml").write_text("services:\n  model:\n    image: example/image@sha256:" + "b" * 64 + "\n")
        return ProjectPaths(root)

    def test_bundle_round_trip_is_verified_and_import_does_not_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._project(root)
            archive = root / "offline.tar"
            created = bundle.create(paths, archive, "tiny-model", include_model=True)
            self.assertTrue(created["containsModelArtifacts"])
            verified = bundle.verify(archive)
            self.assertTrue(verified["hostAdmissionRequired"])
            (root / "models" / "tiny" / "tiny.gguf").unlink()
            imported = bundle.import_artifacts(paths, archive)
            self.assertFalse(imported["selected"])
            self.assertFalse(imported["runtimeStarted"])

    def test_bundle_rejects_path_traversal_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "unsafe.tar"
            payload = Path(directory) / "payload"
            payload.write_bytes(b"x")
            with tarfile.open(archive, "w") as handle:
                handle.add(payload, arcname="../escape")
            with self.assertRaises(IntegrityError):
                bundle.verify(archive)


class StorageTests(unittest.TestCase):
    def test_gc_is_limited_to_old_partial_and_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "cache").mkdir()
            partial = root / "models" / "download.gguf.part"
            model = root / "models" / "keep.gguf"
            partial.write_bytes(b"partial")
            model.write_bytes(b"model")
            old = time.time() - 30 * 86400
            os.utime(partial, (old, old))
            candidates = storage.gc_candidates(ProjectPaths(root), older_than_days=14)
            self.assertEqual([item["path"] for item in candidates], ["models/download.gguf.part"])
            storage.delete_candidates(ProjectPaths(root), candidates)
            self.assertFalse(partial.exists())
            self.assertTrue(model.exists())

    def test_gc_suppresses_candidates_during_unfinished_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            partial = root / "models" / "download.gguf.part"
            partial.write_bytes(b"partial")
            old = time.time() - 30 * 86400
            os.utime(partial, (old, old))
            store = TransactionStore(ProjectPaths(root))
            store.begin("deploy", "model", {})
            self.assertEqual(
                storage.gc_candidates(ProjectPaths(root), older_than_days=14), []
            )


if __name__ == "__main__":
    unittest.main()
