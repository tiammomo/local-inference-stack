#!/usr/bin/env python3
"""Detailed integrated deployment identity checks used by the public CLI."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from scripts.local_http import direct_urlopen
    from scripts.runtime_identity import runtime_mismatches
except ModuleNotFoundError:
    from local_http import direct_urlopen
    from runtime_identity import runtime_mismatches


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from local_inference_stack.catalog import (  # noqa: E402
    CatalogError,
    load_catalog,
    model_by_id,
)

MANIFEST_PATH = ROOT_DIR / "deployments" / "qwen3.5-9b-rtx5070ti" / "manifest.json"
CONTRACT_PATH = ROOT_DIR / "contracts" / "local-qwen-provider-v1.json"
CATALOG_PATH = ROOT_DIR / "catalog" / "models.json"
CONTAINER_NAME = "qwen35-9b-q5km"
MODELPORT_CONTAINER_NAME = "modelport-modelport-1"
MODELPORT_POSTGRES_CONTAINER_NAME = "modelport-postgres-1"
MODELPORT_DASHBOARD_CONTAINER_NAME = "modelport-dashboard-1"
MODELPORT_NETWORK_NAME = "modelport_default"
BACKUP_FUTURE_SKEW_SECONDS = 300
MAX_REVIEWED_CONFIGURATION_BYTES = 4 * 1024 * 1024
ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")


MODELPORT_SECURITY_POLICIES: dict[str, dict[str, Any]] = {
    "modelport": {
        "containerName": MODELPORT_CONTAINER_NAME,
        "user": "modelport",
        "networkAliases": {MODELPORT_CONTAINER_NAME, "modelport"},
        "portBindings": {
            "38082/tcp": {"hostIp": "127.0.0.1", "hostPort": "38082"}
        },
        "mounts": {
            "/data": {
                "type": "volume",
                "rw": True,
                "mode": "rw",
                "name": "modelport_modelport-data",
            },
            "/config/.env": {"type": "bind", "rw": False, "mode": "ro"},
            "/config/config.toml": {
                "type": "bind",
                "rw": False,
                "mode": "ro",
            },
        },
        "extraHosts": {"host.docker.internal:host-gateway"},
    },
    "postgres": {
        "containerName": MODELPORT_POSTGRES_CONTAINER_NAME,
        "user": "postgres",
        "networkAliases": {MODELPORT_POSTGRES_CONTAINER_NAME, "postgres"},
        "portBindings": {},
        "mounts": {
            "/var/lib/postgresql": {
                "type": "volume",
                "rw": True,
                "mode": "rw",
                "name": "modelport_modelport-postgres-18",
            }
        },
        "extraHosts": set(),
    },
    "dashboard": {
        "containerName": MODELPORT_DASHBOARD_CONTAINER_NAME,
        "user": "nginx",
        "networkAliases": {MODELPORT_DASHBOARD_CONTAINER_NAME, "dashboard"},
        # The ModelPort UI host port is owned by ModelPort. This repository only
        # requires its sole listener to remain loopback-bound.
        "portBindings": {"8080/tcp": None},
        "mounts": {},
        "extraHosts": set(),
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_json(*command: str) -> Any:
    output = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    ).stdout
    return json.loads(output)


def get_json(url: str) -> Any:
    with direct_urlopen(url, timeout=10) as response:
        return json.load(response)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="compare the running deployment with its pinned manifest"
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    return parser.parse_args()


def failure_result(error: Exception) -> dict[str, Any]:
    """Return a bounded machine-readable preflight failure without a traceback."""

    return {
        "schemaVersion": 1,
        "deploymentId": None,
        "status": "failed",
        "checks": [],
        "summary": {"passed": 0, "failed": 1},
        "failure": {
            "stage": "preflight",
            "type": type(error).__name__,
            "detail": str(error)[:500],
        },
    }


def _is_loopback_binding(binding: Any) -> bool:
    if not isinstance(binding, dict):
        return False
    host_ip = binding.get("HostIp")
    host_port = binding.get("HostPort")
    if not isinstance(host_ip, str) or not isinstance(host_port, str):
        return False
    try:
        address = ipaddress.ip_address(host_ip)
        port = int(host_port)
    except ValueError:
        return False
    return address.is_loopback and 1 <= port <= 65535 and str(port) == host_port


def _secure_tmpfs(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"/tmp"}:
        return False
    options = value.get("/tmp")
    if not isinstance(options, str):
        return False
    tokens = set(options.split(","))
    return {"rw", "noexec", "nosuid"}.issubset(tokens) and any(
        token.startswith("size=") for token in tokens
    )


def _mount_mismatches(
    container: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    mismatches: list[str] = []
    raw_mounts = container.get("Mounts")
    if not isinstance(raw_mounts, list):
        return ["mounts"]
    actual: dict[str, dict[str, Any]] = {}
    for mount in raw_mounts:
        if not isinstance(mount, dict):
            mismatches.append("mounts")
            continue
        destination = mount.get("Destination")
        if not isinstance(destination, str) or not destination or destination in actual:
            mismatches.append("mounts")
            continue
        actual[destination] = mount

    expected = policy["mounts"]
    if set(actual) != set(expected):
        mismatches.append("mounts.destinations")
    for destination, wanted in expected.items():
        mount = actual.get(destination)
        if mount is None:
            continue
        for field, docker_field in (("type", "Type"), ("rw", "RW"), ("mode", "Mode")):
            if mount.get(docker_field) != wanted[field]:
                mismatches.append(f"mounts.{destination}.{field}")
        if mount.get("Propagation") not in {"", "rprivate"}:
            mismatches.append(f"mounts.{destination}.propagation")
        if wanted["type"] == "volume":
            if mount.get("Name") != wanted.get("name"):
                mismatches.append(f"mounts.{destination}.name")
        else:
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                mismatches.append(f"mounts.{destination}.source")

    host = container.get("HostConfig") or {}
    binds = host.get("Binds")
    if not isinstance(binds, list) or len(binds) != len(expected):
        mismatches.append("binds")
    else:
        bind_targets: dict[str, str] = {}
        for value in binds:
            if not isinstance(value, str):
                mismatches.append("binds")
                continue
            parts = value.rsplit(":", 2)
            if len(parts) != 3 or parts[1] in bind_targets:
                mismatches.append("binds")
                continue
            bind_targets[parts[1]] = parts[2]
        if set(bind_targets) != set(expected):
            mismatches.append("binds.destinations")
        for destination, wanted in expected.items():
            if bind_targets.get(destination) != wanted["mode"]:
                mismatches.append(f"binds.{destination}.mode")
    return mismatches


def container_security_mismatches(
    container: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    """Return a bounded, secret-free list of fail-closed envelope mismatches."""

    if not isinstance(container, dict):
        return ["container"]
    mismatches: list[str] = []
    state = container.get("State")
    config = container.get("Config")
    host = container.get("HostConfig")
    network_settings = container.get("NetworkSettings")
    if not isinstance(state, dict):
        mismatches.append("state")
        state = {}
    if not isinstance(config, dict):
        mismatches.append("config")
        config = {}
    if not isinstance(host, dict):
        mismatches.append("hostConfig")
        host = {}
    if not isinstance(network_settings, dict):
        mismatches.append("networkSettings")
        network_settings = {}

    checks = {
        "name": container.get("Name", "").lstrip("/")
        == policy["containerName"],
        "state.status": state.get("Status") == "running",
        "state.running": state.get("Running") is True,
        "state.health": (state.get("Health") or {}).get("Status") == "healthy",
        "image.configured": isinstance(config.get("Image"), str)
        and bool(config.get("Image")),
        "image.id": isinstance(container.get("Image"), str)
        and str(container.get("Image")).startswith("sha256:"),
        "user": config.get("User") == policy["user"],
        "privileged": host.get("Privileged") is False,
        "readOnlyRootfs": host.get("ReadonlyRootfs") is True,
        "capAdd": host.get("CapAdd") in (None, []),
        "capDrop": sorted(host.get("CapDrop") or []) == ["ALL"],
        "securityOpt": sorted(host.get("SecurityOpt") or [])
        == ["no-new-privileges:true"],
        "publishAllPorts": host.get("PublishAllPorts") is False,
        "autoRemove": host.get("AutoRemove") is False,
        "pidsLimit": isinstance(host.get("PidsLimit"), int)
        and not isinstance(host.get("PidsLimit"), bool)
        and 1 <= host["PidsLimit"] <= 4096,
        "init": host.get("Init") is True,
        "shmSize": isinstance(host.get("ShmSize"), int)
        and not isinstance(host.get("ShmSize"), bool)
        and host["ShmSize"] > 0,
        "tmpfs": _secure_tmpfs(host.get("Tmpfs")),
        "restartPolicy": host.get("RestartPolicy")
        == {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "networkMode": host.get("NetworkMode") == MODELPORT_NETWORK_NAME,
        "pidMode": host.get("PidMode") == "",
        "ipcMode": host.get("IpcMode") == "private",
        "utsMode": host.get("UTSMode") == "",
        "usernsMode": host.get("UsernsMode") == "",
        "cgroupnsMode": host.get("CgroupnsMode") == "private",
        "devices": host.get("Devices") in (None, []),
        "deviceRequests": host.get("DeviceRequests") in (None, []),
        "groupAdd": host.get("GroupAdd") in (None, []),
        "extraHosts": set(host.get("ExtraHosts") or []) == policy["extraHosts"],
        "links": host.get("Links") in (None, []),
        "dns": host.get("Dns") == [],
        "dnsOptions": host.get("DnsOptions") == [],
        "dnsSearch": host.get("DnsSearch") == [],
        "sysctls": host.get("Sysctls") in (None, {}),
    }
    mismatches.extend(name for name, passed in checks.items() if not passed)

    expected_ports = policy["portBindings"]
    port_bindings = host.get("PortBindings")
    if not isinstance(port_bindings, dict) or set(port_bindings) != set(expected_ports):
        mismatches.append("portBindings")
    else:
        for container_port, expected_binding in expected_ports.items():
            bindings = port_bindings.get(container_port)
            if not isinstance(bindings, list) or len(bindings) != 1:
                mismatches.append(f"portBindings.{container_port}")
                continue
            binding = bindings[0]
            if not _is_loopback_binding(binding):
                mismatches.append(f"portBindings.{container_port}.loopback")
            if expected_binding is not None and (
                binding.get("HostIp") != expected_binding["hostIp"]
                or binding.get("HostPort") != expected_binding["hostPort"]
            ):
                mismatches.append(f"portBindings.{container_port}.identity")

    networks = network_settings.get("Networks")
    if not isinstance(networks, dict) or set(networks) != {MODELPORT_NETWORK_NAME}:
        mismatches.append("networks")
    else:
        network = networks[MODELPORT_NETWORK_NAME]
        aliases = network.get("Aliases") if isinstance(network, dict) else None
        if not isinstance(aliases, list) or not policy["networkAliases"].issubset(
            set(aliases)
        ):
            mismatches.append("networks.aliases")

    mismatches.extend(_mount_mismatches(container, policy))
    return sorted(set(mismatches))


def _canonical_sha256(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _environment_map(value: Any) -> dict[str, str] | None:
    if not isinstance(value, list):
        return None
    result: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str) or "=" not in entry:
            return None
        key, item = entry.split("=", 1)
        if not key or key in result:
            return None
        result[key] = item
    return result


def _safe_reviewed_file(path: Path, *, private: bool = False) -> bytes | None:
    """Read one stable reviewed file without following its final symlink."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        disallowed_mode = 0o077 if private else 0o022
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & disallowed_mode
            or before.st_size > MAX_REVIEWED_CONFIGURATION_BYTES
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REVIEWED_CONFIGURATION_BYTES:
                return None
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
            before.st_nlink,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_mode,
            after.st_uid,
            after.st_nlink,
        )
        if total != before.st_size or identity_before != identity_after:
            return None
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _parse_reviewed_env(body: bytes) -> dict[str, str] | None:
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            return None
        if len(tokens) != 1 or "=" not in tokens[0]:
            return None
        key, value = tokens[0].split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in values:
            return None
        values[key] = value
    return values


