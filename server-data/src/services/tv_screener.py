"""
tradingview-screener 服务封装 — 市场筛选器

基于 tradingview-screener 库，通过 HTTP 调用 TradingView Scanner API，
实现市场扫描、品种筛选、排行等功能。
"""

from typing import Optional

import pandas as pd
from tradingview_screener import Query, Column, col


# 预置筛选方案：常用筛选条件组合
PRESET_SCANS: dict[str, dict] = {
    "top_gainers": {
        "label": "涨幅最大",
        "order_by": "change",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "change", "volume", "relative_volume_10d_calc"],
        "description": "涨幅最大的品种",
    },
    "top_losers": {
        "label": "跌幅最大",
        "order_by": "change",
        "order_ascending": True,
        "limit": 50,
        "columns": ["name", "close", "change", "volume", "relative_volume_10d_calc"],
        "description": "跌幅最大的品种",
    },
    "most_active": {
        "label": "最活跃",
        "order_by": "volume",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "change", "volume", "relative_volume_10d_calc"],
        "description": "成交量最大的品种",
    },
    "overbought": {
        "label": "RSI 超买 (>70)",
        "order_by": "RSI",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "RSI", "change", "volume"],
        "description": "RSI 高于 70（超买）的品种",
        "filters": [(Column("RSI") > 70)],
    },
    "oversold": {
        "label": "RSI 超卖 (<30)",
        "order_by": "RSI",
        "order_ascending": True,
        "limit": 50,
        "columns": ["name", "close", "RSI", "change", "volume"],
        "description": "RSI 低于 30（超卖）的品种",
        "filters": [(Column("RSI") < 30)],
    },
    "high_volume": {
        "label": "放量品种",
        "order_by": "relative_volume_10d_calc",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "change", "volume", "relative_volume_10d_calc"],
        "description": "相对成交量（10日均）最高的品种",
    },
    "large_cap": {
        "label": "大市值",
        "order_by": "market_cap_basic",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "market_cap_basic", "change", "volume"],
        "description": "市值最大的品种",
    },
    "bullish_ma": {
        "label": "均线多头排列",
        "order_by": "market_cap_basic",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "change", "EMA5", "EMA20", "EMA50"],
        "description": "收盘价站上 EMA5/20/50 的品种",
        "filters": [
            (Column("close") > Column("EMA5")),
            (Column("close") > Column("EMA20")),
            (Column("close") > Column("EMA50")),
        ],
    },
    "breakout_52w_high": {
        "label": "突破52周新高",
        "order_by": "market_cap_basic",
        "order_ascending": False,
        "limit": 50,
        "columns": ["name", "close", "price_52_week_high", "change", "volume"],
        "description": "当前价格接近或突破 52 周新高的品种",
        "filters": [(Column("close") >= Column("price_52_week_high"))],
    },
}

# 市场 → 筛选字段映射
MARKET_MAP: dict[str, str] = {
    "america":     "america",
    "美股":         "america",
    "crypto":      "crypto",
    "加密货币":      "crypto",
    "coin":        "coin",
    "forex":       "forex",
    "外汇":         "forex",
    "cfd":         "cfd",
    "futures":     "futures",
    "期货":         "futures",
    "bond":        "bond",
    "债券":         "bond",
    "china":       "china",
    "A股":          "china",
    "hongkong":    "hongkong",
    "港股":         "hongkong",
    "japan":       "japan",
    "日本":         "japan",
    "uk":          "uk",
    "英国":         "uk",
    "india":       "india",
    "印度":         "india",
    "germany":     "germany",
    "德国":         "germany",
    "australia":   "australia",
    "澳洲":         "australia",
    "canada":      "canada",
    "加拿大":       "canada",
    "korea":       "korea",
    "韩国":         "korea",
    "taiwan":      "taiwan",
    "中国台湾":      "taiwan",
    "brazil":      "brazil",
    "巴西":         "brazil",
    "russia":      "russia",
    "俄罗斯":       "russia",
}


