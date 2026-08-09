from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


integrated = load_module(
    "verify_integrated_hardening_test",
    ROOT / "scripts" / "verify-integrated-deployment.py",
)
operations_report = load_module(
    "operations_report_hardening_test", ROOT / "scripts" / "operations-report.py"
)
soak_check = load_module(
    "soak_check_hardening_test", ROOT / "scripts" / "soak-check.py"
)


def secure_container(policy_name: str) -> dict:
    policy = integrated.MODELPORT_SECURITY_POLICIES[policy_name]
    mounts = []
    binds = []
    for destination, wanted in policy["mounts"].items():
        if wanted["type"] == "volume":
            source = wanted["name"]
            mount = {
                "Type": "volume",
                "Name": wanted["name"],
                "Source": f"/var/lib/docker/volumes/{wanted['name']}/_data",
                "Destination": destination,
                "RW": wanted["rw"],
                "Mode": wanted["mode"],
                "Propagation": "",
            }
        else:
            source = f"/secure{destination}"
            mount = {
                "Type": "bind",
                "Source": source,
                "Destination": destination,
                "RW": wanted["rw"],
                "Mode": wanted["mode"],
                "Propagation": "rprivate",
            }
        mounts.append(mount)
        binds.append(f"{source}:{destination}:{wanted['mode']}")

    port_bindings = {}
    for container_port, expected in policy["portBindings"].items():
        if expected is None:
            binding = {"HostIp": "127.0.0.1", "HostPort": "33002"}
        else:
            binding = {
                "HostIp": expected["hostIp"],
                "HostPort": expected["hostPort"],
            }
        port_bindings[container_port] = [binding]

    return {
        "Name": f"/{policy['containerName']}",
        "Image": "sha256:" + "1" * 64,
        "State": {
            "Status": "running",
            "Running": True,
            "Health": {"Status": "healthy"},
        },
        "Config": {
            "Image": f"{policy_name}:reviewed",
            "User": policy["user"],
            "Cmd": [f"run-{policy_name}"],
            "Entrypoint": [f"/{policy_name}-entrypoint"],
            "WorkingDir": f"/{policy_name}",
            "Healthcheck": {
                "Test": ["CMD", f"/{policy_name}-health"],
                "Interval": 10_000_000_000,
                "Timeout": 5_000_000_000,
                "Retries": 3,
            },
            "Env": [
                "PATH=/usr/local/bin:/usr/bin:/bin",
                f"{policy_name.upper()}_MODE=reviewed",
                f"{policy_name.upper()}_TOKEN=test-only-secret",
            ],
            "Labels": {
                "com.docker.compose.project": "modelport",
                "com.docker.compose.service": policy_name,
            },
        },
        "HostConfig": {
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PublishAllPorts": False,
            "AutoRemove": False,
            "PidsLimit": 512,
            "Init": True,
            "ShmSize": 64 * 1024 * 1024,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=64m"},
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": integrated.MODELPORT_NETWORK_NAME,
            "PidMode": "",
            "IpcMode": "private",
            "UTSMode": "",
            "UsernsMode": "",
            "CgroupnsMode": "private",
            "Devices": None,
            "DeviceRequests": None,
            "GroupAdd": None,
            "ExtraHosts": sorted(policy["extraHosts"]),
            "Links": None,
            "Dns": [],
            "DnsOptions": [],
            "DnsSearch": [],
            "Sysctls": None,
            "PortBindings": port_bindings,
            "Binds": binds,
        },
        "Mounts": mounts,
        "NetworkSettings": {
            "Networks": {
                integrated.MODELPORT_NETWORK_NAME: {
                    "Aliases": sorted(policy["networkAliases"])
                }
            }
        },
    }


