"""Hidden acceptance tests for the "Trust the pin" chunk.

Run from the root of a netpin checkout (the oracle's cwd)::

    python3 <scenario>/acceptance/test_trust_the_pin.py

Kept outside the fixture so the model under test never sees or edits them.
Uses only the stable wiring (``netpin.cli.build_app``, ``netpin.api``).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, os.getcwd())

from netpin.api import make_server, serve_in_thread  # noqa: E402
from netpin.cli import build_app  # noqa: E402

TOKEN = "acceptance"
FUTURE = 4_102_444_800
PAST = 1_000_000_000
MAC1 = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"
MAC3 = "aa:bb:cc:dd:ee:03"


class Base(unittest.TestCase):
    lease_lines: tuple[str, ...] = ()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.lease_path = root / "leases"
        self.write_leases(*self.lease_lines)
        self.hostdir = root / "hosts.d"
        self.app = build_app(
            db_path=root / "db",
            lease_path=root / "leases",
            hostdir_path=self.hostdir,
            token=TOKEN,
            subnet="192.168.1.0/24",
        )
        self.server = make_server(self.app)
        serve_in_thread(self.server)
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def write_leases(self, *lines: str) -> None:
        self.lease_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {TOKEN}")
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
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.hostdir.glob("*.conf"))}

    def assertConflict(self, result: tuple[int, Any], conflicting_mac: str) -> None:  # noqa: N802
        status, body = result
        self.assertEqual(status, 409, body)
        self.assertEqual(body.get("error"), "ip_conflict", body)
        self.assertEqual(body.get("conflicting_mac"), conflicting_mac, body)


class ReservationConflictTest(Base):
    def test_post_rejects_ip_reserved_for_another_mac(self) -> None:
        self.assertEqual(self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})[0], 201)
        before = self.published()
        self.assertConflict(
            self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.10"}), MAC1
        )
        self.assertEqual(self.call("GET", f"/reservations/{MAC2}")[0], 404)
        self.assertEqual(self.published(), before)

    def test_put_rejects_ip_reserved_for_another_mac_and_changes_nothing(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})
        self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.11"})
        before = self.published()
        self.assertConflict(self.call("PUT", f"/reservations/{MAC2}", {"ip": "192.168.1.10"}), MAC1)
        self.assertEqual(self.call("GET", f"/reservations/{MAC2}")[1]["ip"], "192.168.1.11")
        self.assertEqual(self.published(), before)

    def test_resaving_own_ip_is_not_a_conflict(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})
        status, body = self.call(
            "PUT", f"/reservations/{MAC1}", {"ip": "192.168.1.10", "hostname": "nas"}
        )
        self.assertEqual((status, body["hostname"]), (200, "nas"))


class LeaseConflictTest(Base):
    lease_lines = (
        f"{FUTURE} AA:BB:CC:DD:EE:01 192.168.1.20 laptop *",
        "0 aa-bb-cc-dd-ee-01 192.168.1.21 laptop-wired *",
        f"{PAST} aa:bb:cc:dd:ee:01 192.168.1.22 laptop-old *",
        f"{FUTURE} aa:bb:cc:dd:ee:02 192.168.1.30 phone *",
    )

    def test_active_lease_of_another_mac_conflicts(self) -> None:
        self.assertConflict(
            self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.20"}), MAC1
        )
        self.assertEqual(self.published(), {})

    def test_infinite_lease_of_another_mac_conflicts(self) -> None:
        self.assertConflict(
            self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.21"}), MAC1
        )

    def test_expired_lease_does_not_conflict(self) -> None:
        self.assertEqual(
            self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.22"})[0], 201
        )

    def test_own_active_lease_does_not_conflict(self) -> None:
        self.assertEqual(
            self.call("POST", "/reservations", {"mac": MAC2, "ip": "192.168.1.30"})[0], 201
        )

    def test_put_into_another_macs_lease_conflicts(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC3, "ip": "192.168.1.40"})
        self.assertConflict(self.call("PUT", f"/reservations/{MAC3}", {"ip": "192.168.1.30"}), MAC2)
        self.assertEqual(self.call("GET", f"/reservations/{MAC3}")[1]["ip"], "192.168.1.40")


class ReserveFromLeaseTest(Base):
    lease_lines = (
        f"{PAST} aa:bb:cc:dd:ee:02 192.168.1.29 phone-old *",
        f"{FUTURE} AA:BB:CC:DD:EE:02 192.168.1.30 Phone *",
        f"{PAST} aa:bb:cc:dd:ee:03 192.168.1.40 tablet *",
        f"{FUTURE} aa:bb:cc:dd:ee:04 10.0.0.4 offnet *",
        "0 aa:bb:cc:dd:ee:05 192.168.1.50 * *",
    )

    def test_post_without_ip_pins_the_active_lease(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": "AABBCCDDEE02"})
        self.assertEqual(status, 201, body)
        self.assertEqual((body["mac"], body["ip"], body["hostname"]), (MAC2, "192.168.1.30", "phone"))
        self.assertEqual(
            self.published(), {"aa-bb-cc-dd-ee-02.conf": f"dhcp-host={MAC2},192.168.1.30,phone\n"}
        )

    def test_body_hostname_overrides_lease_hostname(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": MAC2, "hostname": "pixel"})
        self.assertEqual((status, body["ip"], body["hostname"]), (201, "192.168.1.30", "pixel"))

    def test_infinite_lease_counts_as_active(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": "aa:bb:cc:dd:ee:05"})
        self.assertEqual((status, body["ip"], body["hostname"]), (201, "192.168.1.50", ""))

    def test_only_expired_lease_is_no_active_lease(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": MAC3})
        self.assertEqual((status, body.get("error")), (422, "no_active_lease"), body)
        self.assertEqual(self.published(), {})

    def test_off_subnet_lease_is_no_active_lease(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": "aa:bb:cc:dd:ee:04"})
        self.assertEqual((status, body.get("error")), (422, "no_active_lease"), body)

    def test_unknown_mac_is_no_active_lease(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": "aa:bb:cc:dd:ee:99"})
        self.assertEqual((status, body.get("error")), (422, "no_active_lease"), body)

    def test_from_lease_still_checks_reservation_conflicts(self) -> None:
        self.write_leases()
        self.assertEqual(
            self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.30"})[0], 201
        )
        self.write_leases(*self.lease_lines)
        self.assertConflict(self.call("POST", "/reservations", {"mac": MAC2}), MAC1)

    def test_put_still_requires_ip(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC2})
        status, body = self.call("PUT", f"/reservations/{MAC2}", {"hostname": "x"})
        self.assertEqual((status, body.get("error")), (422, "invalid"), body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