def _reviewed_bind_source_matches(source: Any, expected: Path) -> bool:
    if not isinstance(source, str) or not source.startswith("/"):
        return False
    expected_text = os.path.realpath(expected)
    if os.path.realpath(source) == expected_text:
        return True
    # Docker Desktop for WSL exposes bind sources to the daemon under a stable
    # path whose basename is SHA256(original absolute Linux source path).
    parts = Path(source).parts
    return (
        len(parts) == 9
        and parts[:7]
        == ("/", "run", "desktop", "mnt", "host", "wsl", "docker-desktop-bind-mounts")
        and Path(source).name
        == hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    )


def _compose_identity_mismatches(
    container: dict[str, Any], compose_identity: Any, policy_name: str,
    environment: dict[str, str]
) -> list[str]:
    if not isinstance(compose_identity, dict):
        return ["composeIdentity"]
    config = container.get("Config") or {}
    labels = config.get("Labels") if isinstance(config, dict) else None
    host = container.get("HostConfig") or {}
    mounts = container.get("Mounts")
    if not isinstance(labels, dict) or not isinstance(host, dict) or not isinstance(
        mounts, list
    ):
        return ["composeIdentity"]
    mismatches: list[str] = []
    if labels.get("com.docker.compose.project") != compose_identity.get("project"):
        mismatches.append("compose.project")
    if labels.get("com.docker.compose.service") != compose_identity.get("service"):
        mismatches.append("compose.service")

    expected_bindings = (
        {
            "/config/.env": ".env",
            "/config/config.toml": "config.toml",
        }
        if policy_name == "modelport"
        else {}
    )
    bindings = compose_identity.get("bindings")
    if not isinstance(bindings, dict) or bindings != expected_bindings:
        return [*mismatches, "compose.bindings"]
    working_dir_value = labels.get("com.docker.compose.project.working_dir")
    if not isinstance(working_dir_value, str) or not os.path.isabs(working_dir_value):
        return [*mismatches, "compose.workingDirectory"]
    try:
        working_dir = Path(working_dir_value).resolve(strict=True)
    except OSError:
        return [*mismatches, "compose.workingDirectory"]
    if not working_dir.is_dir():
        return [*mismatches, "compose.workingDirectory"]

    config_file = compose_identity.get("configFile")
    if config_file != "docker-compose.yml":
        mismatches.append("compose.configFile")
    config_files_label = labels.get("com.docker.compose.project.config_files")
    try:
        expected_config_file = (working_dir / str(config_file)).resolve(strict=True)
        actual_config_file = Path(str(config_files_label)).resolve(strict=True)
    except OSError:
        mismatches.append("compose.configFile")
    else:
        if actual_config_file != expected_config_file:
            mismatches.append("compose.configFile")

    file_identities = compose_identity.get("files")
    expected_file_keys = (
        {"docker-compose.yml", "config.toml", ".env"}
        if policy_name == "modelport"
        else {"docker-compose.yml"}
    )
    if not isinstance(file_identities, dict) or set(file_identities) != expected_file_keys:
        mismatches.append("compose.files")
        file_identities = {}
    for relative in sorted(expected_file_keys - {".env"}):
        recorded = file_identities.get(relative)
        body = _safe_reviewed_file(working_dir / relative)
        if (
            not isinstance(recorded, dict)
            or set(recorded) != {"sha256"}
            or not isinstance(recorded.get("sha256"), str)
            or SHA256_HEX.fullmatch(recorded["sha256"]) is None
            or body is None
            or hashlib.sha256(body).hexdigest() != recorded["sha256"]
        ):
            mismatches.append(f"compose.files.{relative}.sha256")

    if policy_name == "modelport":
        recorded_env = file_identities.get(".env")
        source_env = _parse_reviewed_env(
            _safe_reviewed_file(working_dir / ".env", private=True) or b""
        )
        if not isinstance(recorded_env, dict) or source_env is None:
            mismatches.append("compose.files..env")
        else:
            expected_keys = recorded_env.get("keys")
            secret_keys = recorded_env.get("secretKeys")
            if (
                set(recorded_env) != {"keys", "secretKeys", "publicSha256"}
                or not isinstance(expected_keys, list)
                or not all(isinstance(key, str) for key in expected_keys)
                or sorted(source_env) != sorted(expected_keys)
            ):
                mismatches.append("compose.files..env.keys")
            if (
                not isinstance(secret_keys, list)
                or not all(isinstance(key, str) for key in secret_keys)
                or not set(secret_keys).issubset(source_env)
            ):
                mismatches.append("compose.files..env.secretKeys")
                secret_keys = []
            public_source = {
                key: value for key, value in source_env.items() if key not in secret_keys
            }
            if _canonical_sha256(public_source) != recorded_env.get("publicSha256"):
                mismatches.append("compose.files..env.publicSha256")
            if any(environment.get(key) != value for key, value in source_env.items()):
                mismatches.append("compose.files..env.runtimeAgreement")

    mount_sources = {
        mount.get("Destination"): mount.get("Source")
        for mount in mounts
        if isinstance(mount, dict) and isinstance(mount.get("Destination"), str)
    }
    bind_sources: dict[str, str] = {}
    raw_binds = host.get("Binds")
    if isinstance(raw_binds, list):
        for bind in raw_binds:
            if not isinstance(bind, str):
                continue
            parts = bind.rsplit(":", 2)
            if len(parts) == 3:
                bind_sources[parts[1]] = parts[0]
    for destination, relative in bindings.items():
        if not isinstance(destination, str) or not isinstance(relative, str):
            mismatches.append("compose.bindings")
            continue
        try:
            expected = (working_dir / relative).resolve(strict=True)
        except OSError:
            mismatches.append(f"compose.bindings.{destination}.expectedSource")
            continue
        mount_source = mount_sources.get(destination)
        bind_source = bind_sources.get(destination)
        if mount_source != bind_source:
            mismatches.append(f"compose.bindings.{destination}.sourceAgreement")
            continue
        if not _reviewed_bind_source_matches(mount_source, expected):
            mismatches.append(f"compose.bindings.{destination}.reviewedSource")
    return mismatches


