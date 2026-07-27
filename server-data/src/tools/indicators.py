"""
技术指标工具 — get_technical_indicators + get_indicator_summary

通过 MCP 工具暴露 TvIndicatorsService，
让 AI 助手可以查询 TradingView 的 100+ 技术指标和买卖建议。
"""

from src.services.tv_indicators import TvIndicatorsService, RECOMMENDATION_CN
from src.utils.config import DEFAULT_EXCHANGE, DEFAULT_INTERVAL, INTERVALS


# 全局服务实例（延迟初始化）
_service: TvIndicatorsService | None = None


def _get_service() -> TvIndicatorsService:
    """获取 TvIndicatorsService 单例"""
    global _service
    if _service is None:
        _service = TvIndicatorsService()
    return _service


async def get_technical_indicators(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    interval: str = DEFAULT_INTERVAL,
    indicators: list[str] | None = None,
) -> dict:
    """
    获取完整技术指标分析

    返回 TradingView 上 100+ 技术指标的计算结果，
    包括震荡指标(RSI/MACD/Stoch/CCI/ADX等)、
    移动平均线(EMA/SMA/VWMA/HullMA/Ichimoku等)、
    以及综合买卖建议总结。

    Args:
        symbol: 品种代码，如 "AAPL"、"TSLA"、"BTCUSDT"
        exchange: 交易所，如 "NASDAQ"、"BINANCE"、"SSE"
        interval: K线周期：1m/5m/15m/30m/1h/2h/4h/1d/1W/1M
        indicators: 可选，指定需要的指标列表。不传则返回全部

    Returns:
        {
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "interval": "1d",
            "summary": {
                "RECOMMENDATION": "BUY",
                "BUY": 14, "SELL": 4, "NEUTRAL": 8
            },
            "oscillators": { ... 震荡指标详情 },
            "moving_averages": { ... 均线指标详情 },
            "indicators": { ... 所有原始指标值 }
        }
    """
    if interval not in INTERVALS:
        return {
            "error": True,
            "message": f"不支持的K线周期: {interval}。可选: {INTERVALS}",
        }

    service = _get_service()
    try:
        result = service.get_analysis(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
        )

        # 将买卖建议翻译为中文
        result["summary"]["RECOMMENDATION_CN"] = RECOMMENDATION_CN.get(
            result["summary"]["RECOMMENDATION"], result["summary"]["RECOMMENDATION"]
        )
        result["oscillators"]["RECOMMENDATION_CN"] = RECOMMENDATION_CN.get(
            result["oscillators"]["RECOMMENDATION"], result["oscillators"]["RECOMMENDATION"]
        )
        result["moving_averages"]["RECOMMENDATION_CN"] = RECOMMENDATION_CN.get(
            result["moving_averages"]["RECOMMENDATION"], result["moving_averages"]["RECOMMENDATION"]
        )

        return result

    except ValueError as e:
        return {"error": True, "message": str(e)}
    except Exception as e:
        return {
            "error": True,
            "message": f"获取技术指标失败: {str(e)}",
        }


async def get_indicator_summary(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    interval: str = DEFAULT_INTERVAL,
) -> dict:
    """
    获取技术指标精简总结

    只返回买卖建议和关键指标摘要，
    比 get_technical_indicators 更简洁。

    Args:
        symbol: 品种代码
        exchange: 交易所
        interval: K线周期

    Returns:
        {
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "interval": "1d",
            "recommendation": "买入",
            "summary": {"BUY": 14, "SELL": 4, "NEUTRAL": 8},
            "oscillators_recommendation": "中性",
            "moving_averages_recommendation": "强力买入",
            "key_indicators": {
                "RSI": 58.2,
                "MACD": 2.1,
                "close": 245.3,
                ...
            }
        }
    """
    if interval not in INTERVALS:
        return {
            "error": True,
            "message": f"不支持的K线周期: {interval}。可选: {INTERVALS}",
        }

    service = _get_service()
    try:
        result = service.get_analysis(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
        )

        # 提取关键指标值（最常用的几个）
        raw = result.get("indicators", {})
        key_indicators = {}
        for key in ["close", "open", "high", "low", "volume",
                     "RSI", "RSI[1]",
                     "MACD.macd", "MACD.signal",
                     "EMA20", "EMA50", "EMA200",
                     "BB.upper", "BB.lower",
                     "ADX", "Stoch.K", "Stoch.D",
                     "CCI20", "AO", "W.R"]:
            if key in raw and raw[key] is not None:
                key_indicators[key] = raw[key]

        return {
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "recommendation": RECOMMENDATION_CN.get(
                result["summary"]["RECOMMENDATION"], result["summary"]["RECOMMENDATION"]
            ),
            "summary": result["summary"],
            "oscillators_recommendation": RECOMMENDATION_CN.get(
                result["oscillators"]["RECOMMENDATION"], result["oscillators"]["RECOMMENDATION"]
            ),
            "moving_averages_recommendation": RECOMMENDATION_CN.get(
                result["moving_averages"]["RECOMMENDATION"], result["moving_averages"]["RECOMMENDATION"]
            ),
            "key_indicators": key_indicators,
        }

    except ValueError as e:
        return {"error": True, "message": str(e)}
    except Exception as e:
        return {
            "error": True,
            "message": f"获取指标摘要失败: {str(e)}",
        }


