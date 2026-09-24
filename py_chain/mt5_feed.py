# -*- coding: utf-8 -*-
"""EXNESS MT5 行情数据层：M1 拉取 → 服务器时间转UTC → 按本仓口径重采样 → 入库 bars.db。

设计依据（spec/plans/SPEC_live_exness_mt5.md，2026-09-23 实测核实）：
- bars.db 的 240/D 时间戳为纽约 17:00 锚定（240 落 UTC 01/05/09/13/17/21 夏令时，非 14400
  整数倍；D=NY 日界随 DST 漂移）；3/15/60 为 UTC epoch 整数倍对齐。
  → 只拉 M1 自行重采样：3/15/60 按秒数分箱；240/D 按 America/New_York 17:00 日界分箱。
  → 禁用 MT5 自带 H4/D1（EET 服务器日界在美欧 DST 相位差窗口偏离 NY17:00 一小时）。
- OANDA 口径：日维护窗=NY 17:00~18:00 无K线；周末=周六全天+周日 NY 18:00 前无K线。
  EXNESS 近 24/7 → weekend="drop" 默认开，会话过滤对齐算法所见历史结构。
- 服务器时间→UTC 分段转换：近 3 天用动态 offset（最近 tick 服务器时间戳 vs UTC 实测、
  整点量化——Python 包无 TimeTradeServer/TimeGMT，MQL5 专属），历史按规则表判定。
  规则实测（2026-09-24 probe+tick 核验）：Exness-MT5Trial5 服务器钟=UTC+0 固定（无 DST）
  → 默认 rule="utc"（恒 0）；"us"/"eu"=EET 假设（冬 +2h/夏 +3h）留作他服务器备用。
  mt5_align 对拍可再验证（时间戳匹配率即最终裁判）。

约定：本模块与 mt5_broker.py 是全仓唯一允许 `import MetaTrader5` 的文件。
bar dict 形状 {time,open,high,low,close}（time=Unix秒UTC），与引擎/data_store 完全一致。
绝不把 MT5 bar 写进 OANDA:* symbol（污染回测锚点库）。
"""

import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import MetaTrader5 as mt5
except ImportError:  # 单测/CI 无终端环境：仅纯函数部分可用
    mt5 = None

from . import data_store

NY_TZ = ZoneInfo("America/New_York")       # 规则区 us（美东 DST，与纽约 17:00 日界自洽）
EU_TZ = ZoneInfo("Europe/Bucharest")       # 规则区 eu（欧盟 DST，EET 常用对照）
RULE_TZ = {"us": NY_TZ, "eu": EU_TZ}

DEFAULT_PERIODS = ("D", "240", "60", "15", "3")
M1_SEC = 60
# 分箱秒数：3/15/60 直接 epoch 对齐分箱；240/D 走纽约日界分箱（ny_session_bins）
BIN_SEC = {"3": 180, "15": 900, "60": 3600}
# 近 3 天（秒）内用动态 offset 转换，之外用 DST 规则表
DYNAMIC_WINDOW = 3 * 86400
# tail 默认 M1 根数：覆盖 D 桶最坏情况（周五临收盘 D 桶已积累 ~23h≈1380 根，含被过滤的
# 维护窗 60 根），1560 留余量；240 桶最多 240 根
DEFAULT_TAIL_M1 = 1560

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 服务器时间 → UTC（纯函数）
# ---------------------------------------------------------------------------

def _is_dst(utc_ts, tz):
    """该 UTC 时刻在规则区 tz 是否处于夏令时。"""
    return datetime.fromtimestamp(utc_ts, tz).dst().total_seconds() != 0


def offset_at(utc_ts, rule="us"):
    """给定 UTC 时刻的服务器偏移秒数。rule="utc"=恒 0（Exness-MT5Trial5 实测，
    2026-09-24：服务器钟=UTC，无 DST）；"us"/"eu"=EET 假设（冬 +2h / 夏 +3h）。"""
    if rule == "utc":
        return 0
    return 2 * 3600 + (3600 if _is_dst(utc_ts, RULE_TZ[rule]) else 0)


