"""Unified public CLI for the local inference stack control plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from . import (
    attestation,
    bundle,
    calibration,
    configuration,
    catalog,
    credentials,
    health,
    migration,
    reference,
    storage,
)
from .paths import ProjectPaths
from .deployment import (
    ActionKind,
    CatalogDeploymentSpec,
    DeploymentSpecError,
    RolloutActionKind,
    build_rollback_rollout_plan,
    build_upgrade_rollout_plan,
    load_approved_catalog_spec,
    parse_deployment_plan,
)
from .materials import canonical_sha256
from .result import (
    AdmissionError,
    CommandResult,
    ConfigError,
    ExitCode,
    ExternalError,
    IntegrityError,
    NextAction,
    RecoveryError,
    StackError,
    UsageError,
)
from .runner import run
from .rollout import (
    RollbackPointer,
    RollbackSpec,
    RollbackSpecError,
    RollbackStore,
    RollbackStoreError,
)
from .rollout_runtime import (
    capture_rollback_spec,
    recovery_original as rollback_recovery_original,
    verify_rollback_spec,
    write_anchor_selection,
)
from .transactions import (
    RECOVERY_DEPLOYMENT_KEYS,
    RECOVERY_REQUIRED_DEPLOYMENT_KEYS,
    SCHEMA_VERSION as TRANSACTION_SCHEMA_VERSION,
    ROLLOUT_INTENT_POLICY_ID,
    TransactionStore,
    recovery_original_is_safe,
)


RUNTIME_PULL_POLICY_ENV = "LOCAL_INFERENCE_RUNTIME_PULL_POLICY"
RUNTIME_PULL_POLICY_NEVER = "never"
ROLLOUT_PREFLIGHT_TRANSACTION_ID = "00000000-0000-4000-8000-000000000000"
ROLLOUT_PREFLIGHT_CAPTURED_AT = "1970-01-01T00:00:00Z"


class StableParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def parser() -> argparse.ArgumentParser:
    root = StableParser(description=__doc__)
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--json",
        action="store_true",
        help="emit the versioned command-result object (accepted before or after the command)",
    )
    commands = root.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="read-only host assessment")
    plan.add_argument("--model")
    plan.add_argument("--vram-gib", type=float)
    plan.add_argument("--ram-gib", type=float)

    commands.add_parser("doctor", help="read-only prerequisite and safety checks")
    status = commands.add_parser("status", help="read-only runtime and transaction status")
    status.add_argument(
        "--scope",
        choices=("standalone", "integrated", "all"),
        default="standalone",
    )

    verify = commands.add_parser("verify", help="verify repository, configuration, or model")
    verify.add_argument(
        "--scope",
        choices=("repository", "config", "model", "standalone", "integrated", "all"),
        default="repository",
    )
    verify.add_argument("--model")
    verify.add_argument("--cached", action="store_true")

    deploy = commands.add_parser("deploy", help="execute catalog-generated deployment steps")
    deploy.add_argument("--model")
    deploy.add_argument("--yes", action="store_true")

    upgrade = commands.add_parser(
        "upgrade",
        help="replace production through one typed maintenance-window transaction",
    )
    upgrade.add_argument("--model", required=True)
    upgrade.add_argument("--yes", action="store_true")

    rollback_parser = commands.add_parser(
        "rollback",
        help="inspect or consume the one active immutable rollback anchor",
    )
    rollback_parser.add_argument("--yes", action="store_true")

    accept = commands.add_parser("accept", help="run a supported acceptance tier")
    accept.add_argument("tier", choices=("quick", "standard", "full"), nargs="?", default="quick")
    accept.add_argument("--yes", action="store_true")

    release = commands.add_parser("release", help="serial candidate acceptance and production restore")
    release.add_argument("mode", choices=("quick", "long"), nargs="?", default="quick")
    release.add_argument("--yes", action="store_true")

    profile = commands.add_parser("profile", help="switch the fixed production profile")
    profile.add_argument("name", choices=("latency", "throughput"))
    profile.add_argument("--yes", action="store_true")

    reconcile = commands.add_parser("reconcile", help="inspect or repair durable runtime state")
    reconcile.add_argument("--yes", action="store_true")

    config = commands.add_parser("config", help="typed runtime configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("check")
    config_render = config_commands.add_parser("render")
    config_render.add_argument("--write", action="store_true")
    config_render.add_argument("--yes", action="store_true")

    attest = commands.add_parser("attest", help="reusable-validation evidence")
    attest_commands = attest.add_subparsers(dest="attest_command", required=True)
    attest_create = attest_commands.add_parser("create")
    attest_create.add_argument("--evidence", required=True, type=Path)
    attest_create.add_argument("--output", required=True, type=Path)
    attest_sign = attest_commands.add_parser("sign")
    attest_sign.add_argument("path", type=Path)
    attest_sign.add_argument("--tool", choices=("minisign", "cosign"), required=True)
    attest_sign.add_argument("--secret-key", type=Path, required=True)
    attest_sign.add_argument("--signature", type=Path, required=True)
    attest_sign.add_argument("--yes", action="store_true")
    attest_verify = attest_commands.add_parser("verify")
    attest_verify.add_argument("path", type=Path)
    attest_verify.add_argument("--require-signature", action="store_true")
    attest_verify.add_argument("--tool", choices=("minisign", "cosign"))
    attest_verify.add_argument("--public-key", type=Path)
    attest_verify.add_argument("--signature", type=Path)
    attest_verify.add_argument("--trusted-key-sha256")
    attest_verify.add_argument("--for-promotion", action="store_true")

    bundles = commands.add_parser("bundle", help="offline reproducibility bundle")
    bundle_commands = bundles.add_subparsers(dest="bundle_command", required=True)
    bundle_create = bundle_commands.add_parser("create")
    bundle_create.add_argument("--model", required=True)
    bundle_create.add_argument("--output", required=True, type=Path)
    bundle_create.add_argument("--include-model", action="store_true")
    bundle_create.add_argument("--image-archive", type=Path)
    bundle_create.add_argument("--yes", action="store_true")
    bundle_verify = bundle_commands.add_parser("verify")
    bundle_verify.add_argument("path", type=Path)
    bundle_import = bundle_commands.add_parser("import")
    bundle_import.add_argument("path", type=Path)
    bundle_import.add_argument("--yes", action="store_true")

    calibrate = commands.add_parser("calibrate", help="offline candidate profile calibration")
    calibrate_commands = calibrate.add_subparsers(dest="calibrate_command", required=True)
    calibrate_commands.add_parser("plan")
    calibrate_run = calibrate_commands.add_parser("run")
    calibrate_run.add_argument("--output", type=Path)
    calibrate_run.add_argument("--yes", action="store_true")

    storage_parser = commands.add_parser("storage", help="storage inventory and safe GC")
    storage_commands = storage_parser.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("report")
    gc = storage_commands.add_parser("gc")
    gc.add_argument("--older-than-days", type=int, default=14)
    gc.add_argument("--yes", action="store_true")

    credentials_parser = commands.add_parser("credentials", help="credential metadata audit")
    credentials_commands = credentials_parser.add_subparsers(dest="credentials_command", required=True)
    credentials_commands.add_parser("audit")
    credential_migrate = credentials_commands.add_parser("migrate-systemd")
    credential_migrate.add_argument("kind", choices=("operations", "backup", "alerting"))
    credential_migrate.add_argument("--yes", action="store_true")

    migrate_parser = commands.add_parser("migrate", help="explicit local compatibility migration")
    migrate_mode = migrate_parser.add_mutually_exclusive_group()
    migrate_mode.add_argument("--check", action="store_true")
    migrate_mode.add_argument("--yes", action="store_true")

    reference_parser = commands.add_parser("reference", help="generated control-plane reference")
    reference_mode = reference_parser.add_mutually_exclusive_group()
    reference_mode.add_argument("--check", action="store_true")
    reference_mode.add_argument("--write", action="store_true")
    reference_parser.add_argument("--yes", action="store_true")
    return root


def _require_yes(value: bool, action: str) -> None:
    if not value:
        raise UsageError(f"{action} changes local state; review the plan and rerun with --yes")


def _legacy_plan(paths: ProjectPaths, args: argparse.Namespace) -> dict[str, Any]:
    argv = ["python3", "scripts/model-manager.py", "plan", "--json"]
    if args.model:
        argv.extend(("--model", args.model))
    if getattr(args, "vram_gib", None) is not None:
        argv.extend(("--vram-gib", str(args.vram_gib)))
    if getattr(args, "ram_gib", None) is not None:
        argv.extend(("--ram-gib", str(args.ram_gib)))
    result = run(argv, cwd=paths.root, timeout=20)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalError("legacy planner returned invalid JSON") from exc


def _plan_result(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    payload = _legacy_plan(paths, args)
    recommendation = payload.get("recommendation") or {}
    actions: list[NextAction] = []
    if payload.get("readyToDeploy") is True:
        try:
            spec = CatalogDeploymentSpec.from_catalog_model(recommendation)
            parse_deployment_plan(
                spec,
                payload.get("actionPlan"),
                require_full_lifecycle=True,
            )
        except DeploymentSpecError as error:
            raise ConfigError(
                f"planner returned an invalid typed deployment plan: {error}"
            ) from error
        actions.append(
            NextAction(
                command=f"./stack deploy --model {spec.catalog_id} --yes",
                description="execute the reviewed typed deployment plan",
                requiresApproval=True,
            )
        )
    summary = (
        f"recommended {recommendation.get('id')} ({payload.get('evidenceStatus')}); "
        f"readyToDeploy={str(payload.get('readyToDeploy')).lower()}"
        if recommendation
        else "no catalog model is deployable on the detected host"
    )
    return CommandResult("plan", "ok", summary, facts={"plan": payload}, nextActions=actions)


def _doctor(paths: ProjectPaths) -> CommandResult:
    drift = configuration.check(paths)
    transaction = TransactionStore(paths).reconciliation_plan()
    prerequisites = {
        name: bool(shutil.which(name))
        for name in ("python3", "curl", "flock", "docker", "nvidia-smi")
    }
    supported = platform.system() == "Linux" and platform.machine() == "x86_64"
    healthy = supported and not drift and not transaction["required"] and all(prerequisites.values())
    facts = {
        "platform": {"system": platform.system(), "machine": platform.machine(), "supported": supported},
        "prerequisites": prerequisites,
        "configurationDrift": drift,
        "reconciliationRequired": transaction["required"],
        "reconciliation": transaction,
    }
    code = (
        ExitCode.SUCCESS
        if healthy
        else ExitCode.RECOVERY
        if transaction["required"]
        else ExitCode.CONFIG
    )
    return CommandResult(
        "doctor",
        "ok" if healthy else "attention",
        "all control-plane checks passed" if healthy else "one or more control-plane checks need attention",
        code=int(code),
        facts=facts,
        nextActions=(
            [
                NextAction(
                    "./stack reconcile --json",
                    "inspect the unfinished transaction without changing runtime",
                    False,
                )
            ]
            if transaction["required"]
            else []
        ),
    )


def _standalone_status(paths: ProjectPaths) -> dict[str, Any]:
    runtime = run(["scripts/runtime.sh", "status"], cwd=paths.root, timeout=20, check=False)
    reconciliation = TransactionStore(paths).reconciliation_plan()
    transaction = reconciliation.get("transaction")
    runtime_healthy = runtime.returncode == 0
    control_plane_ready = not reconciliation["required"]
    profile = "unknown"
    for line in runtime.stdout.splitlines():
        if line.startswith("profile="):
            profile = line.split()[0].split("=", 1)[1]
    component = {
        "status": "healthy" if runtime_healthy else "unavailable",
        "summary": runtime.stdout[-4000:],
        "diagnostic": runtime.stderr[-1000:],
        "profile": profile,
    }
    control_plane = {
        "status": "healthy" if control_plane_ready else "attention",
        "classification": reconciliation["classification"],
        "automaticEligible": reconciliation["automaticEligible"],
    }
    return {
        "scope": "standalone",
        "healthy": runtime_healthy and control_plane_ready,
        "components": {"runtime": component, "controlPlane": control_plane},
        "runtimeHealthy": runtime_healthy,
        "controlPlaneReady": control_plane_ready,
        "profile": profile,
        "runtimeSummary": component["summary"],
        "runtimeDiagnostic": component["diagnostic"],
        "transaction": transaction,
        "reconciliation": reconciliation,
    }


def _status(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    facts: dict[str, Any] = {"scope": args.scope, "components": {}}
    standalone: dict[str, Any] | None = None
    integrated: dict[str, Any] | None = None
    if args.scope in {"standalone", "all"}:
        standalone = _standalone_status(paths)
        facts.update(
            {
                "runtimeHealthy": standalone["runtimeHealthy"],
                "controlPlaneReady": standalone["controlPlaneReady"],
                "runtimeSummary": standalone["runtimeSummary"],
                "runtimeDiagnostic": standalone["runtimeDiagnostic"],
                "transaction": standalone["transaction"],
                "reconciliation": standalone["reconciliation"],
            }
        )
        facts["components"].update(standalone["components"])
    if args.scope in {"integrated", "all"}:
        integrated = health.integrated_status(paths.root)
        facts["integratedConfigured"] = integrated["configured"]
        facts["integratedHealthy"] = integrated["healthy"]
        facts["components"].update(integrated["components"])

    runtime_available = standalone is None or standalone["runtimeHealthy"]
    control_plane_ready = standalone is None or standalone["controlPlaneReady"]
    integrated_unavailable = bool(integrated and integrated["unavailable"])
    if not runtime_available or integrated_unavailable:
        summary = f"{args.scope} status has one or more unavailable components"
        status = "unavailable"
        code = ExitCode.EXTERNAL
    elif not control_plane_ready:
        summary = "runtime is serviceable but the control plane requires reconciliation"
        status = "attention"
        code = ExitCode.RECOVERY
    elif integrated is not None and not integrated["configured"]:
        summary = "integrated components are disabled or not configured"
        status = "ok"
        code = ExitCode.SUCCESS
    elif integrated is not None and not integrated["healthy"]:
        summary = "integrated components are only partially configured"
        status = "attention"
        code = ExitCode.CONFIG
    else:
        summary = f"{args.scope} components are healthy"
        status = "ok"
        code = ExitCode.SUCCESS
    return CommandResult(
        "status",
        status,
        summary,
        code=int(code),
        facts=facts,
    )


def _verify(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    if args.scope != "model" and (args.model is not None or args.cached):
        raise UsageError("--model and --cached are valid only with --scope model")
    if args.scope == "config":
        drift = configuration.check(paths)
        if drift:
            raise ConfigError("generated runtime profiles have drift", facts={"files": drift})
        return CommandResult("verify", "ok", "typed runtime configuration and profiles match")
    if args.scope == "model":
        argv = ["python3", "scripts/model-manager.py", "verify", "--read-only"]
        if args.model:
            argv.extend(("--model", args.model))
        if args.cached:
            argv.append("--cached")
        result = run(argv, cwd=paths.root, timeout=600)
        return CommandResult("verify", "ok", "catalog artifact verification passed", facts={"output": result.stdout[-4000:]})
    verified_components: dict[str, Any] = {}
    artifact_output: str | None = None
    if args.scope in {"standalone", "integrated", "all"}:
        drift = configuration.check(paths)
        if drift:
            raise ConfigError("generated runtime profiles have drift", facts={"files": drift})
        standalone = _standalone_status(paths)
        if not standalone["runtimeHealthy"]:
            raise ExternalError(
                "standalone runtime verification did not pass",
                facts={"scope": "standalone", "components": standalone["components"]},
            )
        if not standalone["controlPlaneReady"]:
            raise RecoveryError(
                "standalone runtime is healthy but an unfinished transaction requires reconciliation",
                facts={
                    "scope": "standalone",
                    "components": standalone["components"],
                    "reconciliation": standalone["reconciliation"],
                },
            )
        if standalone["profile"] not in {"latency", "throughput"}:
            raise IntegrityError(
                "standalone runtime profile could not be identified",
                facts={"scope": "standalone", "components": standalone["components"]},
            )
        identity = run(
            ["scripts/runtime.sh", "assert-profile", standalone["profile"]],
            cwd=paths.root,
            timeout=120,
        )
        integrity = run(
            [
                "python3",
                "scripts/model-manager.py",
                "verify",
                "--cached",
                "--read-only",
            ],
            cwd=paths.root,
            timeout=600,
        )
        verified_components.update(standalone["components"])
        artifact_output = integrity.stdout[-4000:]
        if args.scope == "standalone":
            return CommandResult(
                "verify",
                "ok",
                "standalone runtime, configuration, and active artifact verified",
                facts={
                    "scope": "standalone",
                    "components": standalone["components"],
                    "runtimeIdentity": identity.stdout[-4000:],
                    "artifact": artifact_output,
                },
            )
    if args.scope in {"integrated", "all"}:
        integrated = health.integrated_status(paths.root)
        incomplete = {
            name: component
            for name, component in integrated["components"].items()
            if component["status"] != "healthy"
        }
        if incomplete:
            raise ExternalError(
                "integrated deployment verification did not pass",
                facts={"scope": "integrated", "components": integrated["components"]},
            )
        detailed = run(
            ["python3", "scripts/verify-integrated-deployment.py", "--json"],
            cwd=paths.root,
            timeout=600,
            check=False,
        )
        try:
            deployment_verification = json.loads(detailed.stdout)
        except json.JSONDecodeError as exc:
            raise ExternalError(
                "integrated deployment verifier returned invalid JSON",
                facts={"diagnostic": (detailed.stderr or detailed.stdout)[-1000:]},
            ) from exc
        if (
            detailed.returncode != 0
            or not isinstance(deployment_verification, dict)
            or deployment_verification.get("status") != "passed"
        ):
            raise IntegrityError(
                "integrated deployment identity does not match its reviewed manifest",
                facts={
                    "scope": "integrated",
                    "components": integrated["components"],
                    "deploymentVerification": deployment_verification,
                },
            )
        verified_components.update(integrated["components"])
        facts: dict[str, Any] = {
            "scope": args.scope,
            "components": verified_components,
            "deploymentVerification": deployment_verification,
        }
        if artifact_output is not None:
            facts["artifact"] = artifact_output
        return CommandResult(
            "verify",
            "ok",
            f"{args.scope} deployment components verified",
            facts=facts,
        )
    result = run(["scripts/release-check.sh"], cwd=paths.root, timeout=900)
    return CommandResult("verify", "ok", "repository release checks passed", facts={"output": result.stdout[-8000:]})


def _original_runtime(paths: ProjectPaths) -> dict[str, Any]:
    profile = "unknown"
    probe = run(["scripts/runtime.sh", "status"], cwd=paths.root, timeout=15, check=False)
    for line in probe.stdout.splitlines():
        if line.startswith("profile="):
            profile = line.split()[0].split("=", 1)[1]
    identity: dict[str, Any] | None = None
    container_name: str | None = None
    deployment_profile: dict[str, Any] = {"present": False}
    from scripts.runtime_identity import (
        deployment_values,
        live_container,
        live_runtime_sha256,
        normalized_live_runtime,
    )

    try:
        values = deployment_values()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"cannot safely capture the deployment profile: {exc}") from exc
    container_name = values.get("QWEN_CONTAINER_NAME") or "qwen35-9b-q5km"
    profile_path = paths.root / "profiles" / "deployment.local.env"
    if profile_path.is_file():
        if (
            not RECOVERY_REQUIRED_DEPLOYMENT_KEYS.issubset(values)
            or not set(values).issubset(RECOVERY_DEPLOYMENT_KEYS)
        ):
            raise ConfigError(
                "deployment profile is incomplete or contains fields that cannot be captured safely for recovery"
            )
        canonical_values = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        deployment_profile = {
            "present": True,
            "format": "allowlisted-env-v1",
            "sha256": hashlib.sha256(canonical_values.encode()).hexdigest(),
            "values": dict(sorted(values.items())),
            "containsCredentials": False,
        }
    container = live_container(container_name) if container_name else None
    if container:
        identity = {
            "sha256": live_runtime_sha256(container),
            "configuration": normalized_live_runtime(container),
        }
    container_state = (container or {}).get("State") or {}
    container_health = (container_state.get("Health") or {}).get("Status")
    container_healthy = bool(
        container_state.get("Status") == "running" and container_health == "healthy"
    )
    return {
        "healthy": probe.returncode == 0 or container_healthy,
        "containerHealthy": container_healthy,
        "profile": profile,
        "containerName": container_name,
        "runtimeIdentity": identity,
        "deploymentProfile": deployment_profile,
        "capturedWithoutSecrets": True,
    }


def _restore_deployment_profile(paths: ProjectPaths, original: dict[str, Any]) -> None:
    from scripts.env_utils import atomic_write_private_text

    target = paths.root / "profiles" / "deployment.local.env"
    record = original.get("deploymentProfile") or {"present": False}
    if not record.get("present"):
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise RecoveryError("refusing to remove an unsafe deployment profile")
            target.unlink()
        return
    values = record.get("values")
    canonical_values = (
        json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if isinstance(values, dict)
        else ""
    )
    if (
        record.get("format") != "allowlisted-env-v1"
        or not isinstance(values, dict)
        or not RECOVERY_REQUIRED_DEPLOYMENT_KEYS.issubset(values)
        or not set(values).issubset(RECOVERY_DEPLOYMENT_KEYS)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())
        or record.get("containsCredentials") is not False
        or hashlib.sha256(canonical_values.encode()).hexdigest() != record.get("sha256")
    ):
        raise RecoveryError("recorded deployment profile failed its recovery identity check")
    content = (
        "# Restored from an allowlisted control-plane transaction; local and untracked.\n"
        + "".join(
            f"{key}={shlex.quote(values[key])}\n"
            for key in sorted(values)
        )
    )
    atomic_write_private_text(target, content)


def _verify_restored_runtime(paths: ProjectPaths, original: dict[str, Any]) -> None:
    if not original.get("healthy"):
        container_name = original.get("containerName")
        if container_name:
            daemon = run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                cwd=paths.root,
                timeout=20,
                check=False,
            )
            if daemon.returncode != 0 or not daemon.stdout.strip():
                raise RecoveryError(
                    "cannot verify that production remains stopped while Docker is unavailable"
                )
            probe = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
                cwd=paths.root,
                timeout=20,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "true":
                raise RecoveryError("production was stopped before the transaction but is now running")
        return
    profile = original.get("profile")
    if profile not in {"latency", "throughput"}:
        raise RecoveryError("cannot verify restored production because the original profile is unknown")
    run(["scripts/runtime.sh", "assert-profile", profile], cwd=paths.root, timeout=120)
    expected = (original.get("runtimeIdentity") or {}).get("sha256")
    container_name = original.get("containerName")
    if not expected or not container_name:
        raise RecoveryError(
            "healthy original runtime has no complete pre-transaction identity"
        )
    from scripts.runtime_identity import live_container, live_runtime_sha256

    container = live_container(container_name)
    if not container or live_runtime_sha256(container) != expected:
        raise RecoveryError("restored runtime identity does not match the pre-transaction runtime")


def _transaction_environment(transaction_id: str) -> dict[str, str]:
    return {"QWEN_CONTROL_TRANSACTION_ID": transaction_id}


def _mark_recovery_required(
    transaction: TransactionStore, transaction_id: str, error: BaseException
) -> None:
    try:
        current = transaction.read()
        if (
            current
            and current.get("id") == transaction_id
            and current.get("schemaVersion") == TRANSACTION_SCHEMA_VERSION
            and current.get("state") != "recovery_required"
        ):
            transaction.transition(
                "recovery_required",
                expected_id=transaction_id,
                detail=str(error),
            )
    except StackError:
        # Preserve the initiating error. Any still-active state remains fail-closed
        # and visible to the read-only reconciliation plan.
        pass


def _current_catalog_deployment_admission(
    paths: ProjectPaths, spec: CatalogDeploymentSpec
) -> dict[str, Any]:
    """Independently re-run host admission for the current strict Catalog."""

    result = run(
        [
            "python3",
            "scripts/model-manager.py",
            "admit",
            "--model",
            spec.catalog_id,
            "--json",
        ],
        cwd=paths.root,
        timeout=30,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
        admitted_spec = CatalogDeploymentSpec.from_catalog_model(
            payload["recommendation"]
        )
    except (json.JSONDecodeError, KeyError, DeploymentSpecError) as error:
        raise ConfigError(
            "independent deployment admission returned an invalid Catalog identity"
        ) from error
    if (
        result.returncode != 0
        or payload.get("readyToDeploy") is not True
        or admitted_spec.sha256 != spec.sha256
    ):
        raise AdmissionError(
            "current Catalog and host admission denied this deployment",
            facts={"admission": payload},
        )
    return payload


def _deploy(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "deploy")
    if not args.model:
        raise UsageError("deploy requires an explicit --model Catalog ID")
    payload = _legacy_plan(paths, args)
    if payload.get("actionPlan") is None:
        raise AdmissionError(
            "deployment admission denied; no typed action plan was issued",
            facts={"plan": payload},
        )
    try:
        deployment_spec = CatalogDeploymentSpec.from_catalog_model(
            payload["recommendation"]
        )
        if deployment_spec.catalog_id != args.model:
            raise DeploymentSpecError(
                "planner Catalog ID does not match the explicitly approved --model"
            )
        action_plan = parse_deployment_plan(
            deployment_spec,
            payload.get("actionPlan"),
            require_full_lifecycle=True,
        )
        current_spec, _artifact = load_approved_catalog_spec(
            paths.root / "catalog" / "models.json",
            deployment_spec.catalog_id,
            deployment_spec.sha256,
        )
    except (KeyError, DeploymentSpecError) as error:
        raise ConfigError(f"planner returned an invalid typed deployment plan: {error}") from error
    _current_catalog_deployment_admission(paths, current_spec)
    transaction = TransactionStore(paths)
    state = transaction.begin(
        "deploy",
        current_spec.catalog_id,
        _original_runtime(paths),
        approved_catalog_spec=current_spec.approval_document(),
    )
    transaction_environment = {
        **_transaction_environment(state["id"]),
        "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256": current_spec.sha256,
    }
    try:
        transaction.transition("deploying", expected_id=state["id"])
        for action in action_plan.actions:
            if action.kind is ActionKind.FETCH_ARTIFACT:
                argv = [
                    "./scripts/model-manager.py",
                    "download",
                    "--model",
                    action.catalog_id,
                    "--catalog-spec-sha256",
                    current_spec.sha256,
                    "--artifact-sha256",
                    str(action.artifact_sha256),
                    "--yes",
                ]
            elif action.kind is ActionKind.ACTIVATE_SPEC:
                argv = [
                    "./scripts/model-manager.py",
                    "select",
                    "--model",
                    action.catalog_id,
                    "--catalog-spec-sha256",
                    current_spec.sha256,
                    "--yes",
                ]
            elif action.kind is ActionKind.START_RUNTIME:
                argv = ["./scripts/runtime.sh", "start", "latency"]
            elif action.kind is ActionKind.QUICK_SMOKE:
                argv = ["./scripts/acceptance-suite.sh", "quick"]
                transaction.transition("accepting", expected_id=state["id"])
            else:  # pragma: no cover - Enum parsing above is exhaustive.
                raise ConfigError(f"unsupported deployment action: {action.kind}")
            run(
                argv,
                cwd=paths.root,
                timeout=7200,
                env=transaction_environment,
            )
        state = transaction.transition("completed", expected_id=state["id"])
    except (Exception, KeyboardInterrupt) as exc:
        _mark_recovery_required(transaction, state["id"], exc)
        raise
    return CommandResult("deploy", "ok", "catalog-backed deployment and quick acceptance completed", facts={"transaction": state})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _selected_catalog_spec(
    paths: ProjectPaths,
) -> tuple[CatalogDeploymentSpec, dict[str, str]]:
    """Freeze the exact current private selection against the strict Catalog."""

    from scripts.runtime_identity import deployment_values

    try:
        values = deployment_values()
        model_id = values["QWEN_CATALOG_ID"]
        document = catalog.load_catalog(paths.root / "catalog" / "models.json")
        model = catalog.model_by_id(document, model_id)
        expected = configuration.catalog_deployment_environment(model)
        spec = CatalogDeploymentSpec.from_catalog_model(model)
    except (OSError, KeyError, ValueError, RuntimeError, catalog.CatalogError, DeploymentSpecError) as error:
        raise ConfigError("cannot establish the exact selected Catalog identity") from error
    if values != expected:
        raise ConfigError(
            "selected deployment profile is not the exact current Catalog projection"
        )
    return spec, values


def _rollout_admission(
    paths: ProjectPaths,
    model_id: str,
    *,
    mode: str,
) -> tuple[dict[str, Any], CatalogDeploymentSpec]:
    if mode not in {"existing-selection", "replacement"}:
        raise ConfigError("unsupported rollout admission mode")
    result = run(
        [
            "python3",
            "scripts/model-manager.py",
            "admit",
            "--model",
            model_id,
            f"--{mode}",
            "--json",
        ],
        cwd=paths.root,
        timeout=60,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
        spec = CatalogDeploymentSpec.from_catalog_model(payload["recommendation"])
    except (json.JSONDecodeError, KeyError, DeploymentSpecError) as error:
        raise ConfigError("rollout admission returned an invalid Catalog identity") from error
    expected_mode = f"read-only-{mode}-admission"
    ready_key = (
        "readyToStartExisting"
        if mode == "existing-selection"
        else "readyToReplaceExisting"
    )
    if (
        result.returncode != 0
        or payload.get("mode") != expected_mode
        or spec.catalog_id != model_id
        or payload.get(ready_key) is not True
    ):
        raise AdmissionError(
            f"{mode} rollout admission denied the requested Catalog subject",
            facts={"admission": payload},
        )
    return payload, spec


def _require_recovery_anchor_admission(payload: dict[str, Any]) -> None:
    """Require a source that is already a trusted, validated rollback anchor."""

    if payload.get("catalogRecoveryEligible") is not True:
        raise AdmissionError(
            "the selected source is not an eligible immutable rollback anchor",
            facts={"admission": payload},
        )


def _rollout_intent(
    *,
    rollback_spec: RollbackSpec,
    source: CatalogDeploymentSpec,
    target: CatalogDeploymentSpec,
    plan: Any,
    previous_pointer: RollbackPointer | None,
) -> dict[str, Any]:
    return {
        "policyId": ROLLOUT_INTENT_POLICY_ID,
        "rollbackSpecSha256": rollback_spec.sha256,
        "sourceCatalogSpecSha256": source.sha256,
        "targetCatalogSpecSha256": target.sha256,
        "rolloutPlan": plan.document(),
        "rolloutPlanSha256": plan.sha256,
        "previousRollbackPointer": (
            previous_pointer.document() if previous_pointer is not None else None
        ),
    }


def _rollout_environment(
    transaction_id: str,
    spec: CatalogDeploymentSpec,
    *,
    subject: str,
    ordinal: int,
    kind: str,
) -> dict[str, str]:
    return {
        **_transaction_environment(transaction_id),
        "LOCAL_INFERENCE_APPROVED_CATALOG_SPEC_SHA256": spec.sha256,
        "LOCAL_INFERENCE_ROLLOUT_SUBJECT": subject,
        "LOCAL_INFERENCE_ROLLOUT_ACTION_ORDINAL": str(ordinal),
        "LOCAL_INFERENCE_ROLLOUT_ACTION_KIND": kind,
    }


def _action_result(action: Any, *, output: str = "", facts: Any = None) -> str:
    return canonical_sha256(
        {
            "action": action.document(),
            "outputSha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            "facts": facts,
        }
    )


def _advance_rollout(
    transaction: TransactionStore,
    state: dict[str, Any],
    action: Any,
    *,
    output: str = "",
    facts: Any = None,
) -> dict[str, Any]:
    return transaction.advance_rollout_action(
        expected_id=state["id"],
        expected_state=state["state"],
        expected_action_ordinal=action.ordinal,
        expected_action_kind=action.kind.value,
        result_sha256=_action_result(action, output=output, facts=facts),
    )


def _execute_upgrade_rollout(
    paths: ProjectPaths,
    transaction: TransactionStore,
    state: dict[str, Any],
    source: CatalogDeploymentSpec,
    target: CatalogDeploymentSpec,
    rollback_spec: RollbackSpec,
    previous_pointer: RollbackPointer | None,
) -> dict[str, Any]:
    plan = build_upgrade_rollout_plan(
        source, target, rollback_spec.sha256, admission_granted=True
    )
    store = RollbackStore(paths)
    for action in plan.actions:
        subject_spec = source if action.subject == "source" else target
        environment = _rollout_environment(
            state["id"],
            subject_spec,
            subject=action.subject,
            ordinal=action.ordinal,
            kind=action.kind.value,
        )
        output = ""
        facts: Any = None
        if action.kind is RolloutActionKind.SOURCE_QUICK:
            result = run(
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
                cwd=paths.root,
                timeout=7200,
                env=environment,
            )
            output = result.stdout[-8000:]
        elif action.kind is RolloutActionKind.FETCH_TARGET_ARTIFACT:
            result = run(
                [
                    "scripts/model-manager.py",
                    "download",
                    "--model",
                    target.catalog_id,
                    "--catalog-spec-sha256",
                    target.sha256,
                    "--artifact-sha256",
                    str(action.artifact_sha256),
                    "--replacement",
                    "--yes",
                ],
                cwd=paths.root,
                timeout=7200,
                env=environment,
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.STOP_SOURCE:
            state = transaction.transition(
                "production_stopping",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            result = run(
                ["scripts/runtime.sh", "stop"],
                cwd=paths.root,
                timeout=900,
                env=environment,
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.ACTIVATE_TARGET:
            state = transaction.transition(
                "candidate_starting",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            result = run(
                [
                    "scripts/model-manager.py",
                    "select",
                    "--model",
                    target.catalog_id,
                    "--catalog-spec-sha256",
                    target.sha256,
                    "--replacement",
                    "--yes",
                ],
                cwd=paths.root,
                timeout=900,
                env=environment,
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.START_TARGET:
            result = run(
                ["scripts/runtime.sh", "start", "latency"],
                cwd=paths.root,
                timeout=900,
                env=environment,
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.TARGET_QUICK:
            state = transaction.transition(
                "accepting",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            result = run(
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
                cwd=paths.root,
                timeout=7200,
                env=environment,
            )
            output = result.stdout[-8000:]
        elif action.kind is RolloutActionKind.PUBLISH_ROLLBACK:
            transaction.assert_approved_deployment(
                transaction_id=state["id"],
                catalog_spec_sha256=source.sha256,
                catalog_id=source.catalog_id,
                rollout_subject="source",
                action_ordinal=action.ordinal,
                action_kind=action.kind.value,
            )
            # The source was captured before the maintenance window.  Recheck
            # its current Catalog trust, local artifact/image, host, evidence,
            # Compose rendering, and controller materials immediately before
            # granting it future rollback authority.
            verify_rollback_spec(paths, rollback_spec)
            expected_generation = previous_pointer.generation if previous_pointer else 0
            expected_previous = (
                previous_pointer.active_spec_sha256 if previous_pointer else None
            )
            pointer = store.publish(
                rollback_spec,
                expected_generation=expected_generation,
                expected_previous_sha256=expected_previous,
                transaction_id=state["id"],
                updated_at=_utc_now(),
            )
            facts = pointer.document()
        else:  # pragma: no cover - exact plan construction is exhaustive.
            raise ConfigError(f"unsupported upgrade rollout action: {action.kind}")
        state = _advance_rollout(
            transaction, state, action, output=output, facts=facts
        )
    return transaction.transition(
        "completed",
        expected_id=state["id"],
        expected_state=state["state"],
        expected_action_ordinal=len(plan.actions),
    )


def _upgrade(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    source, _selection = _selected_catalog_spec(paths)
    if source.catalog_id == args.model:
        raise UsageError("upgrade target must differ from the selected source")
    source_admission, admitted_source = _rollout_admission(
        paths, source.catalog_id, mode="existing-selection"
    )
    _require_recovery_anchor_admission(source_admission)
    target_admission, target = _rollout_admission(
        paths, args.model, mode="replacement"
    )
    if admitted_source.sha256 != source.sha256:
        raise IntegrityError("source admission changed the selected Catalog subject")
    facts = {
        "scopePolicy": "same-controller-same-catalog-anchor-v1",
        "sourceCatalogId": source.catalog_id,
        "sourceCatalogSpecSha256": source.sha256,
        "targetCatalogId": target.catalog_id,
        "targetCatalogSpecSha256": target.sha256,
        "networkAcquisitionOnRollbackAllowed": False,
        "qualificationProduced": False,
        "sourceAdmission": source_admission,
        "targetAdmission": target_admission,
    }
    if not args.yes:
        # A dry run is an execution preflight, not merely a Catalog lookup.
        # Reuse the complete read-only anchor capture with a fixed synthetic
        # identity so health, latency, evidence, artifacts, Compose, image,
        # host, and controller drift are rejected before approval.  The
        # synthetic spec is discarded; execution recaptures the source under
        # runtime -> transaction locks with its real transaction identity.
        preflight_original = _original_runtime(paths)
        capture_rollback_spec(
            paths,
            transaction_id=ROLLOUT_PREFLIGHT_TRANSACTION_ID,
            captured_at=ROLLOUT_PREFLIGHT_CAPTURED_AT,
            original=preflight_original,
            source_admission=source_admission,
        )
        return CommandResult(
            "upgrade",
            "ok",
            "upgrade preflight passed; no state changed",
            facts={**facts, "dryRun": True},
            nextActions=[
                NextAction(
                    f"./stack upgrade --model {target.catalog_id} --yes",
                    "capture an immutable source anchor and execute the typed maintenance window",
                    True,
                )
            ],
        )

    transaction = TransactionStore(paths)
    store = RollbackStore(paths)
    captured: dict[str, Any] = {}

    def capture(transaction_id: str, created_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
        locked_source, _ = _selected_catalog_spec(paths)
        locked_admission, locked_admitted_source = _rollout_admission(
            paths, locked_source.catalog_id, mode="existing-selection"
        )
        _require_recovery_anchor_admission(locked_admission)
        _locked_target_admission, locked_target = _rollout_admission(
            paths, args.model, mode="replacement"
        )
        if (
            locked_source.sha256 != source.sha256
            or locked_admitted_source.sha256 != source.sha256
            or locked_target.sha256 != target.sha256
        ):
            raise IntegrityError("Catalog or selected rollout subject changed during locked capture")
        original = _original_runtime(paths)
        rollback_spec = capture_rollback_spec(
            paths,
            transaction_id=transaction_id,
            captured_at=created_at,
            original=original,
            source_admission=locked_admission,
        )
        store.put(rollback_spec)
        previous_pointer = store.read_pointer()
        plan = build_upgrade_rollout_plan(
            source, target, rollback_spec.sha256, admission_granted=True
        )
        captured.update(
            {"rollbackSpec": rollback_spec, "previousPointer": previous_pointer}
        )
        return original, _rollout_intent(
            rollback_spec=rollback_spec,
            source=source,
            target=target,
            plan=plan,
            previous_pointer=previous_pointer,
        )

    state = transaction.begin_rollout(
        "upgrade",
        target.catalog_id,
        capture,
        approved_catalog_spec=target.approval_document(),
    )
    try:
        state = _execute_upgrade_rollout(
            paths,
            transaction,
            state,
            source,
            target,
            captured["rollbackSpec"],
            captured["previousPointer"],
        )
    except (Exception, KeyboardInterrupt) as error:
        _mark_recovery_required(transaction, state["id"], error)
        raise
    return CommandResult(
        "upgrade",
        "ok",
        "typed maintenance-window upgrade completed; quick smoke passed",
        facts={**facts, "dryRun": False, "transaction": state},
    )


def _execute_rollback_rollout(
    paths: ProjectPaths,
    transaction: TransactionStore,
    state: dict[str, Any],
    source: CatalogDeploymentSpec,
    anchor: CatalogDeploymentSpec,
    rollback_spec: RollbackSpec,
    pointer: RollbackPointer,
) -> dict[str, Any]:
    plan = build_rollback_rollout_plan(
        source, anchor, rollback_spec.sha256, admission_granted=True
    )
    store = RollbackStore(paths)
    for action in plan.actions:
        subject_spec = source if action.subject == "source" else anchor
        environment = _rollout_environment(
            state["id"],
            subject_spec,
            subject=action.subject,
            ordinal=action.ordinal,
            kind=action.kind.value,
        )
        output = ""
        facts: Any = None
        if action.kind is RolloutActionKind.STOP_SOURCE:
            state = transaction.transition(
                "production_stopping",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            result = run(
                ["scripts/runtime.sh", "stop"],
                cwd=paths.root,
                timeout=900,
                env=environment,
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.ACTIVATE_TARGET:
            state = transaction.transition(
                "candidate_starting",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            with transaction.authorized_runtime_mutation(
                state["id"],
                catalog_spec_sha256=anchor.sha256,
                catalog_id=anchor.catalog_id,
                rollout_subject="target",
                action_ordinal=action.ordinal,
                action_kind=action.kind.value,
            ):
                verify_rollback_spec(paths, rollback_spec)
                write_anchor_selection(paths, rollback_spec)
            facts = {"selectionSha256": rollback_spec.document()["selection"]["sha256"]}
        elif action.kind is RolloutActionKind.START_TARGET:
            result = run(
                ["scripts/runtime.sh", "start", "latency"],
                cwd=paths.root,
                timeout=900,
                env={
                    **environment,
                    RUNTIME_PULL_POLICY_ENV: RUNTIME_PULL_POLICY_NEVER,
                },
            )
            output = result.stdout[-4000:]
        elif action.kind is RolloutActionKind.TARGET_QUICK:
            state = transaction.transition(
                "accepting",
                expected_id=state["id"],
                expected_state=state["state"],
                expected_action_ordinal=action.ordinal,
            )
            result = run(
                ["scripts/acceptance-suite.sh", "quick", "--no-record"],
                cwd=paths.root,
                timeout=7200,
                env=environment,
            )
            output = result.stdout[-8000:]
        elif action.kind is RolloutActionKind.CLEAR_ROLLBACK:
            transaction.assert_approved_deployment(
                transaction_id=state["id"],
                catalog_spec_sha256=anchor.sha256,
                catalog_id=anchor.catalog_id,
                rollout_subject="target",
                action_ordinal=action.ordinal,
                action_kind=action.kind.value,
            )
            cleared = store.clear(
                expected_generation=pointer.generation,
                expected_previous_sha256=rollback_spec.sha256,
                transaction_id=state["id"],
                updated_at=_utc_now(),
            )
            facts = cleared.document()
        else:  # pragma: no cover - exact plan construction is exhaustive.
            raise ConfigError(f"unsupported rollback action: {action.kind}")
        state = _advance_rollout(
            transaction, state, action, output=output, facts=facts
        )
    return transaction.transition(
        "completed",
        expected_id=state["id"],
        expected_state=state["state"],
        expected_action_ordinal=len(plan.actions),
    )


def _rollback(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    store = RollbackStore(paths)
    pointer = store.read_pointer()
    if pointer is None or pointer.active_spec_sha256 is None:
        raise RecoveryError("no active immutable rollback anchor is available")
    rollback_spec = store.read_spec(pointer.active_spec_sha256)
    verification = verify_rollback_spec(paths, rollback_spec)
    source, _selection = _selected_catalog_spec(paths)
    try:
        anchor = CatalogDeploymentSpec.from_document(
            rollback_spec.document()["catalogSpec"]
        )
        plan = build_rollback_rollout_plan(
            source, anchor, rollback_spec.sha256, admission_granted=True
        )
    except (KeyError, DeploymentSpecError) as error:
        raise ConfigError("active rollback anchor cannot form a typed plan") from error
    facts = {
        **verification,
        "sourceCatalogId": source.catalog_id,
        "sourceCatalogSpecSha256": source.sha256,
        "targetCatalogId": anchor.catalog_id,
        "targetCatalogSpecSha256": anchor.sha256,
        "pointer": pointer.document(),
        "rolloutPlan": plan.document(),
        "networkAcquisitionAllowed": False,
        "qualificationProduced": False,
    }
    if not args.yes:
        return CommandResult(
            "rollback",
            "ok",
            "rollback anchor and typed plan verified; no state changed",
            facts={**facts, "dryRun": True},
            nextActions=[
                NextAction(
                    "./stack rollback --yes",
                    "consume the active one-shot anchor in a maintenance window",
                    True,
                )
            ],
        )

    transaction = TransactionStore(paths)

    def capture(_transaction_id: str, _created_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
        locked_pointer = store.read_pointer()
        if locked_pointer is None or locked_pointer.document() != pointer.document():
            raise RecoveryError("rollback pointer changed during locked capture")
        locked_spec = store.read_spec(rollback_spec.sha256)
        verify_rollback_spec(paths, locked_spec)
        locked_source, _ = _selected_catalog_spec(paths)
        if locked_source.sha256 != source.sha256:
            raise IntegrityError("selected rollback source changed during locked capture")
        locked_plan = build_rollback_rollout_plan(
            source, anchor, rollback_spec.sha256, admission_granted=True
        )
        return rollback_recovery_original(rollback_spec), _rollout_intent(
            rollback_spec=rollback_spec,
            source=source,
            target=anchor,
            plan=locked_plan,
            previous_pointer=pointer,
        )

    state = transaction.begin_rollout(
        "rollback",
        anchor.catalog_id,
        capture,
        approved_catalog_spec=anchor.approval_document(),
    )
    try:
        state = _execute_rollback_rollout(
            paths,
            transaction,
            state,
            source,
            anchor,
            rollback_spec,
            pointer,
        )
    except (Exception, KeyboardInterrupt) as error:
        _mark_recovery_required(transaction, state["id"], error)
        raise
    return CommandResult(
        "rollback",
        "ok",
        "one-shot rollback completed; exact anchor quick smoke passed",
        facts={**facts, "dryRun": False, "transaction": state},
    )


def _accept(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "acceptance")
    environment = {}
    if args.tier in {"standard", "full"}:
        modelport = os.environ.get("MODELPORT_PROJECT_DIR")
        if not modelport:
            raise ConfigError("MODELPORT_PROJECT_DIR is required for standard/full acceptance")
        environment["MODELPORT_PROJECT_DIR"] = modelport
    result = run(["scripts/acceptance-suite.sh", args.tier], cwd=paths.root, timeout=7200, env=environment)
    return CommandResult("accept", "ok", f"{args.tier} acceptance passed", facts={"output": result.stdout[-8000:]})


def _release_catalog_admission(paths: ProjectPaths) -> dict[str, Any]:
    from scripts.runtime_identity import deployment_values

    try:
        model_id = deployment_values().get("QWEN_CATALOG_ID", "qwen35-9b-q5km")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"cannot identify the selected catalog model safely: {exc}") from exc
    payload = _legacy_plan(
        paths,
        argparse.Namespace(model=model_id, vram_gib=None, ram_gib=None),
    )
    if payload.get("catalogDeploymentEligible") is not True:
        raise AdmissionError(
            "candidate release is blocked because the selected catalog entry is not deployment-eligible",
            facts={
                "model": model_id,
                "catalogDeploymentEligible": payload.get(
                    "catalogDeploymentEligible", False
                ),
                "catalogEvidenceStatus": payload.get("catalogEvidenceStatus"),
                "caveats": payload.get("caveats", []),
            },
        )
    return payload


def _release(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "candidate release")
    modelport = os.environ.get("MODELPORT_PROJECT_DIR")
    if not modelport:
        raise ConfigError("MODELPORT_PROJECT_DIR must reference the reviewed compatible checkout")
    _release_catalog_admission(paths)
    transaction = TransactionStore(paths)
    state = transaction.begin("release", args.mode, _original_runtime(paths))
    transaction_environment = {
        "MODELPORT_PROJECT_DIR": modelport,
        **_transaction_environment(state["id"]),
    }
    try:
        transaction.transition("production_stopping", expected_id=state["id"])
        transaction.transition("candidate_starting", expected_id=state["id"])
        transaction.transition("accepting", expected_id=state["id"])
        result = run(
            ["scripts/release-candidate.sh", args.mode],
            cwd=paths.root,
            timeout=14400,
            env=transaction_environment,
        )
        transaction.transition("production_restoring", expected_id=state["id"])
        _verify_restored_runtime(paths, state["original"])
        state = transaction.transition("completed", expected_id=state["id"])
    except (Exception, KeyboardInterrupt) as exc:
        _mark_recovery_required(transaction, state["id"], exc)
        raise
    return CommandResult("release", "ok", "candidate acceptance and production restoration completed", facts={"transaction": state, "output": result.stdout[-8000:]})


def _profile(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "profile switch")
    transaction = TransactionStore(paths)
    initial = transaction.begin("profile", args.name, _original_runtime(paths))
    try:
        transaction.transition("deploying", expected_id=initial["id"])
        result = run(
            ["scripts/runtime.sh", "profile", args.name],
            cwd=paths.root,
            timeout=900,
            env=_transaction_environment(initial["id"]),
        )
        state = transaction.transition("completed", expected_id=initial["id"])
    except (Exception, KeyboardInterrupt) as exc:
        _mark_recovery_required(transaction, initial["id"], exc)
        raise
    return CommandResult("profile", "ok", f"production profile switched to {args.name}", facts={"transaction": state, "output": result.stdout[-4000:]})


def _reconciliation_runtime_plan(
    paths: ProjectPaths, plan: dict[str, Any]
) -> dict[str, Any]:
    if not plan.get("required"):
        return plan
    classified = dict(plan)
    original = (plan.get("transaction") or {}).get("original", {})
    classified["originalSafeToRestore"] = recovery_original_is_safe(original)
    try:
        current = _original_runtime(paths)
    except StackError as exc:
        classified.update(
            {
                "runtimeDisposition": "runtime-classification-unavailable",
                "runtimeDiagnostic": exc.summary,
                "automaticEligible": False,
            }
        )
        return classified
    classified["currentRuntime"] = {
        "healthy": bool(current.get("healthy")),
        "profile": current.get("profile"),
        "containerName": current.get("containerName"),
        "runtimeIdentitySha256": (current.get("runtimeIdentity") or {}).get("sha256"),
    }
    if current.get("healthy"):
        matches_original = False
        if original.get("healthy"):
            try:
                _verify_restored_runtime(paths, original)
                matches_original = True
            except StackError:
                pass
        if matches_original:
            disposition = "original-runtime-already-restored"
        elif (plan.get("transaction") or {}).get("schemaVersion") == 1:
            disposition = "legacy-failure-healthy-runtime-preserved"
        elif classified["originalSafeToRestore"]:
            # A newly deployed runtime can be perfectly healthy and canonical
            # while its acceptance step failed. Health must never supersede the
            # transaction's pre-change identity for a v2 recovery.
            disposition = "restoration-required"
        else:
            disposition = "unsafe-original-review-required"
        classified["runtimeDisposition"] = disposition
        classified["automaticEligible"] = bool(
            plan.get("automaticEligible")
            and disposition
            in {"restoration-required", "original-runtime-already-restored"}
        )
        return classified
    try:
        _verify_restored_runtime(paths, original)
    except StackError as exc:
        classified["runtimeDiagnostic"] = exc.summary
        disposition = (
            "restoration-required"
            if classified["originalSafeToRestore"]
            else "unsafe-original-review-required"
        )
    else:
        disposition = "original-runtime-already-restored"
    classified["runtimeDisposition"] = disposition
    classified["automaticEligible"] = bool(
        plan.get("automaticEligible")
        and classified["originalSafeToRestore"]
        and disposition
        in {"restoration-required", "original-runtime-already-restored"}
    )
    return classified


def _verify_current_runtime_canonical(
    paths: ProjectPaths, current: dict[str, Any]
) -> None:
    profile = current.get("profile")
    if not current.get("healthy") or profile not in {"latency", "throughput"}:
        raise RecoveryError(
            "current healthy runtime cannot be classified safely because its profile is unknown"
        )
    run(
        ["scripts/runtime.sh", "assert-profile", profile],
        cwd=paths.root,
        timeout=120,
    )


def _restore_rollout_pointer(
    paths: ProjectPaths, document: dict[str, Any]
) -> dict[str, Any] | None:
    """Undo only the pointer write described by one persisted rollout intent."""

    intent = document.get("rolloutIntent")
    if not isinstance(intent, dict):
        return None
    try:
        previous_document = intent["previousRollbackPointer"]
        previous = (
            RollbackPointer.from_document(previous_document)
            if previous_document is not None
            else None
        )
        rollback_spec_sha256 = intent["rollbackSpecSha256"]
    except (KeyError, RollbackSpecError) as error:
        raise RecoveryError("rollout recovery pointer intent is invalid") from error
    store = RollbackStore(paths)
    current = store.read_pointer()
    if (current.document() if current else None) == (
        previous.document() if previous else None
    ):
        return current.document() if current else None
    if current is None:
        raise RecoveryError("rollback pointer disappeared during rollout recovery")
    previous_generation = previous.generation if previous else 0
    expected_generation = previous_generation + 1
    operation = document.get("operation")
    if operation == "upgrade":
        expected_current: str | None = rollback_spec_sha256
    elif operation == "rollback":
        expected_current = None
    else:
        raise RecoveryError("non-rollout transaction cannot restore a rollback pointer")
    # A pointer restore is durable before the transaction terminal write.  If
    # the process dies in that window, recognize the exact monotonic successor
    # written by this transaction instead of replaying the old CAS forever.
    current_document = current.document()
    previous_active = previous.active_spec_sha256 if previous else None
    if (
        current.generation == expected_generation + 1
        and current.active_spec_sha256 == previous_active
        and current.previous_spec_sha256 == expected_current
        and current_document.get("updatedByTransactionId") == document["id"]
    ):
        return current_document
    restored = store.restore(
        previous,
        expected_current_generation=expected_generation,
        expected_current_sha256=expected_current,
        transaction_id=document["id"],
        updated_at=_utc_now(),
    )
    return restored.document()


def _verified_rollout_recovery_anchor(
    paths: ProjectPaths,
    document: dict[str, Any],
    original: dict[str, Any],
) -> tuple[RollbackSpec, dict[str, Any]]:
    """Load and verify the exact persisted anchor before a recovery mutation."""

    intent = document.get("rolloutIntent")
    if not isinstance(intent, dict):
        raise RecoveryError("rollout recovery has no immutable intent")
    try:
        spec = RollbackStore(paths).read_spec(intent["rollbackSpecSha256"])
    except (KeyError, RollbackStoreError) as error:
        raise RecoveryError("rollout recovery anchor is unavailable") from error
    if rollback_recovery_original(spec) != original:
        raise RecoveryError("rollout recovery anchor does not match the persisted original")
    verification = verify_rollback_spec(paths, spec)
    return spec, verification


def _verify_rollout_recovery(
    paths: ProjectPaths,
    document: dict[str, Any],
    original: dict[str, Any],
) -> dict[str, Any]:
    """Verify exact anchor identity, bound quick smoke, and pointer rollback."""

    _spec, verification = _verified_rollout_recovery_anchor(
        paths, document, original
    )
    _verify_restored_runtime(paths, original)
    result = run(
        ["scripts/acceptance-suite.sh", "quick", "--no-record"],
        cwd=paths.root,
        timeout=7200,
        env=_transaction_environment(document["id"]),
    )
    pointer = _restore_rollout_pointer(paths, document)
    return {
        "anchor": verification,
        "quickOutputSha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
        "rollbackPointer": pointer,
    }


def _resolve_verified_transaction(
    transaction: TransactionStore,
    document: dict[str, Any],
    target_state: str,
    *,
    detail: str,
) -> dict[str, Any]:
    transaction_id = document.get("id")
    if not isinstance(transaction_id, str):
        raise RecoveryError("reconciliation transaction has no stable identity")
    if document.get("schemaVersion") == 1:
        if document.get("state") == "failed":
            return transaction.resolve_legacy_failed(
                target_state, expected_id=transaction_id, detail=detail
            )
        return transaction.resolve_legacy_active(
            target_state, expected_id=transaction_id, detail=detail
        )
    current = transaction.read()
    if not current:
        raise RecoveryError("reconciliation transaction disappeared")
    if current.get("id") != transaction_id:
        raise RecoveryError("reconciliation transaction identity changed")
    if target_state == "superseded-verified":
        if current["state"] != "recovery_required":
            transaction.transition(
                "recovery_required", expected_id=transaction_id, detail=detail
            )
        return transaction.transition(
            "superseded-verified", expected_id=transaction_id, detail=detail
        )
    if current["state"] != "production_restoring":
        if current["state"] != "recovery_required":
            transaction.transition(
                "recovery_required", expected_id=transaction_id, detail=detail
            )
        transaction.transition(
            "production_restoring", expected_id=transaction_id, detail=detail
        )
    return transaction.transition(
        "failed-restored", expected_id=transaction_id, detail=detail
    )


def _reconcile(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    transaction = TransactionStore(paths)
    base_plan = transaction.reconciliation_plan()
    if not base_plan["required"]:
        return CommandResult(
            "reconcile",
            "ok",
            "no unfinished transaction requires recovery",
            facts=base_plan,
        )
    document = base_plan["transaction"]
    if (
        args.yes
        and document.get("schemaVersion") == TRANSACTION_SCHEMA_VERSION
        and document.get("state") != "recovery_required"
    ):
        # The fence itself acquires runtime.lock before transaction.lock and
        # rechecks PID/start-time/boot identity. A surviving initiator or child
        # runtime mutation therefore cannot be mistaken for an orphan.
        transaction.fence_orphaned(expected_id=document["id"])
        base_plan = transaction.reconciliation_plan()
    plan = _reconciliation_runtime_plan(paths, base_plan)
    if not args.yes:
        recovery_description = "restore production and verify health"
        if plan.get("runtimeDisposition") == "legacy-failure-healthy-runtime-preserved":
            recovery_description = (
                "verify the healthy canonical runtime and close the legacy record "
                "without replacing it"
            )
        return CommandResult(
            "reconcile",
            "recovery-required",
            "unfinished transaction found; inspect actions and rerun with --yes",
            code=int(ExitCode.RECOVERY),
            facts=plan,
            nextActions=[
                NextAction("./stack reconcile --yes", recovery_description, True)
            ],
        )
    document = plan["transaction"]
    if (
        document.get("schemaVersion") == TRANSACTION_SCHEMA_VERSION
        and document.get("state") != "recovery_required"
    ):
        raise RecoveryError(
            "an active transaction cannot be reconciled while its initiating process may still be running",
            facts=plan,
        )
    original = document.get("original", {})
    disposition = plan.get("runtimeDisposition")
    if disposition == "original-runtime-already-restored":
        rollout_recovery: dict[str, Any] | None = None
        if document.get("operation") in {"upgrade", "rollback"}:
            try:
                rollout_recovery = _verify_rollout_recovery(
                    paths, document, original
                )
            except (Exception, KeyboardInterrupt) as exc:
                _mark_recovery_required(transaction, document["id"], exc)
                raise
        state = _resolve_verified_transaction(
            transaction,
            document,
            "failed-restored",
            detail="original runtime was already restored and verified",
        )
        return CommandResult(
            "reconcile",
            "ok",
            "previous failure was already restored and is now verified",
            facts={
                **plan,
                "transaction": state,
                "rolloutRecovery": rollout_recovery,
            },
        )
    if disposition == "legacy-failure-healthy-runtime-preserved":
        # Keep the final live observation, exact identity check, and durable
        # resolution inside the same runtime -> transaction lock boundary.
        # Otherwise another supported runtime mutation could replace the
        # container after verification but before the legacy record is closed.
        with transaction.runtime_boundary():
            current = _original_runtime(paths)
            _verify_current_runtime_canonical(paths, current)
            state = _resolve_verified_transaction(
                transaction,
                document,
                "superseded-verified",
                detail="healthy canonical runtime preserved without automatic replacement",
            )
        return CommandResult(
            "reconcile",
            "ok",
            "healthy canonical runtime was preserved and the prior failure was superseded",
            facts={**plan, "transaction": state},
        )
    if disposition != "restoration-required" or not recovery_original_is_safe(original):
        raise RecoveryError(
            "reconciliation cannot mutate runtime until the current state and original record are safe",
            facts=plan,
        )

    if document.get("operation") in {"upgrade", "rollback"}:
        try:
            # Recovery must not rewrite the selected profile or start Compose
            # until the immutable anchor, current Catalog, controller, local
            # artifacts, image, host, and retained evidence all reverify.  The
            # same verification runs again after startup to close the normal
            # check/use window before the transaction becomes terminal.
            _verified_rollout_recovery_anchor(paths, document, original)
        except (Exception, KeyboardInterrupt) as exc:
            _mark_recovery_required(transaction, document["id"], exc)
            raise

    if document.get("schemaVersion") == TRANSACTION_SCHEMA_VERSION:
        transaction.transition(
            "production_restoring", expected_id=document["id"]
        )
    try:
        failed_runtime = _original_runtime(paths)
        _restore_deployment_profile(paths, original)
    except (Exception, KeyboardInterrupt) as exc:
        _mark_recovery_required(transaction, document["id"], exc)
        raise
    profile = original.get("profile")
    if profile not in {"latency", "throughput"}:
        profile = "latency"
    environment = {
        "QWEN_BOOT_PROFILE": profile,
        "QWEN_RESTORE_PRODUCTION": "true" if original.get("healthy") else "false",
        **_transaction_environment(document["id"]),
    }
    if document.get("operation") in {"upgrade", "rollback"}:
        environment[RUNTIME_PULL_POLICY_ENV] = RUNTIME_PULL_POLICY_NEVER
    failed_container_name = failed_runtime.get("containerName")
    if isinstance(failed_container_name, str) and failed_container_name:
        environment["QWEN_FAILED_CONTAINER_NAME"] = failed_container_name
    if document.get("schemaVersion") == 1:
        environment["QWEN_ALLOW_LEGACY_RECONCILIATION"] = "true"
    try:
        result = run(
            ["scripts/runtime-reconcile.sh"],
            cwd=paths.root,
            timeout=900,
            env=environment,
        )
        _verify_restored_runtime(paths, original)
        rollout_recovery = (
            _verify_rollout_recovery(paths, document, original)
            if document.get("operation") in {"upgrade", "rollback"}
            else None
        )
    except (Exception, KeyboardInterrupt) as exc:
        _mark_recovery_required(transaction, document["id"], exc)
        raise
    state = _resolve_verified_transaction(
        transaction,
        document,
        "failed-restored",
        detail="original runtime restored and verified after failure",
    )
    return CommandResult(
        "reconcile",
        "ok",
        "production reconciliation completed and the failure is restored",
        facts={
            **plan,
            "transaction": state,
            "output": result.stdout[-4000:],
            "rolloutRecovery": rollout_recovery,
        },
    )


def dispatch(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    if args.command == "plan":
        return _plan_result(paths, args)
    if args.command == "doctor":
        return _doctor(paths)
    if args.command == "status":
        return _status(paths, args)
    if args.command == "verify":
        return _verify(paths, args)
    if args.command == "deploy":
        return _deploy(paths, args)
    if args.command == "upgrade":
        return _upgrade(paths, args)
    if args.command == "rollback":
        return _rollback(paths, args)
    if args.command == "accept":
        return _accept(paths, args)
    if args.command == "release":
        return _release(paths, args)
    if args.command == "profile":
        return _profile(paths, args)
    if args.command == "reconcile":
        return _reconcile(paths, args)
    if args.command == "config":
        if args.config_command == "check":
            drift = configuration.check(paths)
            if drift:
                raise ConfigError("generated runtime profiles have drift", facts={"files": drift})
            return CommandResult("config check", "ok", "typed configuration and profiles match")
        if args.write:
            _require_yes(args.yes, "configuration render")
            changed = configuration.write(paths)
            return CommandResult("config render", "ok", "runtime profiles rendered", facts={"changed": changed})
        rendered = {str(path.relative_to(paths.root)): content for path, content in configuration.expected_files(paths).items()}
        return CommandResult("config render", "ok", "rendered profiles without writing", facts={"files": rendered})
    if args.command == "attest":
        if args.attest_command == "create":
            document = attestation.create_draft(paths, args.evidence.resolve())
            attestation.write_private_json(args.output.resolve(), document)
            return CommandResult("attest create", "ok", "unsigned reusable-validation draft created", facts={"path": str(args.output.resolve()), "validationStatus": document["validationStatus"]})
        if args.attest_command == "sign":
            _require_yes(args.yes, "attestation signing")
            facts = attestation.sign_file(
                paths,
                args.path.resolve(),
                args.signature.resolve(),
                args.secret_key.resolve(),
                args.tool,
            )
            return CommandResult("attest sign", "ok", "reusable attestation signed with a detached signature", facts=facts)
        detached_arguments_present = any(
            value is not None
            for value in (
                args.tool,
                args.public_key,
                args.signature,
                args.trusted_key_sha256,
            )
        )
        if detached_arguments_present and not (
            args.require_signature or args.for_promotion
        ):
            raise UsageError(
                "detached signature arguments require --require-signature or --for-promotion"
            )
        if args.trusted_key_sha256 and not args.for_promotion:
            raise UsageError(
                "--trusted-key-sha256 is valid only with --for-promotion"
            )
        if args.require_signature or args.for_promotion:
            if not args.tool or not args.public_key or not args.signature:
                raise UsageError(
                    "detached verification requires --tool, --public-key, and --signature"
                )
            if args.for_promotion and not args.trusted_key_sha256:
                raise UsageError(
                    "--for-promotion requires --trusted-key-sha256 from an external trust anchor"
                )
            facts = attestation.verify_detached(
                paths,
                args.path.resolve(),
                args.signature.resolve(),
                args.public_key.resolve(),
                args.tool,
                trusted_key_sha256=args.trusted_key_sha256,
                require_promotion=args.for_promotion,
            )
            summary = (
                "reusable attestation is cryptographically and promotion valid"
                if args.for_promotion
                else "reusable attestation detached signature is cryptographically valid"
            )
            return CommandResult("attest verify", "ok", summary, facts=facts)
        facts = attestation.verify_file(args.path.resolve(), require_signature=args.require_signature)
        return CommandResult("attest verify", "ok", "reusable attestation structure verified", facts=facts)
    if args.command == "bundle":
        if args.bundle_command == "create":
            _require_yes(args.yes, "bundle creation")
            facts = bundle.create(
                paths,
                args.output.resolve(),
                args.model,
                include_model=args.include_model,
                image_archive=args.image_archive.resolve() if args.image_archive else None,
            )
            return CommandResult("bundle create", "ok", "offline bundle created", facts=facts)
        if args.bundle_command == "verify":
            return CommandResult("bundle verify", "ok", "offline bundle verified", facts=bundle.verify(args.path.resolve()))
        _require_yes(args.yes, "bundle import")
        return CommandResult("bundle import", "ok", "bundle artifacts imported without selecting or starting", facts=bundle.import_artifacts(paths, args.path.resolve()))
    if args.command == "calibrate":
        if args.calibrate_command == "plan":
            return CommandResult("calibrate plan", "ok", "candidate calibration matrix generated", facts=calibration.plan(paths))
        _require_yes(args.yes, "calibration benchmark")
        output = args.output.resolve() if args.output else paths.root / "logs" / "calibration" / "latest.json"
        facts = calibration.run_benchmarks(paths, output)
        complete = facts["measurementsComplete"]
        return CommandResult(
            "calibrate run",
            "ok" if complete else "attention",
            (
                "non-promotable calibration report written; production profile unchanged"
                if complete
                else "calibration report written but one or more measurements failed"
            ),
            code=int(ExitCode.SUCCESS if complete else ExitCode.EXTERNAL),
            facts=facts,
        )
    if args.command == "storage":
        if args.storage_command == "report":
            return CommandResult("storage report", "ok", "local storage inventory completed", facts=storage.inventory(paths))
        candidates = storage.gc_candidates(paths, older_than_days=args.older_than_days)
        if not args.yes:
            return CommandResult("storage gc", "ok", "dry-run only; no files deleted", facts={"dryRun": True, "candidates": candidates})
        removed = storage.delete_candidates(paths, candidates)
        return CommandResult("storage gc", "ok", f"removed {len(removed)} safe temporary files", facts={"dryRun": False, "removed": removed})
    if args.command == "credentials":
        if args.credentials_command == "migrate-systemd":
            _require_yes(args.yes, "credential migration")
            facts = credentials.migrate_to_systemd(paths, args.kind)
            return CommandResult(
                "credentials migrate-systemd",
                "ok",
                "encrypted systemd credential created; plaintext compatibility source retained",
                facts=facts,
            )
        facts = credentials.audit(paths)
        return CommandResult("credentials audit", "ok" if facts["healthy"] else "attention", "credential metadata audit completed without reading values", code=int(ExitCode.SUCCESS if facts["healthy"] else ExitCode.CONFIG), facts=facts)
    if args.command == "migrate":
        facts = migration.check(paths)
        if not facts["compatible"]:
            raise ConfigError("one or more local schemas are incompatible", facts=facts)
        migrations = facts.get("migrationsRequired") or {}
        if getattr(args, "yes", False) and set(migrations) == {
            "selectedDeploymentProfile"
        }:
            transaction = TransactionStore(paths)
            if transaction.reconciliation_plan()["required"]:
                raise RecoveryError(
                    "selected profile migration is blocked by an unfinished transaction"
                )
            with transaction.authorized_runtime_mutation():
                run(
                    ["python3", "scripts/model-manager.py", "verify", "--read-only"],
                    cwd=paths.root,
                    timeout=900,
                )
                profile_migration = configuration.normalize_selected_deployment_profile(
                    paths
                )
            facts = migration.check(paths)
            return CommandResult(
                "migrate",
                "ok",
                "selected deployment profile normalized after full artifact verification",
                facts={**facts, "selectedDeploymentProfileMigration": profile_migration},
            )
        if migrations:
            actions: list[NextAction] = []
            if "transaction" in migrations:
                actions.append(
                    NextAction(
                        "./stack reconcile --json",
                        "classify the legacy transaction before any explicit resolution",
                        False,
                    )
                )
            if set(migrations) == {"selectedDeploymentProfile"}:
                actions.append(
                    NextAction(
                        "./stack migrate --yes",
                        "fully verify the selected artifact and atomically normalize its private profile",
                        True,
                    )
                )
            return CommandResult(
                "migrate",
                "attention",
                "local schemas are readable but explicit migration or reconciliation is required",
                code=int(ExitCode.CONFIG),
                facts=facts,
                nextActions=actions,
            )
        return CommandResult(
            "migrate",
            "ok",
            "all observed schemas are current and readable",
            facts=facts,
        )
    if args.command == "reference":
        expected = reference.render()
        path = paths.root / "docs" / "REFERENCE.md"
        if args.check:
            actual = path.read_text(encoding="utf-8") if path.exists() else ""
            if actual != expected:
                raise ConfigError("generated reference documentation has drift", facts={"path": "docs/REFERENCE.md"})
            return CommandResult("reference", "ok", "generated reference documentation matches")
        if args.write:
            _require_yes(args.yes, "reference generation")
            path.write_text(expected, encoding="utf-8")
            return CommandResult("reference", "ok", "generated reference documentation written", facts={"path": "docs/REFERENCE.md"})
        return CommandResult("reference", "ok", "generated reference rendered without writing", facts={"content": expected})
    raise UsageError(f"unsupported command: {args.command}")


def render_human(result: CommandResult) -> str:
    lines = [result.summary]
    plan = result.facts.get("plan")
    if isinstance(plan, dict):
        recommendation = plan.get("recommendation") or {}
        if recommendation:
            artifact_bytes = sum(item.get("bytes", 0) for item in recommendation.get("artifacts", []) if item.get("required"))
            lines.extend(
                [
                    f"model: {recommendation.get('id')}",
                    f"evidence: {plan.get('evidenceStatus')}",
                    f"host acceptance: {plan.get('hostAcceptanceStatus')}",
                    f"download: {artifact_bytes / 1024**3:.2f} GiB",
                    f"artifact revision: {recommendation.get('artifactRevision')}",
                    f"license: {recommendation.get('license', {}).get('spdx')} (review required)",
                    f"context: {recommendation.get('runtime', {}).get('contextTokens')}",
                    f"free VRAM: {(plan.get('host', {}).get('gpus') or [{}])[0].get('freeVramGiB', 'unknown')} GiB",
                ]
            )
        lines.extend(f"NOTE: {note}" for note in plan.get("caveats", []))
    for action in result.nextActions:
        suffix = " [requires --yes]" if action.requiresApproval else ""
        lines.append(f"next: {action.command}{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    command_name = next((item for item in raw if not item.startswith("-")), "unknown")
    previous_handlers: dict[signal.Signals, Any] = {}

    def interrupt_on_signal(_number: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for candidate in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if candidate is not None:
            previous_handlers[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, interrupt_on_signal)
    try:
        try:
            args = parser().parse_args(raw)
            paths = ProjectPaths.discover()
            result = dispatch(paths, args)
        except RollbackSpecError as exc:
            result = CommandResult(
                command_name,
                "error",
                str(exc),
                code=int(ExitCode.INTEGRITY),
            )
        except RollbackStoreError as exc:
            result = CommandResult(
                command_name,
                "error",
                str(exc),
                code=int(ExitCode.RECOVERY),
            )
        except StackError as exc:
            result = CommandResult(command_name, "error", exc.summary, code=exc.code, facts=exc.facts)
        except KeyboardInterrupt:
            result = CommandResult(command_name, "error", "interrupted", code=int(ExitCode.EXTERNAL))
        except Exception as exc:
            result = CommandResult(
                command_name,
                "error",
                f"unexpected control-plane error: {type(exc).__name__}",
                code=int(ExitCode.EXTERNAL),
            )
    finally:
        for candidate, handler in previous_handlers.items():
            signal.signal(candidate, handler)
    if json_output:
        print(result.to_json())
    else:
        stream = sys.stdout if result.code == 0 else sys.stderr
        print(render_human(result), file=stream)
    return result.code
