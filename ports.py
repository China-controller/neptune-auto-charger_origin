"""充电桩物理端口与状态字符串之间的映射。"""

from typing import Optional, Union


def get_port_status(portstatur: str, port: Union[str, int]) -> Optional[str]:
    """返回物理端口状态；端口号无效时返回 ``None``。"""
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if not isinstance(portstatur, str) or not 1 <= port_number <= len(portstatur):
        return None
    return portstatur[port_number - 1]


def is_port_free(portstatur: str, port: Union[str, int]) -> bool:
    return get_port_status(portstatur, port) == "0"
