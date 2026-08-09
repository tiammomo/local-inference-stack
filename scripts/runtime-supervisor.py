#!/usr/bin/env python3
"""Supervise the approved runtime without bypassing deployment admission."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Callable

try:
    from scripts.env_utils import (
        atomic_write_private_text,
        is_private_regular_file,
        parse_env_file,
    )
    from scripts.local_http import direct_urlopen
except ModuleNotFoundError:  # Direct execution from scripts/.
    from env_utils import atomic_write_private_text, is_private_regular_file, parse_env_file
    from local_http import direct_urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.paths import ProjectPaths  # noqa: E402
from local_inference_stack.transactions import (  # noqa: E402
    TransactionStore,
    open_private_lock,
    recovery_original_is_safe,
)

RUNTIME_UNIT = "qwen-model-runtime.service"
PERMANENT_FAILURE_EXIT = 78


class Step(Enum):
    HEALTHY = "healthy"
    RECOVERED = "recovered"
    WAIT_DOCKER = "wait-docker"
    WAIT_STARTING = "wait-starting"
    WAIT_TRANSACTION = "wait-transaction"
    WAIT_LOCK = "wait-lock"
    PERMANENT_FAILURE = "permanent-failure"


def positive_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if not 1 <= value <= 86400:
        raise ValueError(f"{name} must be in [1, 86400]")
    return value


class RuntimeSupervisor:
    def __init__(
        self,
        root_dir: Path = ROOT_DIR,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root_dir = root_dir
        self.clock = clock
        self.sleeper = sleeper
        self.docker_retry_seconds = positive_seconds(
            "QWEN_DOCKER_RETRY_SECONDS", 60
        )
        self.docker_alert_seconds = positive_seconds(
            "QWEN_DOCKER_ALERT_SECONDS", 600
        )
        self.health_seconds = positive_seconds(
            "QWEN_RUNTIME_HEALTH_SECONDS", 300
        )
        self.start_poll_seconds = positive_seconds(
            "QWEN_RUNTIME_START_POLL_SECONDS", 10
        )
        self.reconcile_timeout_seconds = positive_seconds(
            "QWEN_RECONCILE_TIMEOUT_SECONDS", 600
        )
        self.wait_state = (
            root_dir / "cache" / "runtime-supervisor" / "docker-wait-started"
        )
        self.alert_marker = root_dir / "logs" / "alerts" / f"{RUNTIME_UNIT}.json"
        self.container_name = self._deployment_values().get(
            "QWEN_CONTAINER_NAME", "qwen35-9b-q5km"
        )

    def _deployment_values(self) -> dict[str, str]:
        profile = self.root_dir / "profiles" / "deployment.local.env"
        if not profile.exists():
            return {}
        if not is_private_regular_file(profile):
            raise RuntimeError(
                "deployment.local.env must be a private current-user-owned regular file"
            )
        return parse_env_file(profile)

    def _run(
        self,
        command: list[str],
        *,
        timeout: int,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.root_dir,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )

    def docker_available(self) -> bool:
        try:
            result = self._run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                timeout=10,
                capture=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def runtime_healthy(self) -> bool:
        try:
            with direct_urlopen(
                "http://127.0.0.1:18080/health", timeout=3
            ) as response:
                response.read(1024)
                return 200 <= response.status < 300
        except OSError:
            return False

    def container_health(self) -> str:
        try:
            result = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    self.container_name,
                ],
                timeout=10,
                capture=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown"
        return result.stdout.strip() if result.returncode == 0 else "absent"

    def profile_is_canonical(self) -> bool:
        try:
            result = self._run(
                [str(self.root_dir / "scripts" / "runtime.sh"), "assert-profile", "latency"],
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def reconcile(self) -> bool:
        try:
            result = self._run(
                [str(self.root_dir / "scripts" / "runtime-reconcile.sh")],
                timeout=self.reconcile_timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def reconcile_transaction(self) -> bool:
        try:
            result = self._run(
                [str(self.root_dir / "stack"), "reconcile", "--yes", "--json"],
                timeout=self.reconcile_timeout_seconds,
                capture=True,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def transaction_plan(self) -> dict[str, object]:
        try:
            return TransactionStore(ProjectPaths(self.root_dir)).reconciliation_plan()
        except Exception as error:
            raise RuntimeError(f"cannot inspect the control transaction: {error}") from error

    def runtime_lock_active(self) -> bool:
        path = self.root_dir / "cache" / "locks" / "runtime.lock"
        if not path.exists() and not path.is_symlink():
            return False
        try:
            descriptor = open_private_lock(path)
        except Exception as error:
            raise RuntimeError(f"cannot safely inspect the runtime lock: {error}") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        finally:
            os.close(descriptor)

    def _alert(self, action: str) -> bool:
        try:
            result = self._run(
                [
                    str(self.root_dir / "scripts" / "production-alert.sh"),
                    action,
                    RUNTIME_UNIT,
                ],
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def note_docker_wait(self) -> None:
        now = int(self.clock())
        if self.wait_state.exists():
            if not is_private_regular_file(self.wait_state):
                raise RuntimeError(f"unsafe Docker wait state: {self.wait_state}")
            try:
                started = int(self.wait_state.read_text(encoding="utf-8").strip())
            except ValueError as error:
                raise RuntimeError(
                    f"invalid Docker wait state: {self.wait_state}"
                ) from error
        else:
            started = now
            atomic_write_private_text(self.wait_state, str(started))
        waited = max(0, now - started)
        if self.alert_marker.exists() and not is_private_regular_file(self.alert_marker):
            raise RuntimeError(f"unsafe runtime alert marker: {self.alert_marker}")
        if waited >= self.docker_alert_seconds and not self.alert_marker.exists():
            if not self._alert("fire"):
                raise RuntimeError("failed to record the Docker wait alert")
        print(
            f"Docker backend unavailable; retrying in {self.docker_retry_seconds}s "
            f"(waited {waited}s).",
            flush=True,
        )

    def clear_docker_wait(self) -> None:
        if not self.wait_state.exists():
            return
        if not is_private_regular_file(self.wait_state):
            raise RuntimeError(f"unsafe Docker wait state: {self.wait_state}")
        self.wait_state.unlink()

    def clear_alert(self) -> None:
        if not self._alert("clear"):
            raise RuntimeError("failed to clear the runtime alert")

    def step(self) -> Step:
        transaction = self.transaction_plan()
        if transaction.get("required"):
            document = transaction.get("transaction")
            state = document.get("state") if isinstance(document, dict) else None
            original = document.get("original") if isinstance(document, dict) else None
            eligible = bool(
                transaction.get("classification") == "recovery-required"
                and transaction.get("automaticEligible") is True
                and recovery_original_is_safe(original)
            )
            if not eligible:
                print(
                    f"Control transaction state {state!r} requires review; "
                    "the supervisor will wait without mutating runtime.",
                    flush=True,
                )
                return Step.WAIT_TRANSACTION
            if self.runtime_lock_active():
                print(
                    "Runtime mutation lock is busy; waiting before transaction recovery.",
                    flush=True,
                )
                return Step.WAIT_LOCK
            if not self.docker_available():
                self.note_docker_wait()
                return Step.WAIT_DOCKER
            print(
                "A safe recovery_required transaction was found; running controlled reconciliation.",
                flush=True,
            )
            if not self.reconcile_transaction():
                if self.runtime_lock_active():
                    return Step.WAIT_LOCK
                if not self.docker_available():
                    self.note_docker_wait()
                    return Step.WAIT_DOCKER
                print(
                    "Transaction reconciliation failed with Docker available; "
                    "manual review is required.",
                    file=sys.stderr,
                    flush=True,
                )
                return Step.PERMANENT_FAILURE
            return Step.RECOVERED

        if self.runtime_lock_active():
            print("Runtime mutation lock is busy; waiting.", flush=True)
            return Step.WAIT_LOCK
        if not self.docker_available():
            self.note_docker_wait()
            return Step.WAIT_DOCKER

        self.clear_docker_wait()
        if self.runtime_healthy():
            if not self.profile_is_canonical():
                print(
                    "Runtime is healthy but its profile or container identity drifted; "
                    "automatic mutation is refused.",
                    file=sys.stderr,
                    flush=True,
                )
                return Step.PERMANENT_FAILURE
            self.clear_alert()
            return Step.HEALTHY

        if self.container_health() == "starting":
            print("Runtime container is still starting; waiting before recovery.", flush=True)
            return Step.WAIT_STARTING

        print("Runtime is unhealthy; attempting one controlled reconciliation.", flush=True)
        if not self.reconcile():
            current_transaction = self.transaction_plan()
            if current_transaction.get("required"):
                return Step.WAIT_TRANSACTION
            if self.runtime_lock_active():
                return Step.WAIT_LOCK
            if not self.docker_available():
                self.note_docker_wait()
                return Step.WAIT_DOCKER
            print(
                "Runtime reconciliation failed with Docker available; "
                "manual review is required.",
                file=sys.stderr,
                flush=True,
            )
            return Step.PERMANENT_FAILURE
        if not self.runtime_healthy() or not self.profile_is_canonical():
            print(
                "Runtime did not become healthy and canonical after reconciliation.",
                file=sys.stderr,
                flush=True,
            )
            return Step.PERMANENT_FAILURE
        self.clear_alert()
        return Step.RECOVERED

    def run(self, *, once: bool = False) -> int:
        while True:
            try:
                step = self.step()
            except (OSError, RuntimeError, ValueError) as error:
                print(f"Runtime supervisor stopped safely: {error}", file=sys.stderr)
                return PERMANENT_FAILURE_EXIT
            if step is Step.PERMANENT_FAILURE:
                return PERMANENT_FAILURE_EXIT
            if once:
                return 0 if step in {Step.HEALTHY, Step.RECOVERED} else 75
            if step is Step.WAIT_DOCKER:
                delay = self.docker_retry_seconds
            elif step in {
                Step.WAIT_STARTING,
                Step.WAIT_TRANSACTION,
                Step.WAIT_LOCK,
            }:
                delay = self.start_poll_seconds
            else:
                delay = self.health_seconds
            self.sleeper(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one supervisor cycle for diagnostics instead of monitoring",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        supervisor = RuntimeSupervisor()
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Runtime supervisor configuration is invalid: {error}", file=sys.stderr)
        return PERMANENT_FAILURE_EXIT
    return supervisor.run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
