from __future__ import annotations

import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import cli, health
from local_inference_stack.paths import ProjectPaths
from local_inference_stack.result import RecoveryError, UsageError
from local_inference_stack.runner import RunResult


class HealthProbeTests(unittest.TestCase):
    def test_loopback_failure_is_structured(self) -> None:
        with patch.object(health, "direct_urlopen", side_effect=URLError("offline")):
            result = health.probe_json("http://127.0.0.1:38082/livez", timeout=0.01)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("URLError", result["diagnostic"])

    def test_integrated_status_distinguishes_disabled_and_unavailable(self) -> None:
        units = {
            health.DASHBOARD_UNIT: {"status": "disabled", "unit": health.DASHBOARD_UNIT},
            **{
                unit: {"status": "disabled", "unit": unit}
                for unit in health.OPERATIONS_UNITS
            },
        }
        with patch.object(
            health,
            "_container_state",
            return_value={"Status": "exited"},
        ), patch.object(
            health,
            "_user_unit",
            side_effect=lambda _root, unit: units[unit],
        ):
            result = health.integrated_status(ROOT)
        self.assertFalse(result["healthy"])
        self.assertEqual(result["components"]["modelport"]["status"], "unavailable")
        self.assertEqual(
            result["components"]["operationsDashboard"]["status"], "disabled"
        )

    def test_docker_daemon_failure_is_not_misreported_as_not_configured(self) -> None:
        failed = RunResult(("docker",), 1, "", "cannot connect to Docker daemon")
        with patch.object(health, "run", return_value=failed):
            state = health._container_state(ROOT, health.MODELPORT_CONTAINER)
        self.assertEqual(state["_probeStatus"], "unavailable")
        self.assertIn("Docker daemon", state["_diagnostic"])

    def test_missing_container_is_distinct_from_docker_failure(self) -> None:
        missing = RunResult(("docker",), 1, "", "Error: No such object: missing")
        with patch.object(health, "run", return_value=missing):
            self.assertIsNone(health._container_state(ROOT, "missing"))

    def test_user_bus_failure_is_unavailable_not_not_configured(self) -> None:
        failed = RunResult(("systemctl",), 1, "", "Failed to connect to bus")
        with patch.object(health, "run", return_value=failed):
            result = health._user_unit(ROOT, health.DASHBOARD_UNIT)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("connect to bus", result["diagnostic"])

