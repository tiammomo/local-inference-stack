"""Tests for immutable rollback identities and private rollback storage."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import configuration  # noqa: E402
from local_inference_stack.catalog import load_catalog  # noqa: E402
from local_inference_stack.deployment import CatalogDeploymentSpec  # noqa: E402
from local_inference_stack.materials import canonical_sha256  # noqa: E402
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.rollout import (  # noqa: E402
    MAX_ROLLBACK_POINTER_BYTES,
    MAX_ROLLBACK_SPEC_BYTES,
    ROLLBACK_SCOPE_POLICY,
    RollbackCASMismatch,
    RollbackPointer,
    RollbackSpec,
    RollbackSpecError,
    RollbackStore,
    RollbackStoreError,
)


TRANSACTION_ONE = "0dbdb868-b62f-4471-84b4-0198a0700f09"
TRANSACTION_TWO = "75c644d0-f781-4be1-bbf4-b24ecab3b678"
CAPTURED_AT = "2026-08-12T01:02:03Z"
UPDATED_AT = "2026-08-12T02:03:04Z"


class RollbackFixture:
    @staticmethod
    def model() -> dict:
        return load_catalog(ROOT / "catalog" / "models.json")["models"][0]

    @classmethod
    def spec(cls, *, transaction_id: str = TRANSACTION_ONE) -> RollbackSpec:
        model = cls.model()
        catalog_spec = CatalogDeploymentSpec.from_catalog_model(model)
        artifact = next(
            item
            for item in catalog_spec.artifacts
            if item.required and item.role == "model"
        )
        runtime_configuration = {
            "image": (
                "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:" + "1" * 64
            ),
            "imageId": "sha256:" + "2" * 64,
            "user": "1000:1000",
            "entrypoint": ["/server"],
            "command": ["--model", "/models/model.gguf"],
            "workingDirectory": "",
            "healthcheck": {},
            "environmentKeys": ["HOME", "PATH"],
            "environmentSha256": "d" * 64,
            "privileged": False,
            "readOnly": True,
            "capAdd": [],
            "capDrop": ["ALL"],
            "securityOpt": ["no-new-privileges:true"],
            "pidsLimit": 512,
            "init": True,
            "shmSize": 67108864,
            "tmpfs": {"/tmp": ["nosuid", "rw"]},
            "restart": {"name": "no", "maximumRetryCount": 0},
            "logging": {},
            "publishAllPorts": False,
            "portBindings": {
                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]
            },
            "networkMode": "default",
            "pidMode": "",
            "ipcMode": "private",
            "utsMode": "",
            "usernsMode": "",
            "cgroupnsMode": "private",
            "networks": {"default": ["qwen"]},
            "mounts": [
                {
                    "type": "bind",
                    "source": "/models",
                    "destination": "/models",
                    "rw": False,
                    "mode": "ro",
                    "propagation": "rprivate",
                }
            ],
            "binds": [],
            "devices": [],
            "deviceRequests": [
                {
                    "driver": "nvidia",
                    "count": -1,
                    "deviceIds": [],
                    "capabilities": [["gpu"]],
                    "options": {},
                }
            ],
            "groupAdd": [],
            "extraHosts": [],
            "links": [],
            "dns": [],
            "dnsOptions": [],
            "dnsSearch": [],
            "sysctls": {},
        }
        return RollbackSpec.from_verified_components(
            catalog_spec=catalog_spec,
            selection_values=configuration.catalog_deployment_environment(
                model, uid=1000, gid=1000
            ),
            runtime_profile_name="latency",
            runtime_profile_environment={"QWEN_PARALLEL": "1"},
            artifacts=[
                {
                    "role": artifact.role,
                    "relativePath": (
                        f"models/{catalog_spec.model_directory}/{artifact.filename}"
                    ),
                    "bytes": artifact.bytes,
                    "sha256": artifact.sha256,
                }
            ],
            runtime={
                "configuredImage": (
                    "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:"
                    + "1" * 64
                ),
                "imageId": "sha256:" + "2" * 64,
                "effectiveComposeSha256": "3" * 64,
                "expectedRuntimeIdentitySha256": canonical_sha256(
                    runtime_configuration
                ),
                "expectedRuntimeConfiguration": runtime_configuration,
            },
            controller_git_commit="5" * 40,
            controller_materials={
                "catalog/models.json": "6" * 64,
                "compose.yaml": "7" * 64,
                "src/local_inference_stack/cli.py": "8" * 64,
            },
            host={
                "fingerprintType": "machine-id-sha256-v1",
                "fingerprint": "9" * 64,
                "environmentKind": "wsl2",
                "architecture": "x86_64",
            },
            acceptance={
                "mode": "quick",
                "status": "passed",
                "evidencePath": "logs/acceptance/source-quick.json",
                "evidenceSha256": "a" * 64,
                "evidenceSelfSha256": "b" * 64,
                "finishedAt": "2026-08-12T01:00:00Z",
            },
            source_transaction_id=transaction_id,
            captured_at=CAPTURED_AT,
        )

    @staticmethod
    def resign(document: dict) -> dict:
        document["selfSha256"] = canonical_sha256(
            {key: value for key, value in document.items() if key != "selfSha256"}
        )
        return document


class RollbackSpecTests(unittest.TestCase):
    def test_round_trip_is_canonical_and_self_addressed(self) -> None:
        spec = RollbackFixture.spec()
        parsed = RollbackSpec.from_document(spec.document())
        self.assertEqual(parsed.document(), spec.document())
        self.assertEqual(parsed.sha256, spec.sha256)
        self.assertEqual(parsed.catalog_spec_sha256, spec.catalog_spec_sha256)
        self.assertEqual(parsed.document()["scopePolicy"], ROLLBACK_SCOPE_POLICY)

    def test_self_digest_tampering_is_rejected(self) -> None:
        document = RollbackFixture.spec().document()
        document["capturedAt"] = "2026-08-12T01:02:04Z"
        with self.assertRaisesRegex(RollbackSpecError, "self digest"):
            RollbackSpec.from_document(document)

    def test_extra_and_missing_fields_are_rejected_even_when_resigned(self) -> None:
        for mutation in ("extra", "missing"):
            with self.subTest(mutation=mutation):
                document = RollbackFixture.spec().document()
                if mutation == "extra":
                    document["shellCommand"] = "docker compose up"
                else:
                    del document["host"]
                RollbackFixture.resign(document)
                with self.assertRaisesRegex(RollbackSpecError, "invalid shape"):
                    RollbackSpec.from_document(document)

    def test_nested_extra_and_missing_fields_are_rejected(self) -> None:
        cases = (
            ("runtime-extra", lambda value: value["runtime"].update({"port": 18080})),
            (
                "selection-missing",
                lambda value: value["selection"]["values"].pop("QWEN_MODEL_FILE"),
            ),
            (
                "controller-material-extra",
                lambda value: value["controller"]["materials"][0].update(
                    {"mode": "0644"}
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(case=name):
                document = RollbackFixture.spec().document()
                mutate(document)
                if name == "selection-missing":
                    document["selection"]["sha256"] = canonical_sha256(
                        document["selection"]["values"]
                    )
                if name == "controller-material-extra":
                    document["controller"]["materialsSha256"] = canonical_sha256(
                        document["controller"]["materials"]
                    )
                RollbackFixture.resign(document)
                with self.assertRaises(RollbackSpecError):
                    RollbackSpec.from_document(document)

    def test_artifact_path_traversal_is_rejected_even_when_resigned(self) -> None:
        document = RollbackFixture.spec().document()
        document["artifacts"][0]["relativePath"] = "../outside/model.gguf"
        RollbackFixture.resign(document)
        with self.assertRaisesRegex(RollbackSpecError, "safe relative"):
            RollbackSpec.from_document(document)

    def test_catalog_selection_substitution_is_rejected(self) -> None:
        document = RollbackFixture.spec().document()
        document["selection"]["values"]["QWEN_CATALOG_ID"] = "other-model"
        document["selection"]["sha256"] = canonical_sha256(
            document["selection"]["values"]
        )
        RollbackFixture.resign(document)
        with self.assertRaisesRegex(RollbackSpecError, "does not match"):
            RollbackSpec.from_document(document)

    def test_runtime_configuration_must_match_the_identity_digest(self) -> None:
        document = RollbackFixture.spec().document()
        document["runtime"]["expectedRuntimeConfiguration"]["readOnly"] = False
        RollbackFixture.resign(document)
        with self.assertRaisesRegex(RollbackSpecError, "configuration does not match"):
            RollbackSpec.from_document(document)

    def test_runtime_configuration_is_bound_to_the_top_level_image(self) -> None:
        document = RollbackFixture.spec().document()
        document["runtime"]["configuredImage"] = (
            "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:" + "c" * 64
        )
        RollbackFixture.resign(document)
        with self.assertRaisesRegex(RollbackSpecError, "image identity"):
            RollbackSpec.from_document(document)

    def test_runtime_configuration_has_an_exact_v1_wire_shape(self) -> None:
        for mutation in ("missing", "extra"):
            with self.subTest(mutation=mutation):
                document = RollbackFixture.spec().document()
                configuration = document["runtime"]["expectedRuntimeConfiguration"]
                if mutation == "missing":
                    del configuration["mounts"]
                else:
                    configuration["runtimeVersion"] = "future-v2"
                document["runtime"]["expectedRuntimeIdentitySha256"] = (
                    canonical_sha256(configuration)
                )
                RollbackFixture.resign(document)
                with self.assertRaisesRegex(RollbackSpecError, "invalid shape"):
                    RollbackSpec.from_document(document)

    def test_only_the_latency_anchor_profile_is_in_scope(self) -> None:
        document = RollbackFixture.spec().document()
        document["runtimeProfile"] = {
            "name": "throughput",
            "environment": {"QWEN_PARALLEL": "2"},
            "sha256": canonical_sha256(
                {
                    "name": "throughput",
                    "environment": {"QWEN_PARALLEL": "2"},
                }
            ),
        }
        RollbackFixture.resign(document)
        with self.assertRaisesRegex(RollbackSpecError, "profile identity"):
            RollbackSpec.from_document(document)

    def test_verified_controller_material_list_is_sorted_canonically(self) -> None:
        baseline = RollbackFixture.spec()
        document = baseline.document()
        catalog_spec = CatalogDeploymentSpec.from_document(document["catalogSpec"])
        rebuilt = RollbackSpec.from_verified_components(
            catalog_spec=catalog_spec,
            selection_values=document["selection"]["values"],
            runtime_profile_name="latency",
            runtime_profile_environment={"QWEN_PARALLEL": "1"},
            artifacts=document["artifacts"],
            runtime=document["runtime"],
            controller_git_commit=document["controller"]["gitCommit"],
            controller_materials=list(reversed(document["controller"]["materials"])),
            host=document["host"],
            acceptance=document["acceptance"],
            source_transaction_id=document["sourceTransactionId"],
            captured_at=document["capturedAt"],
        )
        self.assertEqual(rebuilt, baseline)

    def test_pointer_schema_is_exact(self) -> None:
        pointer = {
            "schemaVersion": 1,
            "kind": "local-inference-stack/rollback-pointer",
            "scopePolicy": ROLLBACK_SCOPE_POLICY,
            "generation": 1,
            "activeSpecSha256": "c" * 64,
            "previousSpecSha256": None,
            "updatedAt": UPDATED_AT,
            "updatedByTransactionId": TRANSACTION_ONE,
        }
        parsed = RollbackPointer.from_document(pointer)
        self.assertEqual(parsed.document(), pointer)
        pointer["extra"] = True
        with self.assertRaisesRegex(RollbackSpecError, "invalid shape"):
            RollbackPointer.from_document(pointer)


class RollbackStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.store = RollbackStore(self.paths)
        self.spec = RollbackFixture.spec()

    def _prepare_object_directory(self) -> Path:
        self.store.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store.state_dir.chmod(0o700)
        self.store.object_dir.mkdir(mode=0o700, exist_ok=True)
        self.store.object_dir.chmod(0o700)
        return self.store.spec_path(self.spec.sha256)

    def test_put_is_private_content_addressed_and_idempotent(self) -> None:
        first = self.store.put(self.spec)
        second = self.store.put(self.spec)
        self.assertEqual(first, second)
        self.assertEqual(first.name, f"{self.spec.sha256}.json")
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.read_spec(self.spec.sha256), self.spec)

    def test_put_repairs_only_an_exact_interrupted_publish_link(self) -> None:
        target = self.store.put(self.spec)
        interrupted = target.parent / f".{target.name}.{'a' * 32}.tmp"
        os.link(target, interrupted)
        self.assertEqual(target.stat().st_nlink, 2)

        self.assertEqual(self.store.put(self.spec), target)

        self.assertFalse(interrupted.exists())
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual(self.store.read_spec(self.spec.sha256), self.spec)

    def test_existing_different_object_is_never_replaced(self) -> None:
        target = self._prepare_object_directory()
        competitor = RollbackFixture.spec(transaction_id=TRANSACTION_TWO)
        competitor_body = json.dumps(
            competitor.document(), sort_keys=True, separators=(",", ":")
        )
        target.write_text(competitor_body + "\n", encoding="utf-8")
        target.chmod(0o600)
        with self.assertRaisesRegex(RollbackStoreError, "content address"):
            self.store.put(self.spec)
        self.assertEqual(json.loads(target.read_text()), competitor.document())

    def test_symlink_object_is_rejected_without_touching_target(self) -> None:
        object_path = self._prepare_object_directory()
        victim = self.paths.root / "victim.json"
        victim.write_text("unchanged\n", encoding="utf-8")
        victim.chmod(0o600)
        object_path.symlink_to(victim)
        with self.assertRaises(RollbackStoreError):
            self.store.put(self.spec)
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_hard_link_object_is_rejected(self) -> None:
        object_path = self._prepare_object_directory()
        victim = self.paths.root / "victim.json"
        victim.write_text(json.dumps(self.spec.document()), encoding="utf-8")
        victim.chmod(0o600)
        os.link(victim, object_path)
        with self.assertRaisesRegex(RollbackStoreError, "0600 regular file"):
            self.store.put(self.spec)
        self.assertEqual(victim.stat().st_nlink, 2)

    def test_fifo_object_is_rejected_without_blocking(self) -> None:
        object_path = self._prepare_object_directory()
        os.mkfifo(object_path, mode=0o600)
        with self.assertRaisesRegex(RollbackStoreError, "0600 regular file"):
            self.store.put(self.spec)

    def test_public_object_is_rejected(self) -> None:
        object_path = self._prepare_object_directory()
        object_path.write_text(json.dumps(self.spec.document()), encoding="utf-8")
        object_path.chmod(0o644)
        with self.assertRaisesRegex(RollbackStoreError, "0600 regular file"):
            self.store.put(self.spec)

    def test_oversized_object_is_rejected(self) -> None:
        object_path = self._prepare_object_directory()
        with object_path.open("wb") as handle:
            handle.truncate(MAX_ROLLBACK_SPEC_BYTES + 1)
        object_path.chmod(0o600)
        with self.assertRaisesRegex(RollbackStoreError, "bounded"):
            self.store.read_spec(self.spec.sha256)

    def test_public_state_or_object_directory_is_rejected(self) -> None:
        self.store.state_dir.mkdir(mode=0o700, parents=True)
        self.store.state_dir.chmod(0o755)
        with self.assertRaisesRegex(RollbackStoreError, "0700"):
            self.store.put(self.spec)

    def test_cache_directory_symlink_is_rejected_without_writing_outside(self) -> None:
        outside = self.paths.root / "outside-cache"
        outside.mkdir(mode=0o700)
        (self.paths.root / "cache").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RollbackStoreError):
            self.store.put(self.spec)
        self.assertFalse((outside / "control-plane").exists())

    def test_object_directory_symlink_is_rejected_without_writing_outside(self) -> None:
        self.store.state_dir.mkdir(mode=0o700, parents=True)
        self.store.state_dir.chmod(0o700)
        outside = self.paths.root / "outside-objects"
        outside.mkdir(mode=0o700)
        self.store.object_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RollbackStoreError):
            self.store.put(self.spec)
        self.assertEqual(list(outside.iterdir()), [])

    def test_pointer_publish_compare_and_swap_and_clear_tombstone(self) -> None:
        first = self.store.publish(
            self.spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(first.generation, 1)
        self.assertEqual(first.active_spec_sha256, self.spec.sha256)
        self.assertIsNone(first.previous_spec_sha256)
        self.assertEqual(self.store.pointer_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.read_active(), self.spec)

        replacement = RollbackFixture.spec(transaction_id=TRANSACTION_TWO)
        with self.assertRaisesRegex(RollbackCASMismatch, "precondition"):
            self.store.publish(
                replacement,
                expected_generation=0,
                expected_previous_sha256=None,
                transaction_id=TRANSACTION_TWO,
                updated_at=UPDATED_AT,
            )

        second = self.store.publish(
            replacement,
            expected_generation=1,
            expected_previous_sha256=self.spec.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(second.generation, 2)
        self.assertEqual(second.active_spec_sha256, replacement.sha256)
        self.assertEqual(second.previous_spec_sha256, self.spec.sha256)

        cleared = self.store.clear(
            expected_generation=2,
            expected_previous_sha256=replacement.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(cleared.generation, 3)
        self.assertIsNone(cleared.active_spec_sha256)
        self.assertEqual(cleared.previous_spec_sha256, replacement.sha256)
        self.assertIsNone(self.store.read_active())

        with self.assertRaisesRegex(RollbackCASMismatch, "precondition"):
            self.store.clear(
                expected_generation=2,
                expected_previous_sha256=replacement.sha256,
                transaction_id=TRANSACTION_TWO,
                updated_at=UPDATED_AT,
            )

    def test_pointer_rejects_public_file(self) -> None:
        self.store.publish(
            self.spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        self.store.pointer_path.chmod(0o644)
        with self.assertRaisesRegex(RollbackStoreError, "0600 regular file"):
            self.store.read_pointer()

    def test_restore_republishes_the_exact_previous_active_pointer(self) -> None:
        first = self.store.publish(
            self.spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        replacement = RollbackFixture.spec(transaction_id=TRANSACTION_TWO)
        current = self.store.publish(
            replacement,
            expected_generation=1,
            expected_previous_sha256=self.spec.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        restored = self.store.restore(
            first,
            expected_current_generation=current.generation,
            expected_current_sha256=replacement.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(restored.generation, 3)
        self.assertEqual(restored.active_spec_sha256, self.spec.sha256)
        self.assertEqual(restored.previous_spec_sha256, replacement.sha256)
        self.assertEqual(self.store.read_active(), self.spec)
        with self.assertRaisesRegex(RollbackCASMismatch, "precondition"):
            self.store.restore(
                first,
                expected_current_generation=current.generation,
                expected_current_sha256=replacement.sha256,
                transaction_id=TRANSACTION_TWO,
                updated_at=UPDATED_AT,
            )

    def test_restore_an_absent_or_tombstoned_predecessor_clears_current(self) -> None:
        current = self.store.publish(
            self.spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        restored_absent = self.store.restore(
            None,
            expected_current_generation=current.generation,
            expected_current_sha256=self.spec.sha256,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(restored_absent.generation, 2)
        self.assertIsNone(restored_absent.active_spec_sha256)
        self.assertEqual(restored_absent.previous_spec_sha256, self.spec.sha256)

        replacement = RollbackFixture.spec(transaction_id=TRANSACTION_TWO)
        published = self.store.publish(
            replacement,
            expected_generation=restored_absent.generation,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        restored_tombstone = self.store.restore(
            restored_absent,
            expected_current_generation=published.generation,
            expected_current_sha256=replacement.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(restored_tombstone.generation, 4)
        self.assertIsNone(restored_tombstone.active_spec_sha256)
        self.assertEqual(
            restored_tombstone.previous_spec_sha256, replacement.sha256
        )
        self.assertIsNone(self.store.read_active())

    def test_restore_after_clear_republishes_the_active_anchor(self) -> None:
        previous = self.store.publish(
            self.spec,
            expected_generation=0,
            expected_previous_sha256=None,
            transaction_id=TRANSACTION_ONE,
            updated_at=UPDATED_AT,
        )
        current = self.store.clear(
            expected_generation=previous.generation,
            expected_previous_sha256=self.spec.sha256,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )

        with self.assertRaisesRegex(RollbackCASMismatch, "precondition"):
            self.store.restore(
                previous,
                expected_current_generation=current.generation - 1,
                expected_current_sha256=None,
                transaction_id=TRANSACTION_TWO,
                updated_at=UPDATED_AT,
            )

        forged_predecessor = RollbackPointer.from_document(
            {
                **previous.document(),
                "activeSpecSha256": "e" * 64,
            }
        )
        with self.assertRaisesRegex(RollbackCASMismatch, "exact predecessor"):
            self.store.restore(
                forged_predecessor,
                expected_current_generation=current.generation,
                expected_current_sha256=None,
                transaction_id=TRANSACTION_TWO,
                updated_at=UPDATED_AT,
            )

        restored = self.store.restore(
            previous,
            expected_current_generation=current.generation,
            expected_current_sha256=None,
            transaction_id=TRANSACTION_TWO,
            updated_at=UPDATED_AT,
        )
        self.assertEqual(restored.generation, current.generation + 1)
        self.assertEqual(restored.active_spec_sha256, self.spec.sha256)
        self.assertIsNone(restored.previous_spec_sha256)
        self.assertEqual(self.store.read_active(), self.spec)

    def test_pointer_reader_rejects_symlink_hardlink_fifo_and_oversize(self) -> None:
        for unsafe_kind in ("symlink", "hardlink", "fifo", "oversize"):
            with self.subTest(kind=unsafe_kind), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                store = RollbackStore(paths)
                spec = RollbackFixture.spec()
                pointer = store.publish(
                    spec,
                    expected_generation=0,
                    expected_previous_sha256=None,
                    transaction_id=TRANSACTION_ONE,
                    updated_at=UPDATED_AT,
                )
                store.pointer_path.unlink()
                if unsafe_kind == "fifo":
                    os.mkfifo(store.pointer_path, mode=0o600)
                else:
                    victim = paths.root / "pointer-victim"
                    if unsafe_kind == "oversize":
                        with victim.open("wb") as handle:
                            handle.truncate(MAX_ROLLBACK_POINTER_BYTES + 1)
                    else:
                        victim.write_text(
                            json.dumps(pointer.document()), encoding="utf-8"
                        )
                    victim.chmod(0o600)
                    if unsafe_kind == "symlink":
                        store.pointer_path.symlink_to(victim)
                    elif unsafe_kind == "hardlink":
                        os.link(victim, store.pointer_path)
                    else:
                        victim.rename(store.pointer_path)
                with self.assertRaises(RollbackStoreError):
                    store.read_pointer()


if __name__ == "__main__":
    unittest.main()
