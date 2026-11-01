"""Real symlinks and HTTP probes; only the systemd boundary is substituted."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from library.safe_release import ReleaseError, ReleaseManager, release_lock


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = self.server.state
        status = state.get("health", 200) if self.path == "/health" else 200
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        body = json.dumps({"version": state.get("version", "1.0")}).encode()
        self.send_response(status)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # A deliberately short health deadline can close the socket.

    def log_message(self, *args):
        pass


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("v1", "v2", "broken"):
            (self.root / "releases" / name).mkdir(parents=True)
        (self.root / "current").symlink_to("releases/v1")
        base = "http://127.0.0.1:%s" % self.server.server_port
        self.params = dict(root=str(self.root), release="v2", service="demo.service",
                           health_url=base + "/health", version_url=base + "/version",
                           expected_version="2.0", health_timeout=0.2,
                           poll_interval=0.02, request_timeout=0.2, restart_timeout=1)
        self.server.state = dict(health=200, version="1.0")
        self.manager = ReleaseManager(self.params, "/usr/bin/systemctl")
        self.manager.restart = Mock(side_effect=self.restart)

    def restart(self):
        name = (self.root / "current").resolve().name
        self.server.state = dict(health=503 if name == "broken" else 200,
                                 version="1.0" if name == "v1" else "2.0")

    def test_success_reports_actual_target_and_runtime_version(self):
        result = self.manager.run()
        self.assertTrue(result["changed"])
        self.assertTrue(result["update_succeeded"])
        self.assertEqual(result["previous_version"], "1.0")
        self.assertEqual(result["version"], "2.0")
        self.assertEqual((self.root / "current").resolve().name, "v2")
        self.manager.restart.assert_called_once()

    def test_repeat_does_not_replace_link_or_restart(self):
        self.manager.run()
        old_inode = (self.root / "current").lstat().st_ino
        self.manager.restart.reset_mock()
        result = self.manager.run()
        self.assertFalse(result["changed"])
        self.assertTrue(result["update_succeeded"])
        self.assertEqual((self.root / "current").lstat().st_ino, old_inode)
        self.manager.restart.assert_not_called()

    def test_check_predicts_change_without_writing_or_restarting(self):
        before = (self.root / "current").lstat()
        with patch.object(self.manager, "switch", side_effect=AssertionError("check wrote")):
            result = self.manager.run(check_mode=True)
        self.assertTrue(result["changed"])
        self.assertIsNone(result["update_succeeded"])
        self.assertEqual(result["current_release"], "v1")
        after = (self.root / "current").lstat()
        self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_ctime_ns),
                         (before.st_ino, before.st_mtime_ns, before.st_ctime_ns))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["current", "releases"])
        self.manager.restart.assert_not_called()

    def test_check_of_healthy_active_target_predicts_no_change(self):
        self.params.update(release="v1", expected_version="1.0")
        self.assertFalse(self.manager.run(check_mode=True)["changed"])
        self.manager.restart.assert_not_called()

    def test_active_but_unhealthy_target_is_restarted(self):
        self.params.update(release="v1", expected_version="1.0")
        self.server.state["health"] = 503
        result = self.manager.run()
        self.assertTrue(result["changed"])
        self.assertTrue(result["update_succeeded"])
        self.manager.restart.assert_called_once()

    def test_broken_target_restores_previous_but_still_fails_task(self):
        self.params["release"] = "broken"
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["update_succeeded"])
        self.assertEqual(result["health_after"]["status"], 503)
        self.assertTrue(result["rollback"]["succeeded"])
        self.assertEqual(result["rollback"]["health"]["status"], 200)
        self.assertEqual(result["current_release"], "v1")
        self.assertEqual(result["version"], "1.0")
        self.assertEqual(self.manager.restart.call_count, 2)

    def test_recovery_health_failure_is_distinct_from_update_failure(self):
        self.params["release"] = "broken"
        def always_unhealthy():
            self.restart()
            self.server.state["health"] = 503
        self.manager.restart.side_effect = always_unhealthy
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertFalse(result["rollback"]["succeeded"])
        self.assertTrue(result["rollback"]["restored_symlink"])
        self.assertEqual(result["rollback"]["health"]["status"], 503)
        self.assertIn("Recovery health", result["rollback"]["error"])
        self.assertEqual(result["current_release"], "v1")

    def test_restart_error_triggers_recovery_and_preserves_original_error(self):
        def fail_first_restart():
            if self.manager.restart.call_count == 1:
                raise ReleaseError("first restart failed")
            self.restart()
        self.manager.restart.side_effect = fail_first_restart
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertEqual(result["update_error"], "first restart failed")
        self.assertTrue(result["rollback"]["succeeded"])

    def test_recovery_restart_failure_does_not_claim_runtime_version(self):
        self.manager.restart.side_effect = [ReleaseError("update restart"), ReleaseError("rollback restart")]
        result = self.manager.run()
        self.assertFalse(result["rollback"]["succeeded"])
        self.assertTrue(result["rollback"]["restored_symlink"])
        self.assertIn("rollback restart", result["rollback"]["error"])
        self.assertIsNone(result["version"])

    def test_symlink_recovery_failure_reports_actual_selected_release(self):
        self.params["release"] = "broken"
        real_switch = self.manager.switch
        def switch(name):
            if name == "v1":
                raise OSError("read-only directory")
            real_switch(name)
        self.manager.switch = switch
        result = self.manager.run()
        self.assertFalse(result["rollback"]["restored_symlink"])
        self.assertFalse(result["rollback"]["succeeded"])
        self.assertEqual(result["current_release"], "broken")

    def test_failed_atomic_replace_leaves_original_and_no_temporary_link(self):
        with patch("library.safe_release.os.replace", side_effect=PermissionError("denied")):
            result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertFalse(result["changed"])
        self.assertFalse(result["rollback"]["attempted"])
        self.assertEqual((self.root / "current").resolve().name, "v1")
        self.assertFalse(list(self.root.glob(".current.*")))
        self.manager.restart.assert_not_called()

    def test_missing_and_traversal_release_fail_before_mutation(self):
        for name in ("missing", "../v2", "/tmp/other"):
            with self.subTest(name=name):
                self.params["release"] = name
                with self.assertRaises(ReleaseError):
                    self.manager.run()
        self.manager.restart.assert_not_called()

    def test_release_symlink_escape_is_rejected(self):
        (self.root / "releases" / "escape").symlink_to(self.root)
        self.params["release"] = "escape"
        with self.assertRaises(ReleaseError):
            self.manager.run()

    def test_current_outside_releases_is_rejected(self):
        (self.root / "current").unlink()
        external = self.root / "other" / "v1"
        external.mkdir(parents=True)
        (self.root / "current").symlink_to(external)
        with self.assertRaises(ReleaseError):
            self.manager.run()

    def test_runtime_version_mismatch_fails_despite_http_200(self):
        self.params["expected_version"] = "wrong-version"
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertEqual(result["health_after"]["status"], 200)
        self.assertIn("Expected version", result["update_error"])
        self.assertTrue(result["rollback"]["succeeded"])

    def test_wait_preserves_version_diagnosis_when_deadline_expires_between_requests(self):
        version_error = dict(ok=False, status=200, version="1.0", error="Expected version 2.0, got 1.0")
        deadline_error = dict(ok=False, status=None, version=None, error="Health deadline exceeded")
        with patch.object(self.manager, "probe", side_effect=[version_error, deadline_error]), \
                patch("library.safe_release.time.monotonic", return_value=0), \
                patch("library.safe_release.time.sleep"):
            result = self.manager.wait_healthy("2.0")
        self.assertEqual(result, version_error)

    def test_redirect_is_not_accepted_as_healthy(self):
        self.params["health_url"] = self.params["health_url"].replace("/health", "/redirect")
        probe = self.manager.probe()
        self.assertFalse(probe["ok"])
        self.assertEqual(probe["status"], 302)

    def test_http_ignores_proxy_and_never_reads_netrc(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}), \
                patch("ansible.module_utils.urls.netrc.netrc", side_effect=AssertionError("netrc was read")):
            self.assertTrue(self.manager.probe()["ok"])

    def test_ansible_http_errors_become_health_diagnostics(self):
        from ansible.module_utils.urls import SSLValidationError
        with patch.object(self.manager.http, "open", side_effect=SSLValidationError("certificate failed")):
            result = self.manager.probe()
        self.assertFalse(result["ok"])
        self.assertIn("certificate failed", result["error"])

    def test_zero_negative_and_nonfinite_timeouts_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            self.params["health_timeout"] = value
            with self.assertRaises(ReleaseError):
                self.manager.run()

    def test_directory_lock_rejects_concurrent_writer_without_files(self):
        with release_lock(self.root):
            with self.assertRaises(ReleaseError):
                with release_lock(self.root):
                    self.fail("Acquired a conflicting lock")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["current", "releases"])

    def test_systemctl_nonzero_and_timeout_are_clear_errors(self):
        for effect in (subprocess.TimeoutExpired("systemctl", 1),
                       subprocess.CompletedProcess([], 1, "", "Unit demo.service failed")):
            with patch("library.safe_release.subprocess.run") as runner:
                if isinstance(effect, Exception):
                    runner.side_effect = effect
                else:
                    runner.return_value = effect
                with self.assertRaisesRegex(ReleaseError, "restart failed"):
                    ReleaseManager.restart(self.manager)
                self.assertEqual(runner.call_args.args[0], ["/usr/bin/systemctl", "restart", "demo.service"])


if __name__ == "__main__":
    unittest.main()
