"""
tradingview-ta 服务封装 — 技术指标分析

基于 tradingview-ta 库，通过 HTTP 请求 TradingView Scanner API
获取 100+ 技术指标的计算结果和买卖建议。
"""

from typing import Optional

from tradingview_ta import TA_Handler, Interval, get_multiple_analysis


# tradingview-ta 的 Interval 枚举 → 字符串映射
_INTERVAL_MAP: dict[str, str] = {
    "1m":  Interval.INTERVAL_1_MINUTE,
    "5m":  Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h":  Interval.INTERVAL_1_HOUR,
    "2h":  Interval.INTERVAL_2_HOURS,
    "4h":  Interval.INTERVAL_4_HOURS,
    "1d":  Interval.INTERVAL_1_DAY,
    "1W":  Interval.INTERVAL_1_WEEK,
    "1M":  Interval.INTERVAL_1_MONTH,
}

# 交易所 → Screener 映射表
# tradingview-ta 通过 screener 区分市场类型
_EXCHANGE_TO_SCREENER: dict[str, str] = {
    # 美股
    "NASDAQ":   "america",
    "NYSE":     "america",
    "AMEX":     "america",
    # A股
    "SSE":      "china",
    "SZSE":     "china",
    # 港股
    "HKEX":     "hongkong",
    # 加密货币
    "BINANCE":  "crypto",
    "COINBASE": "crypto",
    "BYBIT":    "crypto",
    "KUCOIN":   "crypto",
    "BINANCEUS":"crypto",
    "BITSTAMP": "crypto",
    "BITFINEX": "crypto",
    "BITTREX":  "crypto",
    # 外汇
    "FX_IDC":   "forex",
    "OANDA":    "forex",
    # CFD
    "TVC":      "cfd",
    # 印度
    "NSE":      "india",
    "BSE":      "india",
    # 其他主要市场
    "TSE":      "japan",      # 东京
    "LSE":      "uk",         # 伦敦
    "XETR":     "germany",    # 德国
    "ASX":      "australia",  # 澳洲
    "TSX":      "canada",     # 加拿大
    "KRX":      "korea",      # 韩国
    "TWSE":     "taiwan",     # 中国台湾
    "MOEX":     "russia",     # 俄罗斯
    "EURONEXT": "europe",     # 泛欧
    "BMFBOVESPA":"brazil",   # 巴西
}