def container_executable_mismatches(
    container: dict[str, Any], identity: Any, policy_name: str
) -> list[str]:
    """Compare executable identity without returning environment values."""

    if not isinstance(identity, dict):
        return ["reviewedIdentity"]
    config = container.get("Config")
    if not isinstance(config, dict):
        return ["config"]
    checks = {
        "image.configured": config.get("Image") == identity.get("configuredImage"),
        "image.id": container.get("Image") == identity.get("imageId"),
        "command": (config.get("Cmd") or []) == identity.get("command"),
        "entrypoint": (config.get("Entrypoint") or []) == identity.get("entrypoint"),
        "workingDirectory": (config.get("WorkingDir") or "")
        == identity.get("workingDirectory"),
        "healthcheck": (config.get("Healthcheck") or {})
        == identity.get("healthcheck"),
    }
    mismatches = [name for name, passed in checks.items() if not passed]

    environment_identity = identity.get("environment")
    environment = _environment_map(config.get("Env"))
    if not isinstance(environment_identity, dict) or environment is None:
        mismatches.append("environment")
    else:
        expected_keys = environment_identity.get("keys")
        secret_keys = environment_identity.get("secretKeys")
        if (
            not isinstance(expected_keys, list)
            or not all(isinstance(key, str) for key in expected_keys)
            or sorted(environment) != sorted(expected_keys)
        ):
            mismatches.append("environment.keys")
        if (
            not isinstance(secret_keys, list)
            or not all(isinstance(key, str) for key in secret_keys)
            or not set(secret_keys).issubset(environment)
            or any(not environment[key] for key in secret_keys if key in environment)
        ):
            mismatches.append("environment.secretKeys")
        if isinstance(secret_keys, list) and all(
            isinstance(key, str) for key in secret_keys
        ):
            public_environment = {
                key: value for key, value in environment.items() if key not in secret_keys
            }
            if _canonical_sha256(public_environment) != environment_identity.get(
                "publicSha256"
            ):
                mismatches.append("environment.publicSha256")

    mismatches.extend(
        _compose_identity_mismatches(
            container, identity.get("compose"), policy_name, environment or {}
        )
    )
    return sorted(set(mismatches))


