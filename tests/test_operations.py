from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = load_module("operations_report_test", ROOT / "scripts" / "operations-report.py")
dashboard = load_module(
    "operations_dashboard_test", ROOT / "scripts" / "operations-dashboard.py"
)
tool_workflow = load_module(
    "tool_workflow_test", ROOT / "scripts" / "tool-workflow-eval.py"
)
soak_check = load_module("soak_check_test", ROOT / "scripts" / "soak-check.py")


class OperationsReportTests(unittest.TestCase):
    def test_operations_contract_keeps_upstream_least_privilege_gap_explicit(self) -> None:
        contract = json.loads(
            (ROOT / "contracts" / "modelport-operations-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(
            contract["authorization"]["currentAdapterIsLeastPrivilege"]
        )
        self.assertEqual(
            contract["authorization"]["requiredScope"],
            "operations:aggregate:read",
        )
        self.assertFalse(
            contract["migrationGate"]["dashboardCredentialAccessAllowed"]
        )

    def test_docker_nanosecond_timestamp_is_parsed_for_soak_gate(self) -> None:
        parsed = soak_check.parse_docker_time("2026-07-19T11:38:11.052738421Z")
        self.assertEqual(parsed.microsecond, 52_738)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_soak_gate_accepts_the_public_verify_command_result(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "schemaVersion": 1,
                    "command": "verify",
                    "status": "ok",
                    "code": 0,
                    "summary": "all deployment components verified",
                    "facts": {},
                    "nextActions": [],
                }
            ),
            stderr="",
        )
        passed, summary = soak_check.deployment_verification_result(completed)
        self.assertTrue(passed)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["code"], 0)

    def test_soak_gate_rejects_a_failed_public_verify_result(self) -> None:
        completed = SimpleNamespace(
            returncode=7,
            stdout=json.dumps(
                {
                    "schemaVersion": 1,
                    "command": "verify",
                    "status": "error",
                    "code": 7,
                }
            ),
            stderr="control plane attention",
        )
        passed, summary = soak_check.deployment_verification_result(completed)
        self.assertFalse(passed)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["code"], 7)

    def test_soak_gate_rejects_the_removed_verifier_result_shape(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "schemaVersion": 1,
                    "status": "passed",
                    "summary": {"passed": 10, "failed": 0},
                }
            ),
            stderr="",
        )
        passed, summary = soak_check.deployment_verification_result(completed)
        self.assertFalse(passed)
        self.assertEqual(summary, "invalid CommandResult")

    def test_backup_snapshot_reports_age_and_restrictive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            backup_dir.chmod(0o700)
            archive = backup_dir / "modelport-20260719T000000Z.tar.gz"
            archive.write_bytes(b"backup")
            archive.chmod(0o600)
            now_ms = archive.stat().st_mtime_ns // 1_000_000 + 3_600_000
            with patch.dict("os.environ", {"MODELPORT_BACKUP_DIR": directory}):
                snapshot = report.backup_snapshot(now_ms)
            self.assertTrue(snapshot["available"])
            self.assertEqual(snapshot["archiveCount"], 1)
            self.assertEqual(snapshot["latestAgeHours"], 1.0)
            self.assertTrue(snapshot["securePermissions"])

    def test_backup_snapshot_rejects_symlink_only_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            target = backup_dir / "outside.tar.gz"
            target.write_bytes(b"backup")
            (backup_dir / "modelport-linked.tar.gz").symlink_to(target)
            with patch.dict("os.environ", {"MODELPORT_BACKUP_DIR": directory}):
                snapshot = report.backup_snapshot()
            self.assertFalse(snapshot["available"])

    def test_synthetic_traffic_prefers_explicit_class_and_keeps_legacy_match(self) -> None:
        self.assertTrue(report.is_synthetic_traffic({"trafficClass": "synthetic"}))
        self.assertTrue(
            report.is_synthetic_traffic({"provider": "local_tool_acceptance_123"})
        )
        self.assertFalse(report.is_synthetic_traffic({"trafficClass": "diagnostic"}))

    def test_input_buckets_include_cache_tokens(self) -> None:
        row = {"inputTokens": 60_000, "cacheReadTokens": 34_000}
        self.assertEqual(report.effective_input_tokens(row), 94_000)
        self.assertEqual(report.input_bucket(row), "92K-128K")

    def test_service_availability_excludes_client_cancellations(self) -> None:
        rows = [
            {
                "model": "qwen3.5-code",
                "status": "success",
                "terminalReason": "completed",
                "latencyMs": 100,
                "firstByteLatencyMs": 40,
            },
            {
                "model": "qwen3.5-code",
                "status": "failed",
                "terminalReason": "downstream_cancelled",
                "latencyMs": 50,
                "firstByteLatencyMs": 20,
            },
        ]
        summary = report.performance_summary(rows, "model")[0]
        self.assertEqual(summary["successRate"], 0.5)
        self.assertEqual(summary["serviceAvailabilityRate"], 1.0)
        self.assertEqual(summary["clientCancellations"], 1)
        self.assertEqual(summary["firstByteLatencyMs"]["p95"], 40)
        self.assertEqual(summary["firstByteLatencyMs"]["samples"], 2)

    def test_tool_summary_separates_protocol_and_continuation_semantics(self) -> None:
        rows = [
            {"toolUseRequested": True, "status": "success", "toolOutcome": "tool_called"},
            {
                "toolUseRequested": True,
                "status": "success",
                "toolOutcome": "final_answer",
            },
            {
                "toolUseRequested": True,
                "status": "failed",
                "toolOutcome": "protocol_error",
            },
            {"toolUseRequested": True, "status": "success", "toolOutcome": "completed"},
            {
                "toolUseRequested": True,
                "status": "success",
                "toolOutcome": "tool_called",
                "toolRepairAttempted": True,
                "toolRepairRecovered": True,
            },
        ]

        summary = report.tool_use_summary(rows)

        self.assertEqual(summary["requestSuccessRate"], 0.8)
        self.assertEqual(summary["modelToolCalls"], 2)
        self.assertEqual(summary["continuationCompletions"], 1)
        self.assertEqual(summary["observedRequests"], 4)
        self.assertEqual(summary["protocolPassRate"], 0.75)
        self.assertEqual(summary["decisionCoverageRate"], 1.0)
        self.assertEqual(summary["repairAttempts"], 1)
        self.assertEqual(summary["repairRecoveries"], 1)
        self.assertEqual(summary["repairRecoveryRate"], 1.0)

    def test_readiness_payload_is_interpreted_fail_closed(self) -> None:
        self.assertTrue(report.modelport_ready({"status": "ok"}))
        self.assertTrue(report.modelport_ready({"ready": True}))
        self.assertFalse(report.modelport_ready({"ready": False, "detail": "starting"}))
        self.assertFalse(report.modelport_ready({"message": "not ready"}))
        self.assertFalse(report.modelport_ready("ready"))

    def test_terminal_reason_output_is_bounded(self) -> None:
        self.assertEqual(
            report.bounded_terminal_reason({"terminalReason": "completed"}),
            "completed",
        )
        self.assertEqual(
            report.bounded_terminal_reason(
                {"terminalReason": "raw upstream text containing customer data"}
            ),
            "other",
        )


