import unittest
from unittest.mock import AsyncMock, patch

import main


class BeginChargeTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_keeps_same_msgflag_across_network_error(self):
        api = AsyncMock(side_effect=[
            {"success": True, "obj": "same-flag"},
            main.PlatformRequestError("temporary"),
            {"success": False, "msg": "设备无响应"},
            {"success": True, "msg": "启动成功"},
        ])

        async def immediate_confirm(request, on_result=None, **_kwargs):
            result = None
            for attempt in range(1, 4):
                result = await request()
                if on_result:
                    on_result(attempt, result)
                if result.get("success"):
                    return result
            return result

        with (
            patch.object(main, "post_platform_api", api),
            patch.object(main, "confirm_charge", immediate_confirm),
        ):
            result = await main.begin_charge(None, "dev", "12", 1821, {})

        self.assertTrue(result["success"])
        confirmation_params = [call.args[2] for call in api.await_args_list[1:]]
        self.assertEqual([item["msgflag"] for item in confirmation_params], ["same-flag"] * 3)
        self.assertEqual(confirmation_params[-1]["money"], 7)
        self.assertEqual(confirmation_params[-1]["beforemoney"], 1821)
