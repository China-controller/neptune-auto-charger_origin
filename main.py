"""Neptune 断电后自动恢复充电。"""

import argparse
import aiohttp
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Tuple

from charge_confirmation import (
    CONFIRMATION_INTERVAL_SECONDS, MAX_CONFIRMATION_ATTEMPTS, confirm_charge,
)
from charge_request import build_charge_params
from ports import get_port_status, is_port_free
from config import (
    OPEN_ID, AREA_ID, EMPLOYEE_ID, MAX_CHARGE_TIME, BASE_URL,
    POWER_OFF_WINDOW_START_HOUR, POWER_OFF_WINDOW_START_MINUTE,
    POWER_OFF_WINDOW_END_HOUR, POWER_OFF_WINDOW_END_MINUTE,
    POWER_OFF_END_TYPE, validate_config,
)

MAX_RETRIES = 10
RETRY_INTERVAL = 2 * 60
STABILITY_CHECKS = 2
STABILITY_INTERVAL = 15
TZ_BEIJING = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/142.0 Mobile Safari/537.36 MicroMessenger/8.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/wx/indexn.html?openId={OPEN_ID}&areaid={AREA_ID}",
    "Accept": "*/*",
}


class ChargeResult(Enum):
    SUCCESS = "success"
    NO_RECORD = "no_record"
    PORT_BUSY = "port_busy"
    ERROR = "error"
    DRY_RUN = "dry_run"
    UNSTABLE = "unstable"


class PlatformRequestError(RuntimeError):
    pass


class PlatformBusinessError(RuntimeError):
    pass


def log(message: str):
    now = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


async def post_platform_api(session, endpoint: str, data: dict, operation: str) -> dict:
    try:
        async with session.post(f"{BASE_URL}{endpoint}", data=data, headers=HEADERS) as resp:
            if not 200 <= resp.status < 300:
                body = (await resp.text())[:200].replace("\n", " ")
                raise PlatformRequestError(f"{operation} HTTP {resp.status}，响应摘要={body!r}")
            try:
                result = await resp.json()
            except (aiohttp.ContentTypeError, ValueError) as exc:
                body = (await resp.text())[:200].replace("\n", " ")
                raise PlatformRequestError(f"{operation} 返回内容不是有效 JSON，响应摘要={body!r}") from exc
            if not isinstance(result, dict):
                raise PlatformRequestError(f"{operation} 返回的数据结构异常")
            return result
    except asyncio.TimeoutError as exc:
        raise PlatformRequestError(f"{operation} 超时（30 秒内未完成）") from exc
    except aiohttp.ClientError as exc:
        raise PlatformRequestError(f"{operation} 网络错误：{type(exc).__name__}: {exc}") from exc


async def get_user_info(session) -> Optional[dict]:
    result = await post_platform_api(
        session, "/wxn/getUserInfo", {"openId": OPEN_ID, "areaId": AREA_ID}, "获取用户信息"
    )
    if not result.get("success"):
        raise PlatformBusinessError(f"获取用户信息失败：{result.get('msg', '未知错误')}")
    return result.get("obj")


async def get_charge_log(session, term: str) -> list:
    result = await post_platform_api(
        session, "/wxn/getChargeLog", {"employeeid": EMPLOYEE_ID, "term": term}, "获取充电历史"
    )
    if not result.get("success"):
        raise PlatformBusinessError(f"获取充电历史失败：{result.get('msg', '未知错误')}")
    records = result.get("obj", [])
    if not isinstance(records, list):
        raise PlatformBusinessError("获取充电历史失败：记录格式异常")
    return records


async def get_device_info(session, devaddress: str) -> Optional[dict]:
    result = await post_platform_api(
        session, "/wxn/getDeviceInfo", {"areaId": AREA_ID, "devaddress": devaddress}, "获取设备信息"
    )
    if not result.get("success"):
        raise PlatformBusinessError(f"获取设备信息失败：{result.get('msg', '未知错误')}")
    return result.get("obj")


