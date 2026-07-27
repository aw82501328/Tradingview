"""
行情数据工具 — 历史K线 + 品种搜索

通过 MCP 工具暴露 TvDatafeedService 的核心能力，
让 Cursor/Claude 等 AI 助手可以直接查询市场数据。
"""

from src.services.tv_datafeed import TvDatafeedService, dataframe_to_dict
from src.utils.config import DEFAULT_EXCHANGE, DEFAULT_INTERVAL, DEFAULT_N_BARS, MAX_N_BARS, INTERVALS


# 全局服务实例（延迟初始化，避免影响导入）
_service: TvDatafeedService | None = None


def _get_service() -> TvDatafeedService:
    """获取 TvDatafeedService 单例"""
    global _service
    if _service is None:
        _service = TvDatafeedService()
    return _service


async def get_historical_data(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    interval: str = DEFAULT_INTERVAL,
    n_bars: int = DEFAULT_N_BARS,
    extended_session: bool = False,
) -> dict:
    """
    获取历史K线数据（OHLCV）

    Args:
        symbol: 品种代码，如 "AAPL"（苹果）、"TSLA"（特斯拉）、
                "BTCUSDT"（比特币）、"ETHUSDT"（以太坊）、
                "000001"（平安银行）
        exchange: 交易所代码，如 "NASDAQ"、"NYSE"、"BINANCE"、"SSE"
                  默认 "NASDAQ"
        interval: K线周期:
                  "1m"  = 1分钟,  "5m"  = 5分钟,
                  "15m" = 15分钟, "30m" = 30分钟,
                  "1h"  = 1小时,  "4h"  = 4小时,
                  "1d"  = 日线,   "1W"  = 周线,
                  "1M"  = 月线
                  默认 "1d"
        n_bars: 获取K线数量（1-5000），默认 100
        extended_session: 是否包含盘前/盘后数据（仅股票），默认 False

    Returns:
        {
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "interval": "1d",
            "columns": ["datetime", "open", "high", "low", "close", "volume"],
            "count": 100,
            "data": [
                {"datetime": "2026-07-24T00:00:00", "open": 245.3, ... },
                ...
            ]
        }
    """
    if interval not in INTERVALS:
        return {
            "error": True,
            "message": f"不支持的K线周期: {interval}。可选: {INTERVALS}",
        }

    if n_bars > MAX_N_BARS:
        n_bars = MAX_N_BARS
    if n_bars < 1:
        n_bars = 1

    service = _get_service()
    try:
        df = service.get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            n_bars=n_bars,
            extended_session=extended_session,
        )

        if df is None or df.empty:
            return {
                "error": True,
                "message": (
                    f"无法获取 {exchange}:{symbol} 的 {interval} K线数据。"
                    f"可能原因：品种代码或交易所名称不正确、网络连接问题。"
                ),
            }

        result = dataframe_to_dict(df)
        result["symbol"] = symbol
        result["exchange"] = exchange
        result["interval"] = interval
        return result

    except Exception as e:
        return {
            "error": True,
            "message": f"获取数据失败: {str(e)}",
        }


async def search_symbol(query: str, exchange: str = "") -> dict:
    """
    搜索品种代码

    输入品种名称关键词，返回匹配的品种列表（含完整代码和交易所信息）。

    Args:
        query: 搜索关键词，如 "Apple"、"BTC"、"上证"、"NIFTY"
        exchange: 可选，限定交易所。留空则搜索所有市场

    Returns:
        {
            "query": "Apple",
            "count": 5,
            "results": [
                {"symbol": "AAPL", "description": "APPLE INC", "exchange": "NASDAQ", "type": "stock"},
                ...
            ]
        }
    """
    service = _get_service()
    try:
        exchange_arg = exchange if exchange else ""
        results = service.search_symbol(query=query, exchange=exchange_arg)

        return {
            "query": query,
            "count": len(results),
            "results": results,
        }

    except Exception as e:
        return {
            "error": True,
            "message": f"搜索失败: {str(e)}",
        }
