"""
市场筛选器工具 — scan_market + search_by_price + list_presets

通过 MCP 暴露 TvScreenerService，让 AI 助手可以扫描和筛选市场。
"""

from src.services.tv_screener import TvScreenerService


_service: TvScreenerService | None = None


def _get_service() -> TvScreenerService:
    global _service
    if _service is None:
        _service = TvScreenerService()
    return _service


async def scan_market(
    market: str = "america",
    preset: str = "",
    order_by: str = "",
    order_ascending: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    市场扫描与排行

    支持 9 种预置筛选方案和自定义排序，覆盖股票/加密货币/外汇/期货等市场。

    Args:
        market: 市场，支持中英文
                美股="america", 加密货币="crypto", 外汇="forex",
                期货="futures", A股="china", 港股="hongkong",
                日本="japan", 印度="india", 等
        preset: 预置方案（留空则按默认排序列出）:
                "top_gainers"      — 涨幅最大
                "top_losers"       — 跌幅最大
                "most_active"       — 最活跃（成交量最大）
                "overbought"        — RSI 超买 (>70)
                "oversold"          — RSI 超卖 (<30)
                "high_volume"       — 放量品种（相对成交量最高）
                "large_cap"         — 大市值
                "bullish_ma"        — 均线多头排列
                "breakout_52w_high" — 突破52周新高
        order_by: 自定义排序字段（如 "change"、"volume"、"RSI"）
        order_ascending: 是否升序（默认降序）
        limit: 返回数量（1-500），默认 50
        offset: 偏移量（分页用）
    """
    service = _get_service()
    preset_arg = preset if preset else None

    try:
        return service.scan(
            market=market,
            preset=preset_arg,
            order_by=order_by if order_by else None,
            order_ascending=order_ascending,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        return {"error": True, "message": f"扫描失败: {str(e)}"}


async def search_by_price(
    market: str = "america",
    min_price: float = 0,
    max_price: float = 0,
    min_volume: int = 0,
    min_market_cap: float = 0,
    order_by: str = "volume",
    order_ascending: bool = False,
    limit: int = 50,
) -> dict:
    """
    按价格/成交量/市值筛选品种

    精确筛选满足价格区间、最低成交量、最低市值条件的品种。

    Args:
        market: 市场，如 "america"、"crypto"、"china"
        min_price: 最低价格（0 表示不限制）
        max_price: 最高价格（0 表示不限制）
        min_volume: 最低成交量（0 表示不限制）
        min_market_cap: 最低市值（0 表示不限制）
        order_by: 排序字段，默认 "volume"
        order_ascending: 是否升序
        limit: 返回数量

    使用示例：
    - 美股 10-100 美元: min_price=10, max_price=100
    - 市值超 1000 亿: min_market_cap=100000000000
    - 低价放量: min_price=1, max_price=10, min_volume=1000000
    """
    service = _get_service()
    try:
        return service.search_by_price(
            market=market,
            min_price=min_price if min_price > 0 else None,
            max_price=max_price if max_price > 0 else None,
            min_volume=min_volume if min_volume > 0 else None,
            min_market_cap=min_market_cap if min_market_cap > 0 else None,
            order_by=order_by,
            order_ascending=order_ascending,
            limit=limit,
        )
    except Exception as e:
        return {"error": True, "message": f"筛选失败: {str(e)}"}


async def list_presets() -> dict:
    """列出所有预置筛选方案，返回 9 种可用方案名称和说明。"""
    service = _get_service()
    return service.list_presets()


async def list_markets() -> dict:
    """列出所有支持的市场，返回中英文市场名称对照表。"""
    service = _get_service()
    return service.list_markets()
