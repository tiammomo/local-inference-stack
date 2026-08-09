"""Read-only, structured health probes for optional local components."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from scripts.local_http import direct_urlopen

from .result import ExternalError
from .runner import run


MODELPORT_CONTAINER = "modelport-modelport-1"
MODELPORT_URL = "http://127.0.0.1:38082/livez"
DASHBOARD_URL = "http://127.0.0.1:33004/api/health"
DASHBOARD_UNIT = "qwen-model-operations-dashboard.service"
OPERATIONS_UNITS = (
    "qwen-model-operations-report.timer",
    "qwen-model-backup.timer",
    "qwen-model-restore-drill.timer",
)


def _component(status: str, *, diagnostic: str = "", **facts: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if diagnostic:
        result["diagnostic"] = diagnostic[:500]
    result.update(facts)
    return result


def probe_json(url: str, *, timeout: float = 3) -> dict[str, Any]:
    """Probe an uncredentialed loopback endpoint without leaking a traceback."""

    try:
        with direct_urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return _component(
            "unavailable",
            diagnostic=f"{type(exc).__name__}: {exc}",
            endpoint=url,
        )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return _component(
            "unavailable",
            diagnostic="health endpoint did not return status=ok",
            endpoint=url,
        )
    return _component("healthy", endpoint=url)


def _container_state(paths: Path, name: str) -> dict[str, Any] | None:
    try:
        result = run(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            cwd=paths,
            timeout=10,
            check=False,
        )
    except ExternalError as exc:
        return {"_probeStatus": "unavailable", "_diagnostic": exc.summary}
    if result.returncode:
        diagnostic = (result.stderr or result.stdout).strip()[:500]
        if "No such object" in diagnostic or "No such container" in diagnostic:
            return None
        return {
            "_probeStatus": "unavailable",
            "_diagnostic": diagnostic or f"docker inspect exited {result.returncode}",
        }
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"Status": "unknown"}
    return state if isinstance(state, dict) else {"Status": "unknown"}


def _user_unit(paths: Path, unit: str) -> dict[str, Any]:
    try:
        loaded = run(
            ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
            cwd=paths,
            timeout=10,
            check=False,
        )
    except ExternalError as exc:
        return _component("unavailable", diagnostic=exc.summary, unit=unit)
    load_state = loaded.stdout.strip()
    if load_state == "not-found":
        return _component("not-configured", unit=unit)
    if loaded.returncode or not load_state:
        diagnostic = (loaded.stderr or loaded.stdout).strip()
        return _component(
            "unavailable",
            diagnostic=diagnostic or f"systemctl show exited {loaded.returncode}",
            unit=unit,
        )
    try:
        enabled = run(
            ["systemctl", "--user", "is-enabled", unit],
            cwd=paths,
            timeout=10,
            check=False,
        )
        active = run(
            ["systemctl", "--user", "is-active", unit],
            cwd=paths,
            timeout=10,
            check=False,
        )
    except ExternalError as exc:
        return _component("unavailable", diagnostic=exc.summary, unit=unit)
    enabled_state = enabled.stdout.strip() or "unknown"
    active_state = active.stdout.strip() or "unknown"
    facts = {"unit": unit, "enabled": enabled_state, "active": active_state}
    if active_state == "active":
        return _component("healthy", **facts)
    if enabled_state in {"disabled", "masked", "static", "indirect"}:
        return _component("disabled", **facts)
    return _component("unavailable", diagnostic="configured unit is not active", **facts)


def integrated_status(root: Path) -> dict[str, Any]:
    """Describe optional ModelPort/operations components without reading credentials."""

    components: dict[str, dict[str, Any]] = {}
    state = _container_state(root, MODELPORT_CONTAINER)
    if state is None:
        components["modelport"] = _component(
            "not-configured", container=MODELPORT_CONTAINER
        )
    elif state.get("_probeStatus") == "unavailable":
        components["modelport"] = _component(
            "unavailable",
            diagnostic=str(state.get("_diagnostic") or "Docker inspection failed"),
            container=MODELPORT_CONTAINER,
        )
    elif state.get("Status") != "running":
        components["modelport"] = _component(
            "unavailable",
            diagnostic="ModelPort container is not running",
            container=MODELPORT_CONTAINER,
            containerState=state.get("Status", "unknown"),
        )
    else:
        components["modelport"] = probe_json(MODELPORT_URL)
        components["modelport"].update(
            {"container": MODELPORT_CONTAINER, "containerState": "running"}
        )

    dashboard = _user_unit(root, DASHBOARD_UNIT)
    if dashboard["status"] == "healthy":
        dashboard_probe = probe_json(DASHBOARD_URL)
        dashboard.update(dashboard_probe)
    components["operationsDashboard"] = dashboard

    for unit in OPERATIONS_UNITS:
        key = unit.removeprefix("qwen-model-").removesuffix(".timer")
        components[key] = _user_unit(root, unit)

    unavailable = [
        name for name, component in components.items() if component["status"] == "unavailable"
    ]
    incomplete = [
        name for name, component in components.items() if component["status"] != "healthy"
    ]
    configured = [
        name
        for name, component in components.items()
        if component["status"] not in {"not-configured", "disabled"}
    ]
    return {
        "scope": "integrated",
        "components": components,
        "configured": bool(configured),
        "healthy": bool(configured) and not incomplete,
        "unavailable": unavailable,
        "incomplete": incomplete,
    }
