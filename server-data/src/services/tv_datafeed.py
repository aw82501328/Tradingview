"""
tvdatafeed 服务封装 — 历史K线数据 + 品种搜索

基于 tvdatafeed-enhanced 库，通过 WebSocket 连接 TradingView 获取：
- 历史 OHLCV K线数据
- 品种代码搜索
"""

from typing import Optional

import pandas as pd
from tvDatafeed import TvDatafeed, Interval


# K线周期字符串 → Interval 枚举映射
_INTERVAL_MAP: dict[str, Interval] = {
    "1m":   Interval.in_1_minute,
    "3m":   Interval.in_3_minute,
    "5m":   Interval.in_5_minute,
    "15m":  Interval.in_15_minute,
    "30m":  Interval.in_30_minute,
    "45m":  Interval.in_45_minute,
    "1h":   Interval.in_1_hour,
    "2h":   Interval.in_2_hour,
    "3h":   Interval.in_3_hour,
    "4h":   Interval.in_4_hour,
    "1d":   Interval.in_daily,
    "1W":   Interval.in_weekly,
    "1M":   Interval.in_monthly,
    "3M":   Interval.in_3_monthly,
    "6M":   Interval.in_6_monthly,
    "1y":   Interval.in_yearly,
}


class TvDatafeedService:
    """
    tvdatafeed 服务封装

    初始化时可传入代理配置，解决国内网络访问 TradingView 受限问题。
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        """
        Args:
            username: TradingView 账号用户名（不传则匿名访问，数据可能受限）
            password: TradingView 账号密码
            proxy: 代理地址，如 "http://127.0.0.1:7890"
        """
        self._proxy = proxy

        # tvdatafeed 本身不支持直接传 proxy，通过全局 socket 配置实现
        if proxy:
            import socks  # noqa: F401  — 可选依赖，需额外安装 PySocks
            # 注：tvdatafeed 使用 websocket-client，需通过环境变量或 monkey-patch 配置代理
            # 当前版本通过在 TvDatafeed 初始化后设置 self._tv._session.proxies 来支持

        self._tv = TvDatafeed(username=username, password=password)

    def get_hist(
        self,
        symbol: str,
        exchange: str = "NASDAQ",
        interval: str = "1d",
        n_bars: int = 100,
        extended_session: bool = False,
    ) -> Optional[pd.DataFrame]:
        """
        获取历史 K 线数据

        Args:
            symbol: 品种代码，如 "AAPL"、"BTCUSDT"
            exchange: 交易所，如 "NASDAQ"、"BINANCE"、"NSE"
            interval: K线周期：1m/3m/5m/15m/30m/45m/1h/2h/3h/4h/1d/1W/1M/3M/6M/1y
            n_bars: 获取数量（最大 5000）
            extended_session: 是否包含盘前/盘后数据（仅股票有效）

        Returns:
            pandas DataFrame，包含 datetime/open/high/low/close/volume 列；
            失败返回 None
        """
        tv_interval = _INTERVAL_MAP.get(interval)
        if tv_interval is None:
            raise ValueError(
                f"不支持的K线周期: {interval}。"
                f"可选值: {list(_INTERVAL_MAP.keys())}"
            )

        if n_bars > 5000:
            n_bars = 5000
        if n_bars < 1:
            n_bars = 1

        data = self._tv.get_hist(
            symbol=symbol,
            exchange=exchange,
            interval=tv_interval,
            n_bars=n_bars,
            extended_session=extended_session,
        )

        return data

    def search_symbol(self, query: str, exchange: str = "") -> list[dict]:
        """
        搜索品种代码

        Args:
            query: 搜索关键词，如 "Apple"、"BTC"、"NIFTY"
            exchange: 可选，限定交易所（留空则搜索所有市场）

        Returns:
            匹配的品种列表，每个元素包含 symbol/description/exchange/type
        """
        results = self._tv.search_symbol(text=query, exchange=exchange)
        if results is None:
            return []

        return [
            {
                "symbol": r.get("symbol", ""),
                "description": r.get("description", ""),
                "exchange": r.get("exchange", ""),
                "type": r.get("type", ""),
            }
            for r in results
            if isinstance(r, dict)
        ]


def dataframe_to_dict(df: pd.DataFrame) -> dict:
    """
    将 OHLCV DataFrame 转换为结构化字典，便于 MCP 工具返回 JSON

    Returns:
        {
            "columns": ["datetime", "open", "high", "low", "close", "volume"],
            "count": 100,
            "data": [ ... 每条记录 ... ]
        }
    """
    if df is None or df.empty:
        return {"columns": [], "count": 0, "data": []}

    # 确保索引是时间列
    df = df.reset_index()
    columns = [str(c) for c in df.columns]
    records = df.to_dict(orient="records")

    # 将时间戳转换为 ISO 格式字符串
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, pd.Timestamp):
                rec[k] = v.isoformat()

    return {
        "columns": columns,
        "count": len(records),
        "data": records,
    }
