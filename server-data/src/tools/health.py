"""
健康检查工具 — 验证 Server 和依赖库状态
"""

import sys
import platform
from importlib.metadata import version as get_version


def check_dependency(package_name: str) -> dict:
    """检查单个依赖库是否可用，返回状态信息"""
    try:
        ver = get_version(package_name)
        return {
            "package": package_name,
            "status": "available",
            "version": ver,
        }
    except Exception:
        return {
            "package": package_name,
            "status": "unavailable",
            "version": None,
        }


def run_health_check() -> dict:
    """
    执行完整健康检查：
    1. Python 环境信息
    2. 核心依赖库状态
    3. 网络连通性（后续扩展）
    """
    dependencies = [
        "mcp",
        "tvdatafeed-enhanced",
        "tradingview_ta",
        "tradingview_screener",
        "pandas",
        "websocket-client",
    ]

    dep_status = [check_dependency(dep) for dep in dependencies]

    all_available = all(d["status"] == "available" for d in dep_status)

    return {
        "status": "ok" if all_available else "degraded",
        "server": "tradingview-data",
        "version": "1.0.0",
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
        },
        "dependencies": dep_status,
        "message": (
            "所有依赖就绪" if all_available
            else "部分依赖不可用，部分功能可能受限"
        ),
    }
