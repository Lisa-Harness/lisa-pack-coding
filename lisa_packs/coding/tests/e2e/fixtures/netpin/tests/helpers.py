"""Shared fixtures: a temp lease file, store, host dir, and a live server."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from netpin.api import make_server, serve_in_thread
from netpin.cli import build_app

TOKEN = "test-token"
FAR_FUTURE = 4_102_444_800  # 2100-01-01


class AppTestCase(unittest.TestCase):
    """Boots a real server on an ephemeral port per test."""

    subnet: str | None = "192.168.1.0/24"
    lease_lines: tuple[str, ...] = ()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.lease_path = root / "dnsmasq.leases"
        self.lease_path.write_text("\n".join(self.lease_lines) + "\n", encoding="utf-8")
        self.hostdir_path = root / "hosts.d"
        self.app = build_app(
            db_path=root / "netpin.db",
            lease_path=self.lease_path,
            hostdir_path=self.hostdir_path,
            token=TOKEN,
            subnet=self.subnet,
        )
        self.server = make_server(self.app)
        serve_in_thread(self.server)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.service.store.close()
        self._tmp.cleanup()

    def call(
        self, method: str, path: str, body: Any = None, *, token: str | None = TOKEN
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def published(self) -> dict[str, str]:
        return self.app.service.hostdir.published()
