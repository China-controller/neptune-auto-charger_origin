"""
Neptune 自动充电脚本

功能：
1. 检测昨天 24 点因断电结束的充电记录
2. 如果存在，自动在同一设备/端口恢复充电
3. 按余额最大化充电，最长 480 分钟
4. 支持重试和启动结果确认，避免设备响应慢时重复下单

用法：
    python main.py

VPS 定时任务：
    5 6 * * * cd /path/to/auto-charger && python3 main.py >> charge.log 2>&1
"""

import aiohttp
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from enum import Enum

from config import (
    OPEN_ID,
    AREA_ID,
    EMPLOYEE_ID,
    MAX_CHARGE_TIME,
    BASE_URL,
    POWER_OFF_WINDOW_START_HOUR,
    POWER_OFF_WINDOW_START_MINUTE,
    POWER_OFF_WINDOW_END_HOUR,
    POWER_OFF_WINDOW_END_MINUTE,
    POWER_OFF_END_TYPE,
    validate_config,
)

# 重试配置
MAX_RETRIES = 10          # 最大重试次数
RETRY_INTERVAL = 2 * 60   # 重试间隔（秒）= 2 分钟
CONFIRM_RETRIES = 4       # 启动结果确认次数
CONFIRM_INTERVAL = 15     # 启动后每 15 秒确认一次

# 时区：北京时间 UTC+8
TZ_BEIJING = timezone(timedelta(hours=8))

# HTTP 请求头
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16) AppleWebKit/537.36 Chrome/142.0 Mobile Safari/537.36 MicroMessenger/8.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/wx/indexn.html?openId={OPEN_ID}&areaid={AREA_ID}",
    "Accept": "*/*",
}


class ChargeResult(Enum):
    """充电结果"""
    SUCCESS = "success"           # 成功
    NO_RECORD = "no_record"       # 无断电记录（不需要重试）
    PORT_BUSY = "port_busy"       # 端口被占用（需要重试）
    ERROR = "error"               # 其他错误（需要重试）


class PlatformRequestError(RuntimeError):
    """GitHub Actions 到平台服务器之间的请求失败。"""


def log(message: str):
    """带时间戳的日志"""
    now = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


async def post_platform_api(
    session: aiohttp.ClientSession,
    endpoint: str,
    data: dict,
    operation: str,
) -> dict:
    """调用平台接口，并提供不包含用户凭据的分段诊断信息。"""
    url = f"{BASE_URL}{endpoint}"
    try:
        async with session.post(url, data=data, headers=HEADERS) as resp:
            if resp.status < 200 or resp.status >= 300:
                body = (await resp.text())[:200].replace("\n", " ")
                raise PlatformRequestError(
                    f"GitHub Actions → 平台服务器失败: {operation} "
                    f"HTTP {resp.status}，响应摘要={body!r}"
                )
            try:
                return await resp.json()
            except (aiohttp.ContentTypeError, ValueError) as exc:
                body = (await resp.text())[:200].replace("\n", " ")
                raise PlatformRequestError(
                    f"GitHub Actions → 平台服务器响应异常: {operation} "
                    f"返回内容不是有效 JSON，响应摘要={body!r}"
                ) from exc
    except asyncio.TimeoutError as exc:
        raise PlatformRequestError(
            f"GitHub Actions → 平台服务器超时: {operation}（30 秒内未完成）"
        ) from exc
    except aiohttp.ClientError as exc:
        raise PlatformRequestError(
            f"GitHub Actions → 平台服务器网络错误: {operation}，"
            f"{type(exc).__name__}: {exc}"
        ) from exc


async def get_user_info(session: aiohttp.ClientSession) -> Optional[dict]:
    """获取用户信息"""
    data = {"openId": OPEN_ID, "areaId": AREA_ID}
    result = await post_platform_api(session, "/wxn/getUserInfo", data, "获取用户信息")
    if result.get("success"):
        return result.get("obj")
    log(f"平台业务响应失败: 获取用户信息，消息={result.get('msg')}")
    return None


async def get_charge_log(session: aiohttp.ClientSession, term: str) -> list:
    """获取指定月份的充电历史记录"""
    data = {"employeeid": EMPLOYEE_ID, "term": term}
    result = await post_platform_api(session, "/wxn/getChargeLog", data, "获取充电历史")
    if result.get("success"):
        return result.get("obj", [])
    log(f"平台业务响应失败: 获取充电历史，消息={result.get('msg')}")
    return []


async def get_device_info(session: aiohttp.ClientSession, devaddress: str) -> Optional[dict]:
    """获取设备信息"""
    data = {"areaId": AREA_ID, "devaddress": devaddress}
    result = await post_platform_api(session, "/wxn/getDeviceInfo", data, "获取设备信息")
    if result.get("success"):
        return result.get("obj")
    log(f"平台业务响应失败: 获取设备信息，消息={result.get('msg')}")
    return None


