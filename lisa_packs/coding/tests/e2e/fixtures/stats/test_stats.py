import unittest

from stats import mean, spread, total


class StatsTest(unittest.TestCase):
    def test_total(self) -> None:
        self.assertEqual(total([1.0, 2.0, 3.0]), 6.0)

    def test_mean(self) -> None:
        self.assertEqual(mean([2.0, 4.0, 6.0]), 4.0)

    def test_mean_empty(self) -> None:
        with self.assertRaises(ValueError):
            mean([])

    def test_spread(self) -> None:
        self.assertEqual(spread([3.0, 9.0, 5.0]), 6.0)


if __name__ == "__main__":
    unittest.main()
