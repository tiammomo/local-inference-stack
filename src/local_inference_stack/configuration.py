"""Strict runtime-profile validation and deterministic derivative rendering."""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path
from typing import Any

from .catalog import CatalogError, load_catalog, model_by_id
from .paths import ProjectPaths
from .result import ConfigError


PROFILE_SCHEMA_VERSION = 2
PROFILE_NAMES = {"latency", "throughput", "candidate"}
KEY_PATTERN = re.compile(r"QWEN_[A-Z0-9_]+$")
INTEGER_KEYS = {
    "QWEN_PARALLEL",
    "QWEN_PUBLISH_PORT",
}
MODEL_RUNTIME_FIELDS = {
    "QWEN_CTX_SIZE": "contextTokens",
    "QWEN_RECOMMENDED_INPUT_TOKENS": "recommendedInputTokens",
    "QWEN_N_PREDICT": "maxOutputTokens",
    "QWEN_CACHE_RAM": "cacheRamMiB",
    "QWEN_CACHE_TYPE_K": "cacheTypeK",
    "QWEN_CACHE_TYPE_V": "cacheTypeV",
    "QWEN_BATCH_SIZE": "batchSize",
    "QWEN_UBATCH_SIZE": "ubatchSize",
}
MODEL_RUNTIME_KEYS = set(MODEL_RUNTIME_FIELDS)
MODE_KEYS = {"QWEN_PARALLEL"}
CANDIDATE_ONLY_KEYS = {
    "QWEN_CACHE_DIR",
    "QWEN_COMPOSE_PROJECT",
    "QWEN_CONTAINER_NAME",
    "QWEN_NETWORK_ALIAS",
    "QWEN_PUBLISH_PORT",
}
SELECTED_DEPLOYMENT_REQUIRED_KEYS = {
    "QWEN_CATALOG_ID",
    "QWEN_MODEL_DIR",
    "QWEN_MODEL_FILE",
    "QWEN_SERVED_MODEL_ID",
    "QWEN_CONTAINER_NAME",
    "QWEN_RUNTIME_UID",
    "QWEN_RUNTIME_GID",
    "MODELPORT_NETWORK_NAME",
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
    if document["schemaVersion"] != PROFILE_SCHEMA_VERSION:
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
        expected_keys = MODE_KEYS | (CANDIDATE_ONLY_KEYS if name == "candidate" else set())
        if set(environment) != expected_keys:
            raise ConfigError(f"profile {name} does not contain the exact supported key set")
        for key, value in environment.items():
            if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
                raise ConfigError(f"profile {name} has unsafe key: {key!r}")
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ConfigError(f"profile {name}/{key} must be a single-line string")
            if key in INTEGER_KEYS and (not value.isdigit() or int(value) <= 0):
                raise ConfigError(f"profile {name}/{key} must be a positive integer")
        overlap = MODEL_RUNTIME_KEYS & set(environment)
        if overlap:
            raise ConfigError(
                f"profile {name} must not override Catalog model runtime keys: "
                + ", ".join(sorted(overlap))
            )
    latency = profiles["latency"]["environment"]
    throughput = profiles["throughput"]["environment"]
    candidate = profiles["candidate"]["environment"]
    if latency["QWEN_PARALLEL"] != "1" or throughput["QWEN_PARALLEL"] != "2":
        raise ConfigError("latency and throughput slot counts violate the supported profiles")
    if candidate["QWEN_PARALLEL"] != latency["QWEN_PARALLEL"]:
        raise ConfigError("candidate must use the latency slot count")
    if candidate.get("QWEN_PUBLISH_PORT") == "18080":
        raise ConfigError("candidate must not publish on the production port")


def render_profile(name: str, profile: dict[str, Any]) -> str:
    lines = [f"# Generated from config/runtime-profiles.json: {profile['description']}"]
    lines.extend(
        f"{key}={shlex.quote(value)}"
        for key, value in sorted(profile["environment"].items())
    )
    return "\n".join(lines) + "\n"


def catalog_runtime_environment(model: dict[str, Any]) -> dict[str, str]:
    """Render model-specific capacity without any latency/throughput mode values."""
    try:
        runtime = model["runtime"]
        values = {
            environment_key: str(runtime[catalog_key])
            for environment_key, catalog_key in MODEL_RUNTIME_FIELDS.items()
        }
    except (KeyError, TypeError) as exc:
        raise ConfigError("Catalog model has incomplete runtime capacity") from exc
    if values["QWEN_CACHE_TYPE_K"] not in {"q8_0"} or values[
        "QWEN_CACHE_TYPE_V"
    ] not in {"q8_0"}:
        raise ConfigError("Catalog model must use the currently supported q8_0 KV cache")
    return values


def catalog_deployment_environment(
    model: dict[str, Any],
    *,
    uid: int | None = None,
    gid: int | None = None,
) -> dict[str, str]:
    """Render the complete private selection projection for one Catalog model."""

    artifact = next(
        (item for item in model.get("artifacts", []) if item.get("role") == "model"),
        None,
    )
    if not isinstance(artifact, dict):
        raise ConfigError("Catalog model has no primary model artifact")
    values = {
        "QWEN_CATALOG_ID": str(model["id"]),
        "QWEN_MODEL_DIR": f"./models/{model['modelDirectory']}",
        "QWEN_MODEL_FILE": str(artifact["filename"]),
        "QWEN_MODEL_DISPLAY_NAME": str(model["displayName"]),
        "QWEN_QUANTIZATION": str(model["quantization"]),
        "QWEN_SERVED_MODEL_ID": str(model["servedModelId"]),
        "QWEN_CONTAINER_NAME": str(model["id"]),
        "QWEN_RUNTIME_UID": str(os.getuid() if uid is None else uid),
        "QWEN_RUNTIME_GID": str(os.getgid() if gid is None else gid),
        "MODELPORT_NETWORK_NAME": "modelport_default",
    }
    values.update(catalog_runtime_environment(model))
    return values


def selected_deployment_values_mode(
    model: dict[str, Any], values: dict[str, str]
) -> str:
    """Classify a private selection without treating legacy omissions as overrides."""

    expected = catalog_deployment_environment(model)
    if values == expected:
        return "exact-current-projection"
    if (
        SELECTED_DEPLOYMENT_REQUIRED_KEYS.issubset(values)
        and set(values).issubset(expected)
        and all(expected.get(key) == value for key, value in values.items())
    ):
        # Missing fields are safe only because `stack config check` proves the
        # Compose defaults are the same current Catalog projection before start.
        return "legacy-compatible-current-defaults"
    return "mismatch"


def _private_selected_values(path: Path) -> dict[str, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ConfigError(f"cannot inspect selected deployment profile: {exc}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ConfigError(
            "selected deployment profile must be a private current-user single-link regular file"
        )
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError(f"cannot read selected deployment profile: {exc}") from exc
    for line_number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(
                f"selected deployment profile has invalid quoting at line {line_number}"
            ) from exc
        if len(tokens) != 1 or "=" not in tokens[0]:
            raise ConfigError(
                f"selected deployment profile has an invalid assignment at line {line_number}"
            )
        key, value = tokens[0].split("=", 1)
        if not KEY_PATTERN.fullmatch(key) and key != "MODELPORT_NETWORK_NAME":
            raise ConfigError(
                f"selected deployment profile has an unsupported key at line {line_number}"
            )
        if key in values:
            raise ConfigError(
                f"selected deployment profile has a duplicate key at line {line_number}"
            )
        values[key] = value
    return values


def selected_deployment_profile_status(paths: ProjectPaths) -> dict[str, Any]:
    path = paths.root / "profiles" / "deployment.local.env"
    if not path.exists() and not path.is_symlink():
        return {"present": False, "status": "absent", "migrationRequired": False}
    values = _private_selected_values(path)
    model_id = values.get("QWEN_CATALOG_ID")
    if not model_id:
        return {
            "present": True,
            "status": "mismatch",
            "migrationRequired": False,
            "modelId": None,
        }
    try:
        catalog = load_catalog(paths.root / "catalog" / "models.json")
        model = model_by_id(catalog, model_id)
    except CatalogError as exc:
        raise ConfigError("selected deployment profile references an invalid Catalog model") from exc
    mode = selected_deployment_values_mode(model, values)
    expected = catalog_deployment_environment(model)
    return {
        "present": True,
        "status": mode,
        "migrationRequired": mode == "legacy-compatible-current-defaults",
        "modelId": model_id,
        "missingKeys": sorted(set(expected) - set(values)) if mode != "mismatch" else [],
    }


def _render_selected_deployment_profile(model: dict[str, Any]) -> str:
    values = catalog_deployment_environment(model)
    header = "# Generated by ./stack from the reviewed Catalog; local and intentionally untracked.\n"
    return header + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items()
    )


def normalize_selected_deployment_profile(paths: ProjectPaths) -> dict[str, Any]:
    """Atomically replace a compatible legacy selection with the exact projection."""

    before = selected_deployment_profile_status(paths)
    if before["status"] == "exact-current-projection":
        return {"changed": False, "before": before, "after": before}
    if before["status"] != "legacy-compatible-current-defaults":
        raise ConfigError(
            "selected deployment profile is not a compatible migration source",
            facts={"selectedDeploymentProfile": before},
        )
    catalog = load_catalog(paths.root / "catalog" / "models.json")
    model = model_by_id(catalog, before["modelId"])
    path = paths.root / "profiles" / "deployment.local.env"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_render_selected_deployment_profile(model))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    after = selected_deployment_profile_status(paths)
    if after["status"] != "exact-current-projection":
        raise ConfigError("selected deployment profile migration did not verify")
    return {"changed": True, "before": before, "after": after}


