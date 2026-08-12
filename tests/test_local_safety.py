"""Tests for loopback HTTP, environment parsing, and manifest integrity."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.env_utils import (
    atomic_write_private_text,
    is_private_regular_file,
    parse_env_file,
    read_private_env_values,
)
from scripts.local_http import is_loopback_host, validate_loopback_url


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_manifest", ROOT_DIR / "scripts" / "verify-manifest.py"
)
assert SPEC and SPEC.loader
VERIFY_MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY_MANIFEST)
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "install_user_services", ROOT_DIR / "scripts" / "install-user-services.py"
)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)
SUPERVISOR_SPEC = importlib.util.spec_from_file_location(
    "runtime_supervisor", ROOT_DIR / "scripts" / "runtime-supervisor.py"
)
assert SUPERVISOR_SPEC and SUPERVISOR_SPEC.loader
SUPERVISOR = importlib.util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(SUPERVISOR)
DOC_COMMAND_SPEC = importlib.util.spec_from_file_location(
    "check_doc_commands", ROOT_DIR / "scripts" / "check-doc-commands.py"
)
assert DOC_COMMAND_SPEC and DOC_COMMAND_SPEC.loader
DOC_COMMANDS = importlib.util.module_from_spec(DOC_COMMAND_SPEC)
DOC_COMMAND_SPEC.loader.exec_module(DOC_COMMANDS)


class LocalHttpTests(unittest.TestCase):
    def test_only_uncredentialed_loopback_urls_are_allowed(self) -> None:
        for url in (
            "http://127.0.0.1:18080/health",
            "http://[::1]:33004/api/health",
            "https://localhost:38082/readyz",
        ):
            self.assertEqual(validate_loopback_url(url), url)
        for url in (
            "http://example.com/",
            "http://127.0.0.1.example.com/",
            "http://user:password@127.0.0.1/",
            "file:///etc/passwd",
            "http://127.0.0.1/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_loopback_url(url)
        self.assertTrue(is_loopback_host("127.9.8.7"))
        self.assertFalse(is_loopback_host("0.0.0.0"))


class EnvironmentTests(unittest.TestCase):
    def test_parser_handles_shell_quotes_without_executing_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.env"
            path.write_text(
                "PLAIN=value\nQUOTED='a value # kept'\nexport DOUBLE=\"two words\"\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_env_file(path),
                {
                    "PLAIN": "value",
                    "QUOTED": "a value # kept",
                    "DOUBLE": "two words",
                },
            )
            path.write_text("BAD=$(id)\n", encoding="utf-8")
            self.assertEqual(parse_env_file(path), {"BAD": "$(id)"})

    def test_private_atomic_writer_sets_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private.env"
            atomic_write_private_text(target, "TOKEN=test\n")
            self.assertTrue(is_private_regular_file(target))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_uid, os.getuid())

    def test_private_credential_reader_filters_and_rejects_unsafe_inodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "credentials.env"
            target.write_text(
                "MODELPORT_AUTH_TOKEN='token value'\n"
                "MODELPORT_BASE_URL=http://attacker.invalid\n"
                "BENCHMARK_TOKENS=1\n",
                encoding="utf-8",
            )
            target.chmod(0o600)
            self.assertEqual(
                read_private_env_values(
                    target, allowed_keys={"MODELPORT_AUTH_TOKEN"}
                ),
                {"MODELPORT_AUTH_TOKEN": "token value"},
            )

            target.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                read_private_env_values(
                    target, allowed_keys={"MODELPORT_AUTH_TOKEN"}
                )
            target.chmod(0o600)
            hardlink = root / "credentials-copy.env"
            os.link(target, hardlink)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                read_private_env_values(
                    target, allowed_keys={"MODELPORT_AUTH_TOKEN"}
                )
            hardlink.unlink()
            symlink = root / "credentials-link.env"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "safely read"):
                read_private_env_values(
                    symlink, allowed_keys={"MODELPORT_AUTH_TOKEN"}
                )

    def test_acceptance_children_receive_no_rollout_mutation_authority(self) -> None:
        script = ROOT_DIR / "scripts" / "acceptance-suite.sh"
        program = f"""
