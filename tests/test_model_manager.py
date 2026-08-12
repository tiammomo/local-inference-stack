"""Tests for catalog validation and deterministic host recommendations."""

from __future__ import annotations

import hashlib
import importlib.util
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.acceptance import (
    RUN_KIND,
    RUNNER_PATH,
    RUN_SCHEMA_VERSION,
    expected_steps,
    step_plan_sha256,
)
from local_inference_stack.catalog import (
    CatalogError,
    load_catalog,
    matches_recorded_hardware_profile,
    recommend as catalog_recommend,
    validate_catalog,
)
from local_inference_stack.deployment import CatalogDeploymentSpec
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.result import RecoveryError
from local_inference_stack.transactions import TransactionStore
from local_inference_stack.host import environment_kind as classify_environment

SPEC = importlib.util.spec_from_file_location(
    "model_manager", ROOT_DIR / "scripts" / "model-manager.py"
)
assert SPEC and SPEC.loader
MODEL_MANAGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL_MANAGER)


def host(
    vram: float,
    ram: float = 96,
    disk: float = 100,
    *,
    free_vram: float | None = None,
    name: str = "test",
    environment_kind: str = "wsl2",
) -> dict:
    available = vram if free_vram is None else free_vram
    return {
        "platform": "linux",
        "environmentKind": environment_kind,
        "architecture": "x86_64",
        "gpus": (
            [
                {
                    "index": 0,
                    "name": name,
                    "vramGiB": vram,
                    "freeVramGiB": available,
                    "driver": "test-driver",
                }
            ]
            if vram
            else []
        ),
        "totalVramGiB": vram,
        "largestGpuVramGiB": vram,
        "totalFreeVramGiB": available,
        "largestFreeVramGiB": available,
        "ramGiB": ram,
        "availableRamGiB": ram,
        "freeDiskGiB": disk,
        "docker": {"available": True, "version": "test"},
        "dockerCompose": {
            "available": True,
            "version": "test",
            "configurationCompatible": True,
        },
        "nvidiaContainerRuntime": True,
        "curl": {"available": True, "version": "test"},
        "python": {"version": "3.12", "supported": True},
        "commands": {"flock": True},
        "user": {"uid": 1000, "gid": 1000},
    }


def catalog_with_future_candidate(catalog: dict) -> dict:
    """Inject a test-only read-only candidate without expanding the real Catalog."""

    candidate_catalog = copy.deepcopy(catalog)
    candidate = copy.deepcopy(candidate_catalog["models"][0])
    candidate.update(
        {
            "id": "fixture-future-candidate",
            "displayName": "Fixture future candidate",
            "status": "estimated",
            "lifecycleRole": "candidate",
            "deploymentEligibility": {
                "automatic": False,
                "reason": "test-fixture-has-no-host-evidence",
            },
            "purpose": "Test-only future candidate",
            "modelDirectory": "fixture-future-candidate",
            "servedModelId": "fixture-future-candidate",
            "requirements": {
                **candidate["requirements"],
                "minVramGiB": 16,
                "recommendedVramGiB": 20,
            },
        }
    )
    candidate.pop("validation", None)
    candidate.pop("validatedHardware", None)
    candidate_catalog["models"].append(candidate)
    return validate_catalog(candidate_catalog)


def catalog_with_validated_role(catalog: dict, role: str) -> dict:
    """Build a schema-valid, test-only signed model in one lifecycle role."""

    validated_catalog = copy.deepcopy(catalog)
    if role == "lts":
        model = validated_catalog["models"][0]
    else:
        model = copy.deepcopy(validated_catalog["models"][0])
        model.update(
            {
                "id": f"fixture-validated-{role}",
                "displayName": f"Fixture validated {role}",
                "purpose": f"Test-only validated {role}",
                "modelDirectory": f"fixture-validated-{role}",
                "servedModelId": f"fixture-validated-{role}",
                "lifecycleRole": role,
            }
        )
        validated_catalog["models"].append(model)
    model.update(
        {
            "status": "validated",
            "deploymentEligibility": {
                "automatic": role == "lts",
                "reason": f"test-only-validated-{role}",
            },
            "validationAttestation": {
                "mode": "full",
                "tool": "minisign",
                "payloadSha256": "a" * 64,
                "trustedKeySha256": "b" * 64,
                "documentPath": f"attestations/fixture-{role}.json",
                "signaturePath": f"attestations/fixture-{role}.minisig",
            },
        }
    )
    return validate_catalog(validated_catalog)


