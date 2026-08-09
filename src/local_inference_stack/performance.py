"""Manifest-backed performance gates and non-promotable baseline collection policy."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = (
    ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
)
MIN_CALIBRATION_RUNS = 3
HARD_GATE_OPERATORS = {
    "decodeTokensPerSecond": "minimum",
    "aggregateTokensPerSecond": "minimum",
    "peakVramMiB": "maximum",
}
WARNING_GATE_OPERATORS = {
    "ttftMs": "maximum",
    "prefillTokensPerSecond": "minimum",
}


class PerformancePolicyError(ValueError):
    """The manifest claims an enforced policy but cannot enforce it safely."""


def _positive_number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PerformancePolicyError(f"{label} must be a finite positive number")
    return float(value)


def _parse_gates(
    document: Any,
    operators: dict[str, str],
    *,
    required: bool,
) -> dict[str, float]:
    if document is None and not required:
        return {}
    if not isinstance(document, dict):
        raise PerformancePolicyError("performance gates must be an object")
    unknown = set(document) - set(operators)
    if unknown:
        raise PerformancePolicyError(
            "unknown performance metrics: " + ", ".join(sorted(unknown))
        )
    if required and set(document) != set(operators):
        missing = set(operators) - set(document)
        raise PerformancePolicyError(
            "enforced performance policy is missing: " + ", ".join(sorted(missing))
        )
    parsed: dict[str, float] = {}
    for metric, gate in document.items():
        operator = operators[metric]
        if not isinstance(gate, dict) or set(gate) != {operator}:
            raise PerformancePolicyError(
                f"{metric} must declare exactly one {operator} threshold"
            )
        parsed[metric] = _positive_number(gate[operator], f"{metric}.{operator}")
    return parsed


@dataclass(frozen=True)
class PerformancePolicy:
    status: str
    calibration_runs: int
    hard_gates: dict[str, float]
    warning_gates: dict[str, float]
    reasons: tuple[str, ...] = ()

    @property
    def enforced(self) -> bool:
        return self.status == "enforced"

    def summary(self, *, baseline_only: bool = False) -> dict[str, Any]:
        if baseline_only:
            eligibility = "baseline-only-not-promotable"
        elif self.enforced:
            eligibility = "requires-benchmark-results"
        else:
            eligibility = "ineligible-pending-baseline"
        return {
            "schemaVersion": 1,
            "performancePolicy": self.status,
            "calibrationRuns": self.calibration_runs,
            "minimumCalibrationRuns": MIN_CALIBRATION_RUNS,
            "hardGates": {
                metric: {HARD_GATE_OPERATORS[metric]: threshold}
                for metric, threshold in self.hard_gates.items()
            },
            "warningGates": {
                metric: {WARNING_GATE_OPERATORS[metric]: threshold}
                for metric, threshold in self.warning_gates.items()
            },
            "baselineCollectionAllowed": True,
            "promotionEligible": False,
            "evidenceEligibility": eligibility,
            "reasons": list(self.reasons),
        }


def load_policy(manifest_path: Path = DEFAULT_MANIFEST) -> PerformancePolicy:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PerformancePolicyError(f"cannot read deployment manifest: {exc}") from exc
    performance = manifest.get("performance")
    if performance is None:
        return PerformancePolicy(
            status="pending-baseline",
            calibration_runs=0,
            hard_gates={},
            warning_gates={},
            reasons=("deployment manifest has no calibrated performance policy",),
        )
    if not isinstance(performance, dict):
        raise PerformancePolicyError("manifest performance policy must be an object")
    status = performance.get("policy", "pending-baseline")
    if status == "pending-baseline":
        runs = performance.get("calibrationRuns", 0)
        if not isinstance(runs, int) or isinstance(runs, bool) or runs < 0:
            raise PerformancePolicyError("performance calibrationRuns must be non-negative")
        return PerformancePolicy(
            status=status,
            calibration_runs=runs,
            hard_gates={},
            warning_gates=_parse_gates(
                performance.get("warningGates"), WARNING_GATE_OPERATORS, required=False
            ),
            reasons=("hard thresholds are not backed by enough calibration runs",),
        )
    if status != "enforced":
        raise PerformancePolicyError(f"unknown performance policy status: {status!r}")
    runs = performance.get("calibrationRuns")
    if (
        not isinstance(runs, int)
        or isinstance(runs, bool)
        or runs < MIN_CALIBRATION_RUNS
    ):
        raise PerformancePolicyError(
            f"enforced performance policy requires at least {MIN_CALIBRATION_RUNS} calibration runs"
        )
    return PerformancePolicy(
        status=status,
        calibration_runs=runs,
        hard_gates=_parse_gates(
            performance.get("hardGates"), HARD_GATE_OPERATORS, required=True
        ),
        warning_gates=_parse_gates(
            performance.get("warningGates"), WARNING_GATE_OPERATORS, required=False
        ),
    )


def _gate_result(
    metric: str,
    actual: float | None,
    threshold: float,
    operator: str,
    *,
    severity: str,
) -> dict[str, Any]:
    if (
        actual is None
        or isinstance(actual, bool)
        or not isinstance(actual, (int, float))
        or not math.isfinite(actual)
    ):
        return {
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
            "actual": actual,
            "passed": False,
            "severity": severity,
            "reason": "metric-not-measured",
        }
    passed = actual >= threshold if operator == "minimum" else actual <= threshold
    return {
        "metric": metric,
        "operator": operator,
        "threshold": threshold,
        "actual": actual,
        "passed": passed,
        "severity": severity,
        "reason": "within-policy" if passed else "threshold-violation",
    }


def evaluate(
    policy: PerformancePolicy,
    metrics: dict[str, float | None],
    *,
    required_metrics: Iterable[str],
    baseline_only: bool = False,
) -> dict[str, Any]:
    required = set(required_metrics)
    unknown = required - set(HARD_GATE_OPERATORS)
    if unknown:
        raise PerformancePolicyError(
            "unknown required performance metrics: " + ", ".join(sorted(unknown))
        )
    if not policy.enforced:
        missing = [
            metric
            for metric in sorted(required)
            if (
                isinstance(metrics.get(metric), bool)
                or not isinstance(metrics.get(metric), (int, float))
                or not math.isfinite(metrics[metric])
                or metrics[metric] <= 0
            )
        ]
        summary = policy.summary(baseline_only=baseline_only)
        if missing:
            summary["reasons"] = [
                *summary["reasons"],
                "required measurements are missing: " + ", ".join(missing),
            ]
        return {
            **summary,
            "metrics": metrics,
            "hardGateResults": [],
            "warningResults": [],
            "hardGatesPassed": None,
            "measurementsComplete": not missing,
            "benchmarkPassed": baseline_only and not missing,
        }
    hard_results = [
        _gate_result(
            metric,
            metrics.get(metric),
            policy.hard_gates[metric],
            HARD_GATE_OPERATORS[metric],
            severity="hard",
        )
        for metric in sorted(required)
    ]
    warning_results = [
        _gate_result(
            metric,
            metrics.get(metric),
            threshold,
            WARNING_GATE_OPERATORS[metric],
            severity="warning",
        )
        for metric, threshold in sorted(policy.warning_gates.items())
    ]
    hard_passed = all(result["passed"] for result in hard_results)
    promotion_eligible = hard_passed and not baseline_only
    if baseline_only:
        eligibility = "baseline-only-not-promotable"
    elif hard_passed:
        eligibility = "performance-gates-passed"
    else:
        eligibility = "performance-gates-failed"
    return {
        "schemaVersion": 1,
        "performancePolicy": policy.status,
        "calibrationRuns": policy.calibration_runs,
        "metrics": metrics,
        "hardGateResults": hard_results,
        "warningResults": warning_results,
        "hardGatesPassed": hard_passed,
        "measurementsComplete": all(
            result["reason"] != "metric-not-measured" for result in hard_results
        ),
        "benchmarkPassed": hard_passed,
        "promotionEligible": promotion_eligible,
        "evidenceEligibility": eligibility,
        "reasons": [],
    }


class PeakVramSampler:
    """Sample single-host NVIDIA memory without changing GPU or runtime state."""

    def __init__(self, interval_seconds: float = 0.1):
        self.interval_seconds = interval_seconds
        self.peak_mib: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            values = [
                float(line.strip())
                for line in result.stdout.splitlines()
                if line.strip()
            ]
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            return
        if values:
            observed = max(values)
            self.peak_mib = observed if self.peak_mib is None else max(self.peak_mib, observed)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> float | None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 3))
        self._sample()
        return self.peak_mib


def default_evidence_path(kind: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return ROOT_DIR / "logs" / "performance" / f"{stamp}-{kind}.json"


def write_private_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="inspect manifest-backed performance policy"
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = load_policy(args.manifest)
    except PerformancePolicyError as exc:
        result = {
            "schemaVersion": 1,
            "performancePolicy": "invalid-policy",
            "promotionEligible": False,
            "evidenceEligibility": "ineligible-invalid-policy",
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print("performancePolicy=invalid-policy")
            print(f"error={exc}")
        return 2
    result = policy.summary(baseline_only=args.baseline_only)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"performancePolicy={result['performancePolicy']}")
        print(f"evidenceEligibility={result['evidenceEligibility']}")
        for reason in result["reasons"]:
            print(f"reason={reason}")
    if policy.enforced or args.baseline_only:
        return 0
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
