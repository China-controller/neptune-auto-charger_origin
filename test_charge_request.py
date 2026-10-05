import unittest

from charge_request import build_charge_params


class ChargeRequestTests(unittest.TestCase):
    def test_balance_option_and_physical_port(self):
        params = build_charge_params("50559123", "12", 1821, {}, 6, "open-id")
        self.assertEqual(params["money"], 7)
        self.assertEqual(params["beforemoney"], 1821)
        self.assertEqual(params["port"], "12")
