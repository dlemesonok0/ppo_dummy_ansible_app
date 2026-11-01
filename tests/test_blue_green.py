"""Exercise two HTTP backends, an observed route, and actual symlink cutovers."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from library.safe_release import BlueGreenManager, ReleaseError


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        slot = self.server.live_slot if self.server.frontend else self.server.slot
        state = self.server.states[slot]
        status = state["health"] if self.path in ("/health", "/healthz") else 200
        self.send_response(status)
        self.send_header("X-Deployment-Slot", slot)
        self.end_headers()
        self.wfile.write(json.dumps(dict(version=state["version"])).encode())

    def log_message(self, *args):
        pass


class BlueGreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.states = {}
        cls.servers = []
        for slot in ("blue", "green", "frontend"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.states = cls.states
            server.frontend = slot == "frontend"
            server.slot = slot
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.servers.append((server, thread))
        cls.router = cls.servers[2][0]

    @classmethod
    def tearDownClass(cls):
        for server, thread in cls.servers:
            server.shutdown()
            server.server_close()
            thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("v1", "v2", "broken"):
            (self.root / "releases" / name).mkdir(parents=True)
        for slot in ("blue", "green"):
            path = self.root / "slots" / slot
            path.mkdir(parents=True)
            (path / "release").symlink_to(self.root / "releases" / "v1")
            (path / "upstream.conf").write_text("prepared config")
        (self.root / "active").symlink_to(self.root / "slots" / "blue")
        self.states.clear()
        self.states.update(blue=dict(health=200, version="1.0"), green=dict(health=200, version="1.0"))
        self.router.live_slot = "blue"
        base = "http://127.0.0.1:%s" % self.router.server_port
        self.params = dict(root=str(self.root), release="v2", service="demo.service",
                           expected_version="2.0", health_url=base + "/health",
                           version_url=base + "/version", request_timeout=0.2,
                           health_timeout=0.2, poll_interval=0.02, restart_timeout=1,
                           blue_port=self.servers[0][0].server_port,
                           green_port=self.servers[1][0].server_port,
                           slot_service_prefix="demo", proxy_service="proxy.service")
        self.manager = BlueGreenManager(self.params, "/usr/bin/systemctl")
        self.manager.service_action = Mock(side_effect=self.action)

    def action(self, action, service):
        if action == "reload":
            self.router.live_slot = self.manager.active_slot()
        else:
            slot = service.split("@")[1].split(".")[0]
            if action == "stop":
                self.states[slot]["health"] = 503
            else:
                name = self.manager.slot_release(slot)
                self.states[slot] = dict(health=503 if name == "broken" else 200,
                                         version="1.0" if name == "v1" else "2.0")

    def test_switch_only_after_direct_health_and_keep_old_slot(self):
        result = self.manager.run()
        self.assertTrue(result["update_succeeded"])
        self.assertEqual(result["active_slot"], "green")
        self.assertEqual(result["routed_slot"], "green")
        self.assertEqual(result["version"], "2.0")
        self.assertTrue(result["candidate_health"]["ok"])
        self.assertEqual(self.states["blue"]["version"], "1.0")
        self.assertEqual(self.states["blue"]["health"], 200)
        self.assertEqual([call.args for call in self.manager.service_action.call_args_list],
                         [("restart", "demo@green.service"), ("reload", "proxy.service")])

    def test_repeat_is_noop_including_proxy_reload(self):
        self.manager.run()
        original_inode = (self.root / "active").lstat().st_ino
        self.manager.service_action.reset_mock()
        result = self.manager.run()
        self.assertFalse(result["changed"])
        self.assertEqual((self.root / "active").lstat().st_ino, original_inode)
        self.manager.service_action.assert_not_called()

    def test_check_mode_writes_nothing_and_starts_no_services(self):
        def snapshot():
            return {str(path): (path.lstat().st_ino, path.lstat().st_mtime_ns, path.lstat().st_ctime_ns)
                    for path in self.root.rglob("*")}
        before = snapshot()
        result = self.manager.run(check_mode=True)
        self.assertTrue(result["changed"])
        self.assertIsNone(result["update_succeeded"])
        self.assertEqual(result["active_slot"], "blue")
        self.assertEqual(before, snapshot())
        self.manager.service_action.assert_not_called()

    def test_check_mode_active_release_is_unchanged(self):
        self.params.update(release="v1", expected_version="1.0")
        self.assertFalse(self.manager.run(check_mode=True)["changed"])

    def test_broken_candidate_never_receives_traffic(self):
        self.params["release"] = "broken"
        before = (self.root / "active").lstat().st_ino
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertEqual(result["candidate_health"]["status"], 503)
        self.assertFalse(result["traffic_switch_attempted"])
        self.assertFalse(result["rollback"]["attempted"])
        self.assertTrue(result["candidate_stopped"])
        self.assertEqual(result["current_release"], "v1")
        self.assertEqual(result["routed_slot"], "blue")
        self.assertEqual((self.root / "active").lstat().st_ino, before)

    def test_bad_public_route_rolls_back_without_restarting_old_slot(self):
        def degrade_after_reload(action, service):
            self.action(action, service)
            if action == "reload" and self.router.live_slot == "green":
                self.states["green"]["health"] = 503
        self.manager.service_action.side_effect = degrade_after_reload
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertTrue(result["rollback"]["succeeded"])
        self.assertEqual(result["routed_slot"], "blue")
        self.assertEqual(result["current_release"], "v1")
        self.assertNotIn(("restart", "demo@blue.service"),
                         [call.args for call in self.manager.service_action.call_args_list])

    def test_reload_failure_recovers_old_configuration(self):
        def fail_cutover(action, service):
            if action == "reload" and self.manager.active_slot() == "green":
                raise ReleaseError("nginx validation failed")
            self.action(action, service)
        self.manager.service_action.side_effect = fail_cutover
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertTrue(result["rollback"]["succeeded"])
        self.assertEqual(self.router.live_slot, "blue")

    def test_failed_recovery_keeps_candidate_running_when_route_is_uncertain(self):
        def fail_all_reloads(action, service):
            self.action(action, service)
            if action == "reload":
                raise ReleaseError("reload command failed")
        self.manager.service_action.side_effect = fail_all_reloads
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertFalse(result["rollback"]["succeeded"])
        self.assertFalse(result["candidate_stopped"])
        self.assertIn("reload", result["rollback"]["error"])

    def test_actual_route_must_match_the_active_pointer(self):
        self.router.live_slot = "green"
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertFalse(result["changed"])
        self.manager.service_action.assert_not_called()

    def test_service_start_failure_does_not_touch_the_active_slot(self):
        def fail_start(action, service):
            if action == "restart":
                raise ReleaseError("candidate start failed")
            self.action(action, service)
        self.manager.service_action.side_effect = fail_start
        result = self.manager.run()
        self.assertTrue(result["failed"])
        self.assertFalse(result["traffic_switch_attempted"])
        self.assertEqual(result["routed_slot"], "blue")


if __name__ == "__main__":
    unittest.main()
