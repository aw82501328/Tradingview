# -*- coding: utf-8 -*-
"""强分型均线V1（fxma_v1）策略引擎：缠论买卖点 + 强分型 + 均线分离 + 固定点数止损止盈。

与缠论V1（BacktestEngine 链路）并行的新策略，复用其增量笔机制
（_advance_cut/BiIncBuilder/合并K/分型/MACD/ATR 累加器/回退保护），
但**跳过支阻位与交易计划链路**（本策略不消费），信号/出场规则独立：

【信号】每个所选进出场周期 P 独立运行，P 每根已收K线收盘时评估：
  1. P 上最新买卖点（findBuyPoints/findSellPoints）属所选类别——
     1买/1卖→1类，2买/类2买、2卖/类2卖→2类，3买/类3买、3卖/类3卖→3类；4类不交易；
     反向点已出现时该点失效（作废，防旧点迟触发）；
  2. 点之后出现强分型（实体口径）：卖点→强顶分型（左肩开盘−右肩收盘
     ≥ strongFxMinPts 点）、买点→强底分型（对称）；
  3. 均线分离：按类别选均线对（1类=maFast1/maSlow1；2/3类=maFast2/maSlow2，
     SMA/EMA 按 P 周期收盘价），当拍收盘 快线低于慢线≥crossMinPts 点（卖）/
     高于慢线≥crossMinPts 点（买）；
  4. 三者齐备的首个收盘拍出信号（每个点只触发一次；pointValidBars 根内未齐备作废）
     → 下一根 P 周期K线开盘价成交。
【出场】持仓所在周期 P 的已收K线触及止损/止盈价（进场价∓stopPts/±tpPts 绝对点数）
  → 收盘确认后下一根 P 周期K线开盘价成交；同根双触按 sameBarPriority（默认止损优先）；
  期末未触发按最新收盘 mark-to-market。
【互斥】mutexScope=global：同向全局一笔未终局持仓（同缠论V1，多空并存）；
  perPeriod：每个进出场周期各自独立互斥。同拍多周期同向共振取最大周期一条。

接口契约与 BacktestEngine 完全一致（run/step_to/append_bars），信号/成交/出场
dict 字段同构（strategyKey=fx1Buy…fx3Sell，exitType=stop|takeProfit），
webapp 三模式与 live_trader 可零改动消费。30S 周期仅回测可用（MT5 实盘行情
由 M1 重采样构成、无法生成 30S，live_trader.load_config 拒启含 30S 的配置）。

跳拍近似（暂停后继续 / step_to 大步推进）：错过窗口内多根 P K线时，出场按
窗口内首根触发K线挂起、成交仍取当前拍下一开盘（成交时序不回退）；信号以窗口
末根收盘拍评估（正常逐拍推进时两者都退化为单根，无近似）。
"""

import time
from bisect import bisect_left, bisect_right
from collections import deque