def reviewed_container_identities(manifest: dict[str, Any]) -> dict[str, Any] | None:
    reviewed = (manifest.get("gateway") or {}).get("reviewedContainerIdentities")
    if (
        not isinstance(reviewed, dict)
        or reviewed.get("schemaVersion") != 1
        or reviewed.get("status") != "reviewed-current"
    ):
        return None
    containers = reviewed.get("containers")
    if not isinstance(containers, dict) or set(containers) != set(
        MODELPORT_SECURITY_POLICIES
    ):
        return None
    for policy_name, identity in containers.items():
        if not isinstance(identity, dict) or set(identity) != {
            "configuredImage",
            "imageId",
            "command",
            "entrypoint",
            "workingDirectory",
            "healthcheck",
            "environment",
            "compose",
        }:
            return None
        compose = identity.get("compose")
        environment = identity.get("environment")
        expected_bindings = (
            {
                "/config/.env": ".env",
                "/config/config.toml": "config.toml",
            }
            if policy_name == "modelport"
            else {}
        )
        expected_files = (
            {"docker-compose.yml", "config.toml", ".env"}
            if policy_name == "modelport"
            else {"docker-compose.yml"}
        )
        if (
            not isinstance(identity.get("configuredImage"), str)
            or not isinstance(identity.get("imageId"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", identity["imageId"]) is None
            or not isinstance(identity.get("command"), list)
            or not isinstance(identity.get("entrypoint"), list)
            or not isinstance(identity.get("workingDirectory"), str)
            or not isinstance(identity.get("healthcheck"), dict)
            or not isinstance(environment, dict)
            or set(environment) != {"keys", "secretKeys", "publicSha256"}
            or not isinstance(environment.get("keys"), list)
            or not isinstance(environment.get("secretKeys"), list)
            or SHA256_HEX.fullmatch(str(environment.get("publicSha256", ""))) is None
            or not isinstance(compose, dict)
            or set(compose) != {"project", "service", "configFile", "bindings", "files"}
            or compose.get("project") != "modelport"
            or compose.get("service") != policy_name
            or compose.get("configFile") != "docker-compose.yml"
            or compose.get("bindings") != expected_bindings
            or not isinstance(compose.get("files"), dict)
            or set(compose["files"]) != expected_files
        ):
            return None
    return containers


def runtime_security_mismatches(
    container: dict[str, Any], manifest: dict[str, Any]
) -> list[str]:
    profile = (manifest.get("profiles") or {}).get("default")
    if profile not in {"latency", "throughput"}:
        return ["profile"]
    return runtime_mismatches(container, profile)


def backup_archive_snapshot(backup_dir: Path, *, now: float) -> dict[str, Any]:
    """Inspect backup age and filesystem metadata without following symlinks."""

    try:
        directory_metadata = backup_dir.lstat()
        directory_secure = (
            stat.S_ISDIR(directory_metadata.st_mode)
            and directory_metadata.st_uid == os.getuid()
            and directory_metadata.st_mode & 0o077 == 0
        )
        archives: list[tuple[Path, os.stat_result]] = []
        for path in backup_dir.glob("modelport-*.tar.gz"):
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                archives.append((path, metadata))
    except OSError:
        return {"available": False, "ageHours": None, "secure": False}
    if not archives:
        return {"available": False, "ageHours": None, "secure": directory_secure}
    _latest, metadata = max(archives, key=lambda item: item[1].st_mtime_ns)
    age_seconds = now - metadata.st_mtime
    archive_secure = (
        metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and metadata.st_mode & 0o077 == 0
    )
    return {
        "available": True,
        "ageHours": round(age_seconds / 3600, 3),
        "futureTimestamp": age_seconds < -BACKUP_FUTURE_SKEW_SECONDS,
        "secure": directory_secure and archive_secure,
    }


def main() -> int:
    args = parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    try:
        catalog = load_catalog(CATALOG_PATH)
        catalog_model = model_by_id(catalog, manifest["model"]["catalogId"])
    except CatalogError as exc:
        raise RuntimeError(
            "integrated verification requires a valid reviewed Catalog model"
        ) from exc
    catalog_artifact = next(
        artifact
        for artifact in catalog_model["artifacts"]
        if artifact["role"] == "model"
    )
    container = command_json("docker", "inspect", CONTAINER_NAME)[0]
    modelport_container = command_json(
        "docker", "inspect", MODELPORT_CONTAINER_NAME
    )[0]
    modelport_postgres_container = command_json(
        "docker", "inspect", MODELPORT_POSTGRES_CONTAINER_NAME
    )[0]
    modelport_dashboard_container = command_json(
        "docker", "inspect", MODELPORT_DASHBOARD_CONTAINER_NAME
    )[0]
    props = get_json("http://127.0.0.1:18080/props")
    slots = get_json("http://127.0.0.1:18080/slots")
    health = get_json("http://127.0.0.1:18080/health")
    modelport_health = get_json("http://127.0.0.1:38082/livez")
    dashboard_health = get_json("http://127.0.0.1:33004/api/health")
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any, passed: bool | None = None) -> None:
        checks.append(
            {
                "name": name,
                "passed": actual == expected if passed is None else passed,
                "actual": actual,
                "expected": expected,
            }
        )

    runtime = manifest["runtime"]
    model = manifest["model"]
    gateway = manifest["gateway"]
    interfaces = manifest["interfaces"]
    configuration = manifest.get("validatedConfiguration", manifest.get("configuration", {}))
    expected_root = str(ROOT_DIR)
    check(
        "deployment validation status",
        (manifest.get("validation") or {}).get("status", "legacy-schema-v1"),
        "validated-current",
    )
    state = container.get("State", {})
    check("container running", state.get("Status"), "running")
    check("container healthy", state.get("Health", {}).get("Status"), "healthy")
    check("runtime health", health.get("status"), "ok")
    check("ModelPort live", modelport_health.get("status"), "ok")
    check("operations dashboard", dashboard_health.get("status"), "ok")
    check("runtime model alias", props.get("model_alias"), manifest["model"]["servedModelId"])
    check("catalog model ID", catalog_model["id"], model["catalogId"])
    check(
        "official model repository",
        catalog_model["modelRepository"],
        model["officialRepository"],
    )
    check("official model revision", catalog_model["modelRevision"], model["modelRevision"])
    check(
        "artifact repository",
        catalog_model["artifactRepository"],
        model["artifactRepository"],
    )
    check(
        "artifact revision",
        catalog_model["artifactRevision"],
        model["artifactRevision"],
    )
    check("artifact filename", catalog_artifact["filename"], model["artifactFilename"])
    check("artifact byte size", catalog_artifact["bytes"], model["artifactBytes"])
    check("artifact SHA256", catalog_artifact["sha256"], model["artifactSha256"])
    check("model license SPDX", catalog_model["license"]["spdx"], model["licenseSpdx"])
    check(
        "model license review required",
        catalog_model["license"]["reviewRequired"],
        model["licenseReviewRequired"],
    )
    check("runtime build", props.get("build_info"), runtime["engineBuild"])
    check("slot count", len(slots), runtime["slots"])
    check(
        "context per slot",
        [slot.get("n_ctx") for slot in slots],
        [runtime["contextTokens"]] * runtime["slots"],
    )
    check("container image", container.get("Config", {}).get("Image"), runtime["containerImage"])
    check("container name", container.get("Name", "").lstrip("/"), interfaces["containerName"])
    check("unprivileged runtime user", container.get("Config", {}).get("User"), "1000:1000")
    host_config = container.get("HostConfig", {})
    check("read-only root filesystem", host_config.get("ReadonlyRootfs"), True)
    check(
        "no-new-privileges",
        any(
            option.startswith("no-new-privileges")
            for option in (host_config.get("SecurityOpt") or [])
        ),
        True,
    )
    check("all capabilities dropped", host_config.get("CapDrop") or [], ["ALL"])
    runtime_log = host_config.get("LogConfig", {}) or {}
    check("runtime log driver", runtime_log.get("Type"), runtime["logging"]["driver"])
    check(
        "runtime log max size",
        (runtime_log.get("Config") or {}).get("max-size"),
        runtime["logging"]["maxSize"],
    )
    check(
        "runtime log max files",
        (runtime_log.get("Config") or {}).get("max-file"),
        str(runtime["logging"]["maxFiles"]),
    )
    check(
        "diagnostic port bindings",
        host_config.get("PortBindings"),
        {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
    )
    runtime_envelope_mismatches = runtime_security_mismatches(container, manifest)
    check(
        "complete runtime security envelope",
        runtime_envelope_mismatches,
        [],
        not runtime_envelope_mismatches,
    )

    modelport_state = modelport_container.get("State", {})
    check("ModelPort container running", modelport_state.get("Status"), "running")
    check(
        "ModelPort container healthy",
        modelport_state.get("Health", {}).get("Status"),
        "healthy",
    )
    check(
        "ModelPort container name",
        modelport_container.get("Name", "").lstrip("/"),
        gateway["containerName"],
    )
    check(
        "ModelPort image ID",
        modelport_container.get("Image"),
        gateway["containerImageId"],
    )
    check(
        "ModelPort configured image",
        modelport_container.get("Config", {}).get("Image"),
        gateway["containerImage"],
    )
    modelport_labels = modelport_container.get("Config", {}).get("Labels", {}) or {}
    check(
        "ModelPort source revision label",
        modelport_labels.get("org.opencontainers.image.revision"),
        gateway["sourceCommit"],
    )
    check(
        "ModelPort source state label",
        modelport_labels.get("io.modelport.source-state"),
        gateway["sourceState"],
    )
    check(
        "ModelPort unprivileged user",
        modelport_container.get("Config", {}).get("User"),
        "modelport",
    )
    modelport_host = modelport_container.get("HostConfig", {})
    check(
        "ModelPort read-only root filesystem",
        modelport_host.get("ReadonlyRootfs"),
        True,
    )
    check(
        "ModelPort privileged mode disabled",
        modelport_host.get("Privileged"),
        False,
    )
    check(
        "ModelPort no-new-privileges",
        any(
            option.startswith("no-new-privileges")
            for option in (modelport_host.get("SecurityOpt") or [])
        ),
        True,
    )
    check(
        "ModelPort capabilities dropped",
        modelport_host.get("CapDrop") or [],
        ["ALL"],
    )
    for label, inspected in (
        ("ModelPort", modelport_container),
        ("ModelPort PostgreSQL", modelport_postgres_container),
        ("ModelPort Dashboard", modelport_dashboard_container),
    ):
        log_config = inspected.get("HostConfig", {}).get("LogConfig", {}) or {}
        check(
            f"{label} log driver",
            log_config.get("Type"),
            gateway["logging"]["driver"],
        )
        check(
            f"{label} log max size",
            (log_config.get("Config") or {}).get("max-size"),
            gateway["logging"]["maxSize"],
        )
        check(
            f"{label} log max files",
            (log_config.get("Config") or {}).get("max-file"),
            str(gateway["logging"]["maxFiles"]),
        )
    check(
        "ModelPort port bindings",
        modelport_host.get("PortBindings"),
        {"38082/tcp": [{"HostIp": "127.0.0.1", "HostPort": "38082"}]},
    )
    integrated_identities = reviewed_container_identities(manifest)
    for label, inspected, policy_name in (
        ("ModelPort", modelport_container, "modelport"),
        ("ModelPort PostgreSQL", modelport_postgres_container, "postgres"),
        ("ModelPort Dashboard", modelport_dashboard_container, "dashboard"),
    ):
        envelope_mismatches = container_security_mismatches(
            inspected, MODELPORT_SECURITY_POLICIES[policy_name]
        )
        if integrated_identities is None:
            envelope_mismatches.append("reviewedIdentity")
        else:
            envelope_mismatches.extend(
                container_executable_mismatches(
                    inspected, integrated_identities.get(policy_name), policy_name
                )
            )
        envelope_mismatches = sorted(set(envelope_mismatches))
        check(
            f"{label} complete security envelope",
            envelope_mismatches,
            [],
            not envelope_mismatches,
        )

    for unit in manifest["operations"]["enabledUnits"]:
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        check(f"{unit} enabled", enabled.stdout.strip(), "enabled")
        check(f"{unit} active", active.stdout.strip(), "active")

    backup_dir = ROOT_DIR / manifest["operations"]["backupDirectory"]
    backup = backup_archive_snapshot(backup_dir, now=time.time())
    check("ModelPort backup available", backup["available"], True)
    if backup["available"]:
        backup_age_hours = backup["ageHours"]
        check(
            "ModelPort backup freshness",
            backup_age_hours,
            (
                f">=-{BACKUP_FUTURE_SKEW_SECONDS}s and "
                f"<={manifest['operations']['backupMaxAgeHours']}h"
            ),
            not backup["futureTimestamp"]
            and backup_age_hours <= manifest["operations"]["backupMaxAgeHours"],
        )
        check("ModelPort backup permissions and ownership", backup["secure"], True)
    command = container.get("Config", {}).get("Cmd", []) or []
    check("KV snapshot path enabled", "--slot-save-path" in command, True)
    if "--slot-save-path" in command:
        slot_path_index = command.index("--slot-save-path") + 1
        check("KV snapshot path", command[slot_path_index], "/cache/slots")

    mounts = {
        mount.get("Destination"): mount.get("Source")
        for mount in container.get("Mounts", [])
    }
    check("model mount", mounts.get("/models"), f"{expected_root}/models/qwen3.5-9b")
    check("cache mount", mounts.get("/cache"), f"{expected_root}/cache")
    labels = container.get("Config", {}).get("Labels", {}) or {}
    check("compose working directory", labels.get("com.docker.compose.project.working_dir"), expected_root)
    check("compose file", labels.get("com.docker.compose.project.config_files"), f"{expected_root}/compose.yaml")

    check("compose SHA256", sha256(ROOT_DIR / "compose.yaml"), configuration["composeSha256"])
    check(
        "latency profile SHA256",
        sha256(ROOT_DIR / "profiles" / "latency.env"),
        configuration["latencyProfileSha256"],
    )
    check(
        "candidate profile SHA256",
        sha256(ROOT_DIR / "profiles" / "candidate.env"),
        configuration["candidateProfileSha256"],
    )
    check("provider contract SHA256", sha256(CONTRACT_PATH), configuration["providerContractSha256"])
    check(
        "quality suite SHA256",
        sha256(ROOT_DIR / "quality" / "cases.json"),
        configuration["qualitySuiteSha256"],
    )
    check(
        "acceptance suite SHA256",
        sha256(ROOT_DIR / "scripts" / "acceptance-suite.sh"),
        configuration["acceptanceSuiteSha256"],
    )
    check(
        "model catalog SHA256",
        sha256(ROOT_DIR / "catalog" / "models.json"),
        configuration["modelCatalogSha256"],
    )
    check(
        "model manager SHA256",
        sha256(ROOT_DIR / "scripts" / "model-manager.py"),
        configuration["modelManagerSha256"],
    )
    check(
        "compatibility checker SHA256",
        sha256(ROOT_DIR / "scripts" / "compatibility-check.py"),
        configuration["compatibilityCheckSha256"],
    )
    check(
        "Tool workflow suite SHA256",
        sha256(ROOT_DIR / "quality" / "tool-workflows.json"),
        configuration["toolWorkflowSuiteSha256"],
    )
    check(
        "Tool workflow harness SHA256",
        sha256(ROOT_DIR / "scripts" / "tool-workflow-eval.py"),
        configuration["toolWorkflowHarnessSha256"],
    )
    check(
        "Tool resilience suite SHA256",
        sha256(ROOT_DIR / "quality" / "tool-resilience-workflows.json"),
        configuration["toolResilienceSuiteSha256"],
    )
    check(
        "dashboard baseline SHA256",
        sha256(ROOT_DIR / "dashboard" / "runtime-baseline.json"),
        configuration["dashboardBaselineSha256"],
    )
    check(
        "dashboard application SHA256",
        sha256(ROOT_DIR / "dashboard" / "app.js"),
        configuration["dashboardApplicationSha256"],
    )
    check(
        "dashboard document SHA256",
        sha256(ROOT_DIR / "dashboard" / "index.html"),
        configuration["dashboardDocumentSha256"],
    )
    check(
        "dashboard server SHA256",
        sha256(ROOT_DIR / "scripts" / "operations-dashboard.py"),
        configuration["dashboardServerSha256"],
    )
    check(
        "dashboard smoke SHA256",
        sha256(ROOT_DIR / "scripts" / "dashboard-smoke.py"),
        configuration["dashboardSmokeSha256"],
    )
    check(
        "operations report SHA256",
        sha256(ROOT_DIR / "scripts" / "operations-report.py"),
        configuration["operationsReportSha256"],
    )
    check(
        "backup orchestrator SHA256",
        sha256(ROOT_DIR / "scripts" / "modelport-backup.sh"),
        configuration["backupOrchestratorSha256"],
    )
    check(
        "soak gate SHA256",
        sha256(ROOT_DIR / "scripts" / "soak-check.py"),
        configuration["soakCheckSha256"],
    )
    check(
        "systemd installer SHA256",
        sha256(ROOT_DIR / "scripts" / "install-user-services.py"),
        configuration["systemdInstallerSha256"],
    )
    check(
        "runtime controller SHA256",
        sha256(ROOT_DIR / "scripts" / "runtime.sh"),
        configuration["runtimeControllerSha256"],
    )
    check(
        "runtime supervisor SHA256",
        sha256(ROOT_DIR / "scripts" / "runtime-supervisor.py"),
        configuration["runtimeSupervisorSha256"],
    )
    check(
        "Python version pin SHA256",
        sha256(ROOT_DIR / ".python-version"),
        configuration["pythonVersionSha256"],
    )
    check(
        "candidate runtime SHA256",
        sha256(ROOT_DIR / "scripts" / "candidate-runtime.sh"),
        configuration["candidateRuntimeSha256"],
    )
    check(
        "release candidate SHA256",
        sha256(ROOT_DIR / "scripts" / "release-candidate.sh"),
        configuration["releaseCandidateSha256"],
    )
    check(
        "release check SHA256",
        sha256(ROOT_DIR / "scripts" / "release-check.sh"),
        configuration["releaseCheckSha256"],
    )
    check(
        "deployment library SHA256",
        sha256(ROOT_DIR / "scripts" / "lib" / "deployment.sh"),
        configuration["deploymentLibrarySha256"],
    )
    check(
        "acceptance evidence writer SHA256",
        sha256(ROOT_DIR / "scripts" / "acceptance-evidence.py"),
        configuration["acceptanceEvidenceWriterSha256"],
    )
    check("contract provider", contract.get("provider"), interfaces["modelportProvider"])
    check(
        "contract served model",
        contract.get("runtime", {}).get("servedModelId"),
        manifest["model"]["servedModelId"],
    )
    check(
        "contract context limit",
        contract.get("limits", {}).get("contextTokens"),
        runtime["contextTokens"],
    )
    check(
        "contract reasoning input limit",
        contract.get("limits", {}).get("recommendedReasoningInputTokens"),
        runtime["recommendedReasoningInputTokens"],
    )

    integrity = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "model-manager.py"),
            "verify",
            "--cached",
            "--read-only",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    check(
        "active model integrity",
        integrity.stdout.strip() or integrity.stderr.strip(),
        "pinned SHA256",
        integrity.returncode == 0,
    )

    failed = [item for item in checks if not item["passed"]]
    result = {
        "schemaVersion": 1,
        "deploymentId": manifest["deploymentId"],
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "summary": {"passed": len(checks) - len(failed), "failed": len(failed)},
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            marker = "PASS" if item["passed"] else "FAIL"
            print(f"[{marker}] {item['name']}: {item['actual']}")
        print(
            f"\nDeployment verification {result['status']}: "
            f"{result['summary']['passed']} passed, {result['summary']['failed']} failed"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        result = failure_result(error)
        if "--json" in sys.argv[1:]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"Deployment verification failed during preflight: "
                f"{result['failure']['type']}: {result['failure']['detail']}",
                file=sys.stderr,
            )
        raise SystemExit(1) from None
