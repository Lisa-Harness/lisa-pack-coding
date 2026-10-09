import tempfile
import unittest
from pathlib import Path

from netpin.leases import LeaseFile, parse_line


class ParseLineTest(unittest.TestCase):
    def test_parses_and_normalizes(self) -> None:
        lease = parse_line("1700000000 AA:BB:CC:DD:EE:01 192.168.1.20 Laptop 01:aa:bb")
        assert lease is not None
        self.assertEqual(lease.mac, "aa:bb:cc:dd:ee:01")
        self.assertEqual(lease.hostname, "laptop")
        self.assertEqual(lease.client_id, "01:aa:bb")

    def test_star_fields(self) -> None:
        lease = parse_line("0 aa:bb:cc:dd:ee:02 192.168.1.21 * *")
        assert lease is not None
        self.assertEqual((lease.expires, lease.hostname, lease.client_id), (0, "", ""))

    def test_malformed_lines_skip(self) -> None:
        for line in ("", "garbage", "x aa:bb:cc:dd:ee:02 192.168.1.21 *", "1 nope 192.168.1.2 *"):
            self.assertIsNone(parse_line(line))


class LeaseFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "leases"
        self.path.write_text(
            "\n".join(
                [
                    "4102444800 aa:bb:cc:dd:ee:01 192.168.1.20 laptop *",
                    "4102444800 aa:bb:cc:dd:ee:02 192.168.2.20 other *",
                    "1000 AA:BB:CC:DD:EE:01 192.168.1.99 laptop *",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_subnet_filter(self) -> None:
        self.assertEqual(len(LeaseFile(self.path).all()), 3)
        self.assertEqual(len(LeaseFile(self.path, subnet="192.168.1.0/24").all()), 2)

    def test_by_mac_normalizes_and_includes_expired(self) -> None:
        found = LeaseFile(self.path).by_mac("AABBCCDDEE01")
        self.assertEqual(sorted(lease.ip for lease in found), ["192.168.1.20", "192.168.1.99"])

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(LeaseFile(self.path.with_name("absent")).all(), [])