from .backtest import BacktestEngine, close_trade, DEFAULT_WARMUP_BARS
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
    "maType": "SMA",                # 枚举：SMA/EMA
    "maFast1": 8, "maSlow1": 20,    # 一类点均线对
    "maFast2": 5, "maSlow2": 8,     # 二三类点均线对
    "crossMinPts": 2.0,             # 均线上下穿确认点数（快慢线间距≥该值）
    "strongFxMinPts": 0.0,          # 强分型实体最小落差（0=现口径）
    "pointValidBars": 0,            # 点有效期（根；0=不限直到反向点）
    "stopPts": 10.0,                # 止损点数（绝对价差）
    "tpPts": 30.0,                  # 止盈点数
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
                 ma_type="SMA", ma_fast1=8, ma_slow1=20, ma_fast2=5, ma_slow2=8,
                 cross_min_pts=2.0, strong_fx_min_pts=0.0, point_valid_bars=0,
                 stop_pts=10.0, tp_pts=30.0, same_bar_priority="stop",
                 mutex_scope="global", lots=DEFAULT_LOTS, contract_mult=1.0,
                 fill_at_open_bar=False, fine_res=None, marks_params=None):
        self.entry_res = parse_multi(entry_res, ENTRY_RES_OPTIONS, "entryRes")
        self.point_classes = {int(c) for c in
                              parse_multi(point_classes, ("1", "2", "3"), "pointClasses")}
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
        self.strong_fx_min_pts = float(strong_fx_min_pts)
        self.point_valid_bars = int(point_valid_bars)
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
        # 各 P 的均线累计器（收盘价，随 cut 增量推进）与推进水位
        self._fx_ma = {P: {"cut": 0, "mas": (
            MaAcc(self.ma_type, self.ma_fast1), MaAcc(self.ma_type, self.ma_slow1),
            MaAcc(self.ma_type, self.ma_fast2), MaAcc(self.ma_type, self.ma_slow2))}
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
            ma_type=pm.get("maType", "SMA"),
            ma_fast1=pm.get("maFast1", 8), ma_slow1=pm.get("maSlow1", 20),
            ma_fast2=pm.get("maFast2", 5), ma_slow2=pm.get("maSlow2", 8),
            cross_min_pts=pm.get("crossMinPts", 2.0),
            strong_fx_min_pts=pm.get("strongFxMinPts", 0.0),
            point_valid_bars=pm.get("pointValidBars", 0),
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
        f1, s1, f2, s2 = st["mas"]
        for b in lst[st["cut"]:cut]:
            c = b["close"]
            f1.push(c)
            s1.push(c)
            f2.push(c)
            s2.push(c)
        st["cut"] = cut
        return True

    def _fx_align(self):
        """结构对齐（structurePeriods：笔过滤/校准，与买卖点标记阶段同口径）。"""
        from .chan_core import structurePeriods
        barsByPeriod = {res: self._prefix_bars(res) for res in self._fx_align_res}
        self._structure_bis = structurePeriods(
            self._bis, barsByPeriod, self._decision_time,
            self._merged, self._fractals, self._chain_work_cache)

    def _fx_latest_points(self, P):
        """P 上最新买点/卖点（findBuyPoints/findSellPoints 尾点）。@returns (买点|None, 卖点|None)"""
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
        return (buys[-1] if buys else None, sells[-1] if sells else None)

    def _fx_collect(self, allSignals, stats, fired, t):
        """当下评估：各 P 新收K线收盘拍判定 fxma 信号（返回新信号列表）。

        只在 P 有新已收K线时评估（点/分型/均线只随新收K线变化；上级笔变化时刻
        必为 P 的收盘边界——周期链对齐保证不漏）。
        """
        sigs = []
        for P in self.entry_res:
            if not self._fx_sync_ma(P):
                continue  # 本拍 P 无新收盘K线
            buyPt, sellPt = self._fx_latest_points(P)
            for pt, direction, kind in ((buyPt, "long", "bottom"), (sellPt, "short", "top")):
                if pt is None:
                    continue
                cls = POINT_CLASS.get(pt["type"])
                if cls is None or cls not in self.point_classes:
                    continue  # 最新点不属所选类别（4类/未选类别不交易）
                key = (P, pt["type"], pt["time"])
                if key in fired:
                    continue  # 已触发或已作废
                # 反向点已出现 → 点失效（作废，防止旧点迟触发）
                opp = sellPt if direction == "long" else buyPt
                if opp is not None and opp["time"] > pt["time"]:
                    fired.add(key)
                    continue
                # 点有效期：点之后已收盘的 P 周期K线数（当前评估根含在内）
                if self.point_valid_bars > 0:
                    idx = bisect_right(self._times[P], pt["time"])
                    if self._cut[P] - idx > self.point_valid_bars:
                        fired.add(key)
                        continue
                # ① 强分型（点之后，实体口径）
                fxTime = strong_fx_after(self._merged[P], self._fractals[P], pt["time"],
                                         kind, self.strong_fx_min_pts)
                if fxTime is None:
                    continue
                # ② 均线分离（按类别选均线对；卖点=快线低于慢线、买点=快线高于慢线）
                st = self._fx_ma[P]
                fast, slow = (st["mas"][0], st["mas"][1]) if cls == 1 else (st["mas"][2], st["mas"][3])
                if not fast.ready or not slow.ready:
                    continue  # 均线未满周期
                diff = (slow.value - fast.value) if direction == "short" else (fast.value - slow.value)
                if not (diff > 0 and diff >= self.cross_min_pts):
                    continue
                fired.add(key)
                lastBar = self.bars[P]["_list"][self._cut[P] - 1]
                short = direction == "short"
                sig = {
                    "periodX": P, "markRes": P,
                    "time": t, "price": lastBar["close"], "direction": direction,
                    "strategyKey": fx_strategy_key(cls, direction),
                    "pointType": pt["type"], "pointTime": pt["time"], "pointPrice": pt["price"],
                    "strongFxTime": fxTime,
                    "crossGap": round(diff, 4),
                    "realtime": True, "reason": f"{pt['type']}+强分型+均线分离",
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

    def _fx_check_exits(self, open_pos):
        """各持仓：其周期 P 新收K线（完整 high/low）逐根触及止损/止盈 → 挂起待下一开盘成交。

        按K线时间序遍历 _evalCut→cut 窗口（正常逐拍推进时窗口恰 1 根；跳拍时首根
        触发即挂起，成交不回退——见模块 docstring「跳拍近似」）。
        """
        for _slot, _d, pos in self._fx_open_positions(open_pos):
            P = pos["periodX"]
            cut = self._cut.get(P, 0)
            if cut <= pos.get("_evalCut", 0):
                continue  # 本拍 P 无新收盘K线
            if pos.get("pendingExit"):
                pos["_evalCut"] = cut
                continue
            lst = self.bars[P]["_list"]
            short = pos["direction"] == "short"
            for j in range(pos.get("_evalCut", 0), cut):
                bar = lst[j]
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
                pos["pendingExit"] = pick
                pos["exitTriggerTime"] = bar["time"]
                break
            pos["_evalCut"] = cut

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

    def _fx_exec_exit(self, slot, d, pos, t, fb_time, fb_open, stats):
        """执行挂起的出场（下一开盘成交）→ 终局结算，返回平仓 trade 或 None。"""
        pick = pos.pop("pendingExit")
        exitTime, exitPrice = self._fx_fill_price(pos["periodX"], t, fb_time, fb_open)
        tr = close_trade(pos, pick, exitTime, exitPrice)
        slot[d] = None
        stats["closed"] += 1
        if pick == "stop":
            stats["stopped"] = stats.get("stopped", 0) + 1
        return tr

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
            trades.append({
                "tradeNo": len(trades) + 1,
                "periodX": P, "markRes": P,
                "direction": d,
                "strategyKey": s["strategyKey"],
                "signalTime": s["time"], "signalPrice": s["price"],
                "pointType": s.get("pointType"), "pointTime": s.get("pointTime"),
                "entryTime": entryTime, "entryPrice": entryPrice,
                "fillMode": "nextOpen",
                "lots": self.lots, "mult": self.contract_mult,
                "stopRef": entryPrice + self.stop_pts if short else entryPrice - self.stop_pts,
                "tpRef": entryPrice - self.tp_pts if short else entryPrice + self.tp_pts,
                # 中性出场状态机键（live_trader._diff_states/_on_fill 与 chan V1 同位消费）
                "beStop": None, "beDone": False, "halfDone": False,
                "state": "open",
                "exits": [],
                "_evalCut": self._cut.get(P, 0),
            })
            stats["executed"] += 1
            slot[d] = trades[-1]

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

        顺序与缠论V1一致：①出场判定（已收K线触及）→ ②出场成交（解锁互斥）
        → ③收集信号 → ④信号成交（同拍共振取大周期）。fb_bar=None（无下一根且
        不允许进行中成交）时挂起到下一拍再成交，价格仍取信号边界那根 P K线开盘。
        """
        self._fx_check_exits(st["open_pos"])
        exits = []
        if fb_bar is not None:
            for slot, d, pos in self._fx_open_positions(st["open_pos"]):
                if pos.get("pendingExit"):
                    tr = self._fx_exec_exit(slot, d, pos, t, fb_bar["time"], fb_bar["open"],
                                            st["stats"])
                    if tr is not None:
                        exits.append(tr)
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
            on_suppressed=None, paused=None, stopped=None):
        """逐根K线重放（与 BacktestEngine.run 同参同语义；fxma 链路见 _fx_tick）。"""
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

        def _wait_if_paused():
            while paused is not None and paused.is_set():
                if stopped is not None and stopped.is_set():
                    return False
                time.sleep(0.2)
            return True

        # 预热：只推进状态（笔/均线充分建立后开始交易）
        for i in range(start_i):
            if stopped is not None and stopped.is_set():
                break
            if not _wait_if_paused():
                break
            self._advance_cut(fine[i]["time"] + fine_sec)
        if stopped is not None and stopped.is_set():
            return self._finish(st["allSignals"], st["trades"], st["stats"])
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
        return self._finish(st["allSignals"], st["trades"], st["stats"])

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


def run_fxma_backtest(bars_by_period, to_ts=None, start_ts=None, log=None, **kwargs):
    """便捷入口：构建 FxMaEngine 并运行（kwargs 见 FxMaEngine.__init__）。"""
    engine = FxMaEngine(bars_by_period, **kwargs)
    return engine.run(to_ts=to_ts, start_ts=start_ts, log=log)
