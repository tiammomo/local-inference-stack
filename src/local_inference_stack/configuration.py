"""Strict runtime-profile validation and deterministic derivative rendering."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from .paths import ProjectPaths
from .result import ConfigError


PROFILE_NAMES = {"latency", "throughput", "candidate"}
KEY_PATTERN = re.compile(r"QWEN_[A-Z0-9_]+$")
INTEGER_KEYS = {
    "QWEN_BATCH_SIZE",
    "QWEN_CACHE_RAM",
    "QWEN_CTX_SIZE",
    "QWEN_N_PREDICT",
    "QWEN_PARALLEL",
    "QWEN_PUBLISH_PORT",
    "QWEN_UBATCH_SIZE",
}
BASE_KEYS = {
    "QWEN_BATCH_SIZE",
    "QWEN_CACHE_RAM",
    "QWEN_CACHE_TYPE_K",
    "QWEN_CACHE_TYPE_V",
    "QWEN_CTX_SIZE",
    "QWEN_N_PREDICT",
    "QWEN_PARALLEL",
    "QWEN_UBATCH_SIZE",
}
CANDIDATE_ONLY_KEYS = {
    "QWEN_CACHE_DIR",
    "QWEN_COMPOSE_PROJECT",
    "QWEN_CONTAINER_NAME",
    "QWEN_NETWORK_ALIAS",
    "QWEN_PUBLISH_PORT",
    "QWEN_RESTART_POLICY",
}


def load(paths: ProjectPaths) -> dict[str, Any]:
    try:
        document = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read typed runtime configuration: {exc}") from exc
    validate(document)
    return document


def validate(document: Any) -> None:
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "profiles"}:
        raise ConfigError("runtime configuration must contain only schemaVersion and profiles")
    if document["schemaVersion"] != 1:
        raise ConfigError("unsupported runtime configuration schemaVersion")
    profiles = document["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != PROFILE_NAMES:
        raise ConfigError("runtime configuration must define latency, throughput, and candidate")
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) != {"description", "environment"}:
            raise ConfigError(f"profile {name} has unsupported fields")
        if (
            not isinstance(profile["description"], str)
            or not profile["description"].strip()
            or "\n" in profile["description"]
            or "\r" in profile["description"]
        ):
            raise ConfigError(f"profile {name} requires a description")
        environment = profile["environment"]
        if not isinstance(environment, dict) or not environment:
            raise ConfigError(f"profile {name} has no environment values")
        expected_keys = BASE_KEYS | (CANDIDATE_ONLY_KEYS if name == "candidate" else set())
        if set(environment) != expected_keys:
            raise ConfigError(f"profile {name} does not contain the exact supported key set")
        for key, value in environment.items():
            if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
                raise ConfigError(f"profile {name} has unsafe key: {key!r}")
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ConfigError(f"profile {name}/{key} must be a single-line string")
            if key in INTEGER_KEYS and (not value.isdigit() or int(value) <= 0):
                raise ConfigError(f"profile {name}/{key} must be a positive integer")
        if environment["QWEN_CACHE_TYPE_K"] != "q8_0" or environment["QWEN_CACHE_TYPE_V"] != "q8_0":
            raise ConfigError(f"profile {name} must use the validated q8_0 KV cache")
    latency = profiles["latency"]["environment"]
    throughput = profiles["throughput"]["environment"]
    candidate = profiles["candidate"]["environment"]
    if latency["QWEN_PARALLEL"] != "1" or throughput["QWEN_PARALLEL"] != "2":
        raise ConfigError("latency and throughput slot counts violate the supported profiles")
    for key in BASE_KEYS:
        if candidate.get(key) != latency.get(key):
            raise ConfigError(f"candidate must match latency for {key}")
    if candidate.get("QWEN_RESTART_POLICY") != "no":
        raise ConfigError("candidate restart policy must remain 'no'")
    if candidate.get("QWEN_PUBLISH_PORT") == "18080":
        raise ConfigError("candidate must not publish on the production port")


def render_profile(name: str, profile: dict[str, Any]) -> str:
    lines = [f"# Generated from config/runtime-profiles.json: {profile['description']}"]
    lines.extend(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(profile["environment"].items())
    )
    return "\n".join(lines) + "\n"


def expected_files(paths: ProjectPaths) -> dict[Path, str]:
    document = load(paths)
    files = {
        paths.root / "profiles" / f"{name}.env": render_profile(name, profile)
        for name, profile in document["profiles"].items()
    }
    files[paths.root / "dashboard" / "runtime-baseline.json"] = render_dashboard(
        paths, document
    )
    return files


def render_dashboard(paths: ProjectPaths, document: dict[str, Any]) -> str:
    path = paths.root / "dashboard" / "runtime-baseline.json"
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
        catalog = json.loads(
            (paths.root / "catalog" / "models.json").read_text(encoding="utf-8")
        )
        model = next(
            item for item in catalog["models"] if item["id"] == catalog["defaultModel"]
        )
    except (OSError, json.JSONDecodeError, KeyError, StopIteration, TypeError) as exc:
        raise ConfigError(f"cannot render dashboard baseline: {exc}") from exc
    latency = document["profiles"]["latency"]["environment"]
    baseline.update(
        {
            "displayName": model["displayName"],
            "deploymentName": model["servedModelId"],
            "quantization": model["quantization"],
            "kvCache": (
                f"{latency['QWEN_CACHE_TYPE_K'].upper()} / "
                f"{latency['QWEN_CACHE_TYPE_V'].upper()}"
            ),
            "contextTokens": int(latency["QWEN_CTX_SIZE"]),
            "recommendedInputTokens": model["runtime"]["recommendedInputTokens"],
            "maxOutputTokens": int(latency["QWEN_N_PREDICT"]),
            "promptCacheMiB": int(latency["QWEN_CACHE_RAM"]),
            "profile": "latency · 1 × 128K Slot",
        }
    )
    return json.dumps(baseline, ensure_ascii=False, indent=2) + "\n"


def check(paths: ProjectPaths) -> list[str]:
    drift: list[str] = []
    for path, expected in expected_files(paths).items():
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            actual = ""
        if actual != expected:
            drift.append(str(path.relative_to(paths.root)))
    document = load(paths)
    latency = document["profiles"]["latency"]["environment"]
    try:
        catalog = json.loads(
            (paths.root / "catalog" / "models.json").read_text(encoding="utf-8")
        )
        model = next(
            item for item in catalog["models"] if item["id"] == catalog["defaultModel"]
        )
        runtime = model["runtime"]
    except (OSError, json.JSONDecodeError, KeyError, StopIteration, TypeError):
        runtime = {}
    expected_catalog_runtime = {
        "QWEN_CTX_SIZE": runtime.get("contextTokens"),
        "QWEN_N_PREDICT": runtime.get("maxOutputTokens"),
        "QWEN_CACHE_RAM": runtime.get("cacheRamMiB"),
        "QWEN_BATCH_SIZE": runtime.get("batchSize"),
        "QWEN_UBATCH_SIZE": runtime.get("ubatchSize"),
    }
    if any(str(value) != latency.get(key) for key, value in expected_catalog_runtime.items()):
        drift.append("catalog/models.json")
    try:
        compose = (paths.root / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        compose = ""
    compose_defaults = {
        "QWEN_BATCH_SIZE": latency["QWEN_BATCH_SIZE"],
        "QWEN_UBATCH_SIZE": latency["QWEN_UBATCH_SIZE"],
        "QWEN_CACHE_TYPE_K": latency["QWEN_CACHE_TYPE_K"],
        "QWEN_CACHE_TYPE_V": latency["QWEN_CACHE_TYPE_V"],
        "QWEN_CTX_SIZE": latency["QWEN_CTX_SIZE"],
        "QWEN_PARALLEL": latency["QWEN_PARALLEL"],
        "QWEN_N_PREDICT": latency["QWEN_N_PREDICT"],
        "QWEN_CACHE_RAM": latency["QWEN_CACHE_RAM"],
    }
    if any(f"${{{key}:-{value}}}" not in compose for key, value in compose_defaults.items()):
        drift.append("compose.yaml")
    return sorted(set(drift))


def write(paths: ProjectPaths) -> list[str]:
    changed: list[str] = []
    for path, content in expected_files(paths).items():
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        changed.append(str(path.relative_to(paths.root)))
    return sorted(changed)