set -euo pipefail
source {script} help >/dev/null
BOUND_QUALIFICATION=true
export QWEN_CONTROL_TRANSACTION_ID=00000000-0000-4000-8000-000000000001
export LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256={'a' * 64}
export LOCAL_INFERENCE_ROLLOUT_SUBJECT=target
export LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL=6
export LOCAL_INFERENCE_ROLLOUT_ACTION_KIND=target-full
export LOCAL_INFERENCE_RUNTIME_PULL_POLICY=never
export QWEN_RUNTIME_LOCK_HELD=1
export LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN={'b' * 64}
run_acceptance_child python3 - <<'PY'
import json
import os

keys = (
    "QWEN_CONTROL_TRANSACTION_ID",
    "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256",
    "LOCAL_INFERENCE_ROLLOUT_SUBJECT",
    "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL",
    "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND",
    "LOCAL_INFERENCE_RUNTIME_PULL_POLICY",
    "QWEN_RUNTIME_LOCK_HELD",
    "LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN",
)
print(json.dumps({{key: os.environ.get(key) for key in keys}}))
print(os.environ.get("LOCAL_INFERENCE_BOUND_QUALIFICATION"))
PY
"""
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        self.assertTrue(all(value is None for value in json.loads(lines[-2]).values()))
        self.assertEqual(lines[-1], "1")

    def test_private_token_loader_scrubs_rollout_authority_before_parser(self) -> None:
        library = ROOT_DIR / "scripts" / "lib" / "deployment.sh"
        program = f"""
