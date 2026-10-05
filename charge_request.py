"""构造 Neptune beginCharge 请求参数。"""

from typing import Union

BALANCE_MAX_CHARGE_OPTION = 7


def build_charge_params(
    devaddress: str,
    port: Union[str, int],
    beforemoney: int,
    device_info: dict,
    area_id: int,
    open_id: str,
    charge_money: int = BALANCE_MAX_CHARGE_OPTION,
) -> dict:
    """构造启动参数，保留真实物理端口号和账户余额。"""
    return {
        "devaddress": devaddress, "port": port, "money": charge_money,
        "areaId": area_id, "openId": open_id, "beforemoney": beforemoney,
        "devtypeid": device_info.get("devtypeid", 40), "fullStop": 0,
        "payType": 1, "safeOpen": 0,
        "safeCharge": device_info.get("safeCharge", 9), "edtType": 0,
        "efee": device_info.get("efee", 110),
        "eCharge": device_info.get("eCharge", 55),
        "serviceCharge": device_info.get("serviceCharge", 55),
        "userId": 0, "yuan7": 0,
    }