class ScopedStatusTests(unittest.TestCase):
    @staticmethod
    def healthy_standalone() -> dict:
        return {
            "scope": "standalone",
            "healthy": True,
            "components": {
                "runtime": {"status": "healthy", "profile": "latency"},
                "controlPlane": {"status": "healthy"},
            },
            "runtimeHealthy": True,
            "controlPlaneReady": True,
            "profile": "latency",
            "runtimeSummary": "healthy",
            "runtimeDiagnostic": "",
            "transaction": None,
            "reconciliation": {"required": False},
        }

    def test_default_status_scope_remains_standalone(self) -> None:
        args = cli.parser().parse_args(["status"])
        self.assertEqual(args.scope, "standalone")

    def test_integrated_status_does_not_probe_standalone_runtime(self) -> None:
        integrated = {
            "scope": "integrated",
            "components": {
                "modelport": {"status": "not-configured"},
                "operationsDashboard": {"status": "disabled"},
            },
            "configured": False,
            "healthy": False,
            "unavailable": [],
        }
        with patch.object(health, "integrated_status", return_value=integrated), patch.object(
            cli, "run", return_value=RunResult(("unexpected",), 99, "", "")
        ) as mocked_run:
            result = cli._status(ProjectPaths(ROOT), Namespace(scope="integrated"))
        mocked_run.assert_not_called()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.facts["components"]["modelport"]["status"], "not-configured")

    def test_integrated_verify_reports_component_facts(self) -> None:
        integrated = {
            "scope": "integrated",
            "components": {"modelport": {"status": "unavailable"}},
            "configured": True,
            "healthy": False,
            "unavailable": ["modelport"],
        }
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(
                cli, "_standalone_status", return_value=self.healthy_standalone()
            ),
            patch.object(
                cli,
                "run",
                side_effect=(
                    RunResult(("assert-profile",), 0, "canonical", ""),
                    RunResult(("model-manager",), 0, "verified", ""),
                ),
            ),
            patch.object(health, "integrated_status", return_value=integrated),
        ):
            with self.assertRaisesRegex(Exception, "integrated deployment") as raised:
                cli._verify(
                    ProjectPaths(ROOT),
                    Namespace(scope="integrated", model=None, cached=False),
                )
        self.assertEqual(raised.exception.facts["components"], integrated["components"])

    def test_integrated_health_cannot_replace_manifest_identity_verification(self) -> None:
        integrated = {
            "scope": "integrated",
            "components": {"modelport": {"status": "healthy"}},
            "configured": True,
            "healthy": True,
            "unavailable": [],
        }
        detailed = {
            "schemaVersion": 1,
            "status": "failed",
            "summary": {"passed": 5, "failed": 1},
            "checks": [{"name": "ModelPort image", "passed": False}],
        }
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(
                cli, "_standalone_status", return_value=self.healthy_standalone()
            ),
            patch.object(health, "integrated_status", return_value=integrated),
            patch.object(
                cli,
                "run",
                side_effect=(
                    RunResult(("assert-profile",), 0, "canonical", ""),
                    RunResult(("model-manager",), 0, "verified", ""),
                    RunResult(
                        ("verify-integrated-deployment",), 1, json.dumps(detailed), ""
                    ),
                ),
            ),
            self.assertRaisesRegex(Exception, "identity") as raised,
        ):
            cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="integrated", model=None, cached=False),
            )
        self.assertEqual(
            raised.exception.facts["deploymentVerification"]["status"], "failed"
        )

    def test_integrated_verify_cannot_bypass_a_pending_transaction(self) -> None:
        standalone = self.healthy_standalone()
        standalone.update(
            {
                "healthy": False,
                "controlPlaneReady": False,
                "transaction": {"state": "recovery_required"},
                "reconciliation": {"required": True},
            }
        )
        standalone["components"]["controlPlane"] = {"status": "attention"}
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(cli, "_standalone_status", return_value=standalone),
            patch.object(health, "integrated_status") as integrated_probe,
            self.assertRaises(RecoveryError),
        ):
            cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="integrated", model=None, cached=False),
            )
        integrated_probe.assert_not_called()

    def test_partially_configured_integrated_status_is_attention(self) -> None:
        integrated = {
            "scope": "integrated",
            "components": {
                "modelport": {"status": "not-configured"},
                "operationsReport": {"status": "healthy"},
            },
            "configured": True,
            "healthy": False,
            "unavailable": [],
            "incomplete": ["modelport"],
        }
        with patch.object(health, "integrated_status", return_value=integrated):
            result = cli._status(
                ProjectPaths(ROOT), Namespace(scope="integrated")
            )
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.code, 4)

    def test_pending_transaction_keeps_runtime_health_but_returns_attention(self) -> None:
        standalone = {
            "scope": "standalone",
            "healthy": False,
            "components": {
                "runtime": {"status": "healthy", "profile": "latency"},
                "controlPlane": {"status": "attention"},
            },
            "runtimeHealthy": True,
            "controlPlaneReady": False,
            "profile": "latency",
            "runtimeSummary": "healthy",
            "runtimeDiagnostic": "",
            "transaction": {"state": "recovery_required"},
            "reconciliation": {"required": True},
        }
        with patch.object(cli, "_standalone_status", return_value=standalone):
            result = cli._status(ProjectPaths(ROOT), Namespace(scope="standalone"))
        self.assertEqual(result.status, "attention")
        self.assertEqual(result.code, 7)
        self.assertTrue(result.facts["runtimeHealthy"])
        self.assertFalse(result.facts["controlPlaneReady"])

    def test_standalone_verify_runs_exact_runtime_identity_check(self) -> None:
        standalone = {
            "scope": "standalone",
            "healthy": True,
            "components": {
                "runtime": {"status": "healthy", "profile": "latency"},
                "controlPlane": {"status": "healthy"},
            },
            "runtimeHealthy": True,
            "controlPlaneReady": True,
            "profile": "latency",
            "runtimeSummary": "healthy",
            "runtimeDiagnostic": "",
            "transaction": None,
            "reconciliation": {"required": False},
        }
        results = [
            RunResult(("assert-profile",), 0, "runtime_configuration=canonical", ""),
            RunResult(("model-manager",), 0, "verified", ""),
        ]
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(cli, "_standalone_status", return_value=standalone),
            patch.object(cli, "run", side_effect=results) as runner,
        ):
            result = cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="standalone", model=None, cached=False),
            )
        self.assertEqual(result.code, 0)
        self.assertEqual(
            runner.call_args_list[0].args[0],
            ["scripts/runtime.sh", "assert-profile", "latency"],
        )
        self.assertEqual(
            runner.call_args_list[1].args[0],
            [
                "python3",
                "scripts/model-manager.py",
                "verify",
                "--cached",
                "--read-only",
            ],
        )
        self.assertIn("canonical", result.facts["runtimeIdentity"])

    def test_model_scope_uses_the_non_mutating_artifact_verifier(self) -> None:
        completed = RunResult(("model-manager",), 0, "verified", "")
        with patch.object(cli, "run", return_value=completed) as runner:
            cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="model", model="fixture-model", cached=True),
            )
        self.assertEqual(
            runner.call_args.args[0],
            [
                "python3",
                "scripts/model-manager.py",
                "verify",
                "--read-only",
                "--model",
                "fixture-model",
                "--cached",
            ],
        )

    def test_verify_rejects_scope_irrelevant_model_flags(self) -> None:
        with self.assertRaises(UsageError):
            cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="integrated", model="ignored", cached=False),
            )

    def test_standalone_verify_rejects_pending_transaction(self) -> None:
        standalone = {
            "runtimeHealthy": True,
            "controlPlaneReady": False,
            "profile": "latency",
            "components": {"controlPlane": {"status": "attention"}},
            "reconciliation": {"required": True},
        }
        with (
            patch.object(cli.configuration, "check", return_value=[]),
            patch.object(cli, "_standalone_status", return_value=standalone),
            self.assertRaises(RecoveryError),
        ):
            cli._verify(
                ProjectPaths(ROOT),
                Namespace(scope="standalone", model=None, cached=False),
            )


if __name__ == "__main__":
    unittest.main()
