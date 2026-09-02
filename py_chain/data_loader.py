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
import os
import socket
import subprocess
import time
import urllib.request

from .chan_core import intervalSecOf

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]
DEFAULT_CDP_PORT = 9222
CACHE_FILE = "bars_all_tf.json"
# 自动拉起 TradingView 的启动脚本路径（仓库根 .cursor/skills/open-tradingview/scripts/）
OPEN_TV_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".cursor", "skills", "open-tradingview", "scripts", "open_tradingview.js",
)
# 自动拉起后等待端口就绪的轮询参数
CDP_PORT_TIMEOUT = 90
CDP_PORT_POLL = 1.0


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


def _cdp_port_ready(cfg):
    """探测 CDP HTTP 端口是否已监听（约 2 秒超时）。"""
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=2):
            return True
    except OSError:
        return False


def ensure_cdp_ready(cfg, log=None):
    """确保 CDP 端口可用：未监听时自动调用 open_tradingview.js 拉起 TradingView。

    @returns 端口最终是否就绪（True 可继续连接，False 自动拉起失败）
    """
    log = log or (lambda *a, **k: None)
    if _cdp_port_ready(cfg):
        return True
    log(f"TradingView 未运行（CDP 端口 {cfg.port} 未监听），正在自动启动 TradingView ...")
    if not os.path.exists(OPEN_TV_SCRIPT):
        log(f"未找到自动启动脚本：{OPEN_TV_SCRIPT}")
        return False
    try:
        proc = subprocess.run(
            ["node", OPEN_TV_SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CDP_PORT_TIMEOUT)
        for line in (proc.stdout or "").splitlines():
            log(line)
        if proc.returncode != 0:
            for line in (proc.stderr or "").splitlines():
                log(line)
    except FileNotFoundError:
        log("未找到 node，无法自动启动 TradingView")
        return False
    except subprocess.TimeoutExpired:
        log(f"自动启动 TradingView 超时（{CDP_PORT_TIMEOUT}s），端口仍未就绪")
        return False
    ok = _cdp_port_ready(cfg)
    log(f"TradingView 自动启动{'成功' if ok else '失败'}（CDP 端口 {cfg.port} "
        f"{'已就绪' if ok else '未监听'}）")
    return ok


class CDPClient:
    """极简 CDP 客户端：HTTP 找 target + WS 执行 Runtime.evaluate。"""

    def __init__(self, cfg=None, log=None):
        self.cfg = cfg or CDPConfig()
        self._log = log or (lambda *a, **k: print(*a))
        self._ws = None
        self._msg_id = 0

    # ---------------- 连接 ----------------

    def connect(self):
        """GET /json 找 tradingview.com 页面，建立 WebSocket 连接。

        连接前先探测 CDP 端口；端口未监听时自动调用 open_tradingview.js 拉起
        TradingView 桌面端（带调试端口），等待就绪后再重试连接。
        """
        if not _cdp_port_ready(self.cfg):
            if not ensure_cdp_ready(self.cfg, self._log):
                raise CDPError(
                    "TradingView 未运行且自动启动失败，请手动打开 TradingView 桌面端后重试")
        ws_url = self._find_target(retry=60)
        import websocket  # 延迟导入，websocket-client
        # suppress_origin：Chromium 新版默认拒绝非白名单 origin 的 WS 握手（403），
        # 桌面端启动参数未带 --remote-allow-origins 时，必须抑制 Origin 头才能连上。
        self._ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._msg_id = 0
        self._log(f"已连接 CDP：{ws_url}")
        self._wait_api_ready()

    def _wait_api_ready(self, timeout=60):
        """等待 TradingView 图表完全就绪（自动拉起后页面加载有延迟）。

        仅检查 TradingViewApi 存在还不够：桌面端刚启动时 API 对象已注入，
        但图表数据/model 未就绪，此时 setSymbol/setResolution 会抛 "Value is null"。
        这里轮询直到图表 symbol 可读（model 已就绪），超时才抛错。
        """
        expr = (
            "(function() { "
            "try { const c = TradingViewApi.activeChart(); "
            "if (!c) return false; "
            "const sym = c.symbol ? c.symbol() : null; "
            "return !!sym && !!c.chartModel(); "
            "} catch (e) { return false; } })()"
        )
        deadline = time.time() + timeout
        while True:
            try:
                r = self.evaluate(expr)
                if r:
                    self._log("TradingView 图表 API 已就绪")
                    return
            except Exception:
                pass
            if time.time() >= deadline:
                raise CDPError(f"等待 TradingView 图表 API 就绪超时（{timeout}s）")
            time.sleep(1.0)

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

    def _find_target(self, retry=0):
        """GET /json 找 tradingview.com 页面 target。

        @param retry  端口已就绪但图表页面尚未加载完时，轮询重试秒数（0 表示不重试）
        """
        deadline = time.time() + retry
        last_err = None
        while True:
            try:
                with urllib.request.urlopen(f"{self.cfg.http_url}/json", timeout=10) as resp:
                    targets = json.loads(resp.read().decode("utf-8", errors="replace"))
                for t in targets:
                    if t.get("type") == "page" and "tradingview.com" in (t.get("url") or ""):
                        ws = t.get("webSocketDebuggerUrl")
                        if ws:
                            return ws
                last_err = "未找到 tradingview.com 页面 target"
            except OSError as e:
                last_err = str(e)
            if time.time() >= deadline:
                break
            time.sleep(1.0)
        raise CDPError(
            f"{last_err}（端口 {self.cfg.port} 已监听但 TradingView 图表页面未就绪）")

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


def fetch_bars(cfg=None, from_ts=0, cache=True, cache_file=CACHE_FILE, symbol=None, log=None):
    """通过 CDP 拉取多周期历史K线。

    @param cfg        CDPConfig（端口、周期列表、等待时长）
    @param from_ts    UTC 时间戳，只保留 >= from_ts 的K线（过滤加载的更多历史）
    @param cache      为 True 时结果写入 cache_file（默认 bars_all_tf.json）
    @param symbol     可选：拉取前先切换图表品种（如 OANDA:XAUUSD）
    @param log        可选日志回调（默认 print），用于透传自动拉起 TradingView 的过程日志
    @returns { 周期: [{time, open, high, low, close}] }（从大到小排列，时间升序）
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: print(*a))
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


def load_bars(periods=None, from_ts=0, use_cache=False, cache_file=CACHE_FILE, symbol=None, log=None):
    """统一入口：优先读缓存，缓存缺失或 use_cache=False 时走 CDP。

    @param log  可选日志回调（默认 print），透传给 fetch_bars / CDPClient
    """
    if use_cache:
        try:
            return load_cached(cache_file)
        except (OSError, json.JSONDecodeError):
            pass
    cfg = CDPConfig(periods=periods)
    return fetch_bars(cfg=cfg, from_ts=from_ts, cache=True, cache_file=cache_file,
                      symbol=symbol, log=log)


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
