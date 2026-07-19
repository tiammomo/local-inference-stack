#!/usr/bin/env python3
"""Serve the local Qwen operations dashboard and aggregate-only status API."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import importlib.util
import json
import mimetypes
import os
import sqlite3
import socket
import struct
import threading
import time
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "dashboard"
REPORT_DIR = ROOT_DIR / "logs" / "operations"
ALLOWED_WINDOWS = {1.0, 6.0, 24.0, 168.0}
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LIVE_INTERVAL_SECONDS = 2.0
STATUS_INTERVAL_SECONDS = 5.0
HISTORY_INTERVAL_SECONDS = 30.0
HISTORY_DB_PATH = Path(
    os.environ.get(
        "OPERATIONS_HISTORY_DB",
        ROOT_DIR / "logs" / "operations" / "history.sqlite3",
    )
)


class HistoryStore:
    """Bounded aggregate-only history with raw, minute, and hourly retention."""

    NUMERIC_FIELDS = (
        "requests",
        "successRate",
        "availabilityRate",
        "p95LatencyMs",
        "ttftP95Ms",
        "toolUseSuccessRate",
        "cacheHitRate",
        "gpuMemoryUsedMiB",
        "generationTokensPerSecond",
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history_points (
                    resolution TEXT NOT NULL,
                    bucket_ms INTEGER NOT NULL,
                    window_hours REAL NOT NULL,
                    samples INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (resolution, bucket_ms, window_hours)
                )
                """
            )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _bucket(timestamp: int, width_ms: int) -> int:
        return timestamp - timestamp % width_ms

    def _upsert_rollup(
        self,
        connection: sqlite3.Connection,
        resolution: str,
        width_ms: int,
        point: dict[str, Any],
    ) -> None:
        bucket = self._bucket(int(point["timestamp"]), width_ms)
        hours = float(point.get("hours") or 0)
        row = connection.execute(
            "SELECT samples, payload FROM history_points "
            "WHERE resolution = ? AND bucket_ms = ? AND window_hours = ?",
            (resolution, bucket, hours),
        ).fetchone()
        samples = int(row[0]) if row else 0
        previous = json.loads(row[1]) if row else {}
        merged = dict(point)
        merged["timestamp"] = bucket
        merged["resolution"] = resolution
        for field in self.NUMERIC_FIELDS:
            current = point.get(field)
            old = previous.get(field)
            if not isinstance(current, (int, float)):
                merged[field] = old
            elif isinstance(old, (int, float)) and samples:
                merged[field] = round((old * samples + current) / (samples + 1), 6)
        merged["alertCount"] = max(
            int(previous.get("alertCount") or 0),
            int(point.get("alertCount") or 0),
        )
        connection.execute(
            "INSERT OR REPLACE INTO history_points "
            "(resolution, bucket_ms, window_hours, samples, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                resolution,
                bucket,
                hours,
                samples + 1,
                json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def record(self, point: dict[str, Any]) -> None:
        timestamp = point.get("timestamp")
        hours = point.get("hours")
        if not isinstance(timestamp, int) or not isinstance(hours, (int, float)):
            return
        raw = dict(point)
        raw["resolution"] = "raw"
        now_ms = int(time.time() * 1000)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO history_points "
                "(resolution, bucket_ms, window_hours, samples, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "raw",
                    timestamp,
                    float(hours),
                    1,
                    json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self._upsert_rollup(connection, "minute", 60_000, point)
            self._upsert_rollup(connection, "hour", 3_600_000, point)
            connection.execute(
                "DELETE FROM history_points "
                "WHERE resolution = 'raw' AND bucket_ms < ?",
                (now_ms - 24 * 3_600_000,),
            )
            connection.execute(
                "DELETE FROM history_points "
                "WHERE resolution = 'minute' AND bucket_ms < ?",
                (now_ms - 30 * 24 * 3_600_000,),
            )
            connection.execute(
                "DELETE FROM history_points "
                "WHERE resolution = 'hour' AND bucket_ms < ?",
                (now_ms - 365 * 24 * 3_600_000,),
            )

    def query(self, limit: int, hours: float | None) -> list[dict[str, Any]]:
        if hours is None or hours <= 24:
            resolution = "raw"
        elif hours <= 30 * 24:
            resolution = "minute"
        else:
            resolution = "hour"
        statement = (
            "SELECT payload FROM history_points WHERE resolution = ? "
            + ("AND window_hours = ? " if hours is not None else "")
            + "ORDER BY bucket_ms DESC LIMIT ?"
        )
        parameters: tuple[Any, ...] = (
            (resolution, float(hours), limit)
            if hours is not None
            else (resolution, limit)
        )
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]


