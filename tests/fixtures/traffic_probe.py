#!/usr/bin/env python3
"""Bounded, serial HTTP sampling while Ansible changes deployment slots."""

import argparse
from collections import Counter
import json
from pathlib import Path
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("directory")
args = parser.parse_args()
directory = Path(args.directory)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
result = dict(requests=0, errors=0, examples=[], routes={})
routes = Counter()
deadline = time.monotonic() + 900
try:
    while not (directory / "stop").exists() and time.monotonic() < deadline:
        for endpoint in ("health", "version"):
            result["requests"] += 1
            try:
                with opener.open("http://127.0.0.1:8081/" + endpoint, timeout=2) as response:
                    data = json.load(response)
                    slot = response.headers.get("X-Deployment-Slot")
                    if response.status != 200 or slot not in ("blue", "green"):
                        raise RuntimeError("Unexpected status/slot")
                    if endpoint == "version":
                        if data["version"] not in ("6.14.1", "6.15.0"):
                            raise RuntimeError("Unexpected application version")
                        routes[slot + "/" + data["version"]] += 1
            except Exception as exc:
                result["errors"] += 1
                if len(result["examples"]) < 10:
                    result["examples"].append(str(exc))
        time.sleep(0.1)
finally:
    result["routes"] = dict(routes)
    (directory / "report.json").write_text(json.dumps(result))