class ArtifactVerificationTests(unittest.TestCase):
    def test_download_lock_rejects_symlink_hardlink_and_public_file(self) -> None:
        for case in ("symlink", "dangling-symlink", "hardlink", "public"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                locks = root / "cache" / "locks"
                locks.mkdir(parents=True, mode=0o700)
                lock = locks / "download-fixture.lock"
                victim = root / "victim"
                victim.write_text("unchanged", encoding="utf-8")
                victim.chmod(0o600)
                if case == "symlink":
                    lock.symlink_to(victim)
                elif case == "dangling-symlink":
                    lock.symlink_to(root / "missing")
                elif case == "hardlink":
                    os.link(victim, lock)
                else:
                    lock.write_text("lock", encoding="utf-8")
                    lock.chmod(0o644)
                with (
                    patch.object(MODEL_MANAGER, "ROOT_DIR", root),
                    self.assertRaises(SystemExit),
                ):
                    with MODEL_MANAGER.exclusive_local_lock("download-fixture"):
                        self.fail("an unsafe lock crossed the local lock boundary")
                self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged")

    def test_partial_open_rejects_symlink_hardlink_and_public_file(self) -> None:
        for case in ("symlink", "dangling-symlink", "hardlink", "public"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                destination = root / "models" / "fixture"
                destination.mkdir(parents=True, mode=0o700)
                destination.chmod(0o700)
                partial = destination / "model.gguf.part"
                victim = root / "victim"
                victim.write_bytes(b"unchanged")
                victim.chmod(0o600)
                if case == "symlink":
                    partial.symlink_to(victim)
                elif case == "dangling-symlink":
                    partial.symlink_to(root / "missing")
                elif case == "hardlink":
                    os.link(victim, partial)
                else:
                    partial.write_bytes(b"partial")
                    partial.chmod(0o644)
                with (
                    patch.object(MODEL_MANAGER, "ROOT_DIR", root),
                    self.assertRaises(SystemExit),
                ):
                    with MODEL_MANAGER.secure_partial_file(
                        partial, maximum_bytes=100
                    ):
                        self.fail("an unsafe partial crossed the descriptor boundary")
                self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_partial_download_target_is_a_stable_inherited_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "models" / "fixture"
            destination.mkdir(parents=True, mode=0o700)
            destination.chmod(0o700)
            partial = destination / "model.gguf.part"
            with patch.object(MODEL_MANAGER, "ROOT_DIR", root):
                with MODEL_MANAGER.secure_partial_file(
                    partial, maximum_bytes=100
                ) as (descriptor, _directory_descriptor, size):
                    self.assertEqual(size, 0)
                    subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "import os,sys; os.write(int(sys.argv[1]), b'stable')",
                            str(descriptor),
                        ],
                        pass_fds=(descriptor,),
                        check=True,
                    )
            self.assertEqual(partial.read_bytes(), b"stable")

    def test_partial_path_swap_cannot_promote_an_unverified_inode(self) -> None:
        payload = b"approved artifact bytes"
        evil = b"evil artifact payload!!"
        self.assertEqual(len(payload), len(evil))
        artifact = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "models" / "fixture"
            destination.mkdir(parents=True)
            destination.chmod(0o700)
            partial = destination / "model.gguf.part"
            final = destination / "model.gguf"
            partial.write_bytes(payload)
            partial.chmod(0o600)
            original = MODEL_MANAGER.open_fully_verified_artifact

            def verify_then_swap(path, expected):
                descriptor, metadata = original(path, expected)
                replacement = destination / "replacement"
                replacement.write_bytes(evil)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                return descriptor, metadata

            with (
                patch.object(MODEL_MANAGER, "ROOT_DIR", root),
                patch.object(
                    MODEL_MANAGER,
                    "open_fully_verified_artifact",
                    side_effect=verify_then_swap,
                ),
                self.assertRaisesRegex(SystemExit, "changed before promotion"),
            ):
                MODEL_MANAGER.promote_verified_partial(partial, final, artifact)
            self.assertFalse(final.exists())

    def _fixture(self, root: Path) -> tuple[Path, Path, dict[str, object]]:
        model = root / "models" / "tiny.gguf"
        integrity = root / "cache" / "integrity"
        model.parent.mkdir(parents=True)
        integrity.mkdir(parents=True)
        model.parent.chmod(0o700)
        integrity.chmod(0o700)
        body = b"small artifact verification fixture"
        model.write_bytes(body)
        model.chmod(0o600)
        digest = hashlib.sha256(body).hexdigest()
        artifact: dict[str, object] = {
            "filename": model.name,
            "bytes": len(body),
            "sha256": digest,
        }
        stamp = integrity / f"test--{model.name}.sha256.stamp"
        fingerprint = MODEL_MANAGER.artifact_stat_fingerprint(model.stat())
        stamp.write_text(f"{digest}|{fingerprint}\n", encoding="utf-8")
        stamp.chmod(0o600)
        return model, stamp, artifact

    def _patched_paths(self, root: Path):
        return (
            patch.object(MODEL_MANAGER, "ROOT_DIR", root),
            patch.object(MODEL_MANAGER, "INTEGRITY_DIR", root / "cache" / "integrity"),
        )

    def test_cached_verification_uses_secure_descriptors_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, _stamp, artifact = self._fixture(root)
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch, patch.object(
                MODEL_MANAGER,
                "sha256_descriptor",
                side_effect=AssertionError("cached verification must not hash"),
            ) as digest:
                self.assertTrue(
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )
                )
                digest.assert_not_called()

    def test_stale_stamp_runs_full_sha_and_refreshes_the_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, artifact = self._fixture(root)
            stamp.write_text(f"{artifact['sha256']}|stale\n", encoding="utf-8")
            stamp.chmod(0o600)
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch:
                self.assertFalse(
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )
                )
            expected = (
                f"{artifact['sha256']}|"
                f"{MODEL_MANAGER.artifact_stat_fingerprint(model.stat())}"
            )
            self.assertEqual(stamp.read_text(encoding="utf-8").strip(), expected)
            self.assertEqual(stamp.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stamp.stat().st_nlink, 1)

    def test_missing_stamp_runs_full_sha_and_creates_a_private_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, artifact = self._fixture(root)
            stamp.unlink()
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch, patch.object(
                MODEL_MANAGER,
                "sha256_descriptor",
                wraps=MODEL_MANAGER.sha256_descriptor,
            ) as digest:
                self.assertFalse(
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )
                )
                digest.assert_called_once()
            self.assertTrue(stamp.is_file())
            self.assertEqual(stamp.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stamp.stat().st_nlink, 1)

    def test_read_only_verification_hashes_without_creating_or_refreshing_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, artifact = self._fixture(root)
            stale = f"{artifact['sha256']}|stale\n"
            stamp.write_text(stale, encoding="utf-8")
            stamp.chmod(0o600)
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch, patch.object(
                MODEL_MANAGER,
                "sha256_descriptor",
                wraps=MODEL_MANAGER.sha256_descriptor,
            ) as digest:
                self.assertFalse(
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                        write_stamp=False,
                    )
                )
                digest.assert_called_once()
            self.assertEqual(stamp.read_text(encoding="utf-8"), stale)

            stamp.unlink()
            stamp.parent.rmdir()
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch:
                self.assertFalse(
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                        write_stamp=False,
                    )
                )
            self.assertFalse(stamp.parent.exists())

    def test_model_parent_symlink_escape_is_rejected_before_hashing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(project_directory)
            outside = Path(outside_directory)
            outside_model = outside / "tiny.gguf"
            body = b"outside artifact fixture"
            outside_model.write_bytes(body)
            outside_model.chmod(0o600)
            (root / "models").symlink_to(outside, target_is_directory=True)
            (root / "cache" / "integrity").mkdir(parents=True)
            (root / "cache" / "integrity").chmod(0o700)
            artifact = {
                "filename": outside_model.name,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch, patch.object(
                MODEL_MANAGER, "sha256_descriptor"
            ) as digest:
                with self.assertRaisesRegex(SystemExit, "safely open|outside"):
                    MODEL_MANAGER.verify_artifact(
                        root / "models" / outside_model.name,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )
                digest.assert_not_called()

    def test_stamp_parent_symlink_escape_is_rejected_before_hashing(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(project_directory)
            model, stamp, artifact = self._fixture(root)
            outside = Path(outside_directory)
            outside.chmod(0o700)
            escaped_stamp = outside / stamp.name
            stamp.replace(escaped_stamp)
            (root / "cache" / "integrity").rmdir()
            (root / "cache" / "integrity").symlink_to(
                outside, target_is_directory=True
            )
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch, patch.object(
                MODEL_MANAGER, "sha256_descriptor"
            ) as digest:
                with self.assertRaisesRegex(SystemExit, "safely open|outside"):
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )
                digest.assert_not_called()

    def test_cached_stamp_mode_and_artifact_link_count_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, artifact = self._fixture(root)
            root_patch, integrity_patch = self._patched_paths(root)
            stamp.chmod(0o644)
            with root_patch, integrity_patch:
                with self.assertRaisesRegex(SystemExit, "private current-user"):
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )

            stamp.chmod(0o600)
            os.link(model, model.with_name("tiny-hardlink.gguf"))
            root_patch, integrity_patch = self._patched_paths(root)
            with root_patch, integrity_patch:
                with self.assertRaisesRegex(SystemExit, "private current-user"):
                    MODEL_MANAGER.verify_artifact(
                        model,
                        artifact,
                        cached=True,
                        cache_key="test--tiny.gguf",
                    )