set -euo pipefail
source {library}
export QWEN_CONTROL_TRANSACTION_ID=00000000-0000-4000-8000-000000000001
export LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256={'a' * 64}
export LOCAL_INFERENCE_ROLLOUT_SUBJECT=target
export LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL=6
export LOCAL_INFERENCE_ROLLOUT_ACTION_KIND=target-full
export LOCAL_INFERENCE_RUNTIME_PULL_POLICY=never
export QWEN_RUNTIME_LOCK_HELD=1
export LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN={'b' * 64}
export MODELPORT_AUTH_TOKEN=ambient-secret
python3() {{
  local key
  for key in \
    QWEN_CONTROL_TRANSACTION_ID \
    LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256 \
    LOCAL_INFERENCE_ROLLOUT_SUBJECT \
    LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL \
    LOCAL_INFERENCE_ROLLOUT_ACTION_KIND \
    LOCAL_INFERENCE_RUNTIME_PULL_POLICY \
    QWEN_RUNTIME_LOCK_HELD \
    LOCAL_INFERENCE_ACCEPTANCE_RUN_TOKEN \
    MODELPORT_AUTH_TOKEN; do
    [[ -z "${{!key+x}}" ]] || return 41
  done
  printf isolated-token
}}
load_private_modelport_token {ROOT_DIR} /unused/private.env
[[ "$MODELPORT_AUTH_TOKEN" == isolated-token ]]
"""
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ManifestTests(unittest.TestCase):
    def test_manifest_mapping_covers_runtime_and_acceptance_integrity_entrypoints(self) -> None:
        tracked = set(VERIFY_MANIFEST.CONFIGURATION_FILES.values())
        required = {
            ".github/workflows/ci.yml",
            ".gitignore",
            "AGENTS.md",
            "LICENSE",
            "compose.yaml",
            "deployments/qwen3.5-9b-rtx5070ti/README.md",
            "scripts/model-manager.py",
            "scripts/runtime.sh",
            "scripts/runtime_identity.py",
            "scripts/acceptance-suite.sh",
            "scripts/acceptance-evidence.py",
            "scripts/smoke-test.sh",
            "scripts/reasoning-smoke.sh",
            "scripts/modelport-smoke.sh",
            "scripts/modelport-reasoning-smoke.py",
            "scripts/modelport-reasoning-smoke.sh",
            "scripts/modelport-token-count-smoke.sh",
            "scripts/modelport-context-admission-smoke.sh",
            "scripts/context-acceptance.py",
            "scripts/modelport-context-acceptance.sh",
            "scripts/decode-benchmark.py",
            "scripts/concurrency-benchmark.py",
            "profiles/alerting.local.env.example",
            "profiles/operations.env",
            "docs/MODELPORT.md",
            "models/README.md",
        }
        self.assertTrue(required.issubset(tracked), sorted(required - tracked))
        expected = VERIFY_MANIFEST.expected_configuration()
        self.assertIn("controlPlanePackageSha256", expected)
        self.assertIn("unitTestPackageSha256", expected)
        self.assertIn("architectureDecisionsSha256", expected)
        self.assertEqual(
            expected["repositoryMaterialPolicy"],
            VERIFY_MANIFEST.REPOSITORY_MATERIAL_POLICY_ID,
        )
        self.assertEqual(
            expected["fileSetMaterialPolicy"],
            VERIFY_MANIFEST.FILE_SET_SHA256_POLICY_ID,
        )

    def test_repository_material_inventory_has_exact_declared_coverage(self) -> None:
        spec = VERIFY_MANIFEST.REPOSITORY_SNAPSHOT_SPEC
        declared = set(VERIFY_MANIFEST.CONFIGURATION_FILES.values())
        spec.require_paths(ROOT_DIR, declared)
        inventory = spec.inventory(ROOT_DIR)
        self.assertEqual(
            inventory["policyId"],
            VERIFY_MANIFEST.REPOSITORY_MATERIAL_POLICY_ID,
        )
        self.assertEqual(
            {item["path"] for item in inventory["files"]},
            declared,
        )
        aggregate_paths = {
            path
            for material_set in inventory["fileSets"]
            for path in material_set["paths"]
        }
        self.assertTrue(
            {
                path.relative_to(ROOT_DIR).as_posix()
                for path in (ROOT_DIR / "src" / "local_inference_stack").glob("*.py")
            }.issubset(aggregate_paths)
        )
        self.assertTrue(
            {
                path.relative_to(ROOT_DIR).as_posix()
                for path in (ROOT_DIR / "tests").glob("test_*.py")
            }.issubset(aggregate_paths)
        )
        self.assertTrue(
            {
                path.relative_to(ROOT_DIR).as_posix()
                for path in (ROOT_DIR / "docs" / "decisions").glob("*.md")
            }.issubset(aggregate_paths)
        )

    def test_repository_snapshot_rejects_policy_drift(self) -> None:
        drifted = VERIFY_MANIFEST.SnapshotSpec(
            policy_id="local-inference-stack/repository-configuration-materials-v2",
            files=VERIFY_MANIFEST.REPOSITORY_SNAPSHOT_SPEC.files,
            material_sets=VERIFY_MANIFEST.REPOSITORY_SNAPSHOT_SPEC.material_sets,
        )
        with self.assertRaisesRegex(RuntimeError, "snapshot policy changed"):
            drifted.snapshot(
                ROOT_DIR,
                expected_policy_id=VERIFY_MANIFEST.REPOSITORY_MATERIAL_POLICY_ID,
            )

    def test_document_smoke_accepts_only_explicit_migration_attention(self) -> None:
        attention = {"schemaVersion": 1, "status": "attention", "code": 4}
        migration = ("migrate", "--check", "--json")
        self.assertTrue(DOC_COMMANDS.result_is_acceptable(migration, 4, attention))
        self.assertFalse(
            DOC_COMMANDS.result_is_acceptable(
                ("config", "check", "--json"), 4, attention
            )
        )

    def test_manifest_declares_every_repository_configuration_material(self) -> None:
        manifest = VERIFY_MANIFEST.json.loads(
            VERIFY_MANIFEST.MANIFEST_PATH.read_text(encoding="utf-8")
        )
        expected = VERIFY_MANIFEST.expected_configuration()
        current = manifest["repositoryConfiguration"]
        self.assertEqual(set(current), set(expected))

        # Digest freshness is the release-check gate.  Keep this unit test focused
        # on exact material coverage and the surrounding document contract so a
        # multi-file change can defer the single final manifest refresh.
        manifest["repositoryConfiguration"] = expected
        self.assertEqual(
            VERIFY_MANIFEST.verify_document(manifest),
            [],
        )

    def test_manifest_cannot_claim_unreviewed_integrated_identities(self) -> None:
        manifest = VERIFY_MANIFEST.json.loads(
            VERIFY_MANIFEST.MANIFEST_PATH.read_text(encoding="utf-8")
        )
        manifest["gateway"]["reviewedContainerIdentities"] = {
            "schemaVersion": 1,
            "status": "reviewed-current",
            "containers": {"modelport": {}},
        }
        issue_keys = {
            issue["key"] for issue in VERIFY_MANIFEST.verify_document(manifest)
        }
        self.assertIn("gateway.reviewedContainerIdentities", issue_keys)

    def test_systemd_renderer_escapes_portable_checkout_paths(self) -> None:
        root = Path("/tmp/project with $ and %")
        escaped = INSTALLER.systemd_escape_path(root)
        self.assertEqual(escaped, "/tmp/project\\x20with\\x20$$\\x20and\\x20%%")
        body = INSTALLER.rendered_body("qwen-model-runtime.service", root)
        self.assertIn(f"WorkingDirectory={escaped}", body)
        self.assertIn(f"{escaped}/scripts/runtime-supervisor.py", body)

    def test_repository_python_pin_resolves_through_uv(self) -> None:
        executable = INSTALLER.pinned_python(ROOT_DIR)
        result = INSTALLER.subprocess.run(
            [str(executable), "-c", "import platform; print(platform.python_version())"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.stdout.strip(), "3.14.6")


class RuntimeSupervisorTests(unittest.TestCase):
    def supervisor(self, root: Path) -> object:
        return SUPERVISOR.RuntimeSupervisor(root_dir=root, sleeper=lambda _: None)

    @staticmethod
    def safe_original() -> dict[str, object]:
        return {
            "healthy": True,
            "containerHealthy": True,
            "profile": "latency",
            "containerName": "qwen35-9b-q5km",
            "runtimeIdentity": {
                "sha256": "a" * 64,
                "configuration": {"profile": "latency"},
            },
            "deploymentProfile": {"present": False},
            "capturedWithoutSecrets": True,
        }

    def test_healthy_runtime_must_also_have_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(Path(directory))
            with (
                patch.object(supervisor, "docker_available", return_value=True),
                patch.object(supervisor, "runtime_healthy", return_value=True),
                patch.object(supervisor, "profile_is_canonical", return_value=False),
                patch.object(supervisor, "clear_alert") as clear_alert,
                patch.object(supervisor, "reconcile") as reconcile,
            ):
                self.assertIs(
                    supervisor.step(), SUPERVISOR.Step.PERMANENT_FAILURE
                )
                clear_alert.assert_not_called()
                reconcile.assert_not_called()

    def test_docker_wait_records_alert_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            supervisor = SUPERVISOR.RuntimeSupervisor(
                root_dir=Path(directory),
                clock=lambda: now[0],
                sleeper=lambda _: None,
            )
            with patch.object(supervisor, "_alert", return_value=True) as alert:
                supervisor.note_docker_wait()
                alert.assert_not_called()
                now[0] += supervisor.docker_alert_seconds
                supervisor.note_docker_wait()
                alert.assert_called_once_with("fire")

    def test_failed_reconcile_is_transient_only_if_docker_disappeared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(Path(directory))
            with (
                patch.object(
                    supervisor, "docker_available", side_effect=[True, False]
                ),
                patch.object(supervisor, "runtime_healthy", return_value=False),
                patch.object(supervisor, "container_health", return_value="unhealthy"),
                patch.object(supervisor, "reconcile", return_value=False),
                patch.object(supervisor, "note_docker_wait") as note_wait,
            ):
                self.assertIs(supervisor.step(), SUPERVISOR.Step.WAIT_DOCKER)
                note_wait.assert_called_once_with()

    def test_active_transaction_waits_without_probing_or_mutating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SUPERVISOR.TransactionStore(SUPERVISOR.ProjectPaths(root))
            store.begin("release", "quick", self.safe_original())
            supervisor = self.supervisor(root)
            with (
                patch.object(supervisor, "docker_available") as docker,
                patch.object(supervisor, "reconcile") as reconcile,
                patch.object(supervisor, "reconcile_transaction") as transaction_reconcile,
            ):
                self.assertIs(supervisor.step(), SUPERVISOR.Step.WAIT_TRANSACTION)
                docker.assert_not_called()
                reconcile.assert_not_called()
                transaction_reconcile.assert_not_called()

    def test_only_safe_recovery_required_transaction_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SUPERVISOR.TransactionStore(SUPERVISOR.ProjectPaths(root))
            document = store.begin("release", "quick", self.safe_original())
            store.transition(
                "recovery_required",
                expected_id=document["id"],
                detail="injected failure",
            )
            supervisor = self.supervisor(root)
            with (
                patch.object(supervisor, "runtime_lock_active", return_value=False),
                patch.object(supervisor, "docker_available", return_value=True),
                patch.object(supervisor, "reconcile_transaction", return_value=True) as reconcile,
            ):
                self.assertIs(supervisor.step(), SUPERVISOR.Step.RECOVERED)
                reconcile.assert_called_once_with()

    def test_runtime_lock_contention_waits_before_health_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(Path(directory))
            with (
                patch.object(supervisor, "runtime_lock_active", return_value=True),
                patch.object(supervisor, "docker_available") as docker,
                patch.object(supervisor, "runtime_healthy") as healthy,
            ):
                self.assertIs(supervisor.step(), SUPERVISOR.Step.WAIT_LOCK)
                docker.assert_not_called()
                healthy.assert_not_called()


class RuntimeMutationLockTests(unittest.TestCase):
    def test_shell_lock_rejects_forged_inherited_file_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library = ROOT_DIR / "scripts" / "lib" / "deployment.sh"
            command = (
                "exec 203</dev/null; exec 204</dev/null; "
                "export QWEN_RUNTIME_LOCK_HELD=1; "
                f"source {library}; acquire_runtime_lock {directory}"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not reference the project lock files", result.stderr)

    def test_shell_lock_rejects_symlink_without_truncating_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = root / "cache" / "locks"
            locks.mkdir(parents=True)
            target = root / "victim"
            target.write_text("unchanged", encoding="utf-8")
            target.chmod(0o600)
            (locks / "runtime.lock").symlink_to(target)
            library = ROOT_DIR / "scripts" / "lib" / "deployment.sh"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {library}; acquire_runtime_lock {root}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-symlink", result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_shell_lock_rejects_unmatched_active_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SUPERVISOR.TransactionStore(SUPERVISOR.ProjectPaths(root))
            transaction = store.begin(
                "release",
                "quick",
                {
                    "healthy": False,
                    "containerHealthy": False,
                    "profile": "unknown",
                    "containerName": None,
                    "runtimeIdentity": None,
                    "deploymentProfile": {"present": False},
                    "capturedWithoutSecrets": True,
                },
            )
            transaction_id = transaction["id"]
            store.transition("recovery_required", expected_id=transaction_id)
            library = ROOT_DIR / "scripts" / "lib" / "deployment.sh"
            command = (
                f"source {library}; acquire_runtime_lock {root}"
            )
            rejected = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("active control transaction", rejected.stderr)
            accepted = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "QWEN_CONTROL_TRANSACTION_ID": transaction_id},
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_release_signal_exit_and_recovery_failure_precedence(self) -> None:
        script = ROOT_DIR / "scripts" / "release-candidate.sh"
        signalled = subprocess.run(
            [
                "bash",
                "-c",
                f"source {script}; install_release_traps; kill -TERM $$",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(signalled.returncode, 143)
        self.assertIn("final status=143", signalled.stderr)
        precedence = subprocess.run(
            [
                "bash",
                "-c",
                f"source {script}; release_result_status 1 9",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(precedence.returncode, 0)
        self.assertEqual(precedence.stdout.strip(), "70")

    def test_direct_release_catalog_gate_fails_before_runtime_commands(self) -> None:
        script = ROOT_DIR / "scripts" / "release-candidate.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = root / "scripts" / "model-manager.py"
            manager.parent.mkdir(parents=True)
            manager.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'catalogDeploymentEligible': False}))\n",
                encoding="utf-8",
            )
            manager.chmod(0o700)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f"source {script}; ROOT_DIR={root}; "
                        "QWEN_CATALOG_ID=provisional-model; "
                        "require_catalog_deployment_eligible"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("not deployment-eligible", result.stderr)


if __name__ == "__main__":
    unittest.main()