def load_operations_module() -> ModuleType:
    path = ROOT_DIR / "scripts" / "operations-report.py"
    spec = importlib.util.spec_from_file_location("qwen_operations_report", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load operations report module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashboardState:
    def __init__(self, cache_seconds: int) -> None:
        self.module = load_operations_module()
        self.cache_seconds = cache_seconds
        self.lock = threading.Lock()
        self.cache: dict[float, tuple[float, dict[str, Any]]] = {}
        self.live_lock = threading.Lock()
        self.live_cache: tuple[float, dict[str, Any]] | None = None
        self.container_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.history_lock = threading.Lock()
        self.history_store = HistoryStore(HISTORY_DB_PATH)
        self.baseline = json.loads(
            (STATIC_DIR / "runtime-baseline.json").read_text(encoding="utf-8")
        )
        self.admin = self._new_admin()

    def _new_admin(self) -> Any:
        username = os.environ.get("MODELPORT_ADMIN_USERNAME", "")
        password = os.environ.get("MODELPORT_ADMIN_PASSWORD", "")
        if not username or not password:
            raise RuntimeError(
                "MODELPORT_ADMIN_USERNAME and MODELPORT_ADMIN_PASSWORD must be set"
            )
        base_url = os.environ.get("MODELPORT_BASE_URL", "http://127.0.0.1:38082")
        return self.module.AdminClient(base_url, username, password)

    def _args(self, hours: float) -> SimpleNamespace:
        return SimpleNamespace(
            hours=hours,
            modelport_url=os.environ.get(
                "MODELPORT_BASE_URL", "http://127.0.0.1:38082"
            ),
            qwen_url=os.environ.get("QWEN_RUNTIME_URL", "http://127.0.0.1:18080"),
            max_records=int(os.environ.get("OPERATIONS_DASHBOARD_MAX_RECORDS", "5000")),
            unreconciled_baseline=int(
                os.environ.get("OPERATIONS_UNRECONCILED_BASELINE", "0")
            ),
            include_synthetic=False,
            provider=["local_qwen"],
            resolved_model=[],
            failure_rate_warn=float(
                os.environ.get("OPERATIONS_FAILURE_RATE_WARN", "0.05")
            ),
            tool_failure_rate_warn=float(
                os.environ.get("OPERATIONS_TOOL_FAILURE_RATE_WARN", "0.05")
            ),
            p95_latency_ms_warn=int(
                os.environ.get("OPERATIONS_P95_LATENCY_MS_WARN", "180000")
            ),
        )

    def report(self, hours: float, force: bool = False) -> dict[str, Any]:
        with self.lock:
            cached = self.cache.get(hours)
            if not force and cached and time.monotonic() - cached[0] < self.cache_seconds:
                return cached[1]
            try:
                report = self.module.build_report(self._args(hours), admin=self.admin)
            except RuntimeError as error:
                if "401" not in str(error) and "403" not in str(error):
                    raise
                self.admin = self._new_admin()
                report = self.module.build_report(self._args(hours), admin=self.admin)
            self.cache[hours] = (time.monotonic(), report)
            self._record_realtime(report)
            return report

    @staticmethod
    def _history_point(report: dict[str, Any]) -> dict[str, Any]:
        traffic = report.get("traffic", {})
        process = report.get("process", {})
        qwen = process.get("qwen", {}).get("metrics", {})
        gpu = process.get("gpu") or {}
        return {
            "timestamp": report.get("generatedAtEpochMs"),
            "hours": report.get("window", {}).get("hours"),
            "requests": traffic.get("requests", 0),
            "successRate": traffic.get(
                "serviceAvailabilityRate", traffic.get("successRate")
            ),
            "availabilityRate": traffic.get("serviceAvailabilityRate"),
            "p95LatencyMs": traffic.get("latencyMs", {}).get("p95", 0),
            "ttftP95Ms": (
                traffic.get("firstByteLatencyMs", {}).get("p95")
                if traffic.get("firstByteLatencyMs", {}).get("samples", 0) > 0
                else None
            ),
            "toolUseSuccessRate": report.get("toolUse", {}).get(
                "protocolPassRate", report.get("toolUse", {}).get("requestSuccessRate")
            ),
            "cacheHitRate": traffic.get("tokens", {}).get("cacheHitRate"),
            "gpuMemoryUsedMiB": gpu.get("memoryUsedMiB"),
            "generationTokensPerSecond": qwen.get("generatedTokensPerSecond"),
            "alertCount": len(report.get("alerts", [])),
        }

    def _record_realtime(self, report: dict[str, Any]) -> None:
        point = self._history_point(report)
        if not isinstance(point.get("timestamp"), int):
            return
        with self.history_lock:
            self.history_store.record(point)

    def live(self, force: bool = False) -> dict[str, Any]:
        with self.live_lock:
            now = time.monotonic()
            if (
                not force
                and self.live_cache
                and now - self.live_cache[0] < LIVE_INTERVAL_SECONDS * 0.75
            ):
                return self.live_cache[1]
            qwen_url = os.environ.get("QWEN_RUNTIME_URL", "http://127.0.0.1:18080")
            qwen = self.module.qwen_snapshot(qwen_url)
            if (
                not self.container_cache
                or now - self.container_cache[0] >= STATUS_INTERVAL_SECONDS * 2
            ):
                self.container_cache = (now, self.module.container_snapshot())
            snapshot = {
                "generatedAtEpochMs": int(time.time() * 1000),
                "health": {"qwenHealthy": qwen["healthy"]},
                "process": {
                    "qwen": qwen,
                    "gpu": self.module.gpu_snapshot(),
                    "host": self.module.host_snapshot(),
                    "containers": self.container_cache[1],
                },
            }
            self.live_cache = (now, snapshot)
            return snapshot

    def history(self, limit: int, hours: float | None = None) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        for path in sorted(REPORT_DIR.glob("*.json"), reverse=True)[: max(limit * 3, 30)]:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                if report.get("scope", {}).get("providers") != ["local_qwen"]:
                    continue
                points.append(self._history_point(report))
            except (OSError, ValueError, TypeError):
                continue
        with self.history_lock:
            points.extend(self.history_store.query(limit, hours))
        points = [point for point in points if isinstance(point.get("timestamp"), int)]
        if hours is not None:
            points = [point for point in points if point.get("hours") == hours]
        points = list({point["timestamp"]: point for point in points}.values())
        points.sort(key=lambda point: point["timestamp"])
        return points[-limit:]


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "LocalInferenceDashboard/1"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> DashboardState:
        return self.server.dashboard_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/ws":
            self.handle_websocket()
            return
        if parsed.path == "/api/health":
            self.send_json({"status": "ok", "service": "local-inference-dashboard"})
            return
        if parsed.path == "/api/status":
            self.handle_status(parsed.query)
            return
        if parsed.path == "/api/history":
            self.handle_history(parsed.query)
            return
        self.serve_static(parsed.path)

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_common_headers("no-store")
            self.end_headers()
            return
        self.serve_static(parsed.path, head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self.send_json({"error": "read-only dashboard"}, HTTPStatus.METHOD_NOT_ALLOWED)

    def handle_status(self, query: str) -> None:
        params = parse_qs(query)
        try:
            hours = float(params.get("hours", ["24"])[0])
            if hours not in ALLOWED_WINDOWS:
                raise ValueError
            force = params.get("refresh", ["0"])[0] == "1"
            self.send_json(self.state.report(hours, force=force))
        except ValueError:
            self.send_json(
                {"error": "hours must be one of 1, 6, 24, 168"},
                HTTPStatus.BAD_REQUEST,
            )
        except RuntimeError as error:
            self.send_json(
                {"error": "runtime status is temporarily unavailable", "detail": str(error)},
                HTTPStatus.BAD_GATEWAY,
            )

    def handle_history(self, query: str) -> None:
        params = parse_qs(query)
        try:
            limit = int(params.get("limit", ["48"])[0])
            if limit < 1 or limit > 200:
                raise ValueError
            hours = float(params["hours"][0]) if "hours" in params else None
            if hours is not None and hours not in ALLOWED_WINDOWS:
                raise ValueError
            self.send_json({"points": self.state.history(limit, hours)})
        except ValueError:
            self.send_json(
                {"error": "limit must be in [1, 200] and hours in 1, 6, 24, 168"},
                HTTPStatus.BAD_REQUEST,
            )

    def handle_websocket(self) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_json(
                {"error": "websocket upgrade required"}, HTTPStatus.UPGRADE_REQUIRED
            )
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        if origin not in {f"http://{host}", f"https://{host}"}:
            self.send_json({"error": "origin rejected"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get("Sec-WebSocket-Version") != "13":
            self.send_json(
                {"error": "websocket version 13 required"},
                HTTPStatus.UPGRADE_REQUIRED,
            )
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            decoded_key = base64.b64decode(key, validate=True)
        except (ValueError, binascii.Error):
            decoded_key = b""
        if len(decoded_key) != 16:
            self.send_json({"error": "invalid websocket key"}, HTTPStatus.BAD_REQUEST)
            return
        accept = base64.b64encode(
            hashlib.sha1(f"{key}{WEBSOCKET_GUID}".encode("ascii")).digest()
        ).decode("ascii")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        try:
            self.websocket_loop()
        except (BrokenPipeError, ConnectionResetError, EOFError, OSError):
            pass

    def websocket_loop(self) -> None:
        hours = 24.0
        next_live = 0.0
        next_status = 0.0
        next_history = 0.0
        self.connection.settimeout(0.25)
        self.websocket_send(
            {
                "type": "hello",
                "protocolVersion": 1,
                "baseline": self.state.baseline,
                "cadenceSeconds": {
                    "live": LIVE_INTERVAL_SECONDS,
                    "status": STATUS_INTERVAL_SECONDS,
                    "history": HISTORY_INTERVAL_SECONDS,
                },
            }
        )
        while True:
            now = time.monotonic()
            if now >= next_live:
                self.websocket_send({"type": "live", "data": self.state.live()})
                next_live = now + LIVE_INTERVAL_SECONDS
            if now >= next_status:
                report = self.state.report(hours)
                self.websocket_send({"type": "status", "data": report})
                next_status = now + STATUS_INTERVAL_SECONDS
            if now >= next_history:
                self.websocket_send(
                    {"type": "history", "points": self.state.history(120, hours)}
                )
                next_history = now + HISTORY_INTERVAL_SECONDS
            try:
                message = self.websocket_receive()
            except socket.timeout:
                continue
            if message is None:
                return
            action = message.get("type")
            requested_hours = message.get("hours", hours)
            try:
                requested_hours = float(requested_hours)
                if requested_hours not in ALLOWED_WINDOWS:
                    raise ValueError
            except (TypeError, ValueError):
                self.websocket_send(
                    {"type": "error", "message": "hours must be one of 1, 6, 24, 168"}
                )
                continue
            if action == "subscribe":
                hours = requested_hours
                next_status = 0.0
                next_history = 0.0
                self.websocket_send({"type": "subscribed", "hours": hours})
            elif action == "refresh":
                hours = requested_hours
                report = self.state.report(hours, force=True)
                self.websocket_send({"type": "status", "data": report})
                self.websocket_send({"type": "live", "data": self.state.live(force=True)})
                self.websocket_send(
                    {"type": "history", "points": self.state.history(120, hours)}
                )
                next_live = time.monotonic() + LIVE_INTERVAL_SECONDS
                next_status = time.monotonic() + STATUS_INTERVAL_SECONDS
                next_history = time.monotonic() + HISTORY_INTERVAL_SECONDS

    def websocket_receive(self) -> dict[str, Any] | None:
        header = self.websocket_read_exact(2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.websocket_read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.websocket_read_exact(8))[0]
        if not masked or length > 65_536:
            raise OSError("invalid websocket frame")
        mask = self.websocket_read_exact(4)
        payload = self.websocket_read_exact(length)
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 0x8:
            self.websocket_send_frame(payload, opcode=0x8)
            return None
        if opcode == 0x9:
            self.websocket_send_frame(payload, opcode=0xA)
            return {}
        if opcode == 0xA:
            return {}
        if opcode != 0x1 or not first & 0x80:
            raise OSError("unsupported websocket frame")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def websocket_read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.connection.recv(length - len(chunks))
            if not chunk:
                raise EOFError
            chunks.extend(chunk)
        return bytes(chunks)

    def websocket_send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.websocket_send_frame(body)

    def websocket_send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, length))
        elif length <= 65_535:
            header = bytes((0x80 | opcode, 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 127)) + struct.pack("!Q", length)
        self.connection.sendall(header + payload)

    def serve_static(self, request_path: str, head_only: bool = False) -> None:
        relative = "index.html" if request_path in ("", "/") else request_path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            if Path(relative).suffix:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            candidate = STATIC_DIR / "index.html"
        try:
            body = candidate.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_common_headers("no-cache")
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_common_headers("no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_common_headers(self, cache_control: str) -> None:
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def log_message(self, message: str, *args: Any) -> None:
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"client={self.client_address[0]} {message % args}",
            flush=True,
        )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: DashboardState) -> None:
        self.dashboard_state = state
        super().__init__(address, DashboardHandler)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=33004)
    parser.add_argument("--cache-seconds", type=int, default=5)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("dashboard must bind to a loopback address")
    if args.port < 1024 or args.port > 65535:
        parser.error("--port must be in [1024, 65535]")
    if args.cache_seconds < 1 or args.cache_seconds > 300:
        parser.error("--cache-seconds must be in [1, 300]")
    return args


def main() -> int:
    args = parse_args()
    try:
        state = DashboardState(args.cache_seconds)
        server = DashboardServer((args.host, args.port), state)
    except (OSError, RuntimeError) as error:
        print(f"operations dashboard failed to start: {error}", flush=True)
        return 2
    print(f"Local Inference Stack dashboard listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
