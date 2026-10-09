import tempfile
import unittest
from pathlib import Path

from netpin.store import AlreadyExists, NotFound, ReservationStore


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReservationStore(Path(self._tmp.name) / "db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_create_get_list(self) -> None:
        self.store.create("AA-BB-CC-DD-EE-01", "192.168.1.10", "NAS")
        got = self.store.get("aa:bb:cc:dd:ee:01")
        self.assertEqual((got.ip, got.hostname), ("192.168.1.10", "nas"))
        self.assertEqual([r.mac for r in self.store.list()], ["aa:bb:cc:dd:ee:01"])

    def test_duplicate_mac(self) -> None:
        self.store.create("aa:bb:cc:dd:ee:01", "192.168.1.10")
        with self.assertRaises(AlreadyExists):
            self.store.create("AABBCCDDEE01", "192.168.1.11")

    def test_update_and_delete(self) -> None:
        self.store.create("aa:bb:cc:dd:ee:01", "192.168.1.10")
        self.assertEqual(self.store.update("aa:bb:cc:dd:ee:01", "192.168.1.12").ip, "192.168.1.12")
        self.store.delete("aa:bb:cc:dd:ee:01")
        with self.assertRaises(NotFound):
            self.store.get("aa:bb:cc:dd:ee:01")
        with self.assertRaises(NotFound):
            self.store.update("aa:bb:cc:dd:ee:01", "192.168.1.12")

    def test_by_ip(self) -> None:
        self.store.create("aa:bb:cc:dd:ee:01", "192.168.1.10")
        self.assertEqual([r.mac for r in self.store.by_ip("192.168.1.10")], ["aa:bb:cc:dd:ee:01"])
        self.assertEqual(self.store.by_ip("192.168.1.11"), [])
