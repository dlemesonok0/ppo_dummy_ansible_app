#!/usr/bin/env python3
"""Disposable systemd fixture; its failure marker never touches Podinfo."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

root = Path(sys.argv[1])
release = (root / "current").resolve().name
if release == "broken" and (root / "inject_rollback_failure").exists():
    (root / "poisoned").touch()
healthy = release != "broken" and not (root / "poisoned").exists()
version = "fixture-v1" if release == "v1" else "fixture-v2"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/version" or healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(dict(version=version, release=release)).encode())


HTTPServer.allow_reuse_address = True
HTTPServer(("127.0.0.1", int(sys.argv[2])), Handler).serve_forever()
