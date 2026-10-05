import unittest

from ports import get_port_status, is_port_free


class PortTests(unittest.TestCase):
    def test_physical_port_boundaries(self):
        self.assertEqual(get_port_status("100000000001", 1), "1")
        self.assertEqual(get_port_status("100000000001", 12), "1")
        self.assertTrue(is_port_free("000000000000", 12))

    def test_invalid_ports(self):
        self.assertIsNone(get_port_status("000000000000", 0))
        self.assertIsNone(get_port_status("000000000000", 13))
        self.assertFalse(is_port_free("000000000000", "bad"))