def write_reviewed_configuration(working_dir: Path) -> None:
    (working_dir / "docker-compose.yml").write_text(
        "services:\n  modelport:\n    image: modelport:reviewed\n",
        encoding="utf-8",
    )
    (working_dir / "config.toml").write_text(
        "[server]\nmode = 'reviewed'\n", encoding="utf-8"
    )
    env_path = working_dir / ".env"
    env_path.write_text(
        "MODELPORT_MODE=reviewed\nMODELPORT_TOKEN=test-only-secret\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)


def reviewed_identity(
    container: dict, policy_name: str, working_dir: Path
) -> dict:
    config = container["Config"]
    environment = {
        entry.split("=", 1)[0]: entry.split("=", 1)[1]
        for entry in config["Env"]
    }
    secret_keys = [f"{policy_name.upper()}_TOKEN"]
    public_environment = {
        key: value for key, value in environment.items() if key not in secret_keys
    }
    config["Labels"].update(
        {
            "com.docker.compose.project.working_dir": str(working_dir),
            "com.docker.compose.project.config_files": str(
                working_dir / "docker-compose.yml"
            ),
        }
    )
    compose = {
        "project": "modelport",
        "service": policy_name,
        "configFile": "docker-compose.yml",
        "bindings": {},
        "files": {
            "docker-compose.yml": {
                "sha256": hashlib.sha256(
                    (working_dir / "docker-compose.yml").read_bytes()
                ).hexdigest()
            }
        },
    }
    if policy_name == "modelport":
        compose["bindings"] = {
            "/config/.env": ".env",
            "/config/config.toml": "config.toml",
        }
        source_env = {
            "MODELPORT_MODE": "reviewed",
            "MODELPORT_TOKEN": "test-only-secret",
        }
        compose["files"].update(
            {
                "config.toml": {
                    "sha256": hashlib.sha256(
                        (working_dir / "config.toml").read_bytes()
                    ).hexdigest()
                },
                ".env": {
                    "keys": sorted(source_env),
                    "secretKeys": ["MODELPORT_TOKEN"],
                    "publicSha256": integrated._canonical_sha256(
                        {"MODELPORT_MODE": "reviewed"}
                    ),
                },
            }
        )
        for mount in container["Mounts"]:
            destination = mount["Destination"]
            relative = compose["bindings"].get(destination)
            if relative is not None:
                mount["Source"] = str(working_dir / relative)
        rewritten_binds = []
        for bind in container["HostConfig"]["Binds"]:
            source, destination, mode = bind.rsplit(":", 2)
            relative = compose["bindings"].get(destination)
            rewritten_binds.append(
                f"{working_dir / relative if relative is not None else source}:{destination}:{mode}"
            )
        container["HostConfig"]["Binds"] = rewritten_binds
    return {
        "configuredImage": config["Image"],
        "imageId": container["Image"],
        "command": copy.deepcopy(config["Cmd"]),
        "entrypoint": copy.deepcopy(config["Entrypoint"]),
        "workingDirectory": config["WorkingDir"],
        "healthcheck": copy.deepcopy(config["Healthcheck"]),
        "environment": {
            "keys": sorted(environment),
            "secretKeys": secret_keys,
            "publicSha256": integrated._canonical_sha256(public_environment),
        },
        "compose": compose,
    }


class RuntimeHealthContractTests(unittest.TestCase):
    def run_runtime(self, action: str, health_payload: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            commands = {
                "docker": "#!/bin/sh\nexit 0\n",
                "nvidia-smi": "#!/bin/sh\nprintf 'Fake GPU, 1, 16, 0, 40, 20\\n'\n",
                "curl": """#!/usr/bin/env python3
import os
import sys

url = sys.argv[-1]
if url.endswith('/health'):
    print(os.environ['TEST_HEALTH_PAYLOAD'])
elif url.endswith('/slots'):
    print('[{"n_ctx":131072}]')
else:
    raise SystemExit(2)
""",
            }
            for name, body in commands.items():
                path = fake_bin / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o700)
            environment = dict(os.environ)
            environment.update(
                {
                    "PATH": f"{fake_bin}:{environment['PATH']}",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TEST_HEALTH_PAYLOAD": health_payload,
                }
            )
            argv = [str(ROOT / "scripts" / "runtime.sh"), action]
            if action == "assert-profile":
                argv.append("latency")
            return subprocess.run(
                argv,
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
            )

    def test_status_rejects_non_ok_non_object_and_invalid_health_json(self) -> None:
        cases = (
            ('{"status":"error"}', "did not return status=ok"),
            ("[]", "must return a JSON object"),
            ("not-json", "returned invalid JSON"),
        )
        for payload, diagnostic in cases:
            with self.subTest(payload=payload):
                result = self.run_runtime("status", payload)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)

    def test_assert_profile_rejects_http_200_with_unhealthy_json(self) -> None:
        result = self.run_runtime("assert-profile", '{"status":"starting"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not return status=ok", result.stderr)

    def test_status_accepts_object_with_ok_status(self) -> None:
        result = self.run_runtime("status", '{"status":"ok"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('{"status":"ok"}', result.stdout)


class IntegratedSecurityEnvelopeTests(unittest.TestCase):
    def test_reviewed_envelopes_are_accepted_for_every_integrated_container(self) -> None:
        for policy_name, policy in integrated.MODELPORT_SECURITY_POLICIES.items():
            with self.subTest(policy=policy_name):
                self.assertEqual(
                    integrated.container_security_mismatches(
                        secure_container(policy_name), policy
                    ),
                    [],
                )

    def test_security_envelope_rejects_each_escape_surface(self) -> None:
        mutations = {
            "missing privileged field": lambda value: value["HostConfig"].pop(
                "Privileged"
            ),
            "privileged": lambda value: value["HostConfig"].__setitem__(
                "Privileged", True
            ),
            "root user": lambda value: value["Config"].__setitem__("User", "root"),
            "writable root": lambda value: value["HostConfig"].__setitem__(
                "ReadonlyRootfs", False
            ),
            "capability added": lambda value: value["HostConfig"].__setitem__(
                "CapAdd", ["SYS_ADMIN"]
            ),
            "capabilities not dropped": lambda value: value["HostConfig"].__setitem__(
                "CapDrop", []
            ),
            "publish all ports": lambda value: value["HostConfig"].__setitem__(
                "PublishAllPorts", True
            ),
            "host network": lambda value: value["HostConfig"].__setitem__(
                "NetworkMode", "host"
            ),
            "host pid namespace": lambda value: value["HostConfig"].__setitem__(
                "PidMode", "host"
            ),
            "device passthrough": lambda value: value["HostConfig"].__setitem__(
                "Devices", [{"PathOnHost": "/dev/sda"}]
            ),
            "unbounded pids": lambda value: value["HostConfig"].__setitem__(
                "PidsLimit", None
            ),
            "executable tmp": lambda value: value["HostConfig"].__setitem__(
                "Tmpfs", {"/tmp": "rw,size=64m"}
            ),
            "extra network": lambda value: value["NetworkSettings"][
                "Networks"
            ].__setitem__("attacker", {"Aliases": []}),
            "extra host mount": lambda value: (
                value["Mounts"].append(
                    {
                        "Type": "bind",
                        "Source": "/",
                        "Destination": "/host",
                        "RW": True,
                        "Mode": "rw",
                        "Propagation": "rprivate",
                    }
                ),
                value["HostConfig"]["Binds"].append("/:/host:rw"),
            ),
        }
        baseline = secure_container("modelport")
        policy = integrated.MODELPORT_SECURITY_POLICIES["modelport"]
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                candidate = copy.deepcopy(baseline)
                mutate(candidate)
                self.assertTrue(
                    integrated.container_security_mismatches(candidate, policy)
                )

    def test_every_port_binding_is_checked_not_only_the_first(self) -> None:
        candidate = secure_container("modelport")
        candidate["HostConfig"]["PortBindings"]["38082/tcp"].append(
            {"HostIp": "0.0.0.0", "HostPort": "38082"}
        )
        mismatches = integrated.container_security_mismatches(
            candidate, integrated.MODELPORT_SECURITY_POLICIES["modelport"]
        )
        self.assertIn("portBindings.38082/tcp", mismatches)

    def test_postgres_cannot_publish_a_host_port(self) -> None:
        candidate = secure_container("postgres")
        candidate["HostConfig"]["PortBindings"] = {
            "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]
        }
        mismatches = integrated.container_security_mismatches(
            candidate, integrated.MODELPORT_SECURITY_POLICIES["postgres"]
        )
        self.assertIn("portBindings", mismatches)

    def test_dashboard_listener_must_be_loopback(self) -> None:
        candidate = secure_container("dashboard")
        candidate["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostIp"] = (
            "0.0.0.0"
        )
        mismatches = integrated.container_security_mismatches(
            candidate, integrated.MODELPORT_SECURITY_POLICIES["dashboard"]
        )
        self.assertIn("portBindings.8080/tcp.loopback", mismatches)

    def test_executable_identity_rejects_every_substitution_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            write_reviewed_configuration(working_dir)
            baseline = secure_container("modelport")
            identity = reviewed_identity(baseline, "modelport", working_dir)
            self.assertEqual(
                integrated.container_executable_mismatches(
                    baseline, identity, "modelport"
                ),
                [],
            )
            mutations = {
                "image ID": lambda value: value.__setitem__(
                    "Image", "sha256:" + "2" * 64
                ),
                "configured image": lambda value: value["Config"].__setitem__(
                    "Image", "attacker:latest"
                ),
                "command": lambda value: value["Config"].__setitem__(
                    "Cmd", ["sh", "-c", "evil"]
                ),
                "entrypoint": lambda value: value["Config"].__setitem__(
                    "Entrypoint", ["/tmp/entrypoint"]
                ),
                "working directory": lambda value: value["Config"].__setitem__(
                    "WorkingDir", "/tmp"
                ),
                "healthcheck": lambda value: value["Config"].__setitem__(
                    "Healthcheck", {"Test": ["NONE"]}
                ),
                "extra environment": lambda value: value["Config"]["Env"].append(
                    "LD_PRELOAD=/tmp/evil.so"
                ),
                "public environment value": lambda value: value["Config"]["Env"].__setitem__(
                    1, "MODELPORT_MODE=attacker"
                ),
                "empty secret": lambda value: value["Config"]["Env"].__setitem__(
                    2, "MODELPORT_TOKEN="
                ),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    candidate = copy.deepcopy(baseline)
                    mutate(candidate)
                    self.assertTrue(
                        integrated.container_executable_mismatches(
                            candidate, identity, "modelport"
                        )
                    )

    def test_all_three_integrated_containers_accept_exact_executable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            write_reviewed_configuration(working_dir)
            for policy_name in integrated.MODELPORT_SECURITY_POLICIES:
                with self.subTest(policy=policy_name):
                    container = secure_container(policy_name)
                    identity = reviewed_identity(
                        container,
                        policy_name,
                        working_dir,
                    )
                    self.assertEqual(
                        integrated.container_executable_mismatches(
                            container, identity, policy_name
                        ),
                        [],
                    )

    def test_matching_bind_and_mount_sources_cannot_bypass_reviewed_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            write_reviewed_configuration(working_dir)
            baseline = secure_container("modelport")
            identity = reviewed_identity(baseline, "modelport", working_dir)
            candidate = copy.deepcopy(baseline)
            for mount in candidate["Mounts"]:
                if mount["Destination"] in {"/config/.env", "/config/config.toml"}:
                    mount["Source"] = "/etc/passwd"
            candidate["HostConfig"]["Binds"] = [
                (
                    f"/etc/passwd:{destination}:{mode}"
                    if destination in {"/config/.env", "/config/config.toml"}
                    else bind
                )
                for bind in candidate["HostConfig"]["Binds"]
                for _source, destination, mode in [bind.rsplit(":", 2)]
            ]
            mismatches = integrated.container_executable_mismatches(
                candidate, identity, "modelport"
            )
        self.assertIn(
            "compose.bindings./config/.env.reviewedSource", mismatches
        )
        self.assertIn(
            "compose.bindings./config/config.toml.reviewedSource", mismatches
        )

    def test_missing_bind_identity_cannot_disable_source_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            write_reviewed_configuration(working_dir)
            baseline = secure_container("modelport")
            identity = reviewed_identity(baseline, "modelport", working_dir)
            del identity["compose"]["bindings"]
            candidate = copy.deepcopy(baseline)
            for mount in candidate["Mounts"]:
                if mount["Destination"] in {"/config/.env", "/config/config.toml"}:
                    mount["Source"] = "/etc/passwd"
            candidate["HostConfig"]["Binds"] = [
                (
                    f"/etc/passwd:{destination}:{mode}"
                    if destination in {"/config/.env", "/config/config.toml"}
                    else bind
                )
                for bind in candidate["HostConfig"]["Binds"]
                for _source, destination, mode in [bind.rsplit(":", 2)]
            ]
            mismatches = integrated.container_executable_mismatches(
                candidate, identity, "modelport"
            )
        self.assertIn("compose.bindings", mismatches)

    def test_reviewed_configuration_content_drift_is_rejected(self) -> None:
        cases = {
            "docker-compose.yml": (
                "services:\n  modelport:\n    image: attacker:latest\n",
                "compose.files.docker-compose.yml.sha256",
            ),
            "config.toml": (
                "[server]\nmode = 'substituted'\n",
                "compose.files.config.toml.sha256",
            ),
            ".env secret": (
                "MODELPORT_MODE=reviewed\nMODELPORT_TOKEN=substituted-secret\n",
                "compose.files..env.runtimeAgreement",
            ),
            ".env public": (
                "MODELPORT_MODE=substituted\nMODELPORT_TOKEN=test-only-secret\n",
                "compose.files..env.publicSha256",
            ),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                working_dir = Path(directory)
                write_reviewed_configuration(working_dir)
                baseline = secure_container("modelport")
                identity = reviewed_identity(baseline, "modelport", working_dir)
                target = working_dir / (".env" if name.startswith(".env") else name)
                target.write_text(body, encoding="utf-8")
                if target.name == ".env":
                    target.chmod(0o600)
                mismatches = integrated.container_executable_mismatches(
                    baseline, identity, "modelport"
                )
                self.assertIn(expected, mismatches)

    def test_manifest_without_reviewed_current_identities_fails_closed(self) -> None:
        manifest = json.loads(
            (
                ROOT
                / "deployments"
                / "qwen3.5-9b-rtx5070ti"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["gateway"]["reviewedContainerIdentities"]["status"],
            "review-required",
        )
        self.assertIsNone(integrated.reviewed_container_identities(manifest))

    def test_reviewed_identity_schema_requires_every_role_binding_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            working_dir = Path(directory)
            write_reviewed_configuration(working_dir)
            identities = {}
            for policy_name in integrated.MODELPORT_SECURITY_POLICIES:
                container = secure_container(policy_name)
                identities[policy_name] = reviewed_identity(
                    container, policy_name, working_dir
                )
            manifest = {
                "gateway": {
                    "reviewedContainerIdentities": {
                        "schemaVersion": 1,
                        "status": "reviewed-current",
                        "containers": identities,
                    }
                }
            }
            self.assertIsNotNone(
                integrated.reviewed_container_identities(manifest)
            )
            del identities["modelport"]["compose"]["bindings"]
            self.assertIsNone(integrated.reviewed_container_identities(manifest))

    def test_runtime_envelope_delegates_to_exact_runtime_identity(self) -> None:
        manifest = {"profiles": {"default": "latency"}}
        with patch.object(
            integrated, "runtime_mismatches", return_value=["portBindings"]
        ) as verifier:
            self.assertEqual(
                integrated.runtime_security_mismatches({}, manifest),
                ["portBindings"],
            )
        verifier.assert_called_once_with({}, "latency")

    def test_runtime_envelope_rejects_unknown_profile_without_inspection(self) -> None:
        with patch.object(integrated, "runtime_mismatches") as verifier:
            self.assertEqual(
                integrated.runtime_security_mismatches(
                    {}, {"profiles": {"default": "custom"}}
                ),
                ["profile"],
            )
        verifier.assert_not_called()


class BackupFreshnessHardeningTests(unittest.TestCase):
    def make_archive(self, directory: Path, *, mtime: float) -> Path:
        directory.chmod(0o700)
        archive = directory / "modelport-20260809T000000Z.tar.gz"
        archive.write_bytes(b"backup")
        archive.chmod(0o600)
        os.utime(archive, (mtime, mtime))
        return archive

    def test_detailed_verifier_rejects_timestamp_beyond_future_skew(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            backup_dir = Path(directory)
            self.make_archive(
                backup_dir,
                mtime=now + integrated.BACKUP_FUTURE_SKEW_SECONDS + 5,
            )
            snapshot = integrated.backup_archive_snapshot(backup_dir, now=now)
        self.assertTrue(snapshot["available"])
        self.assertLess(snapshot["ageHours"], 0)
        self.assertTrue(snapshot["futureTimestamp"])
        self.assertTrue(snapshot["secure"])

    def test_detailed_verifier_rejects_hard_link_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            backup_dir = Path(directory)
            archive = self.make_archive(backup_dir, mtime=now - 60)
            os.link(archive, backup_dir / "modelport-hardlink.tar.gz")
            snapshot = integrated.backup_archive_snapshot(backup_dir, now=now)
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["secure"])

    def test_detailed_verifier_rejects_wrong_owner_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            backup_dir = Path(directory)
            self.make_archive(backup_dir, mtime=now - 60)
            with patch.object(integrated.os, "getuid", return_value=os.getuid() + 1):
                snapshot = integrated.backup_archive_snapshot(backup_dir, now=now)
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["secure"])

    def test_detailed_verifier_does_not_accept_symlink_as_regular_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            backup_dir.chmod(0o700)
            target = backup_dir / "outside.tar.gz"
            target.write_bytes(b"backup")
            (backup_dir / "modelport-linked.tar.gz").symlink_to(target)
            snapshot = integrated.backup_archive_snapshot(
                backup_dir, now=time.time()
            )
        self.assertFalse(snapshot["available"])

    def test_operations_snapshot_preserves_negative_age_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now_ms = int(time.time() * 1000)
            backup_dir = Path(directory)
            self.make_archive(
                backup_dir,
                mtime=(now_ms / 1000)
                + operations_report.BACKUP_FUTURE_SKEW_SECONDS
                + 5,
            )
            with patch.dict(os.environ, {"MODELPORT_BACKUP_DIR": directory}):
                snapshot = operations_report.backup_snapshot(now_ms)
        self.assertLess(snapshot["latestAgeHours"], 0)
        self.assertTrue(snapshot["futureTimestamp"])
        self.assertEqual(
            operations_report.backup_freshness_alert(snapshot, 36)["code"],
            "modelport_backup_future_timestamp",
        )

    def test_soak_gate_preserves_and_rejects_negative_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            backup_dir = Path(directory)
            self.make_archive(
                backup_dir,
                mtime=now + soak_check.BACKUP_FUTURE_SKEW_SECONDS + 5,
            )
            with patch.object(soak_check, "BACKUP_DIR", backup_dir):
                age_hours, secure = soak_check.latest_backup_age_hours()
        self.assertIsNotNone(age_hours)
        assert age_hours is not None
        self.assertLess(age_hours, 0)
        self.assertTrue(secure)
        self.assertFalse(soak_check.backup_age_is_acceptable(age_hours, 36))


if __name__ == "__main__":
    unittest.main()
