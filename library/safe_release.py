#!/usr/bin/python
"""Switch an installed release and recover the previous one on failure."""

import contextlib
import fcntl
from http.client import HTTPException
import json
import math
import os
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
import uuid

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import ConnectionError as AnsibleConnectionError, MissingModuleError, Request

DOCUMENTATION = r'''
---
module: safe_release
short_description: Activate a prepared release with health checks and rollback
description:
  - Atomically replaces the current symlink and restarts a systemd service.
  - A failed update always fails the task, even when recovery succeeds.
  - In-place mode requires an existing current symlink to a direct child of releases.
  - Does not download images or edit release contents.
  - Blue-green mode starts and checks an inactive slot before gracefully reloading nginx.
options:
  strategy:
    description: In-place restart or two-slot nginx cutover.
    type: str
    choices: [in_place, blue_green]
    default: in_place
  slot_service_prefix:
    description: Blue-green template unit prefix, followed by at-sign and slot name.
    type: str
    default: ansiballz-bg
  proxy_service:
    description: Blue-green frontend service with a validating ExecReload.
    type: str
    default: ansiballz-bg-web.service
  blue_port:
    description: Direct loopback HTTP port of the blue slot.
    type: int
    default: 19901
  green_port:
    description: Direct loopback HTTP port of the green slot.
    type: int
    default: 19902
  root:
    description: Application directory containing releases and strategy state (current or slots/active).
    type: path
    default: /opt/ansiballz-demo
  release:
    description: Prepared directory name under releases.
    type: str
    required: true
  service:
    description: Systemd service to restart in in-place mode.
    type: str
    default: ansiballz-demo.service
  health_url:
    description: URL that must return HTTP 200 without redirects.
    type: str
    default: http://127.0.0.1:8080/health
  version_url:
    description: URL returning a JSON object with a string version.
    type: str
    default: http://127.0.0.1:8080/version
  expected_version:
    description: Optional runtime version required for the requested release.
    type: str
  health_timeout:
    description: Seconds to wait for health, independently for update and rollback.
    type: float
    default: 15
  request_timeout:
    description: Maximum seconds for one HTTP request.
    type: float
    default: 2
  poll_interval:
    description: Seconds between health attempts.
    type: float
    default: 0.5
  restart_timeout:
    description: Maximum seconds for each systemctl operation (restart, reload or stop).
    type: float
    default: 40
attributes:
  check_mode:
    support: full
requirements:
  - Linux with systemd and Python 3
author:
  - Dmitry Lemeshonok
'''

EXAMPLES = r'''
- name: Upgrade Podinfo to v2
  become: true
  safe_release:
    release: v2
    expected_version: '6.15.0'
'''

RETURN = r'''
active_slot:
  description: Slot selected on disk; may differ from the live route after a recovery error.
  type: str
  returned: blue-green mode
routed_slot:
  description: Slot observed through the frontend header, or null when it cannot be confirmed.
  type: str
  returned: blue-green mode
candidate_health:
  description: Direct health and version check before a blue-green traffic switch.
  type: dict
  returned: blue-green mode
previous_release:
  description: Release selected before the task.
  type: str
  returned: always
current_release:
  description: Release actually selected at return time, including check mode.
  type: str
  returned: always
requested_release:
  description: Requested target directory name.
  type: str
  returned: always
update_succeeded:
  description: Whether the target is healthy; null in check mode.
  type: bool
  returned: always
rollback:
  description: Independent recovery status, symlink status and health diagnostics.
  type: dict
  returned: always
health_after:
  description: Last target health observation; does not get replaced by rollback health.
  type: dict
  returned: after a health check of the updated service
'''


class ReleaseError(Exception):
    pass


@contextlib.contextmanager
def release_lock(root):
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReleaseError("Another safe_release operation holds the application lock") from exc
        yield
    finally:
        os.close(descriptor)


