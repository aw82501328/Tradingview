"""
全局配置管理 — 交易所映射、默认参数、支持的市场列表
"""

# 交易所名称映射表（TradingView 内部名称 → 常用名称）
EXCHANGE_MAP = {
    # 美股
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "AMEX": "AMEX",
    # A股
    "SSE": "SSE",        # 上海证券交易所
    "SZSE": "SZSE",      # 深圳证券交易所
    # 港股
    "HKEX": "HKEX",
    # 加密货币
    "BINANCE": "BINANCE",
    "COINBASE": "COINBASE",
    "BYBIT": "BYBIT",
    "KUCOIN": "KUCOIN",
    "BINANCEUS": "BINANCEUS",
    # 外汇
    "FX": "FX_IDC",
    "OANDA": "OANDA",
    # 期货
    "CME": "CME_MINI",
    "NYMEX": "NYMEX",
    "CBOT": "CBOT",
    # 印度
    "NSE": "NSE",
    "BSE": "BSE",
}

# tvdatafeed 支持的 K 线周期枚举
INTERVALS = [
    "1m", "3m", "5m", "15m", "30m",
    "45m",
    "1h", "2h", "3h", "4h",
    "1d", "1W", "1M",
]

# tradingview-ta 支持的 screener 市场列表
SCREENER_MARKETS = [
    "america",      # 美股
    "crypto",       # 加密货币
    "forex",        # 外汇
    "cfd",          # 差价合约
    "china",        # A股
    "india",        # 印度
    "japan",        # 日本
    "uk",           # 英国
    "germany",      # 德国
    "australia",    # 澳洲
    "brazil",       # 巴西
    "canada",       # 加拿大
    "europe",       # 欧洲
    "korea",        # 韩国
    "hongkong",     # 港股
    "russia",       # 俄罗斯
    "taiwan",       # 中国台湾
]

# 默认参数
DEFAULT_EXCHANGE = "NASDAQ"
DEFAULT_INTERVAL = "1d"
DEFAULT_N_BARS = 100
MAX_N_BARS = 5000
