import unittest

from charge_confirmation import confirm_charge


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_request_until_third_success(self):
        results = iter([
            {"success": False, "msg": "设备无响应"},
            {"success": False, "msg": "设备无响应"},
            {"success": True, "msg": "启动成功"},
        ])
        calls = []

        async def request():
            calls.append(True)
            return next(results)

        async def no_sleep(_):
            pass

        result = await confirm_charge(request, max_attempts=5, sleep=no_sleep)
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 3)

    async def test_returns_last_failure_at_limit(self):
        results = iter([
            {"success": False, "msg": "第一次失败"},
            {"success": False, "msg": "最终失败"},
        ])

        async def request():
            return next(results)

        async def no_sleep(_):
            pass

        result = await confirm_charge(request, max_attempts=2, sleep=no_sleep)
        self.assertEqual(result["msg"], "最终失败")
