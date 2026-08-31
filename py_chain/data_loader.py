# -*- coding: utf-8 -*-
"""
CDP 数据加载（Python 移植版，与 load_all_tf.js 对齐）

用 websocket-client 直连 TradingView 桌面端 Chrome DevTools Protocol：
  1. GET http://127.0.0.1:9222/json 找到 tradingview.com 页面 target（webSocketDebuggerUrl）
  2. 逐周期 setResolution → scrollToFirstBar → 读 mainSeries().data().m_bars._items
  3. 产出对齐的多周期 { 周期: [{time, open, high, low, close}] }，缓存为 bars_all_tf.json

配置项见 CDPConfig dataclass；CDP 连接封装见 CDPClient。
"""

import json
import time
import urllib.request

from .chan_core import intervalSecOf

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]
DEFAULT_CDP_PORT = 9222
CACHE_FILE = "bars_all_tf.json"


class CDPConfig:
    """CDP 连接与取数配置。"""

    def __init__(self, port=DEFAULT_CDP_PORT, periods=None, host="127.0.0.1"):
        self.host = host
        self.port = port
        self.periods = list(periods or DEFAULT_PERIODS)
        # 每周期两个等待：setResolution 后等图数据刷新、scrollToFirstBar 后等加载
        self.res_wait = 4.0
        self.scroll_wait = 15.0

    @property
    def http_url(self):
        return f"http://{self.host}:{self.port}"

    def __repr__(self):
        return f"CDPConfig(host={self.host!r}, port={self.port}, periods={self.periods!r})"


class CDPError(RuntimeError):
    pass


class CDPClient:
    """极简 CDP 客户端：HTTP 找 target + WS 执行 Runtime.evaluate。"""

    def __init__(self, cfg=None, log=None):
        self.cfg = cfg or CDPConfig()
        self._log = log or (lambda *a, **k: print(*a))
        self._ws = None
        self._msg_id = 0

    # ---------------- 连接 ----------------

    def connect(self):
        """GET /json 找 tradingview.com 页面，建立 WebSocket 连接。"""
        ws_url = self._find_target()
        import websocket  # 延迟导入，websocket-client
        self._ws = websocket.create_connection(ws_url, timeout=60)
        self._msg_id = 0
        self._log(f"已连接 CDP：{ws_url}")

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _find_target(self):
        with urllib.request.urlopen(f"{self.cfg.http_url}/json", timeout=10) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
        for t in targets:
            if t.get("type") == "page" and "tradingview.com" in (t.get("url") or ""):
                ws = t.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        raise CDPError("未找到 tradingview.com 页面 target，请先打开 TradingView 桌面端并启用远程调试")

    # ---------------- 执行 JS ----------------

    def evaluate(self, expression, await_promise=True, timeout=30000):
        """Runtime.evaluate 执行 JS，返回 result.value（value 类型 object 时自动转为 dict）。"""
        if self._ws is None:
            self.connect()
        self._msg_id += 1
        mid = self._msg_id
        self._ws.send(json.dumps({
            "id": mid,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": bool(await_promise),
                "timeout": timeout,
            },
        }))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise CDPError(f"CDP 执行出错：{msg['error']}")
            result = msg.get("result", {})
            if result.get("exceptionDetails"):
                ex = result["exceptionDetails"]
                text = ex.get("exception", {}).get("description") or ex.get("text")
                raise CDPError(f"JS 异常：{text}")
            value = result.get("result", {}).get("value")
            if isinstance(value, (dict, list)) or value is None:
                return value
            return value


# ============================================================
# 取数
# ============================================================


def _set_resolution(c, res):
    """切换周期。"""
    c.evaluate(f"TradingViewApi.activeChart().setResolution({json.dumps(res)});")


def _set_symbol(c, symbol):
    """切换品种（仅当传入 symbol 时调用）。"""
    c.evaluate(f"TradingViewApi.activeChart().setSymbol({json.dumps(symbol)});")


def _scroll_to_first_bar(c):
    """滚动到第一根K线，触发历史数据加载。"""
    expr = (
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); "
        "const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); "
        "ts.scrollToFirstBar(); return 'ok'; })()"
    )
    c.evaluate(expr)


def _read_bars(c):
    """读取当前周期全部已加载K线。"""
    expr = (
        "(function() { const c = TradingViewApi.activeChart(); "
        "const items = c.chartModel().mainSeries().data().m_bars._items; "
        "const out = []; "
        "for (const it of items) { const v = it.value; "
        "out.push({ time: v[0], open: v[1], high: v[2], low: v[3], close: v[4] }); } "
        "return { res: String(c.resolution()), bars: out, total: items.length }; })()"
    )
    return c.evaluate(expr)


def fetch_bars(cfg=None, from_ts=0, cache=True, cache_file=CACHE_FILE, symbol=None):
    """通过 CDP 拉取多周期历史K线。

    @param cfg        CDPConfig（端口、周期列表、等待时长）
    @param from_ts    UTC 时间戳，只保留 >= from_ts 的K线（过滤加载的更多历史）
    @param cache      为 True 时结果写入 cache_file（默认 bars_all_tf.json）
    @param symbol     可选：拉取前先切换图表品种（如 OANDA:XAUUSD）
    @returns { 周期: [{time, open, high, low, close}] }（从大到小排列，时间升序）
    """
    cfg = cfg or CDPConfig()
    log = lambda *a, **k: print(*a)
    data = {}
    with CDPClient(cfg, log=log) as c:
        if symbol:
            _set_symbol(c, symbol)
            time.sleep(cfg.res_wait)
            log(f"已切换品种：{symbol}")
        for res in cfg.periods:
            _set_resolution(c, res)
            time.sleep(cfg.res_wait)
            _scroll_to_first_bar(c)
            time.sleep(cfg.scroll_wait)
            d = _read_bars(c)
            if not d or not d.get("bars"):
                log(f"警告：周期 {res} 未读到K线")
                continue
            bars = [b for b in d["bars"] if b["time"] >= from_ts]
            # 去重 + 时间升序
            seen = set()
            uniq = []
            for b in sorted(bars, key=lambda x: x["time"]):
                if b["time"] in seen:
                    continue
                seen.add(b["time"])
                uniq.append(b)
            data[res] = uniq
            log(f"已加载 {res}：共 {d['total']} 根，保留 {len(uniq)} 根")
    if cache:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        log(f"已缓存到 {cache_file}")
    return data


def load_cached(cache_file=CACHE_FILE):
    """从缓存文件读取历史K线（跳过 CDP）。"""
    with open(cache_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_bars(periods=None, from_ts=0, use_cache=False, cache_file=CACHE_FILE, symbol=None):
    """统一入口：优先读缓存，缓存缺失或 use_cache=False 时走 CDP。"""
    if use_cache:
        try:
            return load_cached(cache_file)
        except (OSError, json.JSONDecodeError):
            pass
    cfg = CDPConfig(periods=periods)
    return fetch_bars(cfg=cfg, from_ts=from_ts, cache=True, cache_file=cache_file, symbol=symbol)


def align_periods(bars_by_period, periods=None):
    """各周期K线对齐到同一结束时间（点状回测时只取 <= 当前时刻的K线）。

    回测引擎按最小周期逐根推进，为减少重复计算，这里仅提供时间截取辅助。
    @param bars_by_period { 周期: [bars] }
    @returns { 周期: [bars] }（确保各周期存在、时间升序）
    """
    periods = periods or DEFAULT_PERIODS
    out = {}
    for res in periods:
        bars = bars_by_period.get(res) or []
        bars = sorted(bars, key=lambda x: x["time"])
        out[res] = bars
    return out
