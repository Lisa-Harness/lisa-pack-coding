import urllib.error
import urllib.request

from tests.helpers import FAR_FUTURE, AppTestCase

MAC1 = "aa:bb:cc:dd:ee:01"
MAC2 = "aa:bb:cc:dd:ee:02"


class AuthTest(AppTestCase):
    def test_health_is_public(self) -> None:
        self.assertEqual(self.call("GET", "/health", token=None)[0], 200)

    def test_token_required(self) -> None:
        self.assertEqual(self.call("GET", "/reservations", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/reservations", token="wrong")[0], 401)


class RoutingTest(AppTestCase):
    def test_unknown_route_and_method(self) -> None:
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("DELETE", "/reservations")[0], 405)


class LeasesApiTest(AppTestCase):
    lease_lines = (
        f"{FAR_FUTURE} AA:BB:CC:DD:EE:01 192.168.1.20 laptop *",
        f"{FAR_FUTURE} aa:bb:cc:dd:ee:09 10.0.0.9 elsewhere *",
    )

    def test_list_is_subnet_scoped(self) -> None:
        status, body = self.call("GET", "/leases")
        self.assertEqual(status, 200)
        self.assertEqual([lease["mac"] for lease in body], [MAC1])

    def test_get_by_mac(self) -> None:
        self.assertEqual(self.call("GET", "/leases/aabbccddee01")[1][0]["ip"], "192.168.1.20")
        self.assertEqual(self.call("GET", f"/leases/{MAC2}")[0], 404)


class ReservationsApiTest(AppTestCase):
    def test_create_publishes(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": "AA-BB-CC-DD-EE-01", "ip": "192.168.1.10", "hostname": "nas"})
        self.assertEqual(status, 201)
        self.assertEqual(body["mac"], MAC1)
        self.assertEqual(
            self.published(), {"aa-bb-cc-dd-ee-01.conf": f"dhcp-host={MAC1},192.168.1.10,nas\n"}
        )

    def test_create_requires_mac_and_ip(self) -> None:
        self.assertEqual(self.call("POST", "/reservations", {"ip": "192.168.1.10"})[0], 422)
        self.assertEqual(self.call("POST", "/reservations", {"mac": MAC1})[0], 422)

    def test_create_rejects_outside_subnet(self) -> None:
        status, body = self.call("POST", "/reservations", {"mac": MAC1, "ip": "10.0.0.5"})
        self.assertEqual((status, body["error"]), (422, "invalid"))
        self.assertEqual(self.published(), {})

    def test_duplicate_mac_conflicts(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})
        status, body = self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.11"})
        self.assertEqual((status, body["error"]), (409, "already_reserved"))

    def test_update_and_delete(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})
        status, body = self.call("PUT", f"/reservations/{MAC1}", {"ip": "192.168.1.11"})
        self.assertEqual((status, body["ip"]), (200, "192.168.1.11"))
        self.assertIn("192.168.1.11", self.published()["aa-bb-cc-dd-ee-01.conf"])
        self.assertEqual(self.call("DELETE", f"/reservations/{MAC1}")[0], 204)
        self.assertEqual(self.published(), {})
        self.assertEqual(self.call("GET", f"/reservations/{MAC1}")[0], 404)

    def test_update_mac_mismatch(self) -> None:
        self.call("POST", "/reservations", {"mac": MAC1, "ip": "192.168.1.10"})
        status, _ = self.call("PUT", f"/reservations/{MAC1}", {"mac": MAC2, "ip": "192.168.1.10"})
        self.assertEqual(status, 422)

    def test_bad_json(self) -> None:
        req = urllib.request.Request(self.base + "/reservations", data=b"[1]", method="POST")
        req.add_header("Authorization", "Bearer test-token")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:
            self.fail("expected 400")