# ============================================================
# 增强工具 — 多周期分析 + 品种对比
# ============================================================

async def multi_timeframe_analysis(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    timeframes: str = "1d,4h,1h",
) -> dict:
    """
    多周期综合分析

    同时获取多个周期的技术指标，判断跨周期趋势一致性。

    Args:
        symbol: 品种代码，如 "AAPL"、"BTCUSDT"
        exchange: 交易所，如 "NASDAQ"、"BINANCE"
        timeframes: 周期列表，逗号分隔，如 "1d,4h,1h" 或 "1W,1d,4h"

    Returns:
        {
            "symbol": "AAPL",
            "exchange": "NASDAQ",
            "timeframes": {
                "1d": {"recommendation": "买入", "summary": {"BUY": 14, ...}},
                "4h": {"recommendation": "中性", "summary": {...}},
                "1h": {"recommendation": "卖出", "summary": {...}}
            },
            "confluence": {
                "all_agree": false,
                "bullish_count": 1,
                "bearish_count": 1,
                "neutral_count": 1,
                "consensus": "mixed"
            }
        }
    """
    tf_list = [tf.strip() for tf in timeframes.split(",")]
    service = _get_service()

    results = {}
    bullish_count = 0
    bearish_count = 0
    neutral_count = 0

    for tf in tf_list:
        try:
            analysis = service.get_analysis(symbol=symbol, exchange=exchange, interval=tf)
            rec = analysis["summary"].get("RECOMMENDATION", "NEUTRAL")
            rec_cn = RECOMMENDATION_CN.get(rec, rec)

            results[tf] = {
                "recommendation": rec_cn,
                "summary": analysis["summary"],
            }

            if rec in ("STRONG_BUY", "BUY"):
                bullish_count += 1
            elif rec in ("STRONG_SELL", "SELL"):
                bearish_count += 1
            else:
                neutral_count += 1

        except Exception as e:
            results[tf] = {"error": str(e)}

    # 判断一致性
    total = bullish_count + bearish_count + neutral_count
    all_agree = (bullish_count == total) or (bearish_count == total)

    if all_agree and bullish_count == total:
        consensus = "strong_bullish"
    elif all_agree and bearish_count == total:
        consensus = "strong_bearish"
    elif bullish_count > bearish_count and bullish_count > neutral_count:
        consensus = "bullish"
    elif bearish_count > bullish_count and bearish_count > neutral_count:
        consensus = "bearish"
    else:
        consensus = "mixed"

    return {
        "symbol": symbol,
        "exchange": exchange,
        "timeframes": results,
        "confluence": {
            "all_agree": all_agree,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "consensus": consensus,
        },
    }


async def compare_symbols(
    symbols: str,
    exchange: str = DEFAULT_EXCHANGE,
    interval: str = DEFAULT_INTERVAL,
) -> dict:
    """
    多品种技术指标对比

    横向对比多个品种的技术指标，按综合评分排序。

    Args:
        symbols: 品种列表，逗号分隔，如 "AAPL,TSLA,MSFT,GOOGL,NVDA"
        exchange: 交易所，如 "NASDAQ"、"SSE"
        interval: K线周期

    Returns:
        {
            "exchange": "NASDAQ",
            "interval": "1d",
            "ranking": [
                {"symbol": "NVDA", "recommendation": "强力买入", "buy_count": 18, "sell_count": 2},
                {"symbol": "AAPL", "recommendation": "买入", "buy_count": 14, "sell_count": 4},
                ...
            ]
        }
    """
    sym_list = [s.strip() for s in symbols.split(",")]
    service = _get_service()

    results = []
    for sym in sym_list:
        try:
            analysis = service.get_analysis(symbol=sym, exchange=exchange, interval=interval)
            summary = analysis["summary"]
            rec = summary.get("RECOMMENDATION", "NEUTRAL")

            results.append({
                "symbol": sym,
                "exchange": exchange,
                "recommendation": RECOMMENDATION_CN.get(rec, rec),
                "recommendation_raw": rec,
                "buy_count": summary.get("BUY", 0),
                "sell_count": summary.get("SELL", 0),
                "neutral_count": summary.get("NEUTRAL", 0),
                "score": summary.get("BUY", 0) - summary.get("SELL", 0),
                "close": analysis.get("indicators", {}).get("close"),
                "RSI": analysis.get("indicators", {}).get("RSI"),
            })
        except Exception as e:
            results.append({
                "symbol": sym,
                "exchange": exchange,
                "error": str(e),
            })

    # 按评分排序（高到低）
    results.sort(key=lambda x: x.get("score", -999), reverse=True)

    return {
        "exchange": exchange,
        "interval": interval,
        "count": len(results),
        "ranking": results,
    }