async def begin_charge(session, devaddress: str, port, balance: int, device_info: dict) -> dict:
    params = build_charge_params(devaddress, port, balance, device_info, AREA_ID, OPEN_ID)
    result = await post_platform_api(session, "/wxn/beginCharge", params, "创建充电请求")
    if not result.get("success"):
        return {"success": False, "msg": f"第一步失败：{result.get('msg', '未知错误')}"}
    msgflag = result.get("obj")
    if not msgflag:
        return {"success": False, "msg": "未获取到 msgflag"}

    params["msgflag"] = msgflag
    log(
        f"已获取启动凭据，每 {CONFIRMATION_INTERVAL_SECONDS} 秒使用同一 msgflag 确认，"
        f"最多 {MAX_CONFIRMATION_ATTEMPTS} 次"
    )

    async def request_confirmation():
        try:
            return await post_platform_api(session, "/wxn/beginCharge", params, "确认充电启动")
        except PlatformRequestError as exc:
            # 确认阶段网络抖动时保留原订单和 msgflag，避免重新创建订单。
            return {"success": False, "msg": f"确认请求网络异常：{exc}"}

    def report(attempt: int, confirmation: dict):
        message = "成功" if confirmation.get("success") else f"未成功：{confirmation.get('msg', '未知错误')}"
        log(f"设备确认第 {attempt}/{MAX_CONFIRMATION_ATTEMPTS} 次{message}")

    return await confirm_charge(request_confirmation, on_result=report)