def effective_environment(
    model: dict[str, Any], profile: dict[str, Any]
) -> dict[str, str]:
    """Merge a Catalog model base with a disjoint runtime mode deterministically."""
    environment = profile["environment"]
    overlap = MODEL_RUNTIME_KEYS & set(environment)
    if overlap:
        raise ConfigError(
            "runtime mode overrides Catalog model capacity: "
            + ", ".join(sorted(overlap))
        )
    return {**catalog_runtime_environment(model), **environment}


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
        catalog = load_catalog(paths.root / "catalog" / "models.json")
        model = model_by_id(catalog, catalog["defaultModel"])
    except CatalogError as exc:
        raise ConfigError(
            "cannot render dashboard baseline: Catalog is invalid"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot render dashboard baseline: {exc}") from exc
    latency = document["profiles"]["latency"]["environment"]
    runtime = model["runtime"]
    context_tokens = int(runtime["contextTokens"])
    baseline.update(
        {
            "displayName": model["displayName"],
            "deploymentName": model["servedModelId"],
            "quantization": model["quantization"],
            "kvCache": (
                f"{runtime['cacheTypeK'].upper()} / "
                f"{runtime['cacheTypeV'].upper()}"
            ),
            "contextTokens": context_tokens,
            "recommendedInputTokens": runtime["recommendedInputTokens"],
            "maxOutputTokens": int(runtime["maxOutputTokens"]),
            "promptCacheMiB": int(runtime["cacheRamMiB"]),
            "profile": f"latency · {latency['QWEN_PARALLEL']} × {context_tokens // 1024}K Slot",
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
        catalog = load_catalog(paths.root / "catalog" / "models.json")
        model = model_by_id(catalog, catalog["defaultModel"])
        models = catalog["models"]
    except CatalogError:
        model = {}
        models = []
    try:
        for catalog_model in models:
            effective_environment(catalog_model, document["profiles"]["latency"])
            effective_environment(catalog_model, document["profiles"]["throughput"])
            effective_environment(catalog_model, document["profiles"]["candidate"])
        default_runtime = catalog_runtime_environment(model)
    except ConfigError:
        drift.append("catalog/models.json")
        default_runtime = {}
    try:
        compose = (paths.root / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        compose = ""
    compose_defaults = {
        key: value
        for key, value in {**default_runtime, **latency}.items()
        if key != "QWEN_RECOMMENDED_INPUT_TOKENS"
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