async def begin_charge(
    session: aiohttp.ClientSession,
    devaddress: str,
    port: str,
    money: int,
    device_info: dict,
) -> dict:
    """启动充电（两步调用）"""
    params = {
        "devaddress": devaddress,
        "port": port,
        "money": money,
        "areaId": AREA_ID,
        "openId": OPEN_ID,
        "beforemoney": money,
        "devtypeid": device_info.get("devtypeid", 40),
        "fullStop": 0,
        "payType": 1,
        "safeOpen": 0,
        "safeCharge": device_info.get("safeCharge", 9),
        "edtType": 0,
        "efee": device_info.get("efee", 110),
        "eCharge": device_info.get("eCharge", 55),
        "serviceCharge": device_info.get("serviceCharge", 55),
        "userId": 0,
        "yuan7": 0,
    }

    # 第一次调用 - 获取 msgflag
    result1 = await post_platform_api(
        session, "/wxn/beginCharge", params, "启动充电第一步"
    )

    if not result1.get("success"):
        return {"success": False, "msg": f"第一步失败: {result1.get('msg')}"}

    msgflag = result1.get("obj")
    if not msgflag:
        return {"success": False, "msg": "未获取到 msgflag"}

    # 第二次调用 - 带 msgflag 确认
    params["msgflag"] = msgflag
    return await post_platform_api(
        session, "/wxn/beginCharge", params, "启动充电第二步（设备确认）"
    )