def server_offset_from_tick(srv_tick_ts, utc_now, prev=None):
    """由最近 tick 的服务器时间戳推服务器-UTC 偏移（秒），整点量化。纯函数。

    MetaTrader5 Python 包无 TimeTradeServer/TimeGMT（MQL5 专属，5.0.6180 实测），
    以 symbol_info_tick 时间戳（服务器墙上钟 epoch）对比 UTC 当前时刻：
    raw = offset - tick龄。Exness 服务器=EET 类整点偏移，按 3600s 量化可容忍
    tick 龄 <30 分钟（黄金盘中 tick 秒级；边界 2.5h 四舍五入歧义可忽略）；
    |raw|>14h 判为停盘残 tick（周末/长假），返回 prev 保持上次值（首次
    prev=None → 调用方走 DST 规则表兜底）。"""
    if not srv_tick_ts:
        return prev
    raw = int(srv_tick_ts) - int(utc_now)
    if abs(raw) > 14 * 3600:            # 有效时区偏移 ±14h；超出=残 tick
        return prev
    return int(round(raw / 3600.0)) * 3600


def srv_to_utc(srv_ts, rule="us", now=None, now_offset=None):
    """服务器时间戳（秒）→ UTC 时间戳（秒）。

    - |srv_ts - now| < DYNAMIC_WINDOW 时用动态 offset（now_offset=最近 tick 实测偏移，
      调用方传入；不传则全部走规则表）。
    - 其余按规则表（rule="us"/"eu"）。DST 切换邻域最多 1 小时歧义，迭代两次收敛
      （切换发生在周日休市时段，weekend=drop 下基本无影响）。
    """
    if now is not None and now_offset is not None and abs(srv_ts - now) < DYNAMIC_WINDOW:
        return srv_ts - now_offset
    off = offset_at(srv_ts - 2 * 3600, rule)      # 初值：先按冬令时近似出 UTC
    for _ in range(2):
        utc = srv_ts - off
        off2 = offset_at(utc, rule)
        if off2 == off:
            return utc
        off = off2
    return srv_ts - off


# ---------------------------------------------------------------------------
# 纽约 17:00 交易日（纯函数）
# ---------------------------------------------------------------------------

def ny_day_start_utc(utc_ts):
    """UTC 时刻所属交易日的起点（NY 17:00 开市）UTC 时间戳。

    交易日边界：NY 本地 >=17:00 归次日（该日 17:00 NY 起）；<17:00 归当日前一 17:00。
    """
    local = datetime.fromtimestamp(utc_ts, NY_TZ)
    if local.hour >= 17:
        start_local = local.replace(hour=17, minute=0, second=0, microsecond=0)
    else:
        prev = local.fromtimestamp(utc_ts - 86400, NY_TZ)
        start_local = prev.replace(hour=17, minute=0, second=0, microsecond=0)
    return int(start_local.timestamp())


def _bar(time_, o, h, l, c):
    return {"time": int(time_), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c)}


def resample_epoch(m1_bars, sec):
    """3/15/60：按 epoch 整数倍分箱。空箱不出 bar（与 OANDA 缺口口径一致）。"""
    bins = {}
    order = []
    for b in m1_bars:
        k = b["time"] // sec
        g = bins.get(k)
        if g is None:
            bins[k] = g = [k * sec, b["open"], b["high"], b["low"], b["close"]]
            order.append(k)
        else:
            if b["high"] > g[2]:
                g[2] = b["high"]
            if b["low"] < g[3]:
                g[3] = b["low"]
            g[4] = b["close"]
    return [_bar(*bins[k]) for k in sorted(bins)]


