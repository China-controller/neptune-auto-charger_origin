import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import main


class PortStatusTests(unittest.TestCase):
    def test_port_numbers_are_one_based(self):
        self.assertEqual(main.get_port_status("000000100000", "07"), "1")
        self.assertFalse(main.is_port_free("000000100000", "07"))
        self.assertTrue(main.is_port_free("000000100000", "08"))

    def test_different_ports_are_read_dynamically(self):
        statuses = "001000000100"
        self.assertEqual(main.get_port_status(statuses, "03"), "1")
        self.assertEqual(main.get_port_status(statuses, "10"), "1")
        self.assertEqual(main.get_port_status(statuses, "12"), "0")

    def test_invalid_port_is_not_free(self):
        self.assertIsNone(main.get_port_status("0000", "00"))
        self.assertIsNone(main.get_port_status("0000", "05"))
        self.assertFalse(main.is_port_free("0000", "00"))


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_device_response_is_success(self):
        responses = [
            {"portstatur": "000000000000"},
            {"portstatur": "000000100000"},
        ]

        with (
            patch.object(main, "CONFIRM_INTERVAL", 0),
            patch.object(
                main, "get_device_info", AsyncMock(side_effect=responses)
            ),
        ):
            started = await main.confirm_charge_started(
                AsyncMock(), "50559123", "07"
            )

        self.assertTrue(started)


class PlatformDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_identifies_github_to_platform_segment(self):
        class TimeoutSession:
            def post(self, *args, **kwargs):
                raise asyncio.TimeoutError

        with self.assertRaisesRegex(
            main.PlatformRequestError, "GitHub Actions → 平台服务器超时"
        ):
            await main.post_platform_api(
                TimeoutSession(), "/wxn/getUserInfo", {}, "获取用户信息"
            )


if __name__ == "__main__":
    unittest.main()
