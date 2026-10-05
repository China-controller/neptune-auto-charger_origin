import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import main


class RecoveryTests(unittest.TestCase):
    def test_window_includes_0035_and_chooses_latest(self):
        now = datetime(2026, 8, 23, 6, tzinfo=main.TZ_BEIJING)
        earlier = int(datetime(2026, 8, 23, 0, 7, tzinfo=main.TZ_BEIJING).timestamp() * 1000)
        latest = int(datetime(2026, 8, 23, 0, 35, tzinfo=main.TZ_BEIJING).timestamp() * 1000)
        record = main.find_power_off_record([
            {"endtype": 39, "enddt": earlier, "devaddress": "a", "devport": 1},
            {"endtype": 39, "enddt": latest, "devaddress": "b", "devport": 12},
        ], now=now)
        self.assertEqual(record["devaddress"], "b")

    def test_window_excludes_after_0035(self):
        now = datetime(2026, 8, 23, 6, tzinfo=main.TZ_BEIJING)
        ended = int(datetime(2026, 8, 23, 0, 36, tzinfo=main.TZ_BEIJING).timestamp() * 1000)
        self.assertIsNone(main.find_power_off_record([{"endtype": 39, "enddt": ended}], now=now))


class StabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_two_consecutive_charging_states(self):
        with patch.object(main, "check_port", AsyncMock(side_effect=[({}, "1"), ({}, "1")])):
            async def no_sleep(_):
                pass
            self.assertTrue(await main.confirm_charge_stable(None, "dev", 11, sleep=no_sleep))

    async def test_rejects_transient_charging_state(self):
        with patch.object(main, "check_port", AsyncMock(side_effect=[({}, "1"), ({}, "0")])):
            async def no_sleep(_):
                pass
            self.assertFalse(await main.confirm_charge_stable(None, "dev", 11, sleep=no_sleep))


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_calls_begin_charge(self):
        now = datetime.now(main.TZ_BEIJING)
        yesterday = now.date() - timedelta(days=1)
        ended = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 50, tzinfo=main.TZ_BEIJING)
        record = {
            "endtype": 39,
            "enddt": int(ended.timestamp() * 1000),
            "devaddress": "dev",
            "devport": 11,
        }
        with (
            patch.object(main, "get_user_info", AsyncMock(return_value={"readyaccountmoney": 1821})),
            patch.object(main, "get_charge_log", AsyncMock(return_value=[record])),
            patch.object(main, "check_port", AsyncMock(return_value=({"portstatur": "000000000000"}, "0"))),
            patch.object(main, "begin_charge", AsyncMock()) as begin,
        ):
            result, message = await main.try_charge(None, dry_run=True)
        self.assertEqual(result, main.ChargeResult.DRY_RUN)
        self.assertIn("money=7", message)
        begin.assert_not_awaited()