class HistoryStoreTests(unittest.TestCase):
    def test_dashboard_rejects_dns_rebinding_host_header(self) -> None:
        handler = object.__new__(dashboard.DashboardHandler)
        handler.server = SimpleNamespace(server_address=("127.0.0.1", 33004))
        handler.headers = {"Host": "attacker.example:33004"}
        replies = []
        handler.send_json = lambda payload, status: replies.append((payload, status))
        self.assertFalse(handler.validate_host())
        self.assertEqual(replies[0][0], {"error": "host rejected"})

        handler.headers = {"Host": "localhost:33004"}
        self.assertTrue(handler.validate_host())

    def test_dashboard_accepts_only_matching_aggregate_snapshot(self) -> None:
        snapshot = {
            "schemaVersion": 1,
            "generatedAtEpochMs": 1,
            "privacy": {"mode": "aggregate-only"},
            "window": {"hours": 24.0},
        }
        self.assertIs(
            dashboard.DashboardState._validate_snapshot(snapshot, 24.0), snapshot
        )
        with self.assertRaises(RuntimeError):
            dashboard.DashboardState._validate_snapshot(
                {**snapshot, "privacy": {"mode": "raw"}}, 24.0
            )

    def test_dashboard_source_has_no_admin_credential_access(self) -> None:
        source = (ROOT / "scripts" / "operations-dashboard.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("MODELPORT_ADMIN_USERNAME", source)
        self.assertNotIn("MODELPORT_ADMIN_PASSWORD", source)

    def test_history_does_not_invent_ttft_without_stream_samples(self) -> None:
        point = dashboard.DashboardState._history_point(
            {
                "generatedAtEpochMs": 1,
                "window": {"hours": 1},
                "traffic": {
                    "requests": 1,
                    "successRate": 1.0,
                    "latencyMs": {"p95": 120},
                    "firstByteLatencyMs": {"samples": 0, "p95": 0},
                },
                "process": {},
                "toolUse": {},
                "alerts": [],
            }
        )

        self.assertIsNone(point["ttftP95Ms"])

    def test_raw_and_minute_rollup_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = dashboard.HistoryStore(Path(directory) / "history.sqlite3")
            timestamp = int(time.time() * 1000)
            base = {
                "hours": 168.0,
                "requests": 10,
                "successRate": 0.9,
                "availabilityRate": 1.0,
                "p95LatencyMs": 100,
                "ttftP95Ms": 40,
                "toolUseSuccessRate": 1.0,
                "cacheHitRate": 0.25,
                "gpuMemoryUsedMiB": 12_000,
                "generationTokensPerSecond": 90.0,
                "alertCount": 0,
            }
            store.record({**base, "timestamp": timestamp})
            store.record(
                {
                    **base,
                    "timestamp": timestamp + 1,
                    "requests": 20,
                    "ttftP95Ms": 60,
                }
            )
            raw = store.query(10, None)
            minute = store.query(10, 168.0)
            self.assertEqual(len(raw), 2)
            self.assertEqual(len(minute), 1)
            self.assertEqual(minute[0]["requests"], 15.0)
            self.assertEqual(minute[0]["ttftP95Ms"], 50.0)
            self.assertEqual((Path(directory) / "history.sqlite3").stat().st_mode & 0o777, 0o600)


class ToolWorkflowFixtureTests(unittest.TestCase):
    def test_fixture_expands_to_forty_unique_cases_and_five_smoke_cases(self) -> None:
        suite = json.loads(
            (ROOT / "quality" / "tool-workflows.json").read_text(encoding="utf-8")
        )
        cases = tool_workflow.expand_cases(suite)
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({case["id"] for case in cases}), 40)
        self.assertEqual(sum(case["smoke"] for case in cases), 5)
        self.assertEqual(
            next(case for case in cases if case["id"] == "calculator-add")[
                "expectedInput"
            ],
            {"a": 17, "b": 25, "operator": "add"},
        )

    def test_mock_tool_dispatch_rejects_argument_drift(self) -> None:
        case = {
            "expectedTool": "calculate",
            "expectedInput": {"a": 1, "b": 2, "operator": "add"},
            "toolResult": {"result": 3},
        }
        call = {
            "name": "calculate",
            "input": {"a": 1, "b": 2, "operator": "add"},
        }
        self.assertEqual(tool_workflow.execute_mock_tool(case, call), {"result": 3})
        call["input"]["b"] = 4
        with self.assertRaises(ValueError):
            tool_workflow.execute_mock_tool(case, call)

    def test_resilience_fixture_has_bounded_multistep_and_injection_cases(self) -> None:
        suite = json.loads(
            (ROOT / "quality" / "tool-resilience-workflows.json").read_text(
                encoding="utf-8"
            )
        )
        cases = tool_workflow.expand_cases(suite)
        self.assertEqual(len(cases), 4)
        recovery = next(case for case in cases if case["id"] == "error-recovery-retry")
        self.assertEqual(len(tool_workflow.expected_steps(recovery)), 2)
        self.assertTrue(tool_workflow.expected_steps(recovery)[0]["isError"])
        injection = next(case for case in cases if case["id"].startswith("result-injection"))
        self.assertIn("PWNED-7391", injection["finalExcludes"])

    def test_large_mock_tool_result_expansion_is_bounded(self) -> None:
        step = {
            "expectedTool": "read_large_result",
            "expectedInput": {"recordId": "REC-2048"},
            "toolResult": {"summary": "safe"},
            "toolResultRepeat": {"field": "payload", "value": "ab", "count": 8},
        }
        result = tool_workflow.execute_mock_tool(
            {},
            {"name": "read_large_result", "input": {"recordId": "REC-2048"}},
            step,
        )
        self.assertEqual(result["payload"], "ab" * 8)


if __name__ == "__main__":
    unittest.main()