class ReleaseManager:
    def __init__(self, params, systemctl):
        self.params = params
        self.root = os.path.realpath(params["root"])
        self.releases = os.path.join(self.root, "releases")
        self.current = os.path.join(self.root, "current")
        self.systemctl = systemctl
        self.http = Request(use_proxy=False, use_netrc=False, follow_redirects="none",
                            http_agent="safe_release/1.0", timeout=params["request_timeout"])

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.params["release"]):
            raise ReleaseError("release must be a single directory name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service", self.params["service"]):
            raise ReleaseError("service must be a systemd .service name")
        for name in ("health_timeout", "request_timeout", "poll_interval", "restart_timeout"):
            value = self.params[name]
            if not math.isfinite(value) or value <= 0:
                raise ReleaseError("%s must be finite and greater than zero" % name)
        for name in ("health_url", "version_url"):
            parsed = urlsplit(self.params[name])
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ReleaseError("%s must be an HTTP or HTTPS URL" % name)
        if os.path.islink(self.releases) or not os.path.isdir(self.releases):
            raise ReleaseError("releases must be an existing directory, not a symlink")
        self.release_path(self.params["release"])
        self.current_name()

    def release_path(self, name):
        path = os.path.join(self.releases, name)
        if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) != self.releases or not os.path.isdir(path):
            raise ReleaseError("Release is missing or is not a direct directory under releases: %s" % name)
        return path

    def current_name(self):
        if not os.path.islink(self.current):
            raise ReleaseError("current must be an existing release symlink")
        path = os.path.realpath(self.current)
        name = os.path.basename(path)
        if path != self.release_path(name):
            raise ReleaseError("current points outside the prepared releases directory")
        return name

    def switch(self, name):
        temporary = os.path.join(self.root, ".current.%s" % uuid.uuid4().hex)
        try:
            os.symlink(self.release_path(name), temporary)
            os.replace(temporary, self.current)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def restart(self):
        self.service_action("restart", self.params["service"])

    def service_action(self, action, service):
        try:
            command = subprocess.run(
                [self.systemctl, action, service],
                capture_output=True, text=True, timeout=self.params["restart_timeout"],
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError("Service %s failed for %s: %s" % (action, service, exc)) from exc
        if command.returncode:
            raise ReleaseError("Service %s failed for %s (rc=%s): %s" % (
                action, service, command.returncode, (command.stderr or command.stdout)[-2000:].strip()))

    def read_url(self, url, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Health deadline exceeded")
        with self.http.open("GET", url, timeout=min(self.params["request_timeout"], remaining)) as response:
            self.response_slot = response.headers.get("X-Deployment-Slot")
            if response.status != 200:
                raise ReleaseError("Expected HTTP 200, got %s" % response.status)
            return response.read(65537)

    def probe(self, expected_version=None, deadline=None):
        observation = dict(ok=False, status=None, version=None, error=None)
        deadline = deadline or (time.monotonic() + 2 * self.params["request_timeout"])
        try:
            self.read_url(self.params["health_url"], deadline)
            observation["status"] = 200
            body = self.read_url(self.params["version_url"], deadline)
            if len(body) > 65536:
                raise ReleaseError("Version response is too large")
            version = json.loads(body)["version"]
            if not isinstance(version, str) or not version:
                raise ReleaseError("Version response must contain a nonempty string version")
            observation["version"] = version
            if expected_version is not None and version != expected_version:
                raise ReleaseError("Expected version %s, got %s" % (expected_version, version))
            observation["ok"] = True
        except HTTPError as exc:
            observation["status"] = exc.code
            observation["error"] = "HTTP %s from %s" % (exc.code, exc.url)
        except (OSError, ValueError, KeyError, TypeError, HTTPException, URLError,
                AnsibleConnectionError, MissingModuleError, ReleaseError) as exc:
            observation["error"] = str(exc)
        return observation

    def wait_healthy(self, expected_version):
        deadline = time.monotonic() + self.params["health_timeout"]
        observation = self.probe(expected_version, deadline)
        while not observation["ok"] and time.monotonic() < deadline:
            time.sleep(min(self.params["poll_interval"], max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            next_observation = self.probe(expected_version, deadline)
            if next_observation["error"] == "Health deadline exceeded":
                break
            observation = next_observation
        return observation

    def run(self, check_mode=False):
        self.validate()
        previous = self.current_name()
        requested = self.params["release"]
        before = self.probe()
        same_healthy = (previous == requested and before["ok"] and
                        (self.params["expected_version"] is None or before["version"] == self.params["expected_version"]))
        result = dict(
            changed=False, check_mode=check_mode, previous_release=previous,
            requested_release=requested, current_release=previous,
            previous_version=before["version"], version=before["version"],
            health_before=before, health_after=None, update_succeeded=None,
            restart_attempted=False,
            rollback=dict(attempted=False, succeeded=None, restored_symlink=False, error=None, health=None),
        )
        if check_mode:
            result.update(changed=not same_healthy, msg="Would keep the healthy release" if same_healthy else "Would activate/restart and check the requested release; health is not predicted")
            return result
        if same_healthy:
            result.update(update_succeeded=True, health_after=before, msg="Requested release is already active and healthy")
            return result
        try:
            if previous != requested:
                self.switch(requested)
                result.update(changed=True, current_release=requested)
            result.update(changed=True, restart_attempted=True)
            self.restart()
            result["health_after"] = self.wait_healthy(self.params["expected_version"])
            if not result["health_after"]["ok"]:
                raise ReleaseError("Target health check failed: %s" % result["health_after"]["error"])
            result.update(update_succeeded=True, version=result["health_after"]["version"], msg="Requested release activated and healthy")
            return result
        except (OSError, ReleaseError) as exc:
            result.update(failed=True, update_succeeded=False, update_error=str(exc))
        if not result["changed"]:
            result["msg"] = "Update failed before any changes: %s" % result["update_error"]
            return result
        recovery = result["rollback"]
        recovery["attempted"] = True
        try:
            self.switch(previous)
            recovery["restored_symlink"] = True
            self.restart()
            recovery["health"] = self.wait_healthy(before["version"] if before["ok"] else None)
            recovery["succeeded"] = recovery["health"]["ok"]
            if not recovery["succeeded"]:
                raise ReleaseError("Recovery health check failed: %s" % recovery["health"]["error"])
        except (OSError, ReleaseError) as exc:
            recovery.update(succeeded=False, error=str(exc))
        try:
            result["current_release"] = self.current_name()
        except (OSError, ReleaseError):
            result["current_release"] = None
        # A selected symlink alone does not prove which version is running.
        result["version"] = recovery["health"]["version"] if recovery["health"] else None
        result["msg"] = "Update failed; rollback %s. %s" % (
            "succeeded" if recovery["succeeded"] else "FAILED", result["update_error"])
        return result


class BlueGreenManager(ReleaseManager):
    """Two prepared slots, one authoritative nginx include symlink."""

    def __init__(self, params, systemctl):
        super().__init__(params, systemctl)
        self.active = os.path.join(self.root, "active")
        self.slots = os.path.join(self.root, "slots")

    def slot_path(self, slot):
        if slot not in ("blue", "green"):
            raise ReleaseError("Unknown deployment slot")
        path = os.path.join(self.slots, slot)
        if os.path.islink(path) or not os.path.isdir(path) or os.path.realpath(path) != path:
            raise ReleaseError("Slot must be a real directory: %s" % path)
        return path

    def active_slot(self):
        if not os.path.islink(self.active):
            raise ReleaseError("active must be a symlink to slots/blue or slots/green")
        target = os.path.realpath(self.active)
        slot = os.path.basename(target)
        if target != self.slot_path(slot):
            raise ReleaseError("active points outside the prepared slots")
        return slot

    def slot_release(self, slot):
        link = os.path.join(self.slot_path(slot), "release")
        if not os.path.islink(link):
            raise ReleaseError("Slot release must be a symlink: %s" % slot)
        target = os.path.realpath(link)
        name = os.path.basename(target)
        if target != self.release_path(name):
            raise ReleaseError("Slot release points outside releases")
        return name

    def current_name(self):
        return self.slot_release(self.active_slot())

    def validate(self):
        super().validate()
        for slot in ("blue", "green"):
            self.slot_path(slot)
            self.slot_release(slot)
            if not os.path.isfile(os.path.join(self.slot_path(slot), "upstream.conf")):
                raise ReleaseError("Missing slot upstream configuration")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.params["slot_service_prefix"]):
            raise ReleaseError("Invalid slot service prefix")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service", self.params["proxy_service"]):
            raise ReleaseError("Invalid proxy service name")
        ports = (self.params["blue_port"], self.params["green_port"])
        if ports[0] == ports[1] or any(not 1 <= port <= 65535 for port in ports):
            raise ReleaseError("Slot ports must be distinct valid TCP ports")

    def replace_link(self, destination, target):
        temporary = destination + "." + uuid.uuid4().hex
        try:
            os.symlink(target, temporary)
            os.replace(temporary, destination)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def select(self, slot):
        self.replace_link(self.active, os.path.relpath(self.slot_path(slot), self.root))

    def prepare(self, slot, release):
        self.replace_link(os.path.join(self.slot_path(slot), "release"), self.release_path(release))

    def slot_service(self, slot):
        return "%s@%s.service" % (self.params["slot_service_prefix"], slot)

    def route_probe(self, slot, expected_version=None, deadline=None):
        self.response_slot = None
        observation = self.probe(expected_version, deadline)
        observation["slot"] = self.response_slot
        if observation["ok"] and observation["slot"] != slot:
            observation.update(ok=False, error="Expected routed slot %s, observed %s" % (slot, observation["slot"]))
        return observation

    def wait_probe(self, slot, expected_version, direct=False):
        deadline = time.monotonic() + self.params["health_timeout"]
        if direct:
            params = dict(self.params)
            port = params[slot + "_port"]
            params.update(health_url="http://127.0.0.1:%s/healthz" % port,
                          version_url="http://127.0.0.1:%s/version" % port)
            probe = ReleaseManager(params, self.systemctl).probe
        else:
            probe = lambda version, limit: self.route_probe(slot, version, limit)
        observation = probe(expected_version, deadline)
        while not observation["ok"] and time.monotonic() < deadline:
            time.sleep(min(self.params["poll_interval"], max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            next_observation = probe(expected_version, deadline)
            if next_observation["error"] == "Health deadline exceeded":
                break
            observation = next_observation
        return observation

    def run(self, check_mode=False):
        self.validate()
        old = self.active_slot()
        previous = self.slot_release(old)
        candidate = "green" if old == "blue" else "blue"
        requested = self.params["release"]
        before = self.route_probe(old)
        same = previous == requested and before["ok"] and (
            self.params["expected_version"] is None or before["version"] == self.params["expected_version"])
        result = dict(changed=False, strategy="blue_green", check_mode=check_mode,
                      previous_release=previous, requested_release=requested,
                      current_release=previous, active_slot=old, candidate_slot=candidate,
                      routed_slot=before.get("slot"), version=before["version"],
                      health_before=before, candidate_health=None, health_after=None,
                      update_succeeded=None, traffic_switch_attempted=False,
                      rollback=dict(attempted=False, succeeded=None, error=None, health=None),
                      candidate_stopped=False, cleanup_error=None)
        if not before["ok"]:
            result.update(failed=True, update_succeeded=False,
                          msg="Active route is not healthy or disagrees with active symlink: %s" % before["error"])
            return result
        if check_mode:
            result.update(changed=not same, msg="Would keep the healthy slot" if same else
                          "Would start the inactive slot, check it, then reload nginx; candidate health is not predicted")
            return result
        if same:
            result.update(update_succeeded=True, health_after=before, msg="Requested release already serves healthy traffic")
            return result
        candidate_touched = False
        selected = False
        try:
            self.prepare(candidate, requested)
            candidate_touched = True
            result["changed"] = True
            self.service_action("restart", self.slot_service(candidate))
            result["candidate_health"] = self.wait_probe(candidate, self.params["expected_version"], direct=True)
            if not result["candidate_health"]["ok"]:
                raise ReleaseError("Candidate failed before traffic switch: %s" % result["candidate_health"]["error"])
            self.select(candidate)
            selected = True
            result["traffic_switch_attempted"] = True
            self.service_action("reload", self.params["proxy_service"])
            result["health_after"] = self.wait_probe(candidate, self.params["expected_version"])
            if not result["health_after"]["ok"]:
                raise ReleaseError("Public route verification failed: %s" % result["health_after"]["error"])
            result.update(update_succeeded=True, active_slot=candidate, routed_slot=candidate,
                          current_release=requested, version=result["health_after"]["version"],
                          msg="Healthy candidate now serves traffic; previous slot kept running")
            return result
        except (OSError, ReleaseError) as exc:
            result.update(failed=True, update_succeeded=False, update_error=str(exc))
        recovery = result["rollback"]
        safe_to_stop = False
        if selected:
            recovery["attempted"] = True
            try:
                self.select(old)
                self.service_action("reload", self.params["proxy_service"])
                recovery["health"] = self.wait_probe(old, before["version"])
                recovery["succeeded"] = recovery["health"]["ok"]
                if not recovery["succeeded"]:
                    raise ReleaseError("Old route did not recover: %s" % recovery["health"]["error"])
                safe_to_stop = True
            except (OSError, ReleaseError) as exc:
                recovery.update(succeeded=False, error=str(exc))
        else:
            # No cutover occurred; verify that the original route still works.
            result["health_after"] = self.wait_probe(old, before["version"])
            safe_to_stop = result["health_after"]["ok"]
        if candidate_touched and safe_to_stop:
            try:
                self.service_action("stop", self.slot_service(candidate))
                result["candidate_stopped"] = True
            except (OSError, ReleaseError) as exc:
                result["cleanup_error"] = str(exc)
        actual = recovery["health"] if selected else result["health_after"]
        result.update(routed_slot=actual.get("slot") if actual else None,
                      version=actual.get("version") if actual else None)
        try:
            result.update(active_slot=self.active_slot(), current_release=self.current_name())
        except (OSError, ReleaseError):
            result.update(active_slot=None, current_release=None)
        result["msg"] = "%s; %s" % (result["update_error"],
            ("rollback succeeded" if recovery["succeeded"] else "rollback FAILED") if selected
            else "traffic was not switched")
        return result


def main():
    module = AnsibleModule(
        argument_spec=dict(
            root=dict(type="path", default="/opt/ansiballz-demo"),
            release=dict(type="str", required=True),
            service=dict(type="str", default="ansiballz-demo.service"),
            health_url=dict(type="str", default="http://127.0.0.1:8080/health"),
            version_url=dict(type="str", default="http://127.0.0.1:8080/version"),
            expected_version=dict(type="str"),
            health_timeout=dict(type="float", default=15),
            request_timeout=dict(type="float", default=2),
            poll_interval=dict(type="float", default=0.5),
            restart_timeout=dict(type="float", default=40),
            strategy=dict(type="str", choices=["in_place", "blue_green"], default="in_place"),
            slot_service_prefix=dict(type="str", default="ansiballz-bg"),
            proxy_service=dict(type="str", default="ansiballz-bg-web.service"),
            blue_port=dict(type="int", default=19901),
            green_port=dict(type="int", default=19902),
        ),
        supports_check_mode=True,
    )
    systemctl = module.get_bin_path("systemctl", required=True)
    manager_type = BlueGreenManager if module.params["strategy"] == "blue_green" else ReleaseManager
    manager = manager_type(module.params, systemctl)
    try:
        with release_lock(manager.root):
            result = manager.run(module.check_mode)
    except (OSError, ValueError, ReleaseError) as exc:
        module.fail_json(msg=str(exc), changed=False)
    if result.pop("failed", False):
        module.fail_json(**result)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
