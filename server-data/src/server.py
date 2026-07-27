"""
TradingView 数据 MCP Server 入口

基于 FastMCP v2，通过 stdio 协议向 Cursor/Claude 等 MCP 客户端
暴露行情数据、技术指标、市场筛选器等工具。

版本: v1.0.0 — 全部核心功能完成
"""

import asyncio
from mcp.server.fastmcp import FastMCP

from src.tools.health import run_health_check
from src.tools.market_data import get_historical_data, search_symbol
from src.tools.indicators import (
    get_technical_indicators, get_indicator_summary,
    multi_timeframe_analysis, compare_symbols,
)
from src.tools.screener import scan_market, search_by_price, list_presets, list_markets


mcp = FastMCP(
    name="tradingview-data",
    instructions=(
        "TradingView 数据 MCP Server v1.0 — "
        "提供历史K线、技术指标、市场筛选器、多周期分析、品种对比等功能。"
        "支持股票、加密货币、外汇、期货等多市场数据。"
    ),
)


# ============================================================
# 健康检查
# ============================================================

@mcp.tool()
async def health_check() -> dict:
    """服务健康检查 — 返回 Server 状态和所有依赖库可用性。"""
    return run_health_check()


# ============================================================
# 行情数据
# ============================================================

@mcp.tool()
async def get_historical_data_tool(
    symbol: str,
    exchange: str = "NASDAQ",
    interval: str = "1d",
    n_bars: int = 100,
    extended_session: bool = False,
) -> dict:
    """
    获取历史K线数据（OHLCV），最多 5000 根，支持 1分钟到月线。

    常用交易所: NASDAQ/NYSE/BINANCE/SSE/SZSE/HKEX/FX_IDC
    使用示例: AAPL 日线: symbol="AAPL", exchange="NASDAQ", interval="1d"
    """
    return await get_historical_data(
        symbol=symbol, exchange=exchange, interval=interval,
        n_bars=n_bars, extended_session=extended_session,
    )


@mcp.tool()
async def search_symbol_tool(query: str, exchange: str = "") -> dict:
    """搜索品种代码 — 输入名称关键词，返回匹配的品种列表。"""
    return await search_symbol(query=query, exchange=exchange)


# ============================================================
# 技术指标
# ============================================================

@mcp.tool()
async def get_technical_indicators_tool(
    symbol: str, exchange: str = "NASDAQ", interval: str = "1d",
) -> dict:
    """获取完整技术指标分析 — 100+ 指标 + 买卖建议。"""
    return await get_technical_indicators(symbol=symbol, exchange=exchange, interval=interval)


@mcp.tool()
async def get_indicator_summary_tool(
    symbol: str, exchange: str = "NASDAQ", interval: str = "1d",
) -> dict:
    """技术指标精简总结 — 仅返回买卖建议 + 关键指标值。"""
    return await get_indicator_summary(symbol=symbol, exchange=exchange, interval=interval)


@mcp.tool()
async def multi_timeframe_analysis_tool(
    symbol: str,
    exchange: str = "NASDAQ",
    timeframes: str = "1d,4h,1h",
) -> dict:
    """
    多周期综合分析 — 同时获取多个周期技术指标，判断趋势一致性。

    示例: AAPL 日线+4h+1h: symbol="AAPL", exchange="NASDAQ", timeframes="1d,4h,1h"
    """
    return await multi_timeframe_analysis(symbol=symbol, exchange=exchange, timeframes=timeframes)


@mcp.tool()
async def compare_symbols_tool(
    symbols: str,
    exchange: str = "NASDAQ",
    interval: str = "1d",
) -> dict:
    """
    多品种技术指标对比 — 横向对比并按综合评分排序。

    示例: 美股科技五巨头: symbols="AAPL,TSLA,MSFT,GOOGL,NVDA"
    """
    return await compare_symbols(symbols=symbols, exchange=exchange, interval=interval)


# ============================================================
# 市场筛选器
# ============================================================

@mcp.tool()
async def scan_market_tool(
    market: str = "america",
    preset: str = "",
    order_by: str = "",
    order_ascending: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    市场扫描与排行 — 9 种预置方案，覆盖全球主要市场。

    预置方案: top_gainers/top_losers/most_active/overbought/oversold/
              high_volume/large_cap/bullish_ma/breakout_52w_high
    市场: 美股/crypto/加密货币/forex/外汇/china/A股/hongkong/港股/japan/india 等
    示例: 美股涨幅前50: market="美股", preset="top_gainers"
    """
    return await scan_market(
        market=market, preset=preset, order_by=order_by,
        order_ascending=order_ascending, limit=limit, offset=offset,
    )


@mcp.tool()
async def search_by_price_tool(
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
    按价格/成交量/市值筛选品种。

    示例: 美股 10-100 美元: min_price=10, max_price=100
          市值超千亿: min_market_cap=100000000000
    """
    return await search_by_price(
        market=market, min_price=min_price, max_price=max_price,
        min_volume=min_volume, min_market_cap=min_market_cap,
        order_by=order_by, order_ascending=order_ascending, limit=limit,
    )


@mcp.tool()
async def list_presets_tool() -> dict:
    """列出所有预置筛选方案及其说明。"""
    return await list_presets()


@mcp.tool()
async def list_markets_tool() -> dict:
    """列出所有支持的市场（中英文对照）。"""
    return await list_markets()


# ============================================================
# 入口
# ============================================================

def main():
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