def find_power_off_record(logs: list, now: Optional[datetime] = None) -> Optional[dict]:
    now = now or datetime.now(TZ_BEIJING)
    today = now.date()
    yesterday = today - timedelta(days=1)
    start = datetime(
        yesterday.year, yesterday.month, yesterday.day,
        POWER_OFF_WINDOW_START_HOUR, POWER_OFF_WINDOW_START_MINUTE, tzinfo=TZ_BEIJING,
    )
    end = datetime(
        today.year, today.month, today.day,
        POWER_OFF_WINDOW_END_HOUR, POWER_OFF_WINDOW_END_MINUTE, tzinfo=TZ_BEIJING,
    )
    log(f"检测时间窗口: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    candidates = []
    for record in logs:
        if record.get("endtype") != POWER_OFF_END_TYPE or record.get("enddt") is None:
            continue
        try:
            ended_at = datetime.fromtimestamp(float(record["enddt"]) / 1000, tz=TZ_BEIJING)
        except (TypeError, ValueError, OverflowError):
            continue
        if start <= ended_at <= end:
            candidates.append((ended_at, record))
    if not candidates:
        return None
    ended_at, record = max(candidates, key=lambda item: item[0])
    log(
        f"找到最近断电记录: 设备={record.get('devaddress')}, 端口={record.get('devport')}, "
        f"结束时间={ended_at:%Y-%m-%d %H:%M:%S}"
    )
    return record


async def check_port(session, devaddress: str, port) -> Tuple[Optional[dict], Optional[str]]:
    device_info = await get_device_info(session, devaddress)
    if not isinstance(device_info, dict):
        return None, None
    return device_info, get_port_status(device_info.get("portstatur", ""), port)


async def confirm_charge_stable(session, devaddress: str, port, sleep=asyncio.sleep) -> bool:
    """平台确认后要求端口连续两次保持充电态，过滤瞬时启动。"""
    for check in range(1, STABILITY_CHECKS + 1):
        await sleep(STABILITY_INTERVAL)
        _, status = await check_port(session, devaddress, port)
        log(f"稳定性检查 {check}/{STABILITY_CHECKS}：端口 {port} 状态={status}")
        if status != "1":
            return False
    return True


async def try_charge(session, dry_run: bool = False) -> Tuple[ChargeResult, str]:
    try:
        log("获取用户信息...")
        user_info = await get_user_info(session)
        if not isinstance(user_info, dict):
            return ChargeResult.ERROR, "用户信息格式异常"
        try:
            balance = int(user_info.get("readyaccountmoney", 0))
        except (TypeError, ValueError):
            return ChargeResult.ERROR, "账户余额格式异常"
        log(f"当前余额: {balance / 100:.2f} 元")
        if balance < 100:
            return ChargeResult.ERROR, "余额不足 1 元"

        now = datetime.now(TZ_BEIJING)
        term_this = now.strftime("%Y%m")
        log(f"获取 {term_this} 充电历史...")
        logs = await get_charge_log(session, term_this)
        if now.day <= 3:
            term_last = (now.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
            log(f"获取 {term_last} 充电历史...")
            logs.extend(await get_charge_log(session, term_last))
        if not logs:
            return ChargeResult.NO_RECORD, "无充电历史记录"

        record = find_power_off_record(logs, now=now)
        if not record:
            return ChargeResult.NO_RECORD, "未找到符合条件的断电记录"
        devaddress, port = str(record.get("devaddress")), record.get("devport")
        if devaddress in ("", "None"):
            return ChargeResult.ERROR, "断电记录缺少设备地址"

        log(f"获取设备 {devaddress} 信息...")
        device_info, status = await check_port(session, devaddress, port)
        log(f"端口 {port} 状态={status}")
        if device_info is None or status is None:
            return ChargeResult.ERROR, f"端口 {port} 不存在或设备状态格式异常"
        if status == "1":
            log("端口已处于充电态，继续确认状态是否稳定...")
            if await confirm_charge_stable(session, devaddress, port):
                return ChargeResult.SUCCESS, f"设备={devaddress}, 端口={port} 已稳定充电"
            return ChargeResult.ERROR, f"端口 {port} 仅短暂处于充电态"
        if not is_port_free(device_info.get("portstatur", ""), port):
            return ChargeResult.PORT_BUSY, f"端口 {port} 状态异常（状态码 {status}）"

        params = build_charge_params(devaddress, port, balance, device_info, AREA_ID, OPEN_ID)
        if dry_run:
            return ChargeResult.DRY_RUN, (
                f"充电未启动；设备={devaddress}, 物理端口={port}, "
                f"money={params['money']}, beforemoney={params['beforemoney']}"
            )

        # 下单前重新获取设备状态，缩小检查与执行之间的竞态窗口。
        latest_device, latest_status = await check_port(session, devaddress, port)
        log(f"下单前复查：端口 {port} 状态={latest_status}")
        if latest_device is None or latest_status != "0":
            return ChargeResult.PORT_BUSY, f"下单前端口已非空闲（状态码 {latest_status}）"

        log(f"启动充电: 设备={devaddress}, 端口={port}, 余额={balance / 100:.2f}元, 选项=7")
        result = await begin_charge(session, devaddress, port, balance, latest_device)
        if not result.get("success"):
            return ChargeResult.ERROR, f"平台未确认启动：{result.get('msg', '未知错误')}"

        log("平台已确认启动，继续检查端口是否稳定，避免 0 分钟无效订单...")
        if not await confirm_charge_stable(session, devaddress, port):
            return ChargeResult.UNSTABLE, "平台虽确认启动，但端口未持续充电；本轮停止，避免连续创建 0 分钟订单"
        return ChargeResult.SUCCESS, f"设备={devaddress}, 端口={port}, 已通过平台确认和稳定性检查"
    except (PlatformRequestError, PlatformBusinessError) as exc:
        return ChargeResult.ERROR, str(exc)
    except Exception as exc:
        return ChargeResult.ERROR, f"脚本内部异常: {type(exc).__name__}: {exc}"


async def main(dry_run: bool = False):
    log("=" * 50)
    log("Neptune 自动充电脚本启动")
    log("DRY RUN：仅预览，不启动充电" if dry_run else f"重试策略: 最多 {MAX_RETRIES} 次，间隔 {RETRY_INTERVAL // 60} 分钟")
    log("=" * 50)
    errors = validate_config()
    if errors:
        for error in errors:
            log(f"配置错误: {error}")
        return 1

    attempts = 1 if dry_run else MAX_RETRIES
    timeout = aiohttp.ClientTimeout(total=30)
    for attempt in range(1, attempts + 1):
        log(f"\n--- 第 {attempt}/{attempts} 次尝试 ---")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            result, message = await try_charge(session, dry_run=dry_run)
        if result in (ChargeResult.SUCCESS, ChargeResult.DRY_RUN):
            log("=" * 50)
            log("预演完成" if result == ChargeResult.DRY_RUN else "充电已稳定启动！")
            log(f"  {message}")
            if result == ChargeResult.SUCCESS:
                log(f"  充电选项最长时间: {MAX_CHARGE_TIME} 分钟")
            log("=" * 50)
            return 0
        if result == ChargeResult.NO_RECORD:
            log(f"结果: {message}；无需恢复充电")
            return 0
        if result == ChargeResult.UNSTABLE:
            log(f"结果: {message}")
            log("等待下一个独立定时任务再恢复，不在本轮重复下单")
            return 1
        log(f"结果: {message}")
        if attempt < attempts:
            log(f"将在 {RETRY_INTERVAL // 60} 分钟后重试...")
            await asyncio.sleep(RETRY_INTERVAL)
    log("所有重试均失败")
    return 1


def parse_args():
    parser = argparse.ArgumentParser(description="Neptune 自动充电脚本")
    parser.add_argument("-n", "--dry-run", action="store_true", help="预览充电请求但不启动充电")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main(dry_run=args.dry_run)))
