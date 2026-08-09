from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from local_inference_stack import materials
from scripts import runtime_identity


class MaterialInventoryTests(unittest.TestCase):
    def _tree(self, root: Path) -> None:
        (root / "config").mkdir()
        (root / "package").mkdir()
        (root / "config" / "alpha.json").write_text(
            '{"alpha":1}\n', encoding="utf-8"
        )
        (root / "config" / "beta.json").write_text(
            '{"beta":2}\n', encoding="utf-8"
        )
        (root / "package" / "zeta.py").write_text("ZETA = 1\n", encoding="utf-8")
        (root / "package" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")

    def test_inventory_and_file_set_digest_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            material_set = materials.MaterialSet(
                key="packageSha256",
                policy_id=materials.FILE_SET_SHA256_POLICY_ID,
                includes=("package/*.py",),
            )
            first = materials.SnapshotSpec.from_mapping(
                policy_id="local-inference-stack/test-materials-v1",
                files={
                    "betaSha256": "config/beta.json",
                    "alphaSha256": "config/alpha.json",
                },
                material_sets=(material_set,),
            )
            second = materials.SnapshotSpec.from_mapping(
                policy_id="local-inference-stack/test-materials-v1",
                files={
                    "alphaSha256": "config/alpha.json",
                    "betaSha256": "config/beta.json",
                },
                material_sets=(material_set,),
            )

            self.assertEqual(first.inventory(root), second.inventory(root))
            self.assertEqual(
                first.snapshot(
                    root,
                    expected_policy_id="local-inference-stack/test-materials-v1",
                ),
                second.snapshot(
                    root,
                    expected_policy_id="local-inference-stack/test-materials-v1",
                ),
            )
            entries = [
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in sorted((root / "package").glob("*.py"))
            ]
            legacy_digest = hashlib.sha256(
                json.dumps(
                    entries, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                first.snapshot(
                    root,
                    expected_policy_id="local-inference-stack/test-materials-v1",
                )["packageSha256"],
                legacy_digest,
            )

            snapshot = first.snapshot(
                root,
                expected_policy_id="local-inference-stack/test-materials-v1",
            )
            self.assertNotIn("policyId", snapshot)

    def test_file_set_v1_preserves_legacy_unicode_wire_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "模型.py"
            material.write_text("stable\n", encoding="utf-8")
            entries = [
                {
                    "path": "模型.py",
                    "sha256": hashlib.sha256(b"stable\n").hexdigest(),
                }
            ]
            legacy_body = json.dumps(
                entries,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                materials.sha256_file_set((material,), root=root),
                hashlib.sha256(legacy_body).hexdigest(),
            )

    def test_inventory_rejects_path_coverage_gaps_and_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            spec = materials.SnapshotSpec.from_mapping(
                policy_id="local-inference-stack/test-materials-v1",
                files={"alphaSha256": "config/alpha.json"},
                material_sets=(
                    materials.MaterialSet(
                        key="packageSha256",
                        policy_id=materials.FILE_SET_SHA256_POLICY_ID,
                        includes=("package/*.py",),
                    ),
                ),
            )
            spec.require_paths(
                root,
                (
                    "config/alpha.json",
                    "package/alpha.py",
                    "package/zeta.py",
                ),
            )
            with self.assertRaises(materials.MaterialCoverageError):
                spec.require_paths(root, ("config/beta.json",))
            with self.assertRaises(ValueError):
                materials.SnapshotSpec.from_mapping(
                    policy_id="local-inference-stack/test-materials-v1",
                    files={"escapeSha256": "../escape"},
                )
            empty = materials.MaterialSet(
                key="emptySha256",
                policy_id=materials.FILE_SET_SHA256_POLICY_ID,
                includes=("missing/*.py",),
            )
            with self.assertRaises(materials.MaterialCoverageError):
                empty.paths(root)

    def test_snapshot_and_file_set_policy_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._tree(root)
            drifted_snapshot = materials.SnapshotSpec.from_mapping(
                policy_id="local-inference-stack/test-materials-v2",
                files={"alphaSha256": "config/alpha.json"},
            )
            with self.assertRaises(materials.MaterialPolicyDrift):
                drifted_snapshot.snapshot(
                    root,
                    expected_policy_id="local-inference-stack/test-materials-v1",
                )

            drifted_set = materials.MaterialSet(
                key="packageSha256",
                policy_id="local-inference-stack/other-file-set-v1",
                includes=("package/*.py",),
            )
            spec = materials.SnapshotSpec.from_mapping(
                policy_id="local-inference-stack/test-materials-v1",
                files={},
                material_sets=(drifted_set,),
            )
            with self.assertRaises(materials.MaterialPolicyDrift):
                spec.snapshot(
                    root,
                    expected_policy_id="local-inference-stack/test-materials-v1",
                )

    def test_material_hash_rejects_symlink_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("trusted\n", encoding="utf-8")
            linked = root / "linked.txt"
            linked.symlink_to(source)
            with self.assertRaises(materials.MaterialError):
                materials.sha256_file(linked)
            source.chmod(0o666)
            with self.assertRaises(materials.MaterialError):
                materials.sha256_file(source)
            source.chmod(0o600)
            hard_link = root / "hard-link.txt"
            os.link(source, hard_link)
            with self.assertRaises(materials.MaterialError):
                materials.sha256_file(source)


class ArtifactIdentityTests(unittest.TestCase):
    def _artifact(self, root: Path) -> tuple[Path, Path, str]:
        model = root / "models" / "tiny.gguf"
        stamp = root / "cache" / "integrity" / "tiny.sha256.stamp"
        model.parent.mkdir(parents=True)
        stamp.parent.mkdir(parents=True)
        body = b"small non-model fixture"
        model.write_bytes(body)
        model.chmod(0o600)
        digest = hashlib.sha256(body).hexdigest()
        fingerprint = runtime_identity.artifact_stat_fingerprint(model.stat())
        stamp.write_text(f"{digest}|{fingerprint}\n", encoding="utf-8")
        stamp.chmod(0o600)
        return model, stamp, digest

    def test_secure_stat_and_stamp_are_bound_without_rehashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, digest = self._artifact(root)
            identity = runtime_identity.local_artifact_identity(
                model,
                stamp,
                expected_bytes=model.stat().st_size,
                expected_sha256=digest,
                project_root=root,
            )
            self.assertEqual(identity["verification"], "secure-stat-and-integrity-stamp")
            self.assertFalse(identity["requiresFullSha256Verification"])
            self.assertEqual(identity["path"], "models/tiny.gguf")
            self.assertEqual(identity["fingerprint"], runtime_identity.artifact_stat_fingerprint(model.stat()))

    def test_artifact_stat_drift_requires_the_full_sha256_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, digest = self._artifact(root)
            metadata = model.stat()
            os.utime(
                model,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
            )
            with self.assertRaisesRegex(RuntimeError, "full SHA256"):
                runtime_identity.local_artifact_identity(
                    model,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )

    def test_stale_stamp_and_insecure_artifact_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, digest = self._artifact(root)
            stamp.write_text(f"{digest}|stale-stat-fingerprint\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "full SHA256"):
                runtime_identity.local_artifact_identity(
                    model,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )

    def test_model_parent_symlink_cannot_escape_the_project_root(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(project_directory)
            outside = Path(outside_directory)
            model = outside / "tiny.gguf"
            model.write_bytes(b"outside model fixture")
            model.chmod(0o600)
            (root / "models").symlink_to(outside, target_is_directory=True)
            stamp = root / "cache" / "integrity" / "tiny.sha256.stamp"
            stamp.parent.mkdir(parents=True)
            stamp.parent.chmod(0o700)
            digest = hashlib.sha256(model.read_bytes()).hexdigest()
            stamp.write_text(
                f"{digest}|{runtime_identity.artifact_stat_fingerprint(model.stat())}\n",
                encoding="utf-8",
            )
            stamp.chmod(0o600)

            with self.assertRaisesRegex(RuntimeError, "safely open|outside"):
                runtime_identity.local_artifact_identity(
                    root / "models" / model.name,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )

    def test_integrity_stamp_parent_symlink_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as project_directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            root = Path(project_directory)
            model = root / "models" / "tiny.gguf"
            model.parent.mkdir()
            model.write_bytes(b"inside model fixture")
            model.chmod(0o600)
            digest = hashlib.sha256(model.read_bytes()).hexdigest()

            outside = Path(outside_directory)
            outside_stamp = outside / "tiny.sha256.stamp"
            outside_stamp.write_text(
                f"{digest}|{runtime_identity.artifact_stat_fingerprint(model.stat())}\n",
                encoding="utf-8",
            )
            outside_stamp.chmod(0o600)
            (root / "cache").mkdir()
            (root / "cache" / "integrity").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(RuntimeError, "safely open|outside"):
                runtime_identity.local_artifact_identity(
                    model,
                    root / "cache" / "integrity" / outside_stamp.name,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )

    def test_wrong_owner_hard_links_and_non_private_modes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model, stamp, digest = self._artifact(root)
            current_uid = os.getuid()
            with patch.object(runtime_identity.os, "getuid", return_value=current_uid + 1):
                with self.assertRaisesRegex(RuntimeError, "private current-user"):
                    runtime_identity.local_artifact_identity(
                        model,
                        stamp,
                        expected_bytes=model.stat().st_size,
                        expected_sha256=digest,
                        project_root=root,
                    )

            model_hard_link = model.with_name("tiny-copy.gguf")
            os.link(model, model_hard_link)
            with self.assertRaisesRegex(RuntimeError, "private current-user"):
                runtime_identity.local_artifact_identity(
                    model,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )

            model_hard_link.unlink()
            stamp_hard_link = stamp.with_name("tiny-copy.sha256.stamp")
            os.link(stamp, stamp_hard_link)
            with self.assertRaisesRegex(RuntimeError, "private current-user"):
                runtime_identity.local_artifact_identity(
                    model,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )
            stamp_hard_link.unlink()
            model.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "private current-user"):
                runtime_identity.local_artifact_identity(
                    model,
                    stamp,
                    expected_bytes=model.stat().st_size,
                    expected_sha256=digest,
                    project_root=root,
                )


class RuntimeAllowlistTests(unittest.TestCase):
    def _container(self) -> dict:
        return {
            "Image": "sha256:" + "a" * 64,
            "Config": {
                "Image": "example/runtime@sha256:" + "b" * 64,
                "User": "65532:65532",
                "Cmd": ["--model", "/models/tiny.gguf"],
                "Env": ["HOME=/tmp", "CUDA_CACHE_PATH=/cache", "IGNORED=value"],
            },
            "HostConfig": {
                "Privileged": False,
                "ReadonlyRootfs": True,
                "CapAdd": [],
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 512,
                "Init": True,
                "ShmSize": 1073741824,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=65536k"},
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                "LogConfig": {"Type": "json-file", "Config": {"max-file": "3", "max-size": "10m"}},
                "PublishAllPorts": False,
                "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
                "NetworkMode": "modelport_default",
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": -1,
                        "DeviceIDs": [],
                        "Capabilities": [["gpu"]],
                        "Options": {},
                    }
                ],
            },
            "NetworkSettings": {
                "Networks": {
                    "modelport_default": {"Aliases": ["qwen35", "tiny-container"]}
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/models",
                    "Destination": "/models",
                    "RW": False,
                },
                {
                    "Type": "bind",
                    "Source": "/srv/cache",
                    "Destination": "/cache",
                    "RW": True,
                },
            ],
        }

    def test_every_security_relevant_field_is_exact_and_extras_are_rejected(self) -> None:
        baseline = self._container()
        normalized = runtime_identity.normalized_live_runtime(baseline)
        expected = dict(normalized)
        mutations = {
            "privileged": lambda value: value["HostConfig"].__setitem__("Privileged", True),
            "user": lambda value: value["Config"].__setitem__("User", "0:0"),
            "readOnly": lambda value: value["HostConfig"].__setitem__("ReadonlyRootfs", False),
            "securityOpt": lambda value: value["HostConfig"].__setitem__("SecurityOpt", []),
            "capAdd": lambda value: value["HostConfig"].__setitem__("CapAdd", ["SYS_ADMIN"]),
            "mounts": lambda value: value["Mounts"].append(
                {"Type": "bind", "Source": "/", "Destination": "/host", "RW": True}
            ),
            "networks": lambda value: value["NetworkSettings"]["Networks"].__setitem__(
                "unexpected", {"Aliases": ["side-channel"]}
            ),
            "portBindings": lambda value: value["HostConfig"]["PortBindings"].__setitem__(
                "9090/tcp", [{"HostIp": "0.0.0.0", "HostPort": "9090"}]
            ),
            "deviceRequests": lambda value: value["HostConfig"]["DeviceRequests"][0].__setitem__("Count", 1),
            "tmpfs": lambda value: value["HostConfig"]["Tmpfs"].__setitem__("/run", "rw"),
            "pidsLimit": lambda value: value["HostConfig"].__setitem__("PidsLimit", -1),
            "init": lambda value: value["HostConfig"].__setitem__("Init", False),
            "restart": lambda value: value["HostConfig"].__setitem__(
                "RestartPolicy", {"Name": "always", "MaximumRetryCount": 0}
            ),
            "logging": lambda value: value["HostConfig"]["LogConfig"]["Config"].__setitem__("max-size", "1g"),
            "command": lambda value: value["Config"]["Cmd"].append("--unsafe-option"),
            "publishAllPorts": lambda value: value["HostConfig"].__setitem__("PublishAllPorts", True),
            "entrypoint": lambda value: value["Config"].__setitem__(
                "Entrypoint", ["/cache/evil-entrypoint"]
            ),
            "environmentSha256": lambda value: value["Config"]["Env"].append(
                "LD_PRELOAD=/cache/evil.so"
            ),
            "pidMode": lambda value: value["HostConfig"].__setitem__(
                "PidMode", "host"
            ),
            "ipcMode": lambda value: value["HostConfig"].__setitem__(
                "IpcMode", "host"
            ),
            "devices": lambda value: value["HostConfig"].__setitem__(
                "Devices",
                [
                    {
                        "PathOnHost": "/dev/sda",
                        "PathInContainer": "/dev/sda",
                        "CgroupPermissions": "rwm",
                    }
                ],
            ),
            "workingDirectory": lambda value: value["Config"].__setitem__(
                "WorkingDir", "/host"
            ),
            "healthcheck": lambda value: value["Config"].__setitem__(
                "Healthcheck", {"Test": ["NONE"]}
            ),
        }
        with patch.object(runtime_identity, "expected_runtime_subset", return_value=expected):
            self.assertEqual(runtime_identity.runtime_mismatches(baseline), [])
            for mismatch, mutate in mutations.items():
                with self.subTest(mismatch=mismatch):
                    changed = copy.deepcopy(baseline)
                    mutate(changed)
                    self.assertIn(mismatch, runtime_identity.runtime_mismatches(changed))

            changed = copy.deepcopy(baseline)
            changed["Mounts"][0]["Propagation"] = "rshared"
            self.assertIn("mounts", runtime_identity.runtime_mismatches(changed))


if __name__ == "__main__":
    unittest.main()
