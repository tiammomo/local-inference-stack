"""Unified public CLI for the local inference stack control plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from . import attestation, bundle, calibration, configuration, credentials, migration, reference, storage
from .paths import ProjectPaths
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
from .transactions import TERMINAL_STATES, TransactionStore


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
    commands.add_parser("status", help="read-only runtime and transaction status")

    verify = commands.add_parser("verify", help="verify repository, configuration, or model")
    verify.add_argument("--scope", choices=("repository", "config", "model"), default="repository")
    verify.add_argument("--model")
    verify.add_argument("--cached", action="store_true")

    deploy = commands.add_parser("deploy", help="execute catalog-generated deployment steps")
    deploy.add_argument("--model")
    deploy.add_argument("--yes", action="store_true")

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

    migrate_parser = commands.add_parser("migrate", help="explicit schema compatibility check")
    migrate_parser.add_argument("--check", action="store_true", default=True)

    reference_parser = commands.add_parser("reference", help="generated control-plane reference")
    reference_parser.add_argument("--check", action="store_true")
    reference_parser.add_argument("--write", action="store_true")
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
    actions = [
        NextAction(command=item, description="catalog-backed deployment step", requiresApproval=True)
        for item in payload.get("nextCommands", [])
    ]
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
    }
    return CommandResult(
        "doctor",
        "ok" if healthy else "attention",
        "all control-plane checks passed" if healthy else "one or more control-plane checks need attention",
        code=int(ExitCode.SUCCESS if healthy else ExitCode.CONFIG),
        facts=facts,
    )


def _status(paths: ProjectPaths) -> CommandResult:
    runtime = run(["scripts/runtime.sh", "status"], cwd=paths.root, timeout=20, check=False)
    transaction = TransactionStore(paths).read()
    healthy = runtime.returncode == 0
    return CommandResult(
        "status",
        "ok" if healthy else "unavailable",
        "runtime is healthy" if healthy else "runtime status check did not pass",
        code=int(ExitCode.SUCCESS if healthy else ExitCode.EXTERNAL),
        facts={
            "runtimeHealthy": healthy,
            "runtimeSummary": runtime.stdout[-4000:],
            "runtimeDiagnostic": runtime.stderr[-1000:],
            "transaction": transaction,
        },
    )


def _verify(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    if args.scope == "config":
        drift = configuration.check(paths)
        if drift:
            raise ConfigError("generated runtime profiles have drift", facts={"files": drift})
        return CommandResult("verify", "ok", "typed runtime configuration and profiles match")
    if args.scope == "model":
        argv = ["python3", "scripts/model-manager.py", "verify"]
        if args.model:
            argv.extend(("--model", args.model))
        if args.cached:
            argv.append("--cached")
        result = run(argv, cwd=paths.root, timeout=600)
        return CommandResult("verify", "ok", "catalog artifact verification passed", facts={"output": result.stdout[-4000:]})
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
    container_name = values.get("QWEN_CONTAINER_NAME")
    profile_path = paths.root / "profiles" / "deployment.local.env"
    safe_keys = {
        "QWEN_CATALOG_ID",
        "QWEN_MODEL_DIR",
        "QWEN_MODEL_FILE",
        "QWEN_MODEL_DISPLAY_NAME",
        "QWEN_QUANTIZATION",
        "QWEN_SERVED_MODEL_ID",
        "QWEN_CONTAINER_NAME",
        "QWEN_RUNTIME_UID",
        "QWEN_RUNTIME_GID",
        "MODELPORT_NETWORK_NAME",
        "QWEN_CTX_SIZE",
        "QWEN_RECOMMENDED_INPUT_TOKENS",
        "QWEN_N_PREDICT",
        "QWEN_CACHE_RAM",
        "QWEN_BATCH_SIZE",
        "QWEN_UBATCH_SIZE",
    }
    if profile_path.is_file():
        if not set(values).issubset(safe_keys):
            raise ConfigError(
                "deployment profile contains fields that cannot be captured safely for recovery"
            )
        try:
            content = profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read deployment profile for recovery: {exc}") from exc
        deployment_profile = {
            "present": True,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
            "containsCredentials": False,
        }
    container = live_container(container_name) if container_name else None
    if container:
        identity = {
            "sha256": live_runtime_sha256(container),
            "configuration": normalized_live_runtime(container),
        }
    return {
        "healthy": probe.returncode == 0,
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
    content = record.get("content")
    if (
        not isinstance(content, str)
        or record.get("containsCredentials") is not False
        or hashlib.sha256(content.encode()).hexdigest() != record.get("sha256")
    ):
        raise RecoveryError("recorded deployment profile failed its recovery identity check")
    atomic_write_private_text(target, content)


def _verify_restored_runtime(paths: ProjectPaths, original: dict[str, Any]) -> None:
    if not original.get("healthy"):
        container_name = original.get("containerName")
        if container_name:
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
    if expected and container_name:
        from scripts.runtime_identity import live_container, live_runtime_sha256

        container = live_container(container_name)
        if not container or live_runtime_sha256(container) != expected:
            raise RecoveryError("restored runtime identity does not match the pre-transaction runtime")


def _deploy(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "deploy")
    payload = _legacy_plan(paths, args)
    if not payload.get("readyToDeploy") or not payload.get("nextCommands"):
        raise AdmissionError("deployment admission denied", facts={"plan": payload})
    transaction = TransactionStore(paths)
    state = transaction.begin("deploy", payload["recommendation"]["id"], _original_runtime(paths))
    try:
        transaction.transition("deploying")
        completed: list[str] = []
        for command in payload["nextCommands"]:
            argv = shlex.split(command)
            if argv[:2] not in (["./scripts/model-manager.py", "download"], ["./scripts/model-manager.py", "select"], ["./scripts/runtime.sh", "start"], ["./scripts/acceptance-suite.sh", "quick"]):
                raise ConfigError(f"planner returned an unapproved deployment command: {command}")
            if argv[:2] == ["./scripts/acceptance-suite.sh", "quick"]:
                transaction.transition("accepting")
            run(argv, cwd=paths.root, timeout=7200)
            completed.append(command)
        state = transaction.transition("completed")
    except Exception as exc:
        try:
            transaction.transition("failed", detail=str(exc))
        except StackError:
            pass
        raise
    return CommandResult("deploy", "ok", "catalog-backed deployment and quick acceptance completed", facts={"transaction": state})


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


def _release(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "candidate release")
    modelport = os.environ.get("MODELPORT_PROJECT_DIR")
    if not modelport:
        raise ConfigError("MODELPORT_PROJECT_DIR must reference the reviewed compatible checkout")
    transaction = TransactionStore(paths)
    state = transaction.begin("release", args.mode, _original_runtime(paths))
    try:
        transaction.transition("production_stopping")
        transaction.transition("candidate_starting")
        transaction.transition("accepting")
        result = run(
            ["scripts/release-candidate.sh", args.mode],
            cwd=paths.root,
            timeout=14400,
            env={"MODELPORT_PROJECT_DIR": modelport},
        )
        transaction.transition("production_restoring")
        _verify_restored_runtime(paths, state["original"])
        state = transaction.transition("completed")
    except Exception as exc:
        current = transaction.read()
        if current and current["state"] not in TERMINAL_STATES:
            try:
                transaction.transition("failed", detail=str(exc))
            except StackError:
                pass
        raise
    return CommandResult("release", "ok", "candidate acceptance and production restoration completed", facts={"transaction": state, "output": result.stdout[-8000:]})


def _profile(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    _require_yes(args.yes, "profile switch")
    transaction = TransactionStore(paths)
    transaction.begin("profile", args.name, _original_runtime(paths))
    try:
        transaction.transition("deploying")
        result = run(["scripts/runtime.sh", "profile", args.name], cwd=paths.root, timeout=900)
        state = transaction.transition("completed")
    except Exception as exc:
        try:
            transaction.transition("failed", detail=str(exc))
        except StackError:
            pass
        raise
    return CommandResult("profile", "ok", f"production profile switched to {args.name}", facts={"transaction": state, "output": result.stdout[-4000:]})


def _reconcile(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    transaction = TransactionStore(paths)
    plan = transaction.reconciliation_plan()
    if not plan["required"]:
        return CommandResult("reconcile", "ok", "no unfinished transaction requires recovery", facts=plan)
    if not args.yes:
        return CommandResult(
            "reconcile",
            "recovery-required",
            "unfinished transaction found; inspect actions and rerun with --yes",
            code=int(ExitCode.RECOVERY),
            facts=plan,
            nextActions=[NextAction("./stack reconcile --yes", "restore production and verify health", True)],
        )
    original = plan["transaction"].get("original", {})
    _restore_deployment_profile(paths, original)
    profile = original.get("profile")
    if profile not in {"latency", "throughput"}:
        profile = "latency"
    result = run(
        ["scripts/runtime-reconcile.sh"],
        cwd=paths.root,
        timeout=900,
        env={
            "QWEN_BOOT_PROFILE": profile,
            "QWEN_RESTORE_PRODUCTION": "true" if original.get("healthy") else "false",
        },
    )
    _verify_restored_runtime(paths, original)
    current = transaction.read()
    if current and current["state"] == "failed":
        transaction.transition("production_restoring")
    current = transaction.read()
    if current and current["state"] != "production_restoring":
        # Recovery from a non-failed active state becomes explicit failure first.
        transaction.transition("failed", detail="reconciliation invoked")
        transaction.transition("production_restoring")
    state = transaction.transition("completed")
    return CommandResult("reconcile", "ok", "production reconciliation completed", facts={"transaction": state, "output": result.stdout[-4000:]})


def dispatch(paths: ProjectPaths, args: argparse.Namespace) -> CommandResult:
    if args.command == "plan":
        return _plan_result(paths, args)
    if args.command == "doctor":
        return _doctor(paths)
    if args.command == "status":
        return _status(paths)
    if args.command == "verify":
        return _verify(paths, args)
    if args.command == "deploy":
        return _deploy(paths, args)
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
        if args.require_signature:
            if not args.tool or not args.public_key or not args.signature:
                raise UsageError(
                    "--require-signature also requires --tool, --public-key, and --signature"
                )
            facts = attestation.verify_detached(
                paths,
                args.path.resolve(),
                args.signature.resolve(),
                args.public_key.resolve(),
                args.tool,
            )
            return CommandResult("attest verify", "ok", "reusable attestation and detached signature verified", facts=facts)
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
        return CommandResult("calibrate run", "ok", "calibration report written; production profile unchanged", facts=calibration.run_benchmarks(paths, output))
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
        return CommandResult(
            "migrate",
            "ok",
            "all observed schemas are readable and no silent migration is required",
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
    try:
        args = parser().parse_args(raw)
        paths = ProjectPaths.discover()
        result = dispatch(paths, args)
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
    if json_output:
        print(result.to_json())
    else:
        stream = sys.stdout if result.code == 0 else sys.stderr
        print(render_human(result), file=stream)
    return result.code
