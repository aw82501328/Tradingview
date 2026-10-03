# -*- coding: utf-8 -*-
"""强分型均线V1（fxma_v1）策略引擎：缠论买卖点 + 强分型 + 均线分离 + 收盘站线
+ 可选黄金分割附近/上级周期同向（默认关） + 固定点数止损止盈。

与缠论V1（BacktestEngine 链路）并行的新策略，复用其增量笔机制
（_advance_cut/BiIncBuilder/合并K/分型/MACD/ATR 累加器/回退保护），
但**跳过支阻位与交易计划链路**（本策略不消费），信号/出场规则独立：

【信号】每个所选进出场周期 P 独立运行，P 每根已收K线收盘时评估：
  1. P 上最新买卖点（findBuyPoints/findSellPoints）属所选类别——
     1买/1卖→1类，2买/类2买、2卖/类2卖→2类，3买/类3买、3卖/类3卖→3类；4类不交易；
     反向点已出现时该点失效（作废，防旧点迟触发）；
  2. 点之后出现强分型（实体口径）：卖点→强顶分型（左肩开盘−右肩收盘
     ≥ strongFxMinPts 点）、买点→强底分型（对称）；strongFxOn=False 跳过本条；
  3. 均线分离：按类别选均线对（1类=maFast1/maSlow1；2/3类=maFast2/maSlow2，
     SMA/EMA 按 P 周期收盘价），当拍收盘 快线低于慢线≥crossMinPts 点（卖）/
     高于慢线≥crossMinPts 点（买）；maOn=False 跳过本条；
  4. 收盘站线：按类别选站线均线周期（1类=maStand1；2/3类=maStand2，SMA/EMA
     同 maType，按 P 周期收盘价），当拍收盘价 买点须站上（严格大于）/卖点须站下
     （严格小于）该均线；maStandOn=False 跳过本条；
  5. 黄金分割附近（fibNearOn，默认关；仅 2/3 类点，1 类点豁免）：摆动段 =
     前一同侧买卖点价格 → 其后至本点前（时间窗 (前点, 本点]）的 P 周期真实K线
     极值（买取最高价/卖取最低价），按 fibLevels（默认 0.382,0.5,0.618）算回撤位
     （买 H−r×(H−L)/卖 L+r×(H−L)），本点价格须落在任一档位 ±fibNearPts（绝对
     点数）内；无前一同侧点或摆动段退化（≤0）不触发；fibNearOn=False 跳过本条；
  6. 上级周期同向（upperDirOn，默认关；全部类别）：上级周期（UPPER_OF：
     30S→3→15→60→240）当前笔（列表末笔，含形成中）方向须与信号同向（买=up/
     卖=down）；上级无笔不触发；upperDirOn=False 跳过本条；
  7. 所选条件齐备（strongFxOn/maOn/maStandOn/fibNearOn/upperDirOn 均可独立
     关闭；全关=点属所选类别且未失效即当拍触发）的首个收盘拍出信号（每个点只
     触发一次；pointValidBars 根内未齐备作废）→ 下一根 P 周期K线开盘价成交。
【出场】持仓期间按引擎最小周期（fine）已收K线逐根判盘中触及：止损价=进场价∓stopPts、
  止盈价=进场价±tpPts（绝对点数）→ 盘中触价即按该触发价即时成交（与实盘 MT5 SL/TP
  同口径：不等收盘确认、不等下一开盘；进场那根 fine K线收盘后即参与判定）；
  同根双触按 sameBarPriority（默认止损优先）；期末未触发按最新收盘 mark-to-market。
【互斥】mutexScope=global：同向全局一笔未终局持仓（同缠论V1，多空并存）；
  perPeriod：每个进出场周期各自独立互斥。同拍多周期同向共振取最大周期一条。

接口契约与 BacktestEngine 完全一致（run/step_to/append_bars），信号/成交/出场
dict 字段同构（strategyKey=fx1Buy…fx3Sell，exitType=stop|takeProfit），
webapp 三模式与 live_trader 可零改动消费。30S 周期仅回测可用（MT5 实盘行情
由 M1 重采样构成、无法生成 30S，live_trader.load_config 拒启含 30S 的配置）。

跳拍近似（暂停后继续 / step_to 大步推进）：错过窗口内多根 K线时，出场按
窗口内首根触发K线即时按触发价成交（成交时序不回退）；信号以窗口
末根收盘拍评估（正常逐拍推进时两者都退化为单根，无近似）。
"""

import time
from bisect import bisect_left, bisect_right
from collections import deque

from .backtest import BacktestEngine, close_trade, DEFAULT_WARMUP_BARS
from .bt_journal import BtJournal, DIR_LABELS, EXIT_LABELS as FX_EXIT_LABELS
from .chan_core import findBuyPoints, findSellPoints, intervalSecOf, fmtT
from .mark_entry import DEFAULT_LOTS

# 进出场周期可选项（多选；30S 仅回测）
ENTRY_RES_OPTIONS = ("30S", "3", "15", "60")
# 买卖点分类用上级周期（标准周期链 30S→3→15→60→240 自动推导）
UPPER_OF = {"30S": "3", "3": "15", "15": "60", "60": "240"}

# 买卖点标签 → 类别（4买/4卖/类4买/类4卖不在表内=不交易）
POINT_CLASS = {
    "1买": 1, "1卖": 1,
    "2买": 2, "类2买": 2, "2卖": 2, "类2卖": 2,
    "3买": 3, "类3买": 3, "3卖": 3, "类3卖": 3,
}

# 参数中心 fxma 模块默认值（单一来源；param_center.defaults_of 活取）
FXMA_DEFAULTS = {
    "entryRes": "3,15,60",          # 多选：30S/3/15/60（30S 仅回测）
    "pointClasses": "1,2,3",        # 多选：1/2/3
    "maOn": True,                   # 均线分离条件开关（False=跳过均线条件）
    "maType": "SMA",                # 枚举：SMA/EMA
    "maFast1": 8, "maSlow1": 20,    # 一类点均线对
    "maFast2": 5, "maSlow2": 8,     # 二三类点均线对
    "crossMinPts": 2.0,             # 均线上下穿确认点数（快慢线间距≥该值）
    "maStandOn": True,              # 收盘站线条件开关（False=跳过站线条件）
    "maStand1": 5, "maStand2": 5,   # 站线均线周期：一类点 / 二三类点（买站上、卖站下）
    "fibNearOn": False,             # 黄金分割附近条件开关（仅2/3类点；False=跳过）
    "fibLevels": "0.382,0.5,0.618", # 黄金分割档位（逗号串，各档 0<r<1）
    "fibNearPts": 5.0,              # 黄金分割档位容差（绝对点数）
    "upperDirOn": False,            # 上级周期同向条件开关（False=跳过）
    "strongFxOn": True,             # 强分型条件开关（False=跳过强分型条件）
    "strongFxMinPts": 0.0,          # 强分型实体最小落差（0=现口径）
    "pointValidBars": 0,            # 点有效期（根；0=不限直到反向点）
    "pointValidPts": 0.0,           # 点有效期（值；盘中价距点极值上限，0=不限；超距等待回范围）
    "stopPts": 10.0,                # 止损点数（绝对价差；盘中触价即成交，与实盘 MT5 SL 同口径）
    "tpPts": 30.0,                  # 止盈点数（同口径）
    "sameBarPriority": "stop",      # 同根双触优先级：stop/tp
    "mutexScope": "global",         # 同向互斥范围：global/perPeriod
    "lots": 4.0,                    # 默认开仓手数（回测卡/实盘配置显式指定时以其为准）
}