class TvScreenerService:
    """tradingview-screener 服务封装"""

    def __init__(self, timeout: float = 20.0):
        self._timeout = timeout

    def _resolve_market(self, market: str) -> str:
        """将中文/简写市场名解析为 tradingview-screener 的 market 代码"""
        return MARKET_MAP.get(market, market)

    def _build_query(
        self,
        market: str = "america",
        columns: Optional[list[str]] = None,
        filters: Optional[list] = None,
        order_by: Optional[str] = None,
        order_ascending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> Query:
        """构建 Query 对象"""
        q = Query(self._resolve_market(market))

        if columns:
            q.select(*columns)

        if filters:
            q.where(*filters)

        if order_by:
            q.order_by(order_by, ascending=order_ascending)

        q.limit(limit)
        if offset > 0:
            q.offset(offset)

        return q

    def scan(
        self,
        market: str = "america",
        preset: Optional[str] = None,
        columns: Optional[list[str]] = None,
        order_by: Optional[str] = None,
        order_ascending: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        市场扫描
        """
        market_code = self._resolve_market(market)

        if preset and preset in PRESET_SCANS:
            p = PRESET_SCANS[preset]
            columns = columns or p["columns"]
            filters = p.get("filters")
            order_by = order_by or p["order_by"]
            order_ascending = p["order_ascending"]
            limit = limit if limit != 50 else p["limit"]
        else:
            filters = None
            if columns is None:
                columns = ["name", "close", "change", "volume", "market_cap_basic"]

        q = self._build_query(
            market=market_code,
            columns=columns,
            filters=filters,
            order_by=order_by,
            order_ascending=order_ascending,
            limit=limit,
            offset=offset,
        )

        try:
            total_count, df = q.get_scanner_data(timeout=self._timeout)
        except Exception as e:
            return {
                "error": True,
                "message": f"扫描失败: {str(e)}",
                "market": market,
            }

        if df.empty:
            return {
                "market": market,
                "preset": preset,
                "total_count": 0,
                "count": 0,
                "columns": [],
                "data": [],
            }

        records = df.to_dict(orient="records")

        return {
            "market": market,
            "preset": preset,
            "total_count": total_count,
            "count": len(records),
            "columns": [str(c) for c in df.columns],
            "data": records,
        }

    def search_by_price(
        self,
        market: str = "america",
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        min_volume: Optional[int] = None,
        min_market_cap: Optional[float] = None,
        order_by: str = "volume",
        order_ascending: bool = False,
        limit: int = 50,
    ) -> dict:
        """
        按价格/成交量/市值筛选品种
        """
        market_code = self._resolve_market(market)
        columns = ["name", "close", "change", "volume", "market_cap_basic"]
        filters = []

        if min_price is not None:
            filters.append(Column("close") >= min_price)
        if max_price is not None:
            filters.append(Column("close") <= max_price)
        if min_volume is not None:
            filters.append(Column("volume") >= int(min_volume))
        if min_market_cap is not None:
            filters.append(Column("market_cap_basic") >= float(min_market_cap))

        q = self._build_query(
            market=market_code,
            columns=columns,
            filters=filters if filters else None,
            order_by=order_by,
            order_ascending=order_ascending,
            limit=limit,
        )

        try:
            total_count, df = q.get_scanner_data(timeout=self._timeout)
        except Exception as e:
            return {"error": True, "message": f"查询失败: {str(e)}"}

        if df.empty:
            return {
                "market": market,
                "total_count": 0,
                "count": 0,
                "columns": [],
                "data": [],
                "filters": {
                    "min_price": min_price, "max_price": max_price,
                    "min_volume": min_volume, "min_market_cap": min_market_cap,
                },
            }

        records = df.to_dict(orient="records")
        return {
            "market": market,
            "total_count": total_count,
            "count": len(records),
            "columns": [str(c) for c in df.columns],
            "data": records,
            "filters": {
                "min_price": min_price, "max_price": max_price,
                "min_volume": min_volume, "min_market_cap": min_market_cap,
            },
        }

    def list_presets(self) -> dict:
        """列出所有预置筛选方案"""
        presets = {}
        for key, p in PRESET_SCANS.items():
            presets[key] = {
                "label": p["label"],
                "description": p["description"],
            }
        return {"count": len(presets), "presets": presets}

    def list_markets(self) -> dict:
        """列出所有支持的市场"""
        markets = {}
        for key, value in MARKET_MAP.items():
            if key == value:
                markets[key] = value
        return {"count": len(markets), "markets": markets}
