"""Tests for the narrow no-network runtime startup policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import cli  # noqa: E402
from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.runner import RunResult  # noqa: E402
from local_inference_stack.transactions import SCHEMA_VERSION  # noqa: E402


TRANSACTION_ID = "0dbdb868-b62f-4471-84b4-0198a0700f09"


class RuntimeShellPullPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts" / "lib").mkdir(parents=True)
        (self.root / "profiles").mkdir()
        (self.root / "profiles" / "latency.env").write_text("", encoding="utf-8")
        shutil.copy2(ROOT / "scripts" / "runtime.sh", self.root / "scripts" / "runtime.sh")
        (self.root / "scripts" / "lib" / "deployment.sh").write_text(
            """#!/usr/bin/env bash
load_deployment_env() {
  export QWEN_CATALOG_ID=test-model
  export QWEN_CONTAINER_NAME=test-model
  export MODELPORT_NETWORK_NAME=test-network
}
acquire_runtime_lock() { :; }
assert_approved_catalog_spec() { :; }
run_clean_compose() {
  shift
  docker compose "$@"
}
""",
            encoding="utf-8",
        )
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.log = self.root / "docker-argv.txt"
        self._write_executable(
            self.fake_bin / "docker",
            """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  inspect) printf 'true\n' ;;
  network) exit 0 ;;
  compose)
    printf '%s\n' "$@" >"$TEST_DOCKER_ARGV"
    ;;
  *) exit 2 ;;
esac
""",
        )
        self._write_executable(
            self.fake_bin / "curl",
            "#!/usr/bin/env sh\nprintf '{\"status\":\"ok\"}\\n'\n",
        )
        self._write_executable(self.root / "stack", "#!/usr/bin/env sh\nexit 0\n")
        self._write_executable(
            self.root / "scripts" / "model-manager.py",
            "#!/usr/bin/env sh\nexit 0\n",
        )

    @staticmethod
    def _write_executable(path: Path, body: str) -> None:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o700)

    def _start(self, policy: str | None) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "QWEN_START_ATTEMPTS": "1",
            "QWEN_START_INTERVAL_SECONDS": "1",
            "TEST_DOCKER_ARGV": str(self.log),
        }
        if policy is not None:
            environment[cli.RUNTIME_PULL_POLICY_ENV] = policy
        return subprocess.run(
            [str(self.root / "scripts" / "runtime.sh"), "start", "latency"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_never_policy_becomes_compose_pull_never(self) -> None:
        result = self._start("never")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            ["up", "-d", "--pull", "never", "qwen35"],
            [arguments[index : index + 5] for index in range(len(arguments) - 4)],
        )

    def test_default_start_keeps_the_existing_compose_pull_policy(self) -> None:
        result = self._start(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = self.log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("--pull", arguments)
        self.assertIn(
            ["up", "-d", "qwen35"],
            [arguments[index : index + 3] for index in range(len(arguments) - 2)],
        )

    def test_unknown_pull_policy_is_rejected_before_compose(self) -> None:
        result = self._start("always")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unsupported controlled runtime pull policy", result.stderr)
        self.assertFalse(self.log.exists())


class ReconciliationPullPolicyTests(unittest.TestCase):
    @staticmethod
    def _original() -> dict[str, object]:
        return {
            "healthy": False,
            "containerHealthy": False,
            "profile": "unknown",
            "containerName": None,
            "runtimeIdentity": None,
            "deploymentProfile": {"present": False},
            "capturedWithoutSecrets": True,
        }

    def test_only_rollout_reconciliation_receives_the_no_pull_policy(self) -> None:
        for operation, expects_never in (
            ("upgrade", True),
            ("rollback", True),
            ("deploy", False),
        ):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                document = {
                    "schemaVersion": SCHEMA_VERSION,
                    "id": TRANSACTION_ID,
                    "operation": operation,
                    "state": "recovery_required",
                    "original": self._original(),
                }
                plan = {
                    "required": True,
                    "transaction": document,
                    "runtimeDisposition": "restoration-required",
                    "originalSafeToRestore": True,
                }
                transaction = MagicMock()
                transaction.reconciliation_plan.return_value = plan
                completed = RunResult(("scripts/runtime-reconcile.sh",), 0, "restored", "")
                with (
                    patch.object(cli, "TransactionStore", return_value=transaction),
                    patch.object(cli, "_reconciliation_runtime_plan", return_value=plan),
                    patch.object(cli, "_original_runtime", return_value={}),
                    patch.object(cli, "_restore_deployment_profile"),
                    patch.object(cli, "_verify_restored_runtime"),
                    patch.object(
                        cli,
                        "_verified_rollout_recovery_anchor",
                        return_value=(MagicMock(), {}),
                    ),
                    patch.object(cli, "_verify_rollout_recovery", return_value={}),
                    patch.object(
                        cli,
                        "_resolve_verified_transaction",
                        return_value={"state": "failed-restored"},
                    ),
                    patch.object(cli, "run", return_value=completed) as runner,
                ):
                    cli._reconcile(paths, Namespace(yes=True))

                environment = runner.call_args.kwargs["env"]
                if expects_never:
                    self.assertEqual(
                        environment[cli.RUNTIME_PULL_POLICY_ENV],
                        cli.RUNTIME_PULL_POLICY_NEVER,
                    )
                else:
                    self.assertNotIn(cli.RUNTIME_PULL_POLICY_ENV, environment)


if __name__ == "__main__":
    unittest.main()
