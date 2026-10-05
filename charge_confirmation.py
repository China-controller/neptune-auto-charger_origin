"""使用同一个 msgflag 轮询确认充电启动结果。"""

import asyncio
from typing import Awaitable, Callable, Optional

CONFIRMATION_INTERVAL_SECONDS = 6
MAX_CONFIRMATION_ATTEMPTS = 15


async def confirm_charge(
    request_confirmation: Callable[[], Awaitable[dict]],
    max_attempts: int = MAX_CONFIRMATION_ATTEMPTS,
    interval: int = CONFIRMATION_INTERVAL_SECONDS,
    sleep=asyncio.sleep,
    on_result: Optional[Callable[[int, dict], None]] = None,
) -> dict:
    """按网页端节奏重复确认；请求闭包须始终携带同一 msgflag。"""
    last_result = {"success": False, "msg": "未执行充电确认请求"}
    for attempt in range(1, max_attempts + 1):
        await sleep(interval)
        last_result = await request_confirmation()
        if on_result is not None:
            on_result(attempt, last_result)
        if last_result.get("success"):
            return last_result
    return last_result
