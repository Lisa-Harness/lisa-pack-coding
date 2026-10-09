import unittest

from netpin.macs import ValidationError, in_subnet, normalize_hostname, normalize_ip, normalize_mac


class NormalizeMacTest(unittest.TestCase):
    def test_accepts_common_forms(self) -> None:
        for raw in ("AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabbccddeeff", " Aa:bB:cc:DD:ee:ff "):
            self.assertEqual(normalize_mac(raw), "aa:bb:cc:dd:ee:ff")

    def test_rejects_mixed_separators_and_garbage(self) -> None:
        for raw in ("aa:bb-cc:dd:ee:ff", "aa:bb:cc:dd:ee", "zz:bb:cc:dd:ee:ff", ""):
            with self.assertRaises(ValidationError):
                normalize_mac(raw)


class NormalizeIpTest(unittest.TestCase):
    def test_canonical(self) -> None:
        self.assertEqual(normalize_ip(" 192.168.1.10 "), "192.168.1.10")

    def test_rejects_unusable(self) -> None:
        for raw in ("0.0.0.0", "127.0.0.1", "224.0.0.1", "300.1.1.1", "fe80::1"):
            with self.assertRaises(ValidationError):
                normalize_ip(raw)


class HostnameTest(unittest.TestCase):
    def test_star_and_blank_mean_none(self) -> None:
        self.assertEqual(normalize_hostname("*"), "")
        self.assertEqual(normalize_hostname(None), "")

    def test_lowercases(self) -> None:
        self.assertEqual(normalize_hostname("NAS-1"), "nas-1")

    def test_rejects_bad_labels(self) -> None:
        for raw in ("-nas", "nas.local", "a" * 64):
            with self.assertRaises(ValidationError):
                normalize_hostname(raw)


class SubnetTest(unittest.TestCase):
    def test_membership(self) -> None:
        self.assertTrue(in_subnet("192.168.1.9", "192.168.1.0/24"))
        self.assertFalse(in_subnet("192.168.2.9", "192.168.1.0/24"))
        self.assertTrue(in_subnet("10.0.0.1", None))
