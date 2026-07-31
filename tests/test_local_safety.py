"""Tests for loopback HTTP, environment parsing, and manifest integrity."""

from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.env_utils import (
    atomic_write_private_text,
    is_private_regular_file,
    parse_env_file,
)
from scripts.local_http import is_loopback_host, validate_loopback_url


ROOT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_manifest", ROOT_DIR / "scripts" / "verify-manifest.py"
)
assert SPEC and SPEC.loader
VERIFY_MANIFEST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY_MANIFEST)
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "install_user_services", ROOT_DIR / "scripts" / "install-user-services.py"
)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)
SUPERVISOR_SPEC = importlib.util.spec_from_file_location(
    "runtime_supervisor", ROOT_DIR / "scripts" / "runtime-supervisor.py"
)
assert SUPERVISOR_SPEC and SUPERVISOR_SPEC.loader
SUPERVISOR = importlib.util.module_from_spec(SUPERVISOR_SPEC)
SUPERVISOR_SPEC.loader.exec_module(SUPERVISOR)


class LocalHttpTests(unittest.TestCase):
    def test_only_uncredentialed_loopback_urls_are_allowed(self) -> None:
        for url in (
            "http://127.0.0.1:18080/health",
            "http://[::1]:33004/api/health",
            "https://localhost:38082/readyz",
        ):
            self.assertEqual(validate_loopback_url(url), url)
        for url in (
            "http://example.com/",
            "http://127.0.0.1.example.com/",
            "http://user:password@127.0.0.1/",
            "file:///etc/passwd",
            "http://127.0.0.1/#secret",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_loopback_url(url)
        self.assertTrue(is_loopback_host("127.9.8.7"))
        self.assertFalse(is_loopback_host("0.0.0.0"))


class EnvironmentTests(unittest.TestCase):
    def test_parser_handles_shell_quotes_without_executing_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.env"
            path.write_text(
                "PLAIN=value\nQUOTED='a value # kept'\nexport DOUBLE=\"two words\"\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_env_file(path),
                {
                    "PLAIN": "value",
                    "QUOTED": "a value # kept",
                    "DOUBLE": "two words",
                },
            )
            path.write_text("BAD=$(id)\n", encoding="utf-8")
            self.assertEqual(parse_env_file(path), {"BAD": "$(id)"})

    def test_private_atomic_writer_sets_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private.env"
            atomic_write_private_text(target, "TOKEN=test\n")
            self.assertTrue(is_private_regular_file(target))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.stat().st_uid, os.getuid())


class ManifestTests(unittest.TestCase):
    def test_manifest_tracks_every_declared_repository_configuration_file(self) -> None:
        manifest = VERIFY_MANIFEST.json.loads(
            VERIFY_MANIFEST.MANIFEST_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            VERIFY_MANIFEST.verify(manifest["configuration"]),
            [],
        )

    def test_systemd_renderer_escapes_portable_checkout_paths(self) -> None:
        root = Path("/tmp/project with $ and %")
        escaped = INSTALLER.systemd_escape_path(root)
        self.assertEqual(escaped, "/tmp/project\\x20with\\x20$$\\x20and\\x20%%")
        body = INSTALLER.rendered_body("qwen-model-runtime.service", root)
        self.assertIn(f"WorkingDirectory={escaped}", body)
        self.assertIn(f"{escaped}/scripts/runtime-supervisor.py", body)

    def test_repository_python_pin_resolves_through_uv(self) -> None:
        executable = INSTALLER.pinned_python(ROOT_DIR)
        result = INSTALLER.subprocess.run(
            [str(executable), "-c", "import platform; print(platform.python_version())"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.stdout.strip(), "3.14.6")


class RuntimeSupervisorTests(unittest.TestCase):
    def supervisor(self, root: Path) -> object:
        return SUPERVISOR.RuntimeSupervisor(root_dir=root, sleeper=lambda _: None)

    def test_healthy_runtime_must_also_have_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(Path(directory))
            with (
                patch.object(supervisor, "docker_available", return_value=True),
                patch.object(supervisor, "runtime_healthy", return_value=True),
                patch.object(supervisor, "profile_is_canonical", return_value=False),
                patch.object(supervisor, "clear_alert") as clear_alert,
                patch.object(supervisor, "reconcile") as reconcile,
            ):
                self.assertIs(
                    supervisor.step(), SUPERVISOR.Step.PERMANENT_FAILURE
                )
                clear_alert.assert_not_called()
                reconcile.assert_not_called()

    def test_docker_wait_records_alert_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            supervisor = SUPERVISOR.RuntimeSupervisor(
                root_dir=Path(directory),
                clock=lambda: now[0],
                sleeper=lambda _: None,
            )
            with patch.object(supervisor, "_alert", return_value=True) as alert:
                supervisor.note_docker_wait()
                alert.assert_not_called()
                now[0] += supervisor.docker_alert_seconds
                supervisor.note_docker_wait()
                alert.assert_called_once_with("fire")

    def test_failed_reconcile_is_transient_only_if_docker_disappeared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self.supervisor(Path(directory))
            with (
                patch.object(
                    supervisor, "docker_available", side_effect=[True, False]
                ),
                patch.object(supervisor, "runtime_healthy", return_value=False),
                patch.object(supervisor, "container_health", return_value="unhealthy"),
                patch.object(supervisor, "reconcile", return_value=False),
                patch.object(supervisor, "note_docker_wait") as note_wait,
            ):
                self.assertIs(supervisor.step(), SUPERVISOR.Step.WAIT_DOCKER)
                note_wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
