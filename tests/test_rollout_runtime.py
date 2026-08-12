"""Fail-closed tests for the rollback host-evidence adapter.

These tests deliberately keep Docker, Git, GPU discovery, and the live runtime
behind mocks.  Filesystem assertions use only a temporary project root.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.env_utils import parse_env_file  # noqa: E402
from scripts.runtime_identity import normalized_live_runtime  # noqa: E402

from local_inference_stack import configuration, rollout_runtime  # noqa: E402
from local_inference_stack.deployment import CatalogDeploymentSpec  # noqa: E402
from local_inference_stack.materials import (  # noqa: E402
    MaterialError,
    canonical_sha256,
    sha256_file,
)
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.result import (  # noqa: E402
    ConfigError,
    IntegrityError,
    RecoveryError,
)
from local_inference_stack.runner import RunResult  # noqa: E402
from local_inference_stack.transactions import (  # noqa: E402
    recovery_original_is_safe,
)


TRANSACTION_ID = "0dbdb868-b62f-4471-84b4-0198a0700f09"
CAPTURED_AT = "2026-08-12T01:02:03Z"
GIT_REVISION = "1" * 40
COMPOSE_SHA256 = "3" * 64
IMAGE_REFERENCE = "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:" + "4" * 64
IMAGE_ID = "sha256:" + "5" * 64
HOST_IDENTITY = {
    "fingerprintType": "machine-id-sha256-v1",
    "fingerprint": "6" * 64,
    "environmentKind": "wsl2",
    "architecture": "x86_64",
}
CONTROLLER_MATERIALS = {"compose.yaml": "7" * 64}


def _result(argv: list[str], payload: object, *, returncode: int = 0) -> RunResult:
    return RunResult(tuple(argv), returncode, json.dumps(payload), "")


class RolloutRuntimeFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)

        baseline_catalog = json.loads(
            (ROOT / "catalog" / "models.json").read_text(encoding="utf-8")
        )
        lts = baseline_catalog["models"][0]
        model = copy.deepcopy(lts)
        model.update(
            {
                "id": "qwen35-9b-rollback",
                "displayName": "Qwen3.5-9B rollback anchor",
                "status": "validated",
                "lifecycleRole": "rollback",
                "modelDirectory": "qwen3.5-9b-rollback",
                "servedModelId": "qwen3.5-9b-rollback",
                "validation": "Test-only immutable rollback anchor.",
                "validationAttestation": {
                    "mode": "full",
                    "tool": "minisign",
                    "payloadSha256": "8" * 64,
                    "trustedKeySha256": "9" * 64,
                    "documentPath": "deployments/test-anchor/evidence.json",
                    "signaturePath": "deployments/test-anchor/evidence.minisig",
                },
            }
        )
        model["deploymentEligibility"] = {
            "automatic": False,
            "reason": "rollback anchors are never automatic new deployments",
        }
        self.artifact_body = b"test-only-anchor-artifact\n"
        artifact = model["artifacts"][0]
        artifact.update(
            {
                "filename": "test-anchor.gguf",
                "bytes": len(self.artifact_body),
                "sha256": hashlib.sha256(self.artifact_body).hexdigest(),
                "url": (
                    "https://huggingface.co/"
                    f"{model['artifactRepository']}/resolve/"
                    f"{model['artifactRevision']}/test-anchor.gguf?download=true"
                ),
            }
        )
        baseline_catalog["models"].append(model)
        self.model = model
        self.catalog_spec = CatalogDeploymentSpec.from_catalog_model(model)

        (self.root / "catalog").mkdir(parents=True)
        (self.root / "catalog" / "models.json").write_text(
            json.dumps(baseline_catalog), encoding="utf-8"
        )
        (self.root / "config").mkdir()
        (self.root / "config" / "runtime-profiles.json").write_text(
            (ROOT / "config" / "runtime-profiles.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        artifact_path = (
            self.root
            / "models"
            / self.catalog_spec.model_directory
            / self.catalog_spec.artifacts[0].filename
        )
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(self.artifact_body)
        artifact_path.chmod(0o600)
        self.artifact_path = artifact_path

        self.evidence_relative = Path("logs/acceptance/source-quick.json")
        self.evidence_path = self.root / self.evidence_relative
        self.evidence_path.parent.mkdir(parents=True)
        self._write_evidence()

        self.selection = configuration.catalog_deployment_environment(
            self.model, uid=os.getuid(), gid=os.getgid()
        )
        self.runtime_configuration = normalized_live_runtime(
            {
                "Image": IMAGE_ID,
                "Config": {
                    "Image": IMAGE_REFERENCE,
                    "User": f"{os.getuid()}:{os.getgid()}",
                    "Entrypoint": ["/app/llama-server"],
                    "Cmd": ["--model", "/models/test-anchor.gguf"],
                    "WorkingDir": "/app",
                    "Healthcheck": {"Test": ["CMD", "true"]},
                    "Env": ["QWEN_PARALLEL=1"],
                },
                "HostConfig": {
                    "Privileged": False,
                    "ReadonlyRootfs": True,
                    "CapAdd": [],
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 512,
                    "Init": True,
                    "ShmSize": 1073741824,
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=65536k"},
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "LogConfig": {"Type": "json-file", "Config": {}},
                    "PublishAllPorts": False,
                    "PortBindings": {
                        "8080/tcp": [
                            {"HostIp": "127.0.0.1", "HostPort": "18080"}
                        ]
                    },
                    "NetworkMode": "modelport_default",
                    "DeviceRequests": [
                        {
                            "Driver": "nvidia",
                            "Count": -1,
                            "DeviceIDs": [],
                            "Capabilities": [["gpu"]],
                            "Options": {},
                        }
                    ],
                },
                "NetworkSettings": {
                    "Networks": {
                        "modelport_default": {
                            "Aliases": ["qwen35-9b-rollback"]
                        }
                    }
                },
                "Mounts": [],
            }
        )
        self.original = {
            "healthy": True,
            "containerHealthy": True,
            "profile": "latency",
            "containerName": self.selection["QWEN_CONTAINER_NAME"],
            "runtimeIdentity": {
                "sha256": canonical_sha256(self.runtime_configuration),
                "configuration": copy.deepcopy(self.runtime_configuration),
            },
            "deploymentProfile": {
                "present": True,
                "format": "allowlisted-env-v1",
                "sha256": canonical_sha256(self.selection),
                "values": copy.deepcopy(self.selection),
                "containsCredentials": False,
            },
            "capturedWithoutSecrets": True,
        }
        self.admission = {
            "catalogRecoveryEligible": True,
            "readyToStartExisting": True,
            "hostAcceptanceEvidence": {
                "evidence": self.evidence_relative.as_posix(),
                "evidenceSha256": sha256_file(self.evidence_path),
                "evidenceSelfSha256": self._evidence_document()["selfSha256"],
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _evidence_document(
        self,
        *,
        model_id: str | None = None,
        status: str = "passed",
        mode: str = "quick",
    ) -> dict[str, object]:
        document = {
            "schemaVersion": 4,
            "catalogModelId": model_id or self.model["id"],
            "status": status,
            "mode": mode,
            "finishedAt": "2026-08-12T01:00:00Z",
        }
        document["selfSha256"] = canonical_sha256(document)
        return document

    def _write_evidence(
        self,
        *,
        model_id: str | None = None,
        status: str = "passed",
        mode: str = "quick",
        path: Path | None = None,
    ) -> Path:
        target = path or self.evidence_path
        document = self._evidence_document(
            model_id=model_id, status=status, mode=mode
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        target.chmod(0o600)
        return target

    def _capture(self, **overrides: object):
        arguments = {
            "transaction_id": TRANSACTION_ID,
            "captured_at": CAPTURED_AT,
            "original": self.original,
            "source_admission": self.admission,
        }
        arguments.update(overrides)
        with (
            patch.object(
                rollout_runtime,
                "controller_snapshot",
                return_value=(GIT_REVISION, CONTROLLER_MATERIALS),
            ),
            patch.object(
                rollout_runtime,
                "_rendered_compose_sha256",
                return_value=COMPOSE_SHA256,
            ),
            patch.object(
                rollout_runtime, "_host_identity", return_value=HOST_IDENTITY
            ),
        ):
            return rollout_runtime.capture_rollback_spec(self.paths, **arguments)

    def _verify_run(
        self,
        argv: list[str],
        *,
        admission: dict[str, object] | None = None,
        admission_returncode: int = 0,
        image_id: str = IMAGE_ID,
        image_returncode: int = 0,
        calls: list[tuple[str, ...]] | None = None,
        **_kwargs: object,
    ) -> RunResult:
        if calls is not None:
            calls.append(tuple(argv))
        if argv[:3] == ["python3", "scripts/model-manager.py", "admit"]:
            return _result(
                argv,
                admission
                if admission is not None
                else {
                    "mode": "read-only-existing-selection-admission",
                    "recommendation": self.model,
                    "catalogRecoveryEligible": True,
                    "recoveryHostAdmissionPassed": True,
                },
                returncode=admission_returncode,
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            return _result(
                argv, [{"Id": image_id}], returncode=image_returncode
            )
        self.fail(f"unexpected external command in unit test: {argv}")

    def _verify(
        self,
        spec,
        *,
        run_side_effect=None,
        controller=(GIT_REVISION, CONTROLLER_MATERIALS),
        host=HOST_IDENTITY,
        compose=COMPOSE_SHA256,
    ):
        with (
            patch.object(
                rollout_runtime,
                "run",
                side_effect=run_side_effect or self._verify_run,
            ),
            patch.object(
                rollout_runtime, "controller_snapshot", return_value=controller
            ),
            patch.object(rollout_runtime, "_host_identity", return_value=host),
            patch.object(
                rollout_runtime,
                "_rendered_compose_sha256",
                return_value=compose,
            ),
        ):
            return rollout_runtime.verify_rollback_spec(self.paths, spec)


class CaptureRollbackSpecTests(RolloutRuntimeFixture):
    def test_capture_binds_exact_source_and_local_evidence(self) -> None:
        spec = self._capture()
        document = spec.document()
        self.assertEqual(document["selection"]["values"], self.selection)
        self.assertEqual(document["runtimeProfile"]["name"], "latency")
        self.assertEqual(document["artifacts"][0]["sha256"], sha256_file(self.artifact_path))
        self.assertEqual(
            document["acceptance"]["evidenceSha256"], sha256_file(self.evidence_path)
        )
        self.assertEqual(document["controller"]["gitCommit"], GIT_REVISION)

    def test_capture_rejects_unhealthy_or_non_latency_source(self) -> None:
        cases = {
            "unhealthy": {**self.original, "healthy": False},
            "container-unhealthy": {
                **self.original,
                "containerHealthy": False,
            },
            "throughput": {**self.original, "profile": "throughput"},
        }
        for name, original in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(RecoveryError):
                    self._capture(original=original)

    def test_capture_rejects_non_exact_selection_even_with_valid_digest(self) -> None:
        original = copy.deepcopy(self.original)
        original["deploymentProfile"]["values"]["QWEN_MODEL_DISPLAY_NAME"] = "substitute"
        original["deploymentProfile"]["sha256"] = canonical_sha256(
            original["deploymentProfile"]["values"]
        )
        self.assertTrue(recovery_original_is_safe(original))
        with self.assertRaisesRegex(RecoveryError, "strict Catalog projection"):
            self._capture(original=original)

    def test_capture_rejects_missing_or_extra_selection_keys(self) -> None:
        for name, mutate in {
            "missing": lambda values: values.pop("QWEN_CACHE_TYPE_K"),
            "extra": lambda values: values.update({"UNREVIEWED": "1"}),
        }.items():
            with self.subTest(case=name):
                original = copy.deepcopy(self.original)
                mutate(original["deploymentProfile"]["values"])
                original["deploymentProfile"]["sha256"] = canonical_sha256(
                    original["deploymentProfile"]["values"]
                )
                with self.assertRaises(RecoveryError):
                    self._capture(original=original)

    def test_capture_requires_validated_rollback_role_and_both_admission_gates(self) -> None:
        cases = (
            ("status", {**self.model, "status": "provisional"}, self.admission),
            ("role", {**self.model, "lifecycleRole": "candidate"}, self.admission),
            (
                "catalog-trust",
                self.model,
                {**self.admission, "catalogRecoveryEligible": False},
            ),
            (
                "existing-start",
                self.model,
                {**self.admission, "readyToStartExisting": False},
            ),
        )
        for name, model, admission in cases:
            with self.subTest(case=name):
                with (
                    patch.object(
                        rollout_runtime,
                        "_catalog_model",
                        return_value=(model, self.catalog_spec),
                    ),
                    self.assertRaisesRegex(RecoveryError, "trusted validated rollback"),
                ):
                    self._capture(source_admission=admission)


class LocalEvidenceTests(RolloutRuntimeFixture):
    def test_artifact_must_be_private_single_link_and_exact(self) -> None:
        self.assertEqual(
            rollout_runtime._artifact_identities(self.paths, self.catalog_spec)[0][
                "sha256"
            ],
            hashlib.sha256(self.artifact_body).hexdigest(),
        )
        for name, mutate in (
            ("public", lambda: self.artifact_path.chmod(0o640)),
            (
                "hardlink",
                lambda: os.link(self.artifact_path, self.artifact_path.with_suffix(".link")),
            ),
            (
                "changed",
                lambda: self.artifact_path.write_bytes(b"changed-artifact\n"),
            ),
        ):
            with self.subTest(case=name):
                self.artifact_path.unlink(missing_ok=True)
                self.artifact_path.write_bytes(self.artifact_body)
                self.artifact_path.chmod(0o600)
                mutate()
                with self.assertRaises(IntegrityError):
                    rollout_runtime._artifact_identities(self.paths, self.catalog_spec)
                self.artifact_path.with_suffix(".link").unlink(missing_ok=True)

    def test_acceptance_must_be_private_single_link_and_match_subject(self) -> None:
        identity = rollout_runtime.acceptance_identity(
            self.paths, self.model["id"], self.admission
        )
        self.assertEqual(identity["status"], "passed")
        cases = (
            ("public", lambda: self.evidence_path.chmod(0o644)),
            (
                "hardlink",
                lambda: os.link(self.evidence_path, self.evidence_path.with_suffix(".copy.json")),
            ),
            (
                "wrong-model",
                lambda: self._write_evidence(model_id="qwen35-9b-other"),
            ),
            ("failed", lambda: self._write_evidence(status="failed")),
        )
        for name, mutate in cases:
            with self.subTest(case=name):
                self._write_evidence()
                mutate()
                with self.assertRaises(IntegrityError):
                    rollout_runtime.acceptance_identity(
                        self.paths, self.model["id"], self.admission
                    )
                self.evidence_path.with_suffix(".copy.json").unlink(missing_ok=True)

    def test_acceptance_path_cannot_escape_private_log_store(self) -> None:
        unsafe = copy.deepcopy(self.admission)
        for path in (
            "/tmp/evidence.json",
            "logs/acceptance/../elsewhere.json",
            "deployments/evidence.json",
            "logs/acceptance/transient.run.json",
        ):
            with self.subTest(path=path):
                unsafe["hostAcceptanceEvidence"]["evidence"] = path
                with self.assertRaises(IntegrityError):
                    rollout_runtime.acceptance_identity(
                        self.paths, self.model["id"], unsafe
                    )

    def test_acceptance_parent_symlink_is_rejected_by_nofollow_reader(self) -> None:
        real_directory = self.root / "private-evidence"
        real_directory.mkdir(mode=0o700)
        real_path = real_directory / self.evidence_path.name
        self.evidence_path.replace(real_path)
        self.evidence_path.parent.rmdir()
        self.evidence_path.parent.symlink_to(real_directory, target_is_directory=True)
        with self.assertRaises(MaterialError):
            rollout_runtime.acceptance_identity(
                self.paths, self.model["id"], self.admission
            )


class ControllerSnapshotTests(RolloutRuntimeFixture):
    def test_dirty_tracked_tree_is_rejected_before_material_capture(self) -> None:
        dirty = RunResult(
            ("git", "status", "--porcelain=v1", "--untracked-files=no"),
            0,
            " M compose.yaml\n",
            "",
        )
        with (
            patch.object(rollout_runtime, "run", return_value=dirty) as mocked_run,
            self.assertRaisesRegex(ConfigError, "clean tracked worktree"),
        ):
            rollout_runtime.controller_snapshot(self.paths)
        mocked_run.assert_called_once()

    def test_invalid_git_revision_is_rejected(self) -> None:
        calls = iter(
            (
                RunResult(("git", "status"), 0, "", ""),
                RunResult(("git", "rev-parse"), 0, "HEAD\n", ""),
            )
        )
        with (
            patch.object(rollout_runtime, "run", side_effect=lambda *_a, **_k: next(calls)),
            self.assertRaisesRegex(ConfigError, "Git revision"),
        ):
            rollout_runtime.controller_snapshot(self.paths)


class VerifyRollbackSpecTests(RolloutRuntimeFixture):
    def test_verify_rechecks_local_state_without_network_acquisition(self) -> None:
        spec = self._capture()
        calls: list[tuple[str, ...]] = []

        def run(argv, **kwargs):
            return self._verify_run(argv, calls=calls, **kwargs)

        result = self._verify(spec, run_side_effect=run)
        self.assertEqual(result["rollbackSpecSha256"], spec.sha256)
        self.assertFalse(result["networkAcquisitionAllowed"])
        self.assertEqual(
            calls,
            [
                (
                    "python3",
                    "scripts/model-manager.py",
                    "admit",
                    "--model",
                    self.model["id"],
                    "--existing-selection",
                    "--json",
                ),
                ("docker", "image", "inspect", IMAGE_REFERENCE),
            ],
        )

    def test_verify_rejects_catalog_role_status_or_spec_drift(self) -> None:
        spec = self._capture()
        cases = (
            ("status", {**self.model, "status": "provisional"}, self.catalog_spec),
            ("role", {**self.model, "lifecycleRole": "candidate"}, self.catalog_spec),
        )
        for name, model, current_spec in cases:
            with self.subTest(case=name):
                with (
                    patch.object(
                        rollout_runtime,
                        "_catalog_model",
                        return_value=(model, current_spec),
                    ),
                    self.assertRaisesRegex(IntegrityError, "exact rollback anchor"),
                ):
                    rollout_runtime.verify_rollback_spec(self.paths, spec)

    def test_verify_rejects_catalog_trust_denial_or_invalid_json(self) -> None:
        spec = self._capture()
        for name, result in (
            (
                "denied",
                _result([], {"catalogRecoveryEligible": False}, returncode=3),
            ),
            ("invalid-json", RunResult(tuple(), 0, "not-json", "")),
        ):
            with self.subTest(case=name):
                with (
                    patch.object(rollout_runtime, "run", return_value=result),
                    self.assertRaises(IntegrityError),
                ):
                    rollout_runtime.verify_rollback_spec(self.paths, spec)

    def test_verify_rejects_admission_subject_cross_binding(self) -> None:
        spec = self._capture()

        def wrong_subject(argv, **kwargs):
            return self._verify_run(
                argv,
                admission={
                    "mode": "read-only-existing-selection-admission",
                    "recommendation": {**self.model, "id": "another-anchor"},
                    "catalogRecoveryEligible": True,
                    "recoveryHostAdmissionPassed": True,
                },
                admission_returncode=3,
                **kwargs,
            )

        with self.assertRaisesRegex(IntegrityError, "trust"):
            self._verify(spec, run_side_effect=wrong_subject)

    def test_verify_allows_expected_not_selected_exit_but_rejects_command_failure(self) -> None:
        spec = self._capture()

        def not_selected(argv, **kwargs):
            return self._verify_run(
                argv,
                admission={
                    "mode": "read-only-existing-selection-admission",
                    "recommendation": self.model,
                    "catalogRecoveryEligible": True,
                    "recoveryHostAdmissionPassed": True,
                },
                admission_returncode=3,
                **kwargs,
            )

        self.assertTrue(
            self._verify(spec, run_side_effect=not_selected)[
                "localMaterialsVerified"
            ]
        )

        def failed(argv, **kwargs):
            return self._verify_run(
                argv,
                admission={
                    "mode": "read-only-existing-selection-admission",
                    "recommendation": self.model,
                    "catalogRecoveryEligible": True,
                    "recoveryHostAdmissionPassed": True,
                },
                admission_returncode=1,
                **kwargs,
            )

        with self.assertRaisesRegex(IntegrityError, "trust"):
            self._verify(spec, run_side_effect=failed)

    def test_verify_rejects_recovery_host_admission_drift(self) -> None:
        spec = self._capture()

        def host_denied(argv, **kwargs):
            return self._verify_run(
                argv,
                admission={
                    "mode": "read-only-existing-selection-admission",
                    "recommendation": self.model,
                    "catalogRecoveryEligible": True,
                    "recoveryHostAdmissionPassed": False,
                },
                admission_returncode=3,
                **kwargs,
            )

        with self.assertRaisesRegex(IntegrityError, "trust"):
            self._verify(spec, run_side_effect=host_denied)

    def test_verify_rejects_controller_revision_or_material_drift(self) -> None:
        spec = self._capture()
        for name, controller in (
            ("revision", ("a" * 40, CONTROLLER_MATERIALS)),
            ("materials", (GIT_REVISION, {"compose.yaml": "b" * 64})),
        ):
            with self.subTest(case=name):
                with self.assertRaisesRegex(IntegrityError, "controller materials changed"):
                    self._verify(spec, controller=controller)

    def test_verify_rejects_host_compose_and_image_mismatch(self) -> None:
        spec = self._capture()
        wrong_host = {**HOST_IDENTITY, "fingerprint": "c" * 64}
        with self.assertRaisesRegex(IntegrityError, "another host"):
            self._verify(spec, host=wrong_host)
        with self.assertRaisesRegex(IntegrityError, "Compose identity"):
            self._verify(spec, compose="d" * 64)

        def wrong_image(argv, **kwargs):
            return self._verify_run(argv, image_id="sha256:" + "e" * 64, **kwargs)

        with self.assertRaisesRegex(IntegrityError, "image identity"):
            self._verify(spec, run_side_effect=wrong_image)

    def test_verify_rejects_artifact_drift(self) -> None:
        spec = self._capture()
        self.artifact_path.write_bytes(b"tampered\n")
        with self.assertRaises(IntegrityError):
            self._verify(spec)

    def test_verify_rechecks_acceptance_file_privacy(self) -> None:
        spec = self._capture()
        self.evidence_path.chmod(0o644)
        with self.assertRaisesRegex(IntegrityError, "acceptance"):
            self._verify(spec)


class RecoveryProjectionTests(RolloutRuntimeFixture):
    def test_recovery_original_is_safe_and_preserves_exact_runtime(self) -> None:
        spec = self._capture()
        original = rollout_runtime.recovery_original(spec)
        self.assertTrue(recovery_original_is_safe(original))
        self.assertTrue(original["deploymentProfile"]["present"])
        self.assertEqual(original["deploymentProfile"]["values"], self.selection)
        self.assertEqual(
            original["runtimeIdentity"]["configuration"], self.runtime_configuration
        )

    def test_write_anchor_selection_is_private_and_exact(self) -> None:
        spec = self._capture()
        rollout_runtime.write_anchor_selection(self.paths, spec)
        target = self.root / "profiles" / "deployment.local.env"
        metadata = target.lstat()
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertEqual(metadata.st_nlink, 1)
        self.assertEqual(parse_env_file(target), self.selection)


if __name__ == "__main__":
    unittest.main()