def fx_strategy_key(cls, direction):
    """(类别, 方向) → 策略键（信号/成交行溯源；与缠论V1 wait* 键同位消费）。"""
    return f"fx{cls}{'Buy' if direction == 'long' else 'Sell'}"


def parse_multi(value, options, name):
    """多选参数（逗号串/序列）→ 去重保序列表；逐元素校验 ∈ options。"""
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    else:
        items = [str(v).strip() for v in (value or []) if str(v).strip()]
    out, seen = [], set()
    for v in items:
        if v not in options:
            raise ValueError(f"{name} 含非法值 {v!r}（可选：{','.join(options)}）")
        if v not in seen:
            seen.add(v)
            out.append(v)
    if not out:
        raise ValueError(f"{name} 至少选择一项（可选：{','.join(options)}）")
    return out


def parse_fib_levels(value):
    """黄金分割档位（逗号串/序列）→ 去重保序浮点列表；逐元素校验 0<r<1。"""
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    else:
        items = [str(v).strip() for v in (value or []) if str(v).strip()]
    out, seen = [], set()
    for v in items:
        try:
            r = float(v)
        except ValueError:
            raise ValueError(f"fibLevels 含非法值 {v!r}（须为 0<r<1 的比例）") from None
        if not 0.0 < r < 1.0:
            raise ValueError(f"fibLevels 档位须满足 0<r<1（收到 {v}）")
        if r not in seen:
            seen.add(r)
            out.append(r)
    if not out:
        raise ValueError("fibLevels 至少一个档位（0<r<1）")
    return out


class MaAcc:
    """SMA/EMA 增量累计器（收盘价，逐根 push，O(1)）；未满周期 value=None。"""
    __slots__ = ("kind", "period", "buf", "total", "ema")

    def __init__(self, kind, period):
        self.kind, self.period = str(kind).upper(), int(period)
        self.buf = deque(maxlen=self.period)
        self.total = 0.0
        self.ema = None

    def push(self, close):
        if self.kind == "EMA":
            k = 2.0 / (self.period + 1)
            self.ema = close if self.ema is None else self.ema + k * (close - self.ema)
            return
        if len(self.buf) >= self.period:
            self.total -= self.buf[0]
        self.buf.append(close)
        self.total += close

    @property
    def ready(self):
        return self.ema is not None if self.kind == "EMA" else len(self.buf) >= self.period

    @property
    def value(self):
        if self.kind == "EMA":
            return self.ema
        return self.total / self.period if len(self.buf) >= self.period else None


def strong_fx_after(merged, fractals, t, kind, min_pts=0.0):
    """t（含）之后是否出现强分型（kind="top"|"bottom"），返回首个命中的分型时间或 None。

    强分型只比实体、不含影线（与 trading_plan.strong_fractal_after 同口径）：
    底分型：右肩收盘 − 左肩开盘 ≥ min_pts（min_pts=0 时须 >0）；
    顶分型：左肩开盘 − 右肩收盘 ≥ min_pts（对称）。
    """
    for f in fractals:
        if f["type"] != kind or f["time"] < t:
            continue
        i = f["mergedIdx"]
        if i - 1 < 0 or i + 1 >= len(merged):
            continue  # 左/右肩不完整（尾部形成中）
        left, right = merged[i - 1], merged[i + 1]
        if kind == "bottom":
            diff = right["close"] - left["open"]
            if diff > 0 and diff >= min_pts:
                return f["time"]
        else:
            diff = left["open"] - right["close"]
            if diff > 0 and diff >= min_pts:
                return f["time"]
    return None