def ny_session_bins(m1_bars, res):
    """240/D：按纽约 17:00 交易日分箱（D=整交易日；240=交易日内每 4h，锚定日界起点）。

    bin time=箱起点 UTC（D=交易日 17:00 NY；240=日界起点+k*14400），与 bars.db 实测口径一致
    （240 落 UTC 01/05/09/13/17/21 夏令时；D bar time=日起点，如 2026-09-22 21:00 UTC）。
    DST 切换日由 zoneinfo 自动处理（日界随 NY 漂移，240 箱宽仍固定 4h）。
    """
    day_bin = (res == "D")
    bins = {}
    for b in m1_bars:
        day_start = ny_day_start_utc(b["time"])
        if day_bin:
            k = day_start
        else:
            k = day_start + ((b["time"] - day_start) // 14400) * 14400
        g = bins.get(k)
        if g is None:
            bins[k] = [k, b["open"], b["high"], b["low"], b["close"]]
        else:
            if b["high"] > g[2]:
                g[2] = b["high"]
            if b["low"] < g[3]:
                g[3] = b["low"]
            g[4] = b["close"]
    return [_bar(*bins[k]) for k in sorted(bins)]


def session_keep(utc_ts, weekend="drop"):
    """会话过滤（对齐 OANDA 口径）。返回 True=保留。

    weekend="drop"：丢周六全天、周日 NY 18:00 前、每日维护窗 [日界, 日界+1h)。
    weekend="keep"：全保留（EXNESS 24/7 原样，供对拍报告对比）。
    """
    if weekend != "drop":
        return True
    day_start = ny_day_start_utc(utc_ts)
    if utc_ts < day_start + 3600:          # NY 17:00~18:00 日维护窗
        return False
    local = datetime.fromtimestamp(utc_ts, NY_TZ)
    wd = local.weekday()                    # 0=周一 … 5=周六 6=周日
    if wd == 5:
        return False                        # 周六全天休市
    if wd == 6 and (local.hour < 18):
        return False                        # 周日 NY 18:00 前未开市
    return True


def resample(m1_bars, res, weekend="drop"):
    """M1（UTC）→ 目标周期 bars。纯函数：过滤会话 → 分箱。进行中末箱照常输出
    （引擎 append_bars 的 override 语义处理逐拍更新）。"""
    kept = [b for b in m1_bars if session_keep(b["time"], weekend)]
    if res in BIN_SEC:
        return resample_epoch(kept, BIN_SEC[res])
    if res in ("240", "D"):
        return ny_session_bins(kept, res)
    raise ValueError(f"不支持的重采样周期: {res}（实盘周期表永不加 30S——M1 无法构成 30s 桶）")


def closed_bars(bars, res, now_utc=None):
    """过滤掉未收盘桶（bin 起点+周期宽 > now）——入库只写已收盘 bar。

    D 的收盘=下一交易日起点：用「桶起点+24h 保守判定」近似（实际周五桶收盘在周日开市，
    推迟入库至周日也正确——周日本地 18:00 NY 后 24h 窗即过）。"""
    now_utc = int(now_utc if now_utc is not None else time.time())
    sec = 86400 if res == "D" else (BIN_SEC.get(res) or 14400)
    return [b for b in bars if b["time"] + sec <= now_utc]


# ---------------------------------------------------------------------------
# MT5Feed：行情接入
# ---------------------------------------------------------------------------

class MT5Feed:
    """MT5 M1 行情源。attach 本机已登录终端（终端须常驻且已登录，密码存终端凭据）。"""

    def __init__(self, symbol="XAUUSD", periods=DEFAULT_PERIODS, weekend="drop",
                 db_symbol="EXNESS:XAUUSD", rule="utc", log=None):
        self.symbol = symbol
        self.periods = tuple(periods)
        self.weekend = weekend
        self.db_symbol = db_symbol        # 入库 symbol（绝不写 OANDA:*）
        self.rule = rule
        self.log = log or (lambda *a, **k: print(*a))
        self._offset = None               # 动态服务器偏移（秒）

    # -- 连接 ---------------------------------------------------------------

    def connect(self):
        """attach 终端并选中品种。返回 spec 摘要 dict；失败抛 RuntimeError。"""
        if mt5 is None:
            raise RuntimeError("未安装 MetaTrader5 库（pip install MetaTrader5）")
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize 失败：{mt5.last_error()}")
        if not mt5.symbol_select(self.symbol, True):
            mt5.shutdown()
            raise RuntimeError(f"symbol_select {self.symbol} 失败（确认 Market Watch 中"
                               f"品种名，注意 RAW 账户后缀如 XAUUSDm）：{mt5.last_error()}")
        return self.spec()

    def close(self):
        if mt5 is not None:
            mt5.shutdown()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def spec(self):
        si = mt5.symbol_info(self.symbol)
        if si is None:
            raise RuntimeError(f"symbol_info({self.symbol}) 失败")
        return {
            "symbol": si.name, "digits": si.digits, "point": si.point,
            "volume_min": si.volume_min, "volume_step": si.volume_step,
            "volume_max": si.volume_max,
            "stops_level": si.trade_stops_level, "freeze_level": si.trade_freeze_level,
            "filling_mode": si.filling_mode,      # 位掩码：FOK=1 / IOC=2
            "contract_size": si.trade_contract_size,
            "spread_pts": si.spread,
        }

    def _refresh_offset(self):
        """动态服务器偏移（秒）：最近 tick 服务器时间戳 vs UTC（每次拉取前刷新）。
        tick 不可用（未选中/停盘）时保持上次值；首次为 None → srv_to_utc 走规则表。"""
        try:
            t = mt5.symbol_info_tick(self.symbol)
        except Exception:               # pragma: no cover - IPC 异常兜底
            t = None
        self._offset = server_offset_from_tick(
            int(t.time) if (t is not None and t.time) else None,
            int(time.time()), prev=self._offset)
        return self._offset

    # -- 拉取 ---------------------------------------------------------------

    def _rates_to_utc_m1(self, rates):
        now = int(time.time())
        # 字段一律下标访问：numpy≥2 的 np.void 不再支持属性式访问（2026-09-24 真机实测）
        return [{"time": srv_to_utc(int(r["time"]), self.rule, now=now, now_offset=self._offset),
                 "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
                for r in rates]

    def m1_range(self, from_ts, to_ts, with_spread=False):
        """拉 [from_ts, to_ts)（UTC epoch 秒）M1 → UTC dict 列表（不过滤会话）。

        with_spread=True 时附带每根 M1 的 spread 字段（点数，点差分布对拍用）。
        注：copy_rates_range 参数按 UTC epoch 传入；对拍报告含时区自检。"""
        self._refresh_offset()
        rates = mt5.copy_rates_range(
            self.symbol, mt5.TIMEFRAME_M1,
            datetime.fromtimestamp(int(from_ts), timezone.utc),
            datetime.fromtimestamp(int(to_ts), timezone.utc))
        if rates is None:
            raise RuntimeError(f"copy_rates_range 失败：{mt5.last_error()}")
        now = int(time.time())
        out = []
        for r in rates:
            b = {"time": srv_to_utc(int(r["time"]), self.rule, now=now, now_offset=self._offset),
                 "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"])}
            if with_spread:
                b["spread"] = int(r["spread"])
            out.append(b)
        return out

    def tail(self, n_m1=DEFAULT_TAIL_M1, store=True):
        """拉最近 n_m1 根 M1 → UTC → 会话过滤 → 各周期重采样。

        返回 {res: [bar]}（含进行中末箱）；store=True 时已收盘桶 upsert 入库。"""
        self._refresh_offset()
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, int(n_m1))
        if rates is None:
            raise RuntimeError(f"copy_rates_from_pos 失败：{mt5.last_error()}")
        m1 = self._rates_to_utc_m1(rates)
        m1 = [b for b in m1 if session_keep(b["time"], self.weekend)]
        out = {res: resample(m1, res, weekend="keep") for res in self.periods}
        if store:
            now = int(time.time())
            for res in self.periods:
                data_store.upsert_bars(self.db_symbol, res, closed_bars(out[res], res, now))
        return out

    def history(self, min_depth_days=365, chunk_days=30, store=True):
        """深拉 M1（分块）→ 重采样 → 入库。返回 {res: {count, first, last}} 深度摘要。

        注：copy_rates_range 的 from/to 按 UTC epoch 传入；probe/align 会自检时区口径
        （若整体错位 2~3h 即为参数口径差异，届时修正）。"""
        self._refresh_offset()
        now = int(time.time())
        t0 = now - int(min_depth_days) * 86400
        m1_all = []
        cur = t0
        while cur < now:
            nxt = min(cur + int(chunk_days) * 86400, now)
            rates = mt5.copy_rates_range(self.symbol, mt5.TIMEFRAME_M1,
                                         datetime.fromtimestamp(cur, timezone.utc),
                                         datetime.fromtimestamp(nxt, timezone.utc))
            if rates is None:
                self.log(f"  copy_rates_range {cur}~{nxt} 失败（忽略）：{mt5.last_error()}")
            else:
                m1_all.extend(self._rates_to_utc_m1(rates))
            cur = nxt
        m1_all.sort(key=lambda b: b["time"])
        m1_all = [b for b in m1_all if session_keep(b["time"], self.weekend)]
        depth = {}
        for res in self.periods:
            bars = resample(m1_all, res, weekend="keep")
            if store:
                data_store.upsert_bars(self.db_symbol, res, bars)
            depth[res] = {"count": len(bars),
                          "first": bars[0]["time"] if bars else None,
                          "last": bars[-1]["time"] if bars else None}
        if store:
            m1_first = depth.get("3", {}).get("first")
            if m1_first and m1_first > t0 + 86400:      # M1 深度不足（服务器侧上限）
                depth["backfill"] = self.backfill_coarse(t0)
        return depth

    # 粗周期补深：服务器 M1 深度不足时的 240/D 上下文补齐（SPEC 预案）
    COARSE_BACKFILL_RES = ("240", "D")

    def backfill_coarse(self, target_from, source="OANDA:XAUUSD"):
        """用 OANDA 补深 240/D 的更老段（幂等），seam 记录到 live_state。

        只补 [target_from, EXNESS 现存最早 bar)：EXNESS 段永远以本源 M1 重采样
        为准；seam 处两源 OHLC 微结构差异属固有差异（对拍报告在档可查）。
        返回 {res: 补了几根}。"""
        from . import live_store
        live_store.ensure_tables()          # live_state 可能尚未建（直跑 CLI 时）
        added, seams = {}, {}
        for res in self.COARSE_BACKFILL_RES:
            cur = data_store.load_store(self.db_symbol, [res])
            cur_first = cur[res][0]["time"] if cur.get(res) else None
            if cur_first is None:
                continue
            try:
                ond = data_store.load_store(source, [res], from_ts=int(target_from),
                                            to_ts=cur_first - 1).get(res, [])
            except ValueError:
                ond = []          # 源在窗口内无数据（本地 OANDA 深度有限）——尽力而为
            if ond:
                data_store.upsert_bars(self.db_symbol, res, ond)
            added[res] = len(ond)
            seams[res] = {"seam_ts": cur_first, "added": len(ond), "source": source,
                          "source_first": ond[0]["time"] if ond else None}
        live_store.save_state("exness_seam", {
            "updated_at": int(time.time()), "db_symbol": self.db_symbol, "res": seams})
        self.log(f"  OANDA 补深 240/D：{added}（seam 已记录 live_state）")
        return added

    def current_time(self):
        """当前 UTC epoch（墙钟）。与 ReplayFeed.cursor 同语义：引擎 step_to 的
        重放截止时刻（bar 只推进到已入库的最后一根，墙钟只作上限）。"""
        return int(time.time())

    def staleness_sec(self):
        """距最近一根 M1 的秒数（会话内 >300 需告警）。"""
        self._refresh_offset()
        rates = mt5.copy_rates_from_pos(self.symbol, mt5.TIMEFRAME_M1, 0, 1)
        if rates is None or len(rates) == 0:
            return None
        t = srv_to_utc(int(rates[0]["time"]), self.rule,
                       now=int(time.time()), now_offset=self._offset)
        return int(time.time()) - t - 60          # 末根为进行中，减 1 根宽

    # -- probe（M1 验收探测） ------------------------------------------------

    def probe(self, out_path="data/mt5_probe.json"):
        """环境验收探测：账号/spec/周末有无/维护窗/服务器 H4-D1 边界 vs NY17:00 → 落盘。"""
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize 失败：{mt5.last_error()}")
        acc = mt5.account_info()
        ti = mt5.terminal_info()
        spec = self.spec()
        self._refresh_offset()
        now = int(time.time())
        off = self._offset

        # 周六窗口：最近一个完整周六（UTC）
        sat = datetime.fromtimestamp(now, timezone.utc)
        while sat.weekday() != 5:
            sat = datetime.fromtimestamp(sat.timestamp() - 86400, timezone.utc)
        sat0 = int(sat.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        sat_rates = mt5.copy_rates_range(
            self.symbol, mt5.TIMEFRAME_M1,
            datetime.fromtimestamp(sat0, timezone.utc),
            datetime.fromtimestamp(sat0 + 86400, timezone.utc))
        weekend_m1 = 0 if sat_rates is None else len(sat_rates)

        # 维护窗：最近一个工作日的 [日界, 日界+1h)
        day_start = ny_day_start_utc(now - 86400)   # 昨天所在交易日日界
        while day_start > now - 7 * 86400:
            d = datetime.fromtimestamp(day_start, NY_TZ)
            if d.weekday() < 5:
                break
            day_start -= 86400
        mt_rates = mt5.copy_rates_range(
            self.symbol, mt5.TIMEFRAME_M1,
            datetime.fromtimestamp(day_start, timezone.utc),
            datetime.fromtimestamp(day_start + 3600, timezone.utc))
        maintenance_m1 = 0 if mt_rates is None else len(mt_rates)

        # 服务器自带 H4/D1 边界 vs NY17:00：取最近 5 根对照
        def boundaries(tf, sec):
            r = mt5.copy_rates_from_pos(self.symbol, tf, 0, 5)
            if r is None:
                return []
            ts = [srv_to_utc(int(x["time"]), self.rule, now=now, now_offset=off) for x in r]
            return [{"utc": t, "is_ny_anchored": (t - ny_day_start_utc(t)) % sec == 0}
                    for t in ts]

        report = {
            "probe_at_utc": now,
            "account": None if acc is None else {
                "login": acc.login, "server": acc.server, "currency": acc.currency,
                "trade_mode": acc.trade_mode,           # 0=demo 1=contest 2=real
                "margin_mode": acc.margin_mode,         # 2=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                "equity": acc.equity, "leverage": acc.leverage},
            "terminal": None if ti is None else {
                "name": ti.name, "connected": ti.connected, "trade_allowed": ti.trade_allowed},
            "symbol_spec": spec,
            "server_offset_sec": off,
            "saturday_m1_count": weekend_m1,
            "maintenance_hour_m1_count": maintenance_m1,
            "srv_h4_boundaries": boundaries(mt5.TIMEFRAME_H4, 14400),
            "srv_d1_boundaries": boundaries(mt5.TIMEFRAME_D1, 86400),
        }
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            out_path) if not os.path.isabs(out_path) else out_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return report


def main(argv=None):
    """CLI：python -m py_chain.mt5_feed probe|history|tail [--days N] [--symbol X]"""
    import argparse
    ap = argparse.ArgumentParser(description="EXNESS MT5 行情数据层")
    ap.add_argument("cmd", choices=["probe", "history", "tail"])
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--keep-weekend", action="store_true", help="不过滤周末/维护窗")
    args = ap.parse_args(argv)
    feed = MT5Feed(symbol=args.symbol, weekend="keep" if args.keep_weekend else "drop")
    with feed:
        if args.cmd == "probe":
            r = feed.probe()
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.cmd == "history":
            r = feed.history(min_depth_days=args.days)
            for res, d in r.items():
                if res == "backfill":
                    print(f"  OANDA 补深 240/D: {d}")
                    continue
                f = datetime.fromtimestamp(d["first"], timezone.utc) if d["first"] else None
                l = datetime.fromtimestamp(d["last"], timezone.utc) if d["last"] else None
                print(f"  {res:>4}: {d['count']} 根  {f} ~ {l}")
        else:
            r = feed.tail()
            for res, bars in r.items():
                print(f"  {res:>4}: {len(bars)} 根，末根 "
                      f"{datetime.fromtimestamp(bars[-1]['time'], timezone.utc) if bars else '-'}")


if __name__ == "__main__":
    main()
