import unittest
from phoenix_f310_utils import clamp, shape_axis, trigger_fraction

class MappingTests(unittest.TestCase):
    def test_deadzone(self):
        self.assertEqual(shape_axis(0.03, 0.08, 0.25), 0.0)
    def test_extremes(self):
        self.assertAlmostEqual(shape_axis(1.0, 0.08, 0.25), 1.0, places=6)
        self.assertAlmostEqual(shape_axis(-1.0, 0.08, 0.25), -1.0, places=6)
    def test_invert(self):
        self.assertLess(shape_axis(0.8, 0.08, 0.25, invert=True), 0)
    def test_trigger_minus_one_to_one(self):
        self.assertEqual(trigger_fraction(-1.0, -1.0), 0.0)
        self.assertAlmostEqual(trigger_fraction(0.0, -1.0), 0.5)
        self.assertEqual(trigger_fraction(1.0, -1.0), 1.0)
    def test_clamp(self):
        self.assertEqual(clamp(20, 0, 10), 10)

if __name__ == '__main__':
    unittest.main()