class FxMaEngine(BacktestEngine):
    """强分型均线V1 引擎（step_to/run 接口与 BacktestEngine 一致，见模块 docstring）。"""

    def __init__(self, bars_by_period, entry_res="3,15,60", point_classes="1,2,3",
                 ma_on=True, ma_type="SMA", ma_fast1=8, ma_slow1=20,
                 ma_fast2=5, ma_slow2=8,
                 cross_min_pts=2.0, ma_stand_on=True, ma_stand1=5, ma_stand2=5,
                 fib_near_on=False, fib_near_levels="0.382,0.5,0.618",
                 fib_near_pts=5.0, upper_dir_on=False,
                 strong_fx_on=True, strong_fx_min_pts=0.0,
                 point_valid_bars=0, point_valid_pts=0.0,
                 stop_pts=10.0, tp_pts=30.0, same_bar_priority="stop",
                 mutex_scope="global", lots=DEFAULT_LOTS, contract_mult=1.0,
                 fill_at_open_bar=False, fine_res=None, marks_params=None):
        self.entry_res = parse_multi(entry_res, ENTRY_RES_OPTIONS, "entryRes")
        self.point_classes = {int(c) for c in
                              parse_multi(point_classes, ("1", "2", "3"), "pointClasses")}
        self.ma_on = bool(ma_on)
        self.ma_type = str(ma_type).upper()
        if self.ma_type not in ("SMA", "EMA"):
            raise ValueError(f"maType 须为 SMA/EMA 之一（收到 {ma_type!r}）")
        self.ma_fast1, self.ma_slow1 = int(ma_fast1), int(ma_slow1)
        self.ma_fast2, self.ma_slow2 = int(ma_fast2), int(ma_slow2)
        for name, f, s in (("均线对1", self.ma_fast1, self.ma_slow1),
                           ("均线对2", self.ma_fast2, self.ma_slow2)):
            if f < 1 or s < 1 or f >= s:
                raise ValueError(f"{name} 周期须满足 1 ≤ 快线 < 慢线（收到 {f}/{s}）")
        self.cross_min_pts = float(cross_min_pts)
        self.ma_stand_on = bool(ma_stand_on)
        self.ma_stand1, self.ma_stand2 = int(ma_stand1), int(ma_stand2)
        if self.ma_stand1 < 1 or self.ma_stand2 < 1:
            raise ValueError(f"站线均线周期须 ≥1（收到 {ma_stand1}/{ma_stand2}）")
        self.fib_near_on = bool(fib_near_on)
        # 注意：基类 BacktestEngine 自带 fib_levels（支阻 sr 黄金分割比率）属性，
        # fxma 档位用 fib_near_levels 避免被 super().__init__ 覆盖
        self.fib_near_levels = parse_fib_levels(fib_near_levels)
        self.fib_near_pts = float(fib_near_pts)
        if self.fib_near_pts < 0:
            raise ValueError(f"fibNearPts 须 ≥0（收到 {fib_near_pts}）")
        self.upper_dir_on = bool(upper_dir_on)
        self.strong_fx_on = bool(strong_fx_on)
        self.strong_fx_min_pts = float(strong_fx_min_pts)
        self.point_valid_bars = int(point_valid_bars)
        self.point_valid_pts = float(point_valid_pts)
        if self.point_valid_pts < 0:
            raise ValueError(f"pointValidPts 须 ≥0（收到 {point_valid_pts}）")
        self.stop_pts = float(stop_pts)
        self.tp_pts = float(tp_pts)
        if self.stop_pts <= 0 or self.tp_pts <= 0:
            raise ValueError("stopPts/tpPts 必须为正数（绝对点数）")
        if same_bar_priority not in ("stop", "tp"):
            raise ValueError(f"sameBarPriority 须为 stop/tp 之一（收到 {same_bar_priority!r}）")
        self.same_bar_priority = same_bar_priority
        if mutex_scope not in ("global", "perPeriod"):
            raise ValueError(f"mutexScope 须为 global/perPeriod 之一（收到 {mutex_scope!r}）")
        self.mutex_scope = mutex_scope
        # 时间轴 = 最小所选周期（30S 选中时由 30S 承担——fxma 链路远轻于缠论V1可承受；
        # 深度受本地库 30S 数据限制）。引擎周期 = 默认链 + 30S（选中才加）：与 MT5Feed
        # DEFAULT_PERIODS（D/240/60/15/3）兼容（实盘 feed 按该表推送，多载周期无害），
        # 上级笔（30S→3→15→60→240）已全部覆盖
        fine = fine_res or min(self.entry_res, key=lambda r: intervalSecOf(r) or 0)
        periods = ["D", "240", "60", "15", "3"] + (["30S"] if "30S" in self.entry_res else [])
        super().__init__(bars_by_period, periods=periods, warmup_bars=DEFAULT_WARMUP_BARS,
                         lots=lots, contract_mult=contract_mult,
                         fill_at_open_bar=fill_at_open_bar, fine_res=fine,
                         module_params={"marks": dict(marks_params or {})})
        # 信号去重：(P, 点标签, 点时间)——每个买卖点只触发一次（超时/反向点作废也落此集合）
        self._fx_fired = set()
        # 各 P 的均线累计器（收盘价，随 cut 增量推进）与推进水位；
        # mas 顺序：fast1/slow1（一类对）、fast2/slow2（二三类对）、stand1/stand2（站线）
        self._fx_ma = {P: {"cut": 0, "mas": (
            MaAcc(self.ma_type, self.ma_fast1), MaAcc(self.ma_type, self.ma_slow1),
            MaAcc(self.ma_type, self.ma_fast2), MaAcc(self.ma_type, self.ma_slow2),
            MaAcc(self.ma_type, self.ma_stand1), MaAcc(self.ma_type, self.ma_stand2))}
            for P in self.entry_res}
        # findBuyPoints/findSellPoints 记录缓存（chan_core 按 macdArr 对象身份分槽；
        # _resync_bis 重建 macd 时同步清空）
        self._fx_pt_cache = {P: {} for P in self.entry_res}
        # 结构对齐周期（selected ∪ 上级；structurePeriods 输入）
        self._fx_align_res = list(dict.fromkeys(
            [r for r in self.entry_res] + [UPPER_OF[r] for r in self.entry_res]))

    # ---------------- 参数中心 → 引擎 kwargs ----------------

    @staticmethod
    def kwargs_from_params(pm, **override):
        """param_center fxma 模块参数（effective 后的品种桶）→ 引擎构造 kwargs。

        lots 优先级：调用方显式传入（回测卡/实盘配置）> 参数中心 fxma.lots。
        """
        kw = dict(
            entry_res=pm.get("entryRes", "3,15,60"),
            point_classes=pm.get("pointClasses", "1,2,3"),
            ma_on=pm.get("maOn", True),
            ma_type=pm.get("maType", "SMA"),
            ma_fast1=pm.get("maFast1", 8), ma_slow1=pm.get("maSlow1", 20),
            ma_fast2=pm.get("maFast2", 5), ma_slow2=pm.get("maSlow2", 8),
            cross_min_pts=pm.get("crossMinPts", 2.0),
            ma_stand_on=pm.get("maStandOn", True),
            ma_stand1=pm.get("maStand1", 5), ma_stand2=pm.get("maStand2", 5),
            fib_near_on=pm.get("fibNearOn", False),
            fib_near_levels=pm.get("fibLevels", "0.382,0.5,0.618"),
            fib_near_pts=pm.get("fibNearPts", 5.0),
            upper_dir_on=pm.get("upperDirOn", False),
            strong_fx_on=pm.get("strongFxOn", True),
            strong_fx_min_pts=pm.get("strongFxMinPts", 0.0),
            point_valid_bars=pm.get("pointValidBars", 0),
            point_valid_pts=pm.get("pointValidPts", 0.0),
            stop_pts=pm.get("stopPts", 10.0), tp_pts=pm.get("tpPts", 30.0),
            same_bar_priority=pm.get("sameBarPriority", "stop"),
            mutex_scope=pm.get("mutexScope", "global"),
        )
        if pm.get("lots"):
            kw["lots"] = pm["lots"]
        kw.update(override)
        return kw

    # ---------------- 增量状态维护（重写使 fxma 点缓存同步失效） ----------------

    def _resync_bis(self, res):
        super()._resync_bis(res)
        # 批量重同步后 bis/macd 全量重建（对象身份变化），点缓存按身份分槽已自愈；
        # 主动清空避免 id 复用误命中（chan_core 持强引用防复用，双保险）
        if res in self._fx_pt_cache:
            self._fx_pt_cache[res] = {}

    # ---------------- fxma 信号评估 ----------------

    def _fx_sync_ma(self, P):
        """把 P 的均线累计器推进到当前 cut（消费 cut 之前未消化的已收K线收盘价）。"""
        st = self._fx_ma[P]
        cut = self._cut.get(P, 0)
        if cut <= st["cut"]:
            return False
        lst = self.bars[P]["_list"]
        f1, s1, f2, s2, st1, st2 = st["mas"]
        for b in lst[st["cut"]:cut]:
            c = b["close"]
            f1.push(c)
            s1.push(c)
            f2.push(c)
            s2.push(c)
            st1.push(c)
            st2.push(c)
        st["cut"] = cut
        return True

    def _fx_align(self):
        """结构对齐（structurePeriods：笔过滤/校准，与买卖点标记阶段同口径）。"""
        from .chan_core import structurePeriods
        barsByPeriod = {res: self._prefix_bars(res) for res in self._fx_align_res}
        self._structure_bis = structurePeriods(
            self._bis, barsByPeriod, self._decision_time,
            self._merged, self._fractals, self._chain_work_cache)

    def _fx_all_points(self, P):
        """P 上全部买点/卖点列表（findBuyPoints/findSellPoints 已按 time 升序；
        缓存口径同 _fx_latest_points）。@returns (buys, sells)"""
        structure = getattr(self, "_structure_bis", None) or self._bis
        bis = structure.get(P) or []
        upper = structure.get(UPPER_OF[P]) or []
        macd = self._macd[P].to_list()
        cache = self._fx_pt_cache[P]
        mp = self.marks_params or {}
        buys = findBuyPoints(bis, upper, macd, intervalSecOf(P),
                             mp.get("class2ZsTol", 0.0), mp.get("thirdZsTol", 0.0), cache=cache)
        sells = findSellPoints(bis, upper, macd, intervalSecOf(P),
                               mp.get("class2ZsTol", 0.0), mp.get("thirdZsTol", 0.0), cache=cache)
        return buys, sells

    def _fx_latest_points(self, P):
        """P 上最新买点/卖点（findBuyPoints/findSellPoints 已按 time 升序，尾点=时间最新；
        同刻并列取识别序靠后那笔）。@returns (买点|None, 卖点|None)"""
        buys, sells = self._fx_all_points(P)
        return (buys[-1] if buys else None, sells[-1] if sells else None)

    def _fx_prev_point(self, P, pt, side):
        """pt 的前一同侧买卖点（time < pt.time 的最近一个；无则 None）。side="buy"|"sell"。"""
        buys, sells = self._fx_all_points(P)
        lst = buys if side == "buy" else sells
        out = None
        for q in lst:
            if q["time"] >= pt["time"]:
                break
            out = q
        return out

    def _fx_upper_dir(self, P):
        """上级周期（UPPER_OF[P]）当前笔方向（列表末笔，含形成中）；无笔 None。"""
        structure = getattr(self, "_structure_bis", None) or self._bis
        ubis = structure.get(UPPER_OF[P]) or []
        return ubis[-1]["type"] if ubis else None

    def _fx_collect(self, allSignals, stats, fired, t):
        """当下评估：各 P 新收K线收盘拍判定 fxma 信号（返回新信号列表）。

        只在 P 有新已收K线时评估（点/分型/均线只随新收K线变化；上级笔变化时刻
        必为 P 的收盘边界——周期链对齐保证不漏）。交易日志（self._journal，run()
        期间挂载）：各闸门拒绝原因（去重落盘）+ 信号行 + 每 P 状态行。
        """
        jr = getattr(self, "_journal", None)
        sigs = []
        for P in self.entry_res:
            if not self._fx_sync_ma(P):
                continue  # 本拍 P 无新收盘K线
            buyPt, sellPt = self._fx_latest_points(P)
            if jr is not None and jr.enabled:
                try:
                    st = self._fx_ma[P]["mas"]
                    stateCtx = {
                        "buyPtType": buyPt.get("type") if buyPt else None,
                        "buyPtTime": buyPt.get("time") if buyPt else None,
                        "sellPtType": sellPt.get("type") if sellPt else None,
                        "sellPtTime": sellPt.get("time") if sellPt else None,
                        # 均线值随行携带但不参与变化判定（每拍都变）：
                        "ma1Fast": round(st[0].value, 4) if st[0].ready else None,
                        "ma1Slow": round(st[1].value, 4) if st[1].ready else None,
                        "ma2Fast": round(st[2].value, 4) if st[2].ready else None,
                        "ma2Slow": round(st[3].value, 4) if st[3].ready else None,
                        "maStand1": round(st[4].value, 4) if st[4].ready else None,
                        "maStand2": round(st[5].value, 4) if st[5].ready else None,
                    }
                    if self.upper_dir_on:
                        stateCtx["upperDir"] = self._fx_upper_dir(P)
                    jr.state(t, P, stateCtx,
                             volatile=("ma1Fast", "ma1Slow", "ma2Fast", "ma2Slow",
                                       "maStand1", "maStand2"))
                except Exception:
                    pass
            for pt, direction, kind in ((buyPt, "long", "bottom"), (sellPt, "short", "top")):
                if pt is None:
                    continue
                cls = POINT_CLASS.get(pt["type"])
                if cls is None or cls not in self.point_classes:
                    if jr is not None and jr.enabled:
                        jr.reject(t, "fx_class_not_selected", P, pt["time"],
                                  None, ptType=pt["type"],
                                  classes=",".join(str(c) for c in sorted(self.point_classes)))
                    continue  # 最新点不属所选类别（4类/未选类别不交易）
                key = (P, pt["type"], pt["time"])
                if key in fired:
                    continue  # 已触发或已作废
                # 反向点已出现 → 点失效（作废，防止旧点迟触发）
                opp = sellPt if direction == "long" else buyPt
                if opp is not None and opp["time"] > pt["time"]:
                    fired.add(key)
                    if jr is not None and jr.enabled:
                        jr.reject(t, "fx_point_invalidated", P, pt["time"], None,
                                  ptType=pt["type"], oppType=opp.get("type"),
                                  oppTime=opp.get("time"))
                    continue
                # 点有效期：点之后已收盘的 P 周期K线数（当前评估根含在内）
                if self.point_valid_bars > 0:
                    idx = bisect_right(self._times[P], pt["time"])
                    if self._cut[P] - idx > self.point_valid_bars:
                        fired.add(key)
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_point_expired", P, pt["time"], None,
                                      ptType=pt["type"], validBars=self.point_valid_bars)
                        continue
                # 点有效期（值）：评估根盘中价距点极值（买=high−点最低价、卖=点最高价−low）；
                # 等待语义——超距只跳过本拍，点保持存活，价格回到范围内仍可触发
                if self.point_valid_pts > 0:
                    lb = self.bars[P]["_list"][self._cut[P] - 1]
                    drift = (lb["high"] - pt["price"]) if direction == "long" \
                        else (pt["price"] - lb["low"])
                    if drift > self.point_valid_pts:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_point_drift_fail", P, pt["time"], None,
                                      ptType=pt["type"], validPts=self.point_valid_pts)
                        continue
                # ① 强分型（点之后，实体口径；strongFxOn=False 跳过本条件）
                fxTime = None
                if self.strong_fx_on:
                    fxTime = strong_fx_after(self._merged[P], self._fractals[P], pt["time"],
                                             kind, self.strong_fx_min_pts)
                    if fxTime is None:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_no_strong_fx", P, pt["time"], None,
                                      ptType=pt["type"], kind=kind,
                                      minPts=self.strong_fx_min_pts)
                        continue
                # ② 均线分离（按类别选均线对；卖点=快线低于慢线、买点=快线高于慢线；
                #    maOn=False 跳过本条件）
                diff = None
                if self.ma_on:
                    st = self._fx_ma[P]
                    fast, slow = (st["mas"][0], st["mas"][1]) if cls == 1 else (st["mas"][2], st["mas"][3])
                    if not fast.ready or not slow.ready:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_ma_not_ready", P, pt["time"], None,
                                      ptType=pt["type"], maKind=cls)
                        continue  # 均线未满周期
                    diff = (slow.value - fast.value) if direction == "short" else (fast.value - slow.value)
                    if not (diff > 0 and diff >= self.cross_min_pts):
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_ma_gap_fail", P, pt["time"],
                                      fx_strategy_key(cls, direction),
                                      ptType=pt["type"], diff=round(diff, 4),
                                      crossMinPts=self.cross_min_pts,
                                      volatile=("diff",))
                        continue
                # ③ 收盘站线（按类别选站线均线：1类=maStand1、2/3类=maStand2，SMA/EMA
                #    同 maType；买=当拍收盘价严格大于站线均线、卖=严格小于；
                #    maStandOn=False 跳过本条件）
                standGap = None
                standVal = None
                if self.ma_stand_on:
                    lastClose = self.bars[P]["_list"][self._cut[P] - 1]["close"]
                    standAcc = self._fx_ma[P]["mas"][4 if cls == 1 else 5]
                    if not standAcc.ready:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_ma_stand_not_ready", P, pt["time"], None,
                                      ptType=pt["type"], maKind=cls)
                        continue  # 站线均线未满周期
                    standVal = standAcc.value
                    standGap = lastClose - standVal
                    if not (standGap > 0 if direction == "long" else standGap < 0):
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_ma_stand_fail", P, pt["time"],
                                      fx_strategy_key(cls, direction),
                                      ptType=pt["type"], close=round(lastClose, 4),
                                      ma=round(standVal, 4), maP=standAcc.period,
                                      volatile=("close", "ma"))
                        continue
                # ④ 黄金分割附近（fibNearOn=False 跳过；仅 2/3 类点，1 类点豁免）：
                #    摆动段 = 前一同侧买卖点价格 → 其后至本点前（时间窗 (前点, 本点]）
                #    的 P 周期真实K线极值（买取最高/卖取最低）；本点价格须落在任一
                #    fibLevels 档位回撤 ±fibNearPts（绝对点数）内（判定随点固定）
                fibLevel = fibGap = fibRef = fibExt = None
                if self.fib_near_on and cls != 1:
                    prev = self._fx_prev_point(P, pt, "buy" if direction == "long" else "sell")
                    if prev is None:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_fib_no_ref", P, pt["time"], None,
                                      ptType=pt["type"])
                        continue
                    times = self._times[P]
                    lo = bisect_right(times, prev["time"])
                    hi = bisect_right(times, pt["time"])
                    lst = self.bars[P]["_list"]
                    if direction == "long":
                        ext = max((b["high"] for b in lst[lo:hi]), default=None)
                        swing = (ext - prev["price"]) if ext is not None else None
                    else:
                        ext = min((b["low"] for b in lst[lo:hi]), default=None)
                        swing = (prev["price"] - ext) if ext is not None else None
                    if swing is None or swing <= 0:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_fib_swing_fail", P, pt["time"], None,
                                      ptType=pt["type"], refPrice=round(prev["price"], 4),
                                      ext=round(ext, 4) if ext is not None else None)
                        continue
                    best = None  # (gap, level, ratio)
                    for r in self.fib_near_levels:
                        level = ext - r * swing if direction == "long" else ext + r * swing
                        gap = abs(pt["price"] - level)
                        if best is None or gap < best[0]:
                            best = (gap, level, r)
                    if best[0] > self.fib_near_pts:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_fib_not_near", P, pt["time"],
                                      fx_strategy_key(cls, direction),
                                      ptType=pt["type"], ptPrice=round(pt["price"], 4),
                                      refPrice=round(prev["price"], 4), ext=round(ext, 4),
                                      level=round(best[1], 4), ratio=best[2],
                                      gap=round(best[0], 4), tol=self.fib_near_pts)
                        continue
                    fibGap, fibLevel, fibRef, fibExt = best[0], best[2], prev["price"], ext
                # ⑤ 上级周期同向（upperDirOn=False 跳过；全部类别）：上级周期当前笔
                #    （列表末笔，含形成中）方向须与信号同向（买=up/卖=down）
                upperDir = None
                if self.upper_dir_on:
                    upperDir = self._fx_upper_dir(P)
                    if upperDir is None:
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_upper_no_bi", P, pt["time"], None,
                                      ptType=pt["type"], upper=UPPER_OF[P])
                        continue
                    if upperDir != ("up" if direction == "long" else "down"):
                        if jr is not None and jr.enabled:
                            jr.reject(t, "fx_upper_dir_fail", P, pt["time"],
                                      fx_strategy_key(cls, direction),
                                      ptType=pt["type"], upper=UPPER_OF[P], upperDir=upperDir)
                        continue
                fired.add(key)
                lastBar = self.bars[P]["_list"][self._cut[P] - 1]
                short = direction == "short"
                conds = [pt["type"]] + (["强分型"] if self.strong_fx_on else []) \
                    + (["均线分离"] if self.ma_on else []) \
                    + (["站上均线" if direction == "long" else "站下均线"]
                       if self.ma_stand_on else []) \
                    + ([f"黄金分割{fibLevel:g}"] if fibLevel is not None else []) \
                    + ([f"上级{UPPER_OF[P]}同向"] if self.upper_dir_on else [])
                note = (f"{pt['type']} @ {fmtT(pt['time'])} {pt['price']:.2f}")
                if self.strong_fx_on:
                    note += f"｜强分型（{kind}）{fmtT(fxTime)}"
                if self.ma_on:
                    note += f"｜{self.ma_type} 分离 {abs(diff):.2f} ≥ {self.cross_min_pts} 点"
                if self.ma_stand_on:
                    note += (f"｜收盘 {lastBar['close']:.2f} "
                             f"{'>' if direction == 'long' else '<'} "
                             f"{self.ma_type}{self.ma_stand1 if cls == 1 else self.ma_stand2}"
                             f" {standVal:.2f}")
                if fibLevel is not None:
                    note += (f"｜黄金分割 {fibLevel:g} 位（前点 {fibRef:.2f}→极值 "
                             f"{fibExt:.2f}，距 {fibGap:.2f} ≤ {self.fib_near_pts} 点）")
                if self.upper_dir_on:
                    note += f"｜上级 {UPPER_OF[P]} 当前笔 {upperDir}"
                note += f"｜P={P} 收盘 {lastBar['close']:.2f} 出信号"
                sig = {
                    "periodX": P, "markRes": P,
                    "time": t, "price": lastBar["close"], "direction": direction,
                    "strategyKey": fx_strategy_key(cls, direction),
                    "pointType": pt["type"], "pointTime": pt["time"], "pointPrice": pt["price"],
                    "strongFxTime": fxTime,
                    "crossGap": round(diff, 4) if diff is not None else None,
                    "standGap": round(standGap, 4) if standGap is not None else None,
                    "fibLevel": fibLevel,
                    "fibGap": round(fibGap, 4) if fibGap is not None else None,
                    "upperDir": upperDir,
                    "realtime": True, "reason": "+".join(conds),
                    "strategyLabel": f"{self.ma_type}均线V1·{pt['type']}",
                    "signalNote": note,
                    # 信号拍临时 SL/TP（实盘 immediate 下单用；成交拍由引擎 stopRef/tpRef 对齐）
                    "provStop": lastBar["close"] + self.stop_pts if short
                    else lastBar["close"] - self.stop_pts,
                    "provTp": lastBar["close"] - self.tp_pts if short
                    else lastBar["close"] + self.tp_pts,
                }
                stats["signals"] += 1
                stats["long"] += int(direction == "long")
                stats["short"] += int(direction == "short")
                stats["markRes"][P] = stats["markRes"].get(P, 0) + 1
                stats["strategyKeys"][sig["strategyKey"]] = \
                    stats["strategyKeys"].get(sig["strategyKey"], 0) + 1
                allSignals.setdefault(P, []).append(dict(sig, tradeNo=0))
                sigs.append(sig)
                if jr is not None and jr.enabled:
                    try:
                        jr.signal(sig)
                    except Exception:
                        pass
        return sigs

    # ---------------- 出场状态机（固定点数止损/止盈） ----------------

    def _fx_open_positions(self, open_pos):
        """遍历未终局持仓 → [(槽位dict, 方向, pos)]（槽位即互斥作用域的实际字典）。"""
        if self.mutex_scope == "global":
            return [(open_pos, d, open_pos[d]) for d in ("long", "short") if open_pos.get(d)]
        out = []
        for P in self.entry_res:
            slot = open_pos.get(P) or {}
            for d in ("long", "short"):
                if slot.get(d):
                    out.append((slot, d, slot[d]))
        return out

    def _fx_check_exits(self, open_pos, stats):
        """各持仓：fine 周期新收K线（完整 high/low）逐根判盘中触及止损/止盈价
        → 即时按该触发价成交（与实盘 MT5 SL/TP 同口径：盘中触价成交，不等
        收盘确认、不等下一开盘）→ 返回本拍终局的 trade 列表（互斥即时解锁）。

        判定粒度 = 引擎最小周期（fine，持仓不论 P 一律用 fine——实盘 broker 亦
        不分周期按 tick 判）。按K线时间序遍历 _evalCutFine→cut 窗口（正常逐拍
        推进时窗口恰 1 根；跳拍时首根触发即成交，见模块 docstring「跳拍近似」）。
        """
        exits = []
        fine = self.fine_res
        lstF = self.bars[fine]["_list"]
        cutF = self._cut.get(fine, 0)
        jr = getattr(self, "_journal", None)
        for slot, d, pos in self._fx_open_positions(open_pos):
            if cutF <= pos.get("_evalCutFine", 0):
                continue  # 本拍无新收 fine K线
            short = d == "short"
            for j in range(pos.get("_evalCutFine", 0), cutF):
                bar = lstF[j]
                stop_hit = bar["high"] >= pos["stopRef"] if short else bar["low"] <= pos["stopRef"]
                tp_hit = bar["low"] <= pos["tpRef"] if short else bar["high"] >= pos["tpRef"]
                if stop_hit and tp_hit:
                    pick = "stop" if self.same_bar_priority == "stop" else "takeProfit"
                elif stop_hit:
                    pick = "stop"
                elif tp_hit:
                    pick = "takeProfit"
                else:
                    continue
                exitPrice = pos["stopRef"] if pick == "stop" else pos["tpRef"]
                parts = []
                if stop_hit:
                    parts.append((f"最高 {bar['high']:.2f} ≥ 止损位 {pos['stopRef']:.2f}"
                                  if short else
                                  f"最低 {bar['low']:.2f} ≤ 止损位 {pos['stopRef']:.2f}"))
                if tp_hit:
                    parts.append((f"最低 {bar['low']:.2f} ≤ 止盈位 {pos['tpRef']:.2f}"
                                  if short else
                                  f"最高 {bar['high']:.2f} ≥ 止盈位 {pos['tpRef']:.2f}"))
                why = (f"{FX_EXIT_LABELS.get(pick, pick)}盘中触发：P={pos['periodX']} "
                       f"{fmtT(bar['time'])} 当根{'，'.join(parts)}"
                       f" → 按触发价 {exitPrice:.2f} 成交")
                if stop_hit and tp_hit:
                    why += f"（同根双触，按 sameBarPriority={self.same_bar_priority} 取 {pick}）"
                pos["exits"].append({"type": pick, "time": bar["time"],
                                     "price": exitPrice, "why": why})
                tr = close_trade(pos, pick, bar["time"], exitPrice)
                slot[d] = None
                stats["closed"] += 1
                if pick == "stop":
                    stats["stopped"] = stats.get("stopped", 0) + 1
                if jr is not None and jr.enabled:
                    try:
                        jr.exit_event(tr.get("tradeNo"), (tr.get("exits") or [{}])[-1],
                                      tr.get("journalId"))
                        jr.trade_end(tr)
                    except Exception:
                        pass
                exits.append(tr)
                break  # 该持仓已终局
            pos["_evalCutFine"] = cutF
        return exits

    def _fx_fill_price(self, P, t, fb_time, fb_open):
        """t（P 周期开盘边界）那根 P K线的开盘价；P 缺位时以 fine 当根开盘兜底。

        只取 time==t 的 P K线开盘（该根恰在 t 开盘、开盘价当拍可知，无未来函数）；
        不回退到 t 之后更晚的 P K线（批量回测中那是未来数据）。
        """
        times = self._times.get(P) or []
        i = bisect_left(times, t)
        if i < len(times) and times[i] == t:
            return t, self.bars[P]["_list"][i]["open"]
        return fb_time, fb_open

    def _fx_fill_pending(self, trades, pending, open_pos, stats, t, fb_time, fb_open,
                         on_suppressed=None, sup_out=None):
        """成交收集拍信号（互斥 + 同拍共振取大周期 + 固定价位）。

        同向互斥作用域按 mutexScope：global=全局同向一笔；perPeriod=每周期独立。
        pending 按检测周期从大到小排序——同拍同向共振大周期优先成交，其余被互斥过滤。
        """
        for s in sorted(pending, key=lambda x: -(intervalSecOf(x.get("periodX")) or 0)):
            d, P = s["direction"], s["periodX"]
            slot = open_pos if self.mutex_scope == "global" else open_pos.setdefault(P, {})
            if slot.get(d) is not None:
                stats["suppressed"] += 1
                jr = getattr(self, "_journal", None)
                if jr is not None and jr.enabled:
                    blk = slot[d]
                    try:
                        s["suppressedWhy"] = (
                            f"同向互斥（{self.mutex_scope}）：已有{DIR_LABELS.get(d, d)}持仓单 "
                            f"#{blk.get('tradeNo')}（{fmtT(blk.get('entryTime'))} @ "
                            f"{blk.get('entryPrice'):.2f} 进场，{blk.get('strategyKey')}）"
                            f"未终局，本信号不成交")
                        jr.suppressed(t, s, why=s["suppressedWhy"])
                    except Exception:
                        pass
                if sup_out is not None:
                    sup_out.append(s)
                if on_suppressed:
                    try:
                        on_suppressed(s)
                    except Exception:
                        pass
                continue
            entryTime, entryPrice = self._fx_fill_price(P, t, fb_time, fb_open)
            short = d == "short"
            stopRef = entryPrice + self.stop_pts if short else entryPrice - self.stop_pts
            tpRef = entryPrice - self.tp_pts if short else entryPrice + self.tp_pts
            trades.append({
                "tradeNo": len(trades) + 1,
                "journalId": s.get("_jid"),
                "periodX": P, "markRes": P,
                "direction": d,
                "strategyKey": s["strategyKey"],
                "strategyLabel": s.get("strategyLabel"),
                "signalNote": s.get("signalNote") or s.get("reason"),
                "signalTime": s["time"], "signalPrice": s["price"],
                "pointType": s.get("pointType"), "pointTime": s.get("pointTime"),
                "entryTime": entryTime, "entryPrice": entryPrice,
                "fillMode": "nextOpen",
                "lots": self.lots, "mult": self.contract_mult,
                "stopRef": stopRef,
                "stopSource": f"固定点数止损（盘中触价即成交） 进场价"
                              f"{'+' if short else '-'}{self.stop_pts}点",
                "tpRef": tpRef,
                "entryWhy": (
                    f"{DIR_LABELS.get(d, d)}｜{s.get('signalNote') or s.get('reason')}"
                    f"｜下一开盘 {fmtT(entryTime)} @ {entryPrice:.2f} 进场"
                    f"｜止损位 {stopRef:.2f}（{self.stop_pts}点，盘中触价即成交）"
                    f"｜止盈位 {tpRef:.2f}（{self.tp_pts}点，同口径）｜{self.lots} 手"),
                # 中性出场状态机键（live_trader._diff_states/_on_fill 与 chan V1 同位消费）
                "beStop": None, "beDone": False, "halfDone": False,
                "state": "open",
                "exits": [],
                "_evalCutFine": self._cut.get(self.fine_res, 0),
            })
            stats["executed"] += 1
            slot[d] = trades[-1]
            jr = getattr(self, "_journal", None)
            if jr is not None and jr.enabled:
                try:
                    jr.fill(trades[-1])
                except Exception:
                    pass

    # ---------------- 三模式统一推进（run 批量 / step_to 实时） ----------------

    def _fx_init_state(self):
        """跨拍持久状态（run 与 step_to 共用同构初始化）。"""
        return {
            "open_pos": ({"long": None, "short": None} if self.mutex_scope == "global"
                         else {P: {"long": None, "short": None} for P in self.entry_res}),
            "pending": [],     # 待成交信号（下一开盘）
            "trades": [],
            "allSignals": {},
            "fired": set(),
            "stats": {"steps": 0, "signals": 0, "executed": 0, "suppressed": 0, "closed": 0,
                      "long": 0, "short": 0, "markRes": {}, "strategyKeys": {}},
        }

    def _fx_tick(self, st, t, fb_bar, out, on_suppressed=None):
        """单拍推进（决策时刻 t = fine 下一根开盘时刻；fb_bar = 该根 fine K线或 None）。

        ①出场判定（fine 已收K线盘中触及止损/止盈 → 即时按触发价成交，互斥解锁）
        → ②收集信号 → ③信号成交（同拍共振取大周期）。出场不依赖 fb_bar（按
        触发价成交）；进场仍取 fb_bar 开盘——fb_bar=None（无下一根且不允许进行中
        成交）时信号挂起到下一拍再成交，价格仍取信号边界那根 P K线开盘。
        """
        exits = self._fx_check_exits(st["open_pos"], st["stats"])
        sigs = self._fx_collect(st["allSignals"], st["stats"], st["fired"], t)
        st["pending"] += sigs
        fills = []
        if fb_bar is not None and st["pending"]:
            n0 = len(st["trades"])
            self._fx_fill_pending(st["trades"], st["pending"], st["open_pos"], st["stats"],
                                  t, fb_bar["time"], fb_bar["open"],
                                  on_suppressed=on_suppressed, sup_out=out["suppressed"])
            fills = st["trades"][n0:]
            st["pending"] = []
        st["stats"]["steps"] += 1
        out["signals"] += sigs
        out["fills"] += fills
        out["exits"] += exits

    # ---------------- 批量回测 ----------------

    def run(self, to_ts=None, start_ts=None, log=None, log_every=2000,
            on_progress=None, on_signal=None, on_trade=None, on_exit=None,
            on_suppressed=None, paused=None, stopped=None, journal=None,
            journal_symbol=None, journal_strategy="fxma"):
        """逐根K线重放（与 BacktestEngine.run 同参同语义；fxma 链路见 _fx_tick）。
        journal=交易日志（None=默认新建 data/journal NDJSON；False=关闭；实例=注入）。"""
        log = log or (lambda *a, **k: None)
        fine = self.bars[self.fine_res]["_list"]
        n = len(fine)
        start_i = min(n, self.warmup_bars)
        if start_ts is not None:
            # 交易开始时刻口径（同缠论V1）：start_ts 前只推进状态，空仓起步
            si = bisect_left(self._times[self.fine_res], start_ts)
            start_i = min(n, max(1, si))
        end_i = n
        if to_ts is not None:
            end_i = min(end_i, bisect_right(self.bars[self.fine_res]["_times"], to_ts))
        st = self._fx_init_state()
        fine_sec = intervalSecOf(self.fine_res) or 180
        # 交易日志（零开销口径；journal=False 关闭；self._journal 供 _fx_* 方法取用）
        jr = (journal if isinstance(journal, BtJournal)
              else BtJournal(enabled=False) if journal is False
              else BtJournal(strategy=journal_strategy, symbol=journal_symbol))
        self._journal = jr
        if jr.enabled:
            jr.header({
                "engine": "fx_ma", "entryRes": ",".join(self.entry_res),
                "pointClasses": ",".join(str(c) for c in sorted(self.point_classes)),
                "maOn": self.ma_on, "maType": self.ma_type,
                "ma1": [self.ma_fast1, self.ma_slow1], "ma2": [self.ma_fast2, self.ma_slow2],
                "crossMinPts": self.cross_min_pts,
                "maStandOn": self.ma_stand_on,
                "maStand": [self.ma_stand1, self.ma_stand2],
                "fibNearOn": self.fib_near_on,
                "fibLevels": ",".join(str(r) for r in self.fib_near_levels),
                "fibNearPts": self.fib_near_pts,
                "upperDirOn": self.upper_dir_on,
                "strongFxOn": self.strong_fx_on, "strongFxMinPts": self.strong_fx_min_pts,
                "pointValidBars": self.point_valid_bars,
                "pointValidPts": self.point_valid_pts,
                "stopPts": self.stop_pts, "tpPts": self.tp_pts,
                "sameBarPriority": self.same_bar_priority, "mutexScope": self.mutex_scope,
                "lots": self.lots, "contractMult": self.contract_mult,
                "startTs": start_ts, "toTs": to_ts, "fineRes": self.fine_res,
                # 实际加载数据范围（诊断数据源截断，同缠论V1）
                "nBars": {res: len(self.bars[res]["_list"]) for res in self.periods},
                "lastBarTime": fine[-1]["time"] if len(fine) else None,
            })

        def _wait_if_paused():
            while paused is not None and paused.is_set():
                if stopped is not None and stopped.is_set():
                    return False
                time.sleep(0.2)
            return True

        # 预热：只推进状态（笔/均线充分建立后开始交易）；进度/日志同缠论V1
        # （刻度统一 end_i，进度条单调爬升，长预热不静默）
        for i in range(start_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            self._advance_cut(fine[i]["time"] + fine_sec)
            if on_progress:
                try:
                    on_progress(i + 1, end_i)
                except Exception:
                    pass
            if log and (i + 1) % log_every == 0:
                log(f"预热进度：第 {i + 1}/{start_i} 根（{fmtT(fine[i]['time'])}）")
        if stopped is not None and stopped.is_set():
            result = self._finish(st["allSignals"], st["trades"], st["stats"])
            self._journal_close(jr, st["stats"], result)
            return result
        for P in self.entry_res:
            self._fx_sync_ma(P)
        self._fx_align()
        log(f"预热完成：最小周期 {self.fine_res} 已到第 {start_i} 根"
            f"（{fmtT(fine[start_i-1]['time'])}）")

        for i in range(start_i, end_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            t = fine[i + 1]["time"] if i + 1 < end_i else fine[i]["time"] + fine_sec
            if self._advance_cut(t):
                self._fx_align()
            out = {"signals": [], "fills": [], "exits": [], "suppressed": []}
            self._fx_tick(st, t, fine[i + 1] if i + 1 < end_i else None, out,
                          on_suppressed=on_suppressed)
            for s in out["signals"]:
                if on_signal:
                    try:
                        on_signal(s)
                    except Exception:
                        pass
            for tr in out["fills"]:
                if on_trade:
                    try:
                        on_trade(tr)
                    except Exception:
                        pass
            for tr in out["exits"]:
                if on_exit:
                    try:
                        on_exit(tr)
                    except Exception:
                        pass
            if on_progress:
                try:
                    on_progress(i + 1, end_i)
                except Exception:
                    pass
            if log and (i + 1) % log_every == 0:
                s = st["stats"]
                log(f"回测进度：第 {i + 1}/{end_i} 根，累计信号 {s['signals']}，成交 {s['executed']}")
        if log:
            s = st["stats"]
            log(f"回测完成：共 {s['steps']} 步，信号 {s['signals']}，成交 {s['executed']}")
        # 收尾批量重同步（最终笔状态严格等于 batch(全前缀)）后按最新收盘 mark-to-market
        self.resync_all()
        result = self._finish(st["allSignals"], st["trades"], st["stats"])
        self._journal_close(jr, st["stats"], result)
        return result

    # ---------------- 实时/回放（live_trader 与三模式监控消费） ----------------

    def step_to(self, t, execute=False):
        """实时推进到时刻 t（与 BacktestEngine.step_to 同契约）。

        execute=False（预热）：仅推进状态，返回 []（fxma 不在预热期收集信号）。
        execute=True：逐根推进并成交，返回 {signals, fills, exits, suppressed}。
        """
        self._decision_time = t
        if self._replay_needed:
            for res in sorted(self._replay_needed, key=lambda r: intervalSecOf(r) or 0):
                self._rewind_res(res)
            self._replay_needed.clear()
        if not execute:
            self._advance_cut(t)
            for P in self.entry_res:
                self._fx_sync_ma(P)
            return []
        fine = self.bars[self.fine_res]["_list"]
        times = self._times[self.fine_res]
        sec = intervalSecOf(self.fine_res) or 180
        if self._live_st is None:
            self._live_st = {"i": self._cut[self.fine_res], "st": self._fx_init_state()}
            for P in self.entry_res:
                self._fx_sync_ma(P)
            self._fx_align()
        wrap = self._live_st
        st = wrap["st"]
        end_cut = min(len(fine), bisect_right(times, t - sec) if sec > 0
                      else bisect_right(times, t))
        i = wrap["i"]
        if i > end_cut or i > self._cut[self.fine_res]:
            # 回退保护（与基类同语义）：状态倒放 → 回退到当前 cut，清持仓与待成交
            wrap["i"] = i = self._cut[self.fine_res]
            st["open_pos"] = ({"long": None, "short": None} if self.mutex_scope == "global"
                              else {P: {"long": None, "short": None} for P in self.entry_res})
            st["pending"] = []
        out = {"signals": [], "fills": [], "exits": [], "suppressed": []}
        while i < end_cut:
            t_dec = fine[i + 1]["time"] if i + 1 < end_cut else fine[i]["time"] + sec
            if self._advance_cut(t_dec):
                self._fx_align()
            next_ok = (i + 1 < end_cut
                       or (self.fill_at_open_bar and i + 1 == end_cut and i + 1 < len(fine)))
            self._fx_tick(st, t_dec, fine[i + 1] if next_ok else None, out)
            i += 1
        wrap["i"] = i
        return out


def run_fxma_backtest(bars_by_period, to_ts=None, start_ts=None, log=None,
                      journal=None, journal_symbol=None, **kwargs):
    """便捷入口：构建 FxMaEngine 并运行（kwargs 见 FxMaEngine.__init__）。"""
    engine = FxMaEngine(bars_by_period, **kwargs)
    return engine.run(to_ts=to_ts, start_ts=start_ts, log=log,
                      journal=journal, journal_symbol=journal_symbol)