class CatalogTests(unittest.TestCase):
    def test_download_rechecks_active_transaction_before_materialization(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        model = catalog["models"][0]
        approved = CatalogDeploymentSpec.from_catalog_model(model)
        args = Namespace(
            model=approved.catalog_id,
            yes=True,
            all_artifacts=False,
            catalog_spec_sha256=approved.sha256,
            artifact_sha256=approved.artifacts[0].sha256,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TransactionStore(ProjectPaths(root))
            transaction = store.begin(
                "deploy",
                approved.catalog_id,
                {},
                approved_catalog_spec=approved.approval_document(),
            )

            def revoke(_catalog, _model_id, _action):
                store.transition("deploying", expected_id=transaction["id"])
                store.transition("completed", expected_id=transaction["id"])
                return {"readyToDeploy": True}

            with (
                patch.dict(
                    os.environ,
                    {"QWEN_CONTROL_TRANSACTION_ID": transaction["id"]},
                ),
                patch.object(MODEL_MANAGER, "ROOT_DIR", root),
                patch.object(
                    MODEL_MANAGER,
                    "require_deployment_admission",
                    side_effect=revoke,
                ),
                patch.object(
                    MODEL_MANAGER, "ensure_private_project_directory"
                ) as materialize,
                self.assertRaisesRegex(SystemExit, "missing or already terminal"),
            ):
                MODEL_MANAGER.download_model(args, catalog)
            materialize.assert_not_called()

    def test_same_id_catalog_drift_blocks_download_before_materialization(self) -> None:
        catalog = MODEL_MANAGER.load_catalog()
        original = copy.deepcopy(catalog["models"][0])
        approved = CatalogDeploymentSpec.from_catalog_model(original)
        drifted = copy.deepcopy(catalog)
        drifted["models"][0]["purpose"] = "changed after plan approval"
        args = Namespace(
            model=approved.catalog_id,
            yes=True,
            all_artifacts=False,
            catalog_spec_sha256=approved.sha256,
            artifact_sha256=approved.artifacts[0].sha256,
        )
        with (
            patch.dict(
                os.environ,
                {"QWEN_CONTROL_TRANSACTION_ID": "11111111-1111-4111-8111-111111111111"},
            ),
            patch.object(MODEL_MANAGER, "ensure_private_project_directory") as materialize,
            patch.object(MODEL_MANAGER, "require_deployment_admission") as admission,
            patch.object(
                MODEL_MANAGER.TransactionStore,
                "assert_approved_deployment",
            ) as transaction_check,
            self.assertRaisesRegex(SystemExit, "current Catalog deployment"),
        ):
            MODEL_MANAGER.download_model(args, drifted)
        materialize.assert_not_called()
        admission.assert_not_called()
        transaction_check.assert_not_called()

    def test_environment_classification_is_explicit_and_fail_closed(self) -> None:
        self.assertEqual(
            classify_environment(
                system="Linux",
                kernel_release="6.6.87.2-microsoft-standard-WSL2",
            ),
            "wsl2",
        )
        self.assertEqual(
            classify_environment(system="Linux", kernel_release="4.4.0-Microsoft"),
            "wsl1",
        )
        self.assertEqual(
            classify_environment(system="Linux", kernel_release="6.12.1-generic"),
            "native-linux",
        )
        self.assertEqual(
            classify_environment(system="Darwin", kernel_release="24.0.0"),
            "unsupported",
        )

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = MODEL_MANAGER.load_catalog()

    def test_catalog_ids_and_artifacts_are_unique(self) -> None:
        ids = [model["id"] for model in self.catalog["models"]]
        self.assertEqual(ids, ["qwen35-9b-q5km"])
        self.assertEqual(len(ids), len(set(ids)))
        for model in self.catalog["models"]:
            filenames = [artifact["filename"] for artifact in model["artifacts"]]
            self.assertEqual(len(filenames), len(set(filenames)))
            self.assertEqual(len(model["artifacts"]), 1)
            self.assertEqual(model["license"]["spdx"], "Apache-2.0")
            self.assertNotIn("/resolve/main/", model["artifacts"][0]["url"])

    def test_acquisition_record_separates_identity_publisher_and_license_status(self) -> None:
        model = self.catalog["models"][0]
        artifact = model["artifacts"][0]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            MODEL_MANAGER, "ACQUISITION_DIR", Path(directory)
        ), patch.object(MODEL_MANAGER, "command_output", return_value="curl test"):
            MODEL_MANAGER.record_acquisition(
                model, artifact, method="verified-existing-local"
            )
            record_path = next(Path(directory).glob("*.json"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                record["verification"]["identity"],
                "verified-byte-size-and-sha256",
            )
            self.assertEqual(
                record["verification"]["publisher"],
                "not-cryptographically-verified",
            )
            self.assertEqual(record["license"]["reviewRequired"], True)
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            self.assertIn(
                f"/resolve/{model['artifactRevision']}/",
                model["artifacts"][0]["url"],
            )
            runtime = model["runtime"]
            self.assertLess(
                runtime["recommendedInputTokens"] + runtime["maxOutputTokens"],
                runtime["contextTokens"],
            )

    def test_script_uses_package_model_selection(self) -> None:
        self.assertIs(MODEL_MANAGER.recommend, catalog_recommend)

    def test_recommendation_uses_only_evidence_backed_checked_in_entry(self) -> None:
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(11)))
        self.assertEqual(
            MODEL_MANAGER.recommend(self.catalog, host(12))["id"],
            "qwen35-9b-q5km",
        )
        self.assertEqual(
            MODEL_MANAGER.recommend(self.catalog, host(32))["id"],
            "qwen35-9b-q5km",
        )

    def test_future_candidate_is_injected_as_a_read_only_fixture(self) -> None:
        catalog = catalog_with_future_candidate(self.catalog)
        self.assertEqual(
            MODEL_MANAGER.recommend(catalog, host(15))["id"],
            "qwen35-9b-q5km",
        )
        self.assertEqual(
            MODEL_MANAGER.recommend(catalog, host(16))["id"],
            "qwen35-9b-q5km",
        )
        plan = MODEL_MANAGER.plan_for_host(
            Namespace(model="fixture-future-candidate"), catalog, host(16)
        )
        self.assertFalse(plan["hostAdmissionPassed"])
        self.assertFalse(plan["catalogDeploymentEligible"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])

    def test_checked_in_catalog_is_single_and_read_only(self) -> None:
        statuses = [model["status"] for model in self.catalog["models"]]
        self.assertEqual(statuses, ["provisional"])
        model = self.catalog["models"][0]
        assessed = host(
            model["requirements"]["minVramGiB"],
            ram=max(96, model["requirements"]["minRamGiB"]),
            disk=max(100, model["requirements"]["minFreeDiskGiB"]),
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        plan = MODEL_MANAGER.plan_for_host(
            Namespace(model=model["id"]), self.catalog, assessed
        )
        self.assertFalse(plan["hostAdmissionPassed"])
        self.assertFalse(plan["catalogDeploymentEligible"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])

    def test_strict_catalog_validation_rejects_malformed_evidence(self) -> None:
        invalid_date = copy.deepcopy(self.catalog)
        invalid_date["updatedAt"] = "2026-02-30"
        with self.assertRaisesRegex(CatalogError, "ISO calendar date"):
            validate_catalog(invalid_date)

        duplicate = copy.deepcopy(self.catalog)
        duplicate["models"].append(copy.deepcopy(duplicate["models"][0]))
        with self.assertRaisesRegex(CatalogError, "duplicate model id"):
            validate_catalog(duplicate)

        automatic_provisional = copy.deepcopy(self.catalog)
        automatic_provisional["models"][0]["deploymentEligibility"][
            "automatic"
        ] = True
        with self.assertRaisesRegex(CatalogError, "read-only Catalog status"):
            validate_catalog(automatic_provisional)

        unpinned = copy.deepcopy(self.catalog)
        unpinned["models"][0]["artifacts"][0]["url"] = (
            "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/"
            "Qwen3.5-9B-Q5_K_M.gguf?download=true"
        )
        with self.assertRaisesRegex(CatalogError, "reviewed revision"):
            validate_catalog(unpinned)

        malformed = copy.deepcopy(self.catalog)
        malformed["models"][0]["modelDirectory"] = None
        with self.assertRaisesRegex(CatalogError, "malformed Catalog model"):
            validate_catalog(malformed)

        unknown_authority_field = copy.deepcopy(self.catalog)
        unknown_authority_field["models"][0]["deployAnyway"] = True
        with self.assertRaisesRegex(CatalogError, "unsupported or missing fields"):
            validate_catalog(unknown_authority_field)

        missing_performance_policy = copy.deepcopy(self.catalog)
        missing_performance_policy["models"][0].pop("performancePolicy")
        with self.assertRaisesRegex(CatalogError, "unsupported or missing fields"):
            validate_catalog(missing_performance_policy)

        unsafe_performance_policy = copy.deepcopy(self.catalog)
        unsafe_performance_policy["models"][0]["performancePolicy"][
            "manifestPath"
        ] = "../deployments/target/manifest.json"
        with self.assertRaisesRegex(CatalogError, "invalid performance policy"):
            validate_catalog(unsafe_performance_policy)

        untyped_performance_policy = copy.deepcopy(self.catalog)
        untyped_performance_policy["models"][0]["performancePolicy"][
            "manifestSha256"
        ] = "0" * 64
        with self.assertRaisesRegex(CatalogError, "unsupported or missing fields"):
            validate_catalog(untyped_performance_policy)

        oversized_number = copy.deepcopy(self.catalog)
        oversized_number["models"][0]["requirements"]["minVramGiB"] = 10**400
        with self.assertRaisesRegex(CatalogError, "hardware requirements"):
            validate_catalog(oversized_number)

    def test_catalog_loader_rejects_unsafe_files_and_ambiguous_json(self) -> None:
        source = ROOT_DIR / "catalog" / "models.json"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "models.json"
            regular.write_bytes(source.read_bytes())
            regular.chmod(0o600)
            self.assertEqual(load_catalog(regular)["defaultModel"], "qwen35-9b-q5km")

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                source.read_text(encoding="utf-8").replace(
                    '"defaultModel": "qwen35-9b-q5km",',
                    '"defaultModel": "qwen35-9b-q5km",\n  "defaultModel": "qwen35-9b-q5km",',
                    1,
                ),
                encoding="utf-8",
            )
            duplicate.chmod(0o600)
            with self.assertRaisesRegex(CatalogError, "duplicate key"):
                load_catalog(duplicate)

            oversized_json_number = root / "oversized-number.json"
            oversized_json_number.write_text(
                source.read_text(encoding="utf-8").replace(
                    '"minVramGiB": 12',
                    '"minVramGiB": ' + "9" * 5000,
                    1,
                ),
                encoding="utf-8",
            )
            oversized_json_number.chmod(0o600)
            with self.assertRaisesRegex(CatalogError, "Catalog JSON is invalid"):
                load_catalog(oversized_json_number)

            regular.chmod(0o666)
            with self.assertRaisesRegex(CatalogError, "cannot read Catalog"):
                load_catalog(regular)
            regular.chmod(0o600)
            linked = root / "linked.json"
            linked.symlink_to(regular)
            with self.assertRaisesRegex(CatalogError, "cannot read Catalog"):
                load_catalog(linked)

    def test_automatic_admission_requires_exact_tier_one_profile(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        model = catalog["models"][0]
        model["status"] = "validated"
        exact = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        other_gpu = host(16, name="NVIDIA GeForce RTX 4090")
        native = host(
            16,
            name="NVIDIA GeForce RTX 5070 Ti",
            environment_kind="native-linux",
        )
        with patch.object(MODEL_MANAGER, "catalog_deployment_eligible", return_value=True):
            exact_plan = MODEL_MANAGER.plan_for_host(Namespace(model=None), catalog, exact)
            other_plan = MODEL_MANAGER.plan_for_host(Namespace(model=None), catalog, other_gpu)
            native_plan = MODEL_MANAGER.plan_for_host(Namespace(model=None), catalog, native)
        self.assertTrue(exact_plan["hardwareProfileMatch"])
        self.assertTrue(exact_plan["hostAdmissionPassed"])
        self.assertTrue(exact_plan["readyToDeploy"])
        for plan in (other_plan, native_plan):
            self.assertFalse(plan["hardwareProfileMatch"])
            self.assertFalse(plan["hostAdmissionPassed"])
            self.assertFalse(plan["readyToDeploy"])

    def test_static_catalog_signature_boolean_is_never_deployment_authority(self) -> None:
        model = copy.deepcopy(
            MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        )
        model["status"] = "validated"
        model["deploymentEligibility"] = {
            "automatic": True,
            "reason": "locally-edited-static-claim",
        }
        model["validationAttestation"] = {
            "mode": "full",
            "signatureVerified": True,
            "payloadSha256": "a" * 64,
        }
        self.assertFalse(MODEL_MANAGER.catalog_deployment_eligible(model))

        model["validationAttestation"].update(
            {
                "tool": "minisign",
                "trustedKeySha256": "b" * 64,
                "documentPath": "attestations/model.json",
                "signaturePath": "attestations/model.minisig",
            }
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(MODEL_MANAGER.catalog_deployment_eligible(model))
        with patch.object(
            MODEL_MANAGER,
            "catalog_attestation_verification",
            return_value={"promotionEligible": True},
        ):
            self.assertTrue(MODEL_MANAGER.catalog_deployment_eligible(model))

    def test_validated_non_lts_roles_remain_explicit_only(self) -> None:
        for role in ("rollback", "candidate"):
            with self.subTest(role=role):
                catalog = copy.deepcopy(self.catalog)
                anchor = copy.deepcopy(catalog["models"][0])
                anchor.update(
                    {
                        "id": f"fixture-{role}",
                        "displayName": f"Fixture {role}",
                        "servedModelId": f"fixture-{role}",
                        "modelDirectory": f"fixture-{role}",
                        "status": "validated",
                        "lifecycleRole": role,
                        "deploymentEligibility": {
                            "automatic": False,
                            "reason": f"{role}-requires-explicit-workflow",
                        },
                        "validationAttestation": {
                            "mode": "full",
                            "tool": "minisign",
                            "payloadSha256": "a" * 64,
                            "trustedKeySha256": "b" * 64,
                            "documentPath": f"attestations/{role}.json",
                            "signaturePath": f"attestations/{role}.minisig",
                        },
                    }
                )
                catalog["models"].append(anchor)
                validated = validate_catalog(catalog)
                selected = MODEL_MANAGER.recommend(validated, host(16))
                self.assertEqual(selected["lifecycleRole"], "lts")
                with patch.object(
                    MODEL_MANAGER,
                    "catalog_attestation_verification",
                    return_value={"promotionEligible": True},
                ):
                    self.assertFalse(
                        MODEL_MANAGER.catalog_deployment_eligible(anchor)
                    )

    def test_attested_hardware_cannot_be_substituted_during_promotion(self) -> None:
        model = copy.deepcopy(self.catalog["models"][0])
        exact = {
            "environmentKind": "wsl2",
            "architecture": "x86_64",
            "ramGiB": 96,
            "gpus": [
                {
                    "name": "NVIDIA GeForce RTX 5070 Ti",
                    "vramGiB": 16,
                }
            ],
        }
        self.assertTrue(
            MODEL_MANAGER._attested_hardware_matches_profile(model, exact)
        )
        substituted_gpu = copy.deepcopy(exact)
        substituted_gpu["gpus"][0]["name"] = "NVIDIA GeForce RTX 4090"
        substituted_environment = {**exact, "environmentKind": "native-linux"}
        self.assertFalse(
            MODEL_MANAGER._attested_hardware_matches_profile(
                model, substituted_gpu
            )
        )
        self.assertFalse(
            MODEL_MANAGER._attested_hardware_matches_profile(
                model, substituted_environment
            )
        )

    def test_no_cuda_recommendation_below_catalog_floor(self) -> None:
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(0)))

    def test_ram_and_disk_are_fail_closed(self) -> None:
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(16, ram=4)))
        self.assertIsNone(MODEL_MANAGER.recommend(self.catalog, host(16, disk=1)))

    def test_generated_profile_contains_only_selected_catalog_values(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        environment = MODEL_MANAGER.deployment_env(model)
        self.assertIn("QWEN_CATALOG_ID=qwen35-9b-q5km", environment)
        self.assertIn("QWEN_MODEL_FILE=Qwen3.5-9B-Q5_K_M.gguf", environment)
        self.assertIn("QWEN_MODEL_DISPLAY_NAME='Qwen3.5-9B Q5_K_M'", environment)
        self.assertIn("QWEN_CACHE_TYPE_K=q8_0", environment)
        self.assertIn("QWEN_CACHE_TYPE_V=q8_0", environment)
        self.assertNotIn("MODELPORT_AUTH_TOKEN", environment)

    def test_catalog_model_capacity_survives_every_mode_merge(self) -> None:
        profiles = json.loads(
            (ROOT_DIR / "config" / "runtime-profiles.json").read_text()
        )["profiles"]
        runtime_mapping = {
            "QWEN_CTX_SIZE": "contextTokens",
            "QWEN_RECOMMENDED_INPUT_TOKENS": "recommendedInputTokens",
            "QWEN_N_PREDICT": "maxOutputTokens",
            "QWEN_CACHE_RAM": "cacheRamMiB",
            "QWEN_CACHE_TYPE_K": "cacheTypeK",
            "QWEN_CACHE_TYPE_V": "cacheTypeV",
            "QWEN_BATCH_SIZE": "batchSize",
            "QWEN_UBATCH_SIZE": "ubatchSize",
        }
        for model in catalog_with_future_candidate(self.catalog)["models"]:
            base = MODEL_MANAGER.deployment_environment(model)
            for profile_name, profile in profiles.items():
                with self.subTest(model=model["id"], profile=profile_name):
                    overrides = profile["environment"]
                    self.assertTrue(set(runtime_mapping).isdisjoint(overrides))
                    merged = {**base, **overrides}
                    for environment_key, runtime_key in runtime_mapping.items():
                        self.assertEqual(
                            merged[environment_key], str(model["runtime"][runtime_key])
                        )
                    self.assertEqual(
                        merged["QWEN_PARALLEL"],
                        "2" if profile_name == "throughput" else "1",
                    )

    def test_provisional_legacy_record_is_not_a_validated_profile_match(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        self.assertEqual(model["status"], "provisional")
        generic = host(16)
        self.assertFalse(
            MODEL_MANAGER.matches_validated_hardware_profile(model, generic)
        )
        generic["gpus"][0]["name"] = "NVIDIA GeForce RTX 5070 Ti"
        self.assertFalse(
            MODEL_MANAGER.matches_validated_hardware_profile(model, generic)
        )

    def test_provisional_exact_host_can_only_recover_an_existing_selection(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        exact = host(
            16,
            ram=96,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        self.assertFalse(
            MODEL_MANAGER.matches_validated_hardware_profile(model, exact)
        )
        self.assertTrue(matches_recorded_hardware_profile(model, exact))

        with (
            patch.object(MODEL_MANAGER, "host_assessment", return_value=exact),
            patch.object(MODEL_MANAGER, "LOCAL_PROFILE", Path("/private/selection")),
            patch.object(MODEL_MANAGER.Path, "is_file", return_value=True),
            patch.object(MODEL_MANAGER, "selected_model", return_value=model),
            patch.object(
                MODEL_MANAGER,
                "deployment_values",
                return_value=MODEL_MANAGER.deployment_environment(model),
            ),
            patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
        ):
            payload = MODEL_MANAGER.admission_payload(
                self.catalog,
                model["id"],
                existing_selection=True,
            )

        self.assertFalse(payload["readyToDeploy"])
        self.assertFalse(payload["catalogRecoveryEligible"])
        self.assertTrue(payload["selectedConfigurationMatchesCatalog"])
        self.assertEqual(
            payload["selectedConfigurationMode"], "exact-current-projection"
        )
        self.assertTrue(payload["recoveryHardwareProfileMatch"])
        self.assertTrue(payload["recoveryResourcesAvailableNow"])
        self.assertTrue(payload["readyToStartExisting"])

    def test_busy_exact_host_can_admit_a_trusted_lts_replacement(self) -> None:
        catalog = catalog_with_validated_role(self.catalog, "lts")
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        assessed = host(
            16,
            ram=96,
            free_vram=3,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        assessed["availableRamGiB"] = 4
        with (
            patch.object(MODEL_MANAGER, "host_assessment", return_value=assessed),
            patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
            patch.object(
                MODEL_MANAGER,
                "catalog_attestation_verification",
                return_value={"promotionEligible": True},
            ),
        ):
            payload = MODEL_MANAGER.admission_payload(
                catalog,
                model["id"],
                replacement=True,
            )

        self.assertEqual(payload["mode"], "read-only-replacement-admission")
        self.assertFalse(payload["simulatedHost"])
        self.assertTrue(payload["fits"])
        self.assertFalse(payload["resourceAvailableNow"])
        self.assertFalse(payload["hostAdmissionPassed"])
        self.assertFalse(payload["readyToDeploy"])
        self.assertTrue(payload["replacementHostAdmissionPassed"])
        self.assertTrue(payload["catalogDeploymentEligible"])
        self.assertTrue(payload["readyToReplaceExisting"])

    def test_replacement_rejects_wrong_tier_one_hardware(self) -> None:
        catalog = catalog_with_validated_role(self.catalog, "lts")
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        substitutions = (
            host(16, name="NVIDIA GeForce RTX 4090"),
            host(
                16,
                name="NVIDIA GeForce RTX 5070 Ti",
                environment_kind="native-linux",
            ),
        )
        for assessed in substitutions:
            with self.subTest(
                host=assessed["environmentKind"],
                gpu=assessed["gpus"][0]["name"],
            ):
                with (
                    patch.object(
                        MODEL_MANAGER, "host_assessment", return_value=assessed
                    ),
                    patch.object(
                        MODEL_MANAGER,
                        "discover_host_acceptance",
                        return_value=None,
                    ),
                    patch.object(
                        MODEL_MANAGER,
                        "catalog_attestation_verification",
                        return_value={"promotionEligible": True},
                    ),
                ):
                    payload = MODEL_MANAGER.admission_payload(
                        catalog,
                        model["id"],
                        replacement=True,
                    )
                self.assertFalse(payload["hardwareProfileMatch"])
                self.assertFalse(payload["replacementHostAdmissionPassed"])
                self.assertFalse(payload["readyToReplaceExisting"])

    def test_replacement_requires_total_capacity_and_every_prerequisite(self) -> None:
        catalog = catalog_with_validated_role(self.catalog, "lts")
        model = MODEL_MANAGER.model_by_id(catalog, "qwen35-9b-q5km")
        exact = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        cases: dict[str, dict] = {}
        for name in (
            "docker",
            "compose",
            "compose-configuration",
            "nvidia-runtime",
            "curl",
            "python",
            "flock",
            "multi-gpu",
            "vram",
            "ram",
            "disk",
        ):
            assessed = copy.deepcopy(exact)
            if name == "docker":
                assessed["docker"]["available"] = False
            elif name == "compose":
                assessed["dockerCompose"]["available"] = False
            elif name == "compose-configuration":
                assessed["dockerCompose"]["configurationCompatible"] = False
            elif name == "nvidia-runtime":
                assessed["nvidiaContainerRuntime"] = False
            elif name == "curl":
                assessed["curl"]["available"] = False
            elif name == "python":
                assessed["python"]["supported"] = False
            elif name == "flock":
                assessed["commands"]["flock"] = False
            elif name == "multi-gpu":
                assessed["gpus"].append(
                    {
                        "index": 1,
                        "name": "NVIDIA GeForce RTX 5070 Ti",
                        "vramGiB": 16,
                        "freeVramGiB": 16,
                        "driver": "test-driver",
                    }
                )
                assessed["totalVramGiB"] = 32
                assessed["totalFreeVramGiB"] = 32
            elif name == "vram":
                assessed = host(11, name="NVIDIA GeForce RTX 5070 Ti")
            elif name == "ram":
                assessed["ramGiB"] = 32
                assessed["availableRamGiB"] = 32
            else:
                assessed["freeDiskGiB"] = 1
            cases[name] = assessed

        for name, assessed in cases.items():
            with self.subTest(name=name):
                with (
                    patch.object(
                        MODEL_MANAGER, "host_assessment", return_value=assessed
                    ),
                    patch.object(
                        MODEL_MANAGER,
                        "discover_host_acceptance",
                        return_value=None,
                    ),
                    patch.object(
                        MODEL_MANAGER,
                        "catalog_attestation_verification",
                        return_value={"promotionEligible": True},
                    ),
                ):
                    payload = MODEL_MANAGER.admission_payload(
                        catalog,
                        model["id"],
                        replacement=True,
                    )
                self.assertFalse(payload["replacementHostAdmissionPassed"])
                self.assertFalse(payload["readyToReplaceExisting"])

    def test_replacement_requires_current_trusted_lts_automatic_target(self) -> None:
        exact = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        cases = []

        provisional = copy.deepcopy(self.catalog)
        cases.append(("provisional", provisional, provisional["models"][0], True))

        unsigned = catalog_with_validated_role(self.catalog, "lts")
        cases.append(
            (
                "missing-trusted-signature",
                unsigned,
                unsigned["models"][0],
                False,
            )
        )

        manual = catalog_with_validated_role(self.catalog, "lts")
        manual["models"][0]["deploymentEligibility"]["automatic"] = False
        validate_catalog(manual)
        cases.append(("manual-lts", manual, manual["models"][0], True))

        for role in ("candidate", "rollback"):
            catalog = catalog_with_validated_role(self.catalog, role)
            model = MODEL_MANAGER.model_by_id(
                catalog, f"fixture-validated-{role}"
            )
            cases.append((role, catalog, model, True))

        for name, catalog, model, signature_valid in cases:
            with self.subTest(name=name):
                verification = (
                    {"promotionEligible": True} if signature_valid else None
                )
                with (
                    patch.object(MODEL_MANAGER, "host_assessment", return_value=exact),
                    patch.object(
                        MODEL_MANAGER,
                        "discover_host_acceptance",
                        return_value=None,
                    ),
                    patch.object(
                        MODEL_MANAGER,
                        "catalog_attestation_verification",
                        return_value=verification,
                    ),
                ):
                    payload = MODEL_MANAGER.admission_payload(
                        catalog,
                        model["id"],
                        replacement=True,
                    )
                self.assertFalse(payload["catalogDeploymentEligible"])
                self.assertFalse(payload["readyToReplaceExisting"])

    def test_validated_rollback_source_is_catalog_recovery_eligible(self) -> None:
        catalog = catalog_with_validated_role(self.catalog, "rollback")
        model = MODEL_MANAGER.model_by_id(
            catalog, "fixture-validated-rollback"
        )
        exact = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        with (
            patch.object(MODEL_MANAGER, "host_assessment", return_value=exact),
            patch.object(MODEL_MANAGER, "LOCAL_PROFILE", Path("/private/selection")),
            patch.object(MODEL_MANAGER.Path, "is_file", return_value=True),
            patch.object(MODEL_MANAGER, "selected_model", return_value=model),
            patch.object(
                MODEL_MANAGER,
                "deployment_values",
                return_value=MODEL_MANAGER.deployment_environment(model),
            ),
            patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
            patch.object(
                MODEL_MANAGER,
                "catalog_attestation_verification",
                return_value={"promotionEligible": True},
            ),
        ):
            payload = MODEL_MANAGER.admission_payload(
                catalog,
                model["id"],
                existing_selection=True,
            )

        self.assertTrue(payload["catalogRecoveryEligible"])
        self.assertFalse(payload["catalogDeploymentEligible"])
        self.assertTrue(payload["readyToStartExisting"])

    def test_catalog_recovery_eligibility_requires_role_and_live_trust(self) -> None:
        rollback_catalog = catalog_with_validated_role(self.catalog, "rollback")
        rollback = MODEL_MANAGER.model_by_id(
            rollback_catalog, "fixture-validated-rollback"
        )
        with patch.object(
            MODEL_MANAGER,
            "catalog_attestation_verification",
            return_value=None,
        ):
            self.assertFalse(MODEL_MANAGER.catalog_recovery_eligible(rollback))

        for role in ("lts", "candidate"):
            with self.subTest(role=role):
                catalog = catalog_with_validated_role(self.catalog, role)
                model_id = (
                    "qwen35-9b-q5km"
                    if role == "lts"
                    else "fixture-validated-candidate"
                )
                model = MODEL_MANAGER.model_by_id(catalog, model_id)
                with patch.object(
                    MODEL_MANAGER,
                    "catalog_attestation_verification",
                    return_value={"promotionEligible": True},
                ):
                    self.assertFalse(
                        MODEL_MANAGER.catalog_recovery_eligible(model)
                    )

    def test_replacement_cli_is_mutually_exclusive_and_has_no_simulation(self) -> None:
        with patch.object(
            sys,
            "argv",
            [
                "model-manager.py",
                "admit",
                "--model",
                "qwen35-9b-q5km",
                "--replacement",
            ],
        ):
            args = MODEL_MANAGER.parse_args()
        self.assertTrue(args.replacement)
        self.assertFalse(args.existing_selection)

        invalid_arguments = (
            ("--replacement", "--existing-selection"),
            ("--replacement", "--vram-gib", "16"),
            ("--replacement", "--ram-gib", "96"),
        )
        for suffix in invalid_arguments:
            with self.subTest(arguments=suffix):
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "model-manager.py",
                            "admit",
                            "--model",
                            "qwen35-9b-q5km",
                            *suffix,
                        ],
                    ),
                    patch.object(sys, "stderr", StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    MODEL_MANAGER.parse_args()

        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            MODEL_MANAGER.admission_payload(
                self.catalog,
                "qwen35-9b-q5km",
                existing_selection=True,
                replacement=True,
            )

    def test_existing_selection_recovery_treats_current_free_capacity_as_advisory(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        busy_exact_host = host(
            16,
            ram=96,
            free_vram=9.9,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        with (
            patch.object(MODEL_MANAGER, "host_assessment", return_value=busy_exact_host),
            patch.object(MODEL_MANAGER, "LOCAL_PROFILE", Path("/private/selection")),
            patch.object(MODEL_MANAGER.Path, "is_file", return_value=True),
            patch.object(MODEL_MANAGER, "selected_model", return_value=model),
            patch.object(
                MODEL_MANAGER,
                "deployment_values",
                return_value=MODEL_MANAGER.deployment_environment(model),
            ),
            patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
        ):
            payload = MODEL_MANAGER.admission_payload(
                self.catalog,
                model["id"],
                existing_selection=True,
            )

        self.assertFalse(payload["readyToDeploy"])
        self.assertFalse(payload["resourceAvailableNow"])
        self.assertFalse(payload["recoveryResourcesAvailableNow"])
        self.assertTrue(payload["recoveryHostAdmissionPassed"])
        self.assertTrue(payload["readyToStartExisting"])
        self.assertTrue(
            any(
                "Existing-selection recovery may still attempt" in caveat
                for caveat in payload["caveats"]
            )
        )

    def test_legacy_selection_requires_explicit_migration_before_recovery(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        exact = host(
            16,
            ram=96,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        values = MODEL_MANAGER.deployment_environment(model)
        values.pop("QWEN_CACHE_TYPE_K")
        values.pop("QWEN_CACHE_TYPE_V")
        with (
            patch.object(MODEL_MANAGER, "host_assessment", return_value=exact),
            patch.object(MODEL_MANAGER, "LOCAL_PROFILE", Path("/private/selection")),
            patch.object(MODEL_MANAGER.Path, "is_file", return_value=True),
            patch.object(MODEL_MANAGER, "selected_model", return_value=model),
            patch.object(MODEL_MANAGER, "deployment_values", return_value=values),
            patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
        ):
            payload = MODEL_MANAGER.admission_payload(
                self.catalog,
                model["id"],
                existing_selection=True,
            )
        self.assertFalse(payload["selectedConfigurationMatchesCatalog"])
        self.assertEqual(
            payload["selectedConfigurationMode"],
            "legacy-compatible-current-defaults",
        )
        self.assertFalse(payload["readyToStartExisting"])
        self.assertTrue(
            any("./stack migrate --yes" in caveat for caveat in payload["caveats"])
        )

    def test_existing_selection_recovery_rejects_profile_or_host_substitution(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        exact = host(
            16,
            ram=96,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        substitutions = (
            (
                "profile",
                exact,
                {**MODEL_MANAGER.deployment_environment(model), "QWEN_CTX_SIZE": "4096"},
            ),
            (
                "gpu",
                host(16, ram=96, name="NVIDIA GeForce RTX 4090"),
                MODEL_MANAGER.deployment_environment(model),
            ),
            (
                "environment",
                host(
                    16,
                    ram=96,
                    name="NVIDIA GeForce RTX 5070 Ti",
                    environment_kind="native-linux",
                ),
                MODEL_MANAGER.deployment_environment(model),
            ),
        )
        for name, assessed, values in substitutions:
            with self.subTest(name=name):
                with (
                    patch.object(MODEL_MANAGER, "host_assessment", return_value=assessed),
                    patch.object(MODEL_MANAGER, "LOCAL_PROFILE", Path("/private/selection")),
                    patch.object(MODEL_MANAGER.Path, "is_file", return_value=True),
                    patch.object(MODEL_MANAGER, "selected_model", return_value=model),
                    patch.object(MODEL_MANAGER, "deployment_values", return_value=values),
                    patch.object(MODEL_MANAGER, "discover_host_acceptance", return_value=None),
                ):
                    payload = MODEL_MANAGER.admission_payload(
                        self.catalog,
                        model["id"],
                        existing_selection=True,
                    )
                self.assertFalse(payload["readyToStartExisting"])

    def test_multi_gpu_does_not_aggregate_vram_or_enable_automatic_deploy(self) -> None:
        assessed = host(16)
        assessed["gpus"].append(
            {
                "index": 1,
                "name": "test",
                "vramGiB": 16,
                "freeVramGiB": 16,
                "driver": "test-driver",
            }
        )
        assessed["totalVramGiB"] = 32
        assessed["totalFreeVramGiB"] = 32
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertEqual(plan["recommendation"]["id"], "qwen35-9b-q5km")
        self.assertFalse(plan["automaticDeploymentSupported"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])

    def test_busy_gpu_suppresses_deployment_commands(self) -> None:
        assessed = host(
            16,
            free_vram=3,
            name="NVIDIA GeForce RTX 5070 Ti",
        )
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertTrue(plan["fits"])
        self.assertFalse(plan["resourceAvailableNow"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])

    def test_capacity_override_is_never_deployment_authority(self) -> None:
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        plan = MODEL_MANAGER.plan_for_host(
            Namespace(model=None),
            self.catalog,
            assessed,
            simulation=True,
        )
        self.assertTrue(plan["fits"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])
        self.assertTrue(plan["simulatedHost"])

    def test_available_ram_is_part_of_current_resource_admission(self) -> None:
        assessed = host(16, ram=96, name="NVIDIA GeForce RTX 5070 Ti")
        assessed["availableRamGiB"] = 4
        plan = MODEL_MANAGER.plan_for_host(Namespace(model=None), self.catalog, assessed)
        self.assertTrue(plan["fits"])
        self.assertFalse(plan["resourceAvailableNow"])
        self.assertFalse(plan["readyToDeploy"])

    def test_plan_distinguishes_provisional_record_from_host_acceptance(self) -> None:
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        args = Namespace(model=None)
        plan = MODEL_MANAGER.plan_for_host(args, self.catalog, assessed)
        self.assertEqual(
            plan["evidenceStatus"], "estimated-on-this-host"
        )
        self.assertEqual(
            plan["hostAcceptanceStatus"], "not-evaluated-by-read-only-plan"
        )
        self.assertEqual(
            plan["catalogEvidenceStatus"], "provisional-legacy-profile"
        )
        self.assertFalse(plan["catalogDeploymentEligible"])
        self.assertFalse(plan["readyToDeploy"])
        self.assertIsNone(plan["actionPlan"])
        self.assertEqual(
            plan["hostAcceptancePolicy"]["supportedSchemaVersions"], [4, 5]
        )
        self.assertEqual(
            plan["hostAcceptancePolicy"]["standaloneSchemaVersion"], 4
        )
        self.assertEqual(
            plan["hostAcceptancePolicy"]["transactionBoundSchemaVersion"], 5
        )
        self.assertTrue(plan["artifactPolicy"]["licenseReviewRequired"])

    def test_upstream_header_parser_normalizes_hugging_face_evidence(self) -> None:
        headers = MODEL_MANAGER.parse_http_headers(
            'HTTP/2 302\nX-Repo-Commit: abc123\nX-Linked-Etag: "def456"\n'
        )
        self.assertEqual(headers["x-repo-commit"], "abc123")
        self.assertEqual(headers["x-linked-etag"], "def456")

    def test_current_configuration_acceptance_is_host_bound(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        artifact = model["artifacts"][0]
        local_identity = {
            "schemaVersion": 1,
            "verification": "secure-stat-and-integrity-stamp",
            "fingerprint": "test-stat-fingerprint",
        }
        now = datetime(2026, 7, 26, 10, 1, tzinfo=timezone.utc)
        evidence = {
            "schemaVersion": 4,
            "evidenceId": "test",
            "status": "passed",
            "exitCode": 0,
            "failedAtStep": None,
            "terminalStep": "Direct reasoning",
            "durationSeconds": 60,
            "mode": "quick",
            "profile": "latency",
            "startedAt": "2026-07-26T09:59:00+00:00",
            "finishedAt": "2026-07-26T10:00:00+00:00",
            "gitCommit": "a" * 40,
            "gitState": "clean",
            "catalogModelId": model["id"],
            "host": {
                "platform": "linux",
                "environmentKind": "wsl2",
                "architecture": "x86_64",
                "ramGiB": 96,
                "gpus": [
                    {
                        "index": 0,
                        "name": "NVIDIA GeForce RTX 5070 Ti",
                        "vramGiB": 16,
                        "driver": "test-driver",
                    }
                ],
                "fingerprintType": MODEL_MANAGER.HOST_FINGERPRINT_TYPE,
                "fingerprint": MODEL_MANAGER.host_fingerprint(),
            },
            "artifact": {
                "filename": artifact["filename"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "modelRevision": model["modelRevision"],
                "artifactRevision": model["artifactRevision"],
                "integrityVerified": True,
                "localIdentity": local_identity,
            },
            "runtime": {
                "containerName": model["id"],
                "configuredImage": MODEL_MANAGER.configured_runtime_image(),
                "imageId": "sha256:test",
                "containerConfigSha256": "runtime-config-test",
            },
            "configuration": MODEL_MANAGER.acceptance_configuration(model),
            "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
            "privacy": "synthetic acceptance fixture",
        }
        evidence["validationInput"] = MODEL_MANAGER.validation_input(
            self.catalog, model, evidence["configuration"]
        )
        step_names = list(expected_steps("quick"))
        evidence["run"] = {
            "schemaVersion": RUN_SCHEMA_VERSION,
            "kind": RUN_KIND,
            "runId": "test-run",
            "mode": "quick",
            "runner": {
                "path": RUNNER_PATH,
                "sha256": evidence["configuration"]["acceptanceSuiteSha256"],
                "capabilitySha256": "c" * 64,
            },
            "plan": {
                "stepNames": step_names,
                "sha256": step_plan_sha256("quick"),
            },
            "stepResults": [
                {
                    "ordinal": ordinal,
                    "name": name,
                    "startedAt": evidence["startedAt"],
                    "finishedAt": evidence["startedAt"],
                    "durationSeconds": 0,
                    "exitCode": 0,
                    "status": "passed",
                }
                for ordinal, name in enumerate(step_names, start=1)
            ],
            "failedAtStep": None,
            "terminalStep": step_names[-1],
            "manifest": {
                "schemaVersion": RUN_SCHEMA_VERSION,
                "sourcePath": "logs/acceptance/test.run.json",
                "sourceSha256": "a" * 64,
                "selfSha256": "b" * 64,
            },
        }
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        original_live_container = MODEL_MANAGER.live_container
        original_live_sha256 = MODEL_MANAGER.live_runtime_sha256
        original_local_identity = MODEL_MANAGER.local_artifact_identity
        original_manifest_match = MODEL_MANAGER.acceptance_run_manifest_matches
        MODEL_MANAGER.live_container = lambda _name: {
            "Image": "sha256:test",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        }
        MODEL_MANAGER.live_runtime_sha256 = lambda _container: "runtime-config-test"
        MODEL_MANAGER.local_artifact_identity = lambda *_args, **_kwargs: local_identity
        MODEL_MANAGER.acceptance_run_manifest_matches = lambda _evidence: True
        try:
            self.assertTrue(
                MODEL_MANAGER.acceptance_matches_host(
                    model, assessed, evidence, now=now
                )
            )
            MODEL_MANAGER.local_artifact_identity = lambda *_args, **_kwargs: {
                **local_identity,
                "fingerprint": "drifted-stat-fingerprint",
            }
            self.assertFalse(
                MODEL_MANAGER.acceptance_matches_host(
                    model, assessed, evidence, now=now
                )
            )
            MODEL_MANAGER.local_artifact_identity = lambda *_args, **_kwargs: local_identity
        finally:
            MODEL_MANAGER.live_container = original_live_container
            MODEL_MANAGER.live_runtime_sha256 = original_live_sha256
            MODEL_MANAGER.local_artifact_identity = original_local_identity
            MODEL_MANAGER.acceptance_run_manifest_matches = original_manifest_match
        acceptance = {
            "status": "passed-current-configuration",
            "evidence": "logs/acceptance/test.json",
            "finishedAt": evidence["finishedAt"],
            "mode": "quick",
        }
        plan = MODEL_MANAGER.plan_for_host(
            Namespace(model=None), self.catalog, assessed, acceptance
        )
        self.assertEqual(plan["evidenceStatus"], "validated-on-this-host")
        self.assertEqual(
            plan["hostAcceptanceStatus"], "passed-current-configuration"
        )

        evidence["host"]["gpus"][0]["driver"] = "changed-driver"
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        MODEL_MANAGER.live_container = lambda _name: {
            "Image": "sha256:test",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        }
        MODEL_MANAGER.live_runtime_sha256 = lambda _container: "runtime-config-test"
        MODEL_MANAGER.local_artifact_identity = lambda *_args, **_kwargs: local_identity
        MODEL_MANAGER.acceptance_run_manifest_matches = lambda _evidence: True
        try:
            self.assertFalse(
                MODEL_MANAGER.acceptance_matches_host(
                    model, assessed, evidence, now=now
                )
            )
        finally:
            MODEL_MANAGER.live_container = original_live_container
            MODEL_MANAGER.live_runtime_sha256 = original_live_sha256
            MODEL_MANAGER.local_artifact_identity = original_local_identity
            MODEL_MANAGER.acceptance_run_manifest_matches = original_manifest_match

    def test_acceptance_expires_and_rejects_future_timestamps(self) -> None:
        model = MODEL_MANAGER.model_by_id(self.catalog, "qwen35-9b-q5km")
        assessed = host(16, name="NVIDIA GeForce RTX 5070 Ti")
        artifact = model["artifacts"][0]
        now = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
        evidence = {
            "schemaVersion": 4,
            "evidenceId": "test",
            "status": "passed",
            "exitCode": 0,
            "failedAtStep": None,
            "terminalStep": "Direct reasoning",
            "durationSeconds": 60,
            "mode": "quick",
            "profile": "latency",
            "startedAt": (
                now - timedelta(days=31, minutes=1)
            ).isoformat(),
            "finishedAt": (now - timedelta(days=31)).isoformat(),
            "catalogModelId": model["id"],
            "host": {
                "platform": "linux",
                "environmentKind": "wsl2",
                "architecture": "x86_64",
                "ramGiB": 96,
                "gpus": assessed["gpus"],
                "fingerprintType": MODEL_MANAGER.HOST_FINGERPRINT_TYPE,
                "fingerprint": MODEL_MANAGER.host_fingerprint(),
            },
            "artifact": {
                "filename": artifact["filename"],
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
                "modelRevision": model["modelRevision"],
                "artifactRevision": model["artifactRevision"],
                "integrityVerified": True,
            },
            "runtime": {
                "configuredImage": MODEL_MANAGER.configured_runtime_image(),
                "imageId": "sha256:test",
            },
            "configuration": MODEL_MANAGER.acceptance_configuration(model),
            "freshnessPolicy": {"maxAgeDays": 30, "futureSkewSeconds": 300},
        }
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertFalse(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )

        evidence["startedAt"] = now.isoformat()
        evidence["finishedAt"] = (now + timedelta(minutes=6)).isoformat()
        evidence["durationSeconds"] = 360
        evidence["selfSha256"] = MODEL_MANAGER.payload_sha256(evidence)
        self.assertFalse(
            MODEL_MANAGER.acceptance_matches_host(
                model, assessed, evidence, now=now
            )
        )

    def test_schema_v5_qualification_requires_exact_completed_receipt(self) -> None:
        transaction_id = "0dbdb868-b62f-4471-84b4-0198a0700f09"
        ordinal = 6
        binding = {
            "policyId": "local-inference-stack/rollout-qualification-binding-v1",
            "transactionId": transaction_id,
            "operation": "upgrade",
            "rolloutPlanSha256": "1" * 64,
            "actionOrdinal": ordinal,
            "actionKind": "target-full",
            "rollbackSpecSha256": "2" * 64,
            "sourceCatalogSpecSha256": "3" * 64,
            "targetCatalogSpecSha256": "4" * 64,
            "performancePolicySha256": "a" * 64,
            "modelPortSourceIdentitySha256": "b" * 64,
            "qualificationInputSha256": "c" * 64,
        }
        basename = f"qualification-{transaction_id}-{ordinal}"
        evidence = {
            "schemaVersion": 5,
            "evidenceId": basename,
            "selfSha256": "5" * 64,
            "rolloutBinding": binding,
            "frozenInputs": {"liveRuntimeIdentitySha256": "6" * 64},
            "configuration": {"profile": "latency"},
            "run": {
                "stepResults": [{"status": "passed"}],
                "manifest": {
                    "sourcePath": f"logs/acceptance/{basename}.run.json",
                    "sourceSha256": "7" * 64,
                    "selfSha256": "8" * 64,
                },
            },
        }
        receipt = {
            "evidenceSha256": "9" * 64,
            "runManifestPath": f"logs/acceptance/{basename}.run.json",
            "runManifestSha256": "7" * 64,
            "runManifestSelfSha256": "8" * 64,
            "stepResultsSha256": MODEL_MANAGER.sha256_document(
                evidence["run"]["stepResults"]
            ),
            "configurationSha256": MODEL_MANAGER.sha256_document(
                evidence["configuration"]
            ),
            "runtimeIdentitySha256": "6" * 64,
            "rolloutBindingSha256": MODEL_MANAGER.sha256_document(binding),
        }
        store = MagicMock()
        store.completed_upgrade_qualification.return_value = receipt
        with (
            patch.object(
                MODEL_MANAGER,
                "read_secure_evidence_with_sha256",
                return_value=(evidence, "9" * 64),
            ),
            patch.object(MODEL_MANAGER, "TransactionStore", return_value=store),
        ):
            self.assertTrue(
                MODEL_MANAGER.completed_rollout_qualification_matches(evidence)
            )
            store.completed_upgrade_qualification.side_effect = RecoveryError(
                "transaction is not completed"
            )
            self.assertFalse(
                MODEL_MANAGER.completed_rollout_qualification_matches(evidence)
            )


if __name__ == "__main__":
    unittest.main()