def find_power_off_record(logs: list) -> Optional[dict]:
    """
    查找断电记录

    条件：
    1. endtype = 39（断电结束）
    2. 结束时间在【昨天 23:45 - 今天 00:15】之间
    3. 必须是昨天/今天的记录，不能是更早的
    """
    now = datetime.now(TZ_BEIJING)
    today = now.date()
    yesterday = today - timedelta(days=1)

    # 构造精确的时间窗口（包含日期）
    window_start = datetime(
        yesterday.year, yesterday.month, yesterday.day,
        POWER_OFF_WINDOW_START_HOUR, POWER_OFF_WINDOW_START_MINUTE,
        tzinfo=TZ_BEIJING
    )
    window_end = datetime(
        today.year, today.month, today.day,
        POWER_OFF_WINDOW_END_HOUR, POWER_OFF_WINDOW_END_MINUTE,
        tzinfo=TZ_BEIJING
    )

    log(f"检测时间窗口: {window_start.strftime('%Y-%m-%d %H:%M')} ~ {window_end.strftime('%Y-%m-%d %H:%M')}")

    for record in logs:
        endtype = record.get("endtype")
        enddt = record.get("enddt")

        if endtype != POWER_OFF_END_TYPE:
            continue

        if enddt is None:
            continue

        # enddt 是毫秒时间戳
        end_time = datetime.fromtimestamp(enddt / 1000, tz=TZ_BEIJING)

        # 关键检查：结束时间必须在精确的时间窗口内（包含日期）
        if window_start <= end_time <= window_end:
            log(f"找到断电记录: 设备={record.get('devaddress')}, 端口={record.get('devport')}, 结束时间={end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            return record

    return None


def is_port_free(portstatur: str, port: str) -> bool:
    """检查端口是否空闲"""
    try:
        # API 的端口号从 01 开始，状态字符串的下标从 0 开始。
        port_index = int(port) - 1
        if port_index < 0 or port_index >= len(portstatur):
            return False
        return portstatur[port_index] == "0"
    except (ValueError, IndexError):
        return False


def get_port_status(portstatur: str, port: str) -> Optional[str]:
    """返回指定端口状态；端口号无效时返回 None。"""
    try:
        port_index = int(port) - 1
        if port_index < 0 or port_index >= len(portstatur):
            return None
        return portstatur[port_index]
    except (TypeError, ValueError, IndexError):
        return None


async def confirm_charge_started(
    session: aiohttp.ClientSession,
    devaddress: str,
    port: str,
) -> bool:
    """轮询设备状态，处理设备已启动但接口确认超时的情况。"""
    for check in range(1, CONFIRM_RETRIES + 1):
        log(f"等待 {CONFIRM_INTERVAL} 秒后确认启动状态 ({check}/{CONFIRM_RETRIES})...")
        await asyncio.sleep(CONFIRM_INTERVAL)

        device_info = await get_device_info(session, devaddress)
        if not device_info:
            log("确认启动状态时未获取到设备信息")
            continue

        portstatur = device_info.get("portstatur", "")
        status = get_port_status(portstatur, port)
        log(f"确认端口状态: {portstatur}（端口 {port}={status}）")
        if status == "1":
            return True

    return False


async def try_charge(session: aiohttp.ClientSession) -> Tuple[ChargeResult, str]:
    """
    尝试充电

    Returns:
        (ChargeResult, message)
    """
    try:
        # 1. 获取用户信息
        log("获取用户信息...")
        user_info = await get_user_info(session)
        if not user_info:
            return ChargeResult.ERROR, "获取用户信息失败"

        balance = user_info.get("readyaccountmoney", 0)
        log(f"当前余额: {balance / 100:.2f} 元")

        if balance < 100:
            return ChargeResult.ERROR, "余额不足 1 元"

        # 2. 获取充电历史
        now = datetime.now(TZ_BEIJING)
        logs = []

        term_this = now.strftime("%Y%m")
        log(f"获取 {term_this} 充电历史...")
        logs.extend(await get_charge_log(session, term_this))

        if now.day <= 3:
            last_month = now.replace(day=1) - timedelta(days=1)
            term_last = last_month.strftime("%Y%m")
            log(f"获取 {term_last} 充电历史...")
            logs.extend(await get_charge_log(session, term_last))

        if not logs:
            return ChargeResult.NO_RECORD, "无充电历史记录"

        log(f"共获取 {len(logs)} 条充电记录")

        # 3. 查找断电记录
        log("检查断电记录...")
        record = find_power_off_record(logs)
        if not record:
            return ChargeResult.NO_RECORD, "未找到符合条件的断电记录"

        devaddress = str(record.get("devaddress"))
        port = record.get("devport")

        # 4. 获取设备信息
        log(f"获取设备 {devaddress} 信息...")
        device_info = await get_device_info(session, devaddress)
        if not device_info:
            return ChargeResult.ERROR, "获取设备信息失败"

        portstatur = device_info.get("portstatur", "")
        port_status = get_port_status(portstatur, port)
        log(f"端口状态: {portstatur}（端口 {port}={port_status}）")

        if port_status is None:
            return ChargeResult.ERROR, f"端口 {port} 不存在或设备状态格式异常"

        # 5. 检查端口是否空闲
        if not is_port_free(portstatur, port):
            if port_status == "1":
                # 目标端口来自昨晚的断电记录，插头仍在该端口时，使用中表示
                # 先前的启动请求已经生效。将其视为成功可保证重复运行安全。
                return ChargeResult.SUCCESS, f"设备={devaddress}, 端口={port} 已在充电"
            return ChargeResult.PORT_BUSY, f"端口 {port} 状态异常（状态码 {port_status}）"

        log(f"端口 {port} 空闲，准备充电")

        # 6. 启动充电
        log(f"启动充电: 设备={devaddress}, 端口={port}, 金额={balance / 100:.2f}元")
        result = await begin_charge(session, devaddress, port, balance, device_info)

        if result.get("success"):
            return ChargeResult.SUCCESS, f"设备={devaddress}, 端口={port}, 金额={balance / 100:.2f}元"

        # 设备可能已经收到命令，只是平台等待确认时超时。先查询实际端口
        # 状态，再决定是否重试，避免重复创建充电订单。
        error_message = result.get("msg") or "未知错误"
        if "设备无响应" in error_message:
            log(f"平台服务器 → 充电桩设备通信失败: {error_message}")
        else:
            log(f"平台业务响应失败: 启动充电，消息={error_message}")
        if await confirm_charge_started(session, devaddress, port):
            return ChargeResult.SUCCESS, f"设备={devaddress}, 端口={port} 已启动（延迟确认）"
        return ChargeResult.ERROR, f"充电启动失败且状态确认未通过: {error_message}"

    except PlatformRequestError as e:
        return ChargeResult.ERROR, str(e)
    except Exception as e:
        return ChargeResult.ERROR, f"脚本内部异常: {type(e).__name__}: {e}"


async def main():
    log("=" * 50)
    log("Neptune 自动充电脚本启动")
    log(f"重试策略: 最多 {MAX_RETRIES} 次，间隔 {RETRY_INTERVAL // 60} 分钟")
    log("=" * 50)

    # 验证配置
    config_errors = validate_config()
    if config_errors:
        for err in config_errors:
            log(f"配置错误: {err}")
        log("请检查 .env 文件配置")
        return 1

    timeout = aiohttp.ClientTimeout(total=30)

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"\n--- 第 {attempt}/{MAX_RETRIES} 次尝试 ---")

        async with aiohttp.ClientSession(timeout=timeout) as session:
            result, message = await try_charge(session)

        if result == ChargeResult.SUCCESS:
            log("=" * 50)
            log("充电启动成功！")
            log(f"  {message}")
            log(f"  最长时间: {MAX_CHARGE_TIME} 分钟")
            log("=" * 50)
            return 0

        elif result == ChargeResult.NO_RECORD:
            log(f"结果: {message}")
            log("无需恢复充电，退出")
            return 0

        elif result in (ChargeResult.PORT_BUSY, ChargeResult.ERROR):
            log(f"结果: {message}")

            if attempt < MAX_RETRIES:
                log(f"将在 {RETRY_INTERVAL // 60} 分钟后重试...")
                await asyncio.sleep(RETRY_INTERVAL)
            else:
                log("已达到最大重试次数，退出")

    log("=" * 50)
    log("所有重试均失败")
    log("=" * 50)
    return 1


if __name__ == "__main__":
    # 修复 Windows 终端编码
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    sys.exit(asyncio.run(main()))
