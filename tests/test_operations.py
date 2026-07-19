from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
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


class OperationsReportTests(unittest.TestCase):
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
        ]

        summary = report.tool_use_summary(rows)

        self.assertEqual(summary["requestSuccessRate"], 0.75)
        self.assertEqual(summary["modelToolCalls"], 1)
        self.assertEqual(summary["continuationCompletions"], 1)
        self.assertEqual(summary["observedRequests"], 3)
        self.assertEqual(summary["protocolPassRate"], round(2 / 3, 6))
        self.assertEqual(summary["decisionCoverageRate"], 1.0)


class HistoryStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