class TvIndicatorsService:
    """tradingview-ta 服务封装"""

    def __init__(self, proxy: Optional[str] = None, timeout: Optional[float] = 15.0):
        """
        Args:
            proxy: 代理地址，如 "http://127.0.0.1:7890"
            timeout: 请求超时时间（秒）
        """
        self._proxy = proxy
        self._timeout = timeout
        self._proxies = {"http": proxy, "https": proxy} if proxy else None

    def _resolve_screener(self, exchange: str) -> str:
        """根据交易所名称推导对应的 screener"""
        screener = _EXCHANGE_TO_SCREENER.get(exchange.upper())
        if screener is None:
            raise ValueError(
                f"无法匹配交易所 '{exchange}' 的 screener 市场类型。"
                f"支持的前30个交易所: {list(_EXCHANGE_TO_SCREENER.keys())[:30]}"
            )
        return screener

    def _resolve_interval(self, interval: str) -> str:
        """将简写周期映射到 tradingview-ta 的 Interval 字符串"""
        tv_interval = _INTERVAL_MAP.get(interval)
        if tv_interval is None:
            raise ValueError(
                f"不支持的周期: {interval}。"
                f"可选: {list(_INTERVAL_MAP.keys())}"
            )
        return tv_interval

    def get_analysis(
        self,
        symbol: str,
        exchange: str = "NASDAQ",
        interval: str = "1d",
    ) -> dict:
        """
        获取单品种完整技术分析

        Args:
            symbol: 品种代码
            exchange: 交易所代码
            interval: K线周期

        Returns:
            {
                "symbol": "AAPL",
                "exchange": "NASDAQ",
                "interval": "1d",
                "summary": {"RECOMMENDATION": "BUY", "BUY": 14, "NEUTRAL": 8, "SELL": 4},
                "oscillators": {"RECOMMENDATION": "NEUTRAL", "BUY": 3, "SELL": 2, "NEUTRAL": 6,
                                "COMPUTE": {"RSI": "BUY", "MACD": "SELL", ...}},
                "moving_averages": {"RECOMMENDATION": "STRONG_BUY", "BUY": 11, "SELL": 2, "NEUTRAL": 2,
                                    "COMPUTE": {"EMA10": "BUY", "SMA50": "BUY", ...}},
                "indicators": {"close": 245.3, "RSI": 58.2, "MACD.macd": 2.1, ...}
            }
        """
        screener = self._resolve_screener(exchange)
        tv_interval = self._resolve_interval(interval)

        handler = TA_Handler(
            symbol=symbol,
            exchange=exchange,
            screener=screener,
            interval=tv_interval,
            timeout=self._timeout,
            proxies=self._proxies,
        )

        analysis = handler.get_analysis()

        return {
            "symbol": symbol,
            "exchange": exchange,
            "screener": screener,
            "interval": interval,
            "summary": {
                "RECOMMENDATION": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
                "BUY": analysis.summary.get("BUY", 0),
                "SELL": analysis.summary.get("SELL", 0),
                "NEUTRAL": analysis.summary.get("NEUTRAL", 0),
            },
            "oscillators": {
                "RECOMMENDATION": analysis.oscillators.get("RECOMMENDATION", "NEUTRAL"),
                "BUY": analysis.oscillators.get("BUY", 0),
                "SELL": analysis.oscillators.get("SELL", 0),
                "NEUTRAL": analysis.oscillators.get("NEUTRAL", 0),
                "COMPUTE": analysis.oscillators.get("COMPUTE", {}),
            },
            "moving_averages": {
                "RECOMMENDATION": analysis.moving_averages.get("RECOMMENDATION", "NEUTRAL"),
                "BUY": analysis.moving_averages.get("BUY", 0),
                "SELL": analysis.moving_averages.get("SELL", 0),
                "NEUTRAL": analysis.moving_averages.get("NEUTRAL", 0),
                "COMPUTE": analysis.moving_averages.get("COMPUTE", {}),
            },
            "indicators": analysis.indicators,
        }

    def get_multiple_analysis(
        self,
        symbols: list[str],
        screener: str = "america",
        interval: str = "1d",
    ) -> dict:
        """
        批量获取多个品种的技术分析

        Args:
            symbols: 品种列表，格式 ["EXCHANGE:SYMBOL", ...]
                     如 ["NASDAQ:AAPL", "NASDAQ:TSLA", "BINANCE:BTCUSDT"]
            screener: 市场类型（所有品种必须是同一市场）
            interval: K线周期

        Returns:
            {"NASDAQ:AAPL": {...分析结果...}, "NASDAQ:TSLA": {...}, ...}
        """
        tv_interval = self._resolve_interval(interval)

        results = get_multiple_analysis(
            screener=screener,
            interval=tv_interval,
            symbols=symbols,
            timeout=self._timeout,
            proxies=self._proxies,
        )

        output = {}
        for key, analysis in results.items():
            if analysis is None:
                output[key] = {"error": True, "message": "无法获取该品种的分析数据"}
                continue

            output[key] = {
                "symbol": analysis.symbol,
                "exchange": analysis.exchange,
                "screener": screener,
                "interval": interval,
                "summary": {
                    "RECOMMENDATION": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
                    "BUY": analysis.summary.get("BUY", 0),
                    "SELL": analysis.summary.get("SELL", 0),
                    "NEUTRAL": analysis.summary.get("NEUTRAL", 0),
                },
            }

        return output


# 买卖建议的中文说明
RECOMMENDATION_CN: dict[str, str] = {
    "STRONG_BUY":  "强力买入",
    "BUY":         "买入",
    "NEUTRAL":     "中性",
    "SELL":        "卖出",
    "STRONG_SELL": "强力卖出",
}
