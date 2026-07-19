from __future__ import annotations

import importlib.util
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


class HistoryStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
