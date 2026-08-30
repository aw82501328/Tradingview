# -*- coding: utf-8 -*-
"""
缠论策略在线回测引擎（多周期并行 + 绿色共振版，无未来函数）

信号规则（与用户确认一致）：
  - 只有「绿色共振」的 1买/1卖 才开仓（含反手）
  - 平仓不看颜色：红色或绿色的反向 1买/1卖 都触发平仓，红色只平不反手，绿色平仓+反手
  - 无加仓（2买/3买/2卖/3卖 不参与交易）

多周期并行（本版）：
  - 交易周期 3m/15m/60m/240m 各自独立持仓、独立开平（独立账户假设）。
  - 每个周期 T 的绿色点 = T 的 1买/1卖 与「紧邻上级 U」的 1/2/3 类买卖点落在同一点位。
  - 不再做 D→4H→1H→15m 的链式区间套，每个周期只看自己 vs 紧邻上级这一层。
  - D 作为 240 的紧邻上级（仅用于共振判定），本身不交易（无上级、无法定义绿色）。

绿色共振定义（与图上 SKILL 一致）：T 的 1买/1卖 与 U 的买卖点同点位
（价格差 <= max(ATR*0.2, 0.05)，时间差 <= U 一个 bar 时长）。
说明：图上 SKILL 用 60 秒时间容差（依赖跨周期端点校准）；回测不做跨周期端点校准
（校准只影响图上对齐，不影响价格与方向），故用「上级一个 bar 时长」作时间容差，
语义等价于「同一结构极值」。

止损：开仓极值 ± N×ATR；反手/开仓即重置。
成交：信号在 bar 收盘确认（滞后 CONFIRM_BARS 根），下一根 bar 开盘价成交。

用法：
    python vnpy/backtest.py            # 全周期
    python vnpy/backtest.py 3 15       # 只跑指定周期
"""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import chan_core as core

core.CHAN_CFG["gapFilter"] = 1.0
core.CHAN_CFG["debug"] = False

# ---------- 参数 ----------
ATR_FILTER = 0.5          # ATR 过滤系数（与画笔一致）
STOP_N = 1.0              # 止损 = 极值 ± STOP_N × ATR
CONFIRM_BARS = 2          # 信号确认滞后根数（本周期 bar）

TRADE_RES = ["3", "15", "60", "240"]            # 交易周期（多周期并行）
UPPER_OF = {"3": "15", "15": "60", "60": "240", "240": "D"}  # 紧邻上级映射

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "bars_all_tf.json")


# ---------- 单周期计算 ----------
def compute_bis(res, rawBars, lockedPivots=None):
    """单周期算笔。返回 (bis, atr, macdArr)。lockedPivots 为上级笔端点（区间套强制对齐）。"""
    merged = core.mergeBars(rawBars)
    fractals = core.findFractals(merged)
    atr = core.calcATR(rawBars, 14)
    macdArr = core.calcMACD(rawBars)
    bis = core.buildBi(fractals, merged, atr, macdArr, lockedPivots)
    threshold = atr * ATR_FILTER
    bis = [b for b in bis if b["span"] >= threshold]
    bis = core.extendLastBi(bis, rawBars)
    return bis, atr, macdArr


def compute_points(res, bis, upperBis, macdArr):
    """由笔算买卖点。upperBis 为紧邻上级笔（用于 2/3 类区间套），可为 None。"""
    if len(bis) < 3:
        return [], []
    barSec = core.intervalSecOf(res)
    buyPts = core.findBuyPoints(bis, upperBis, macdArr, barSec)
    sellPts = core.findSellPoints(bis, upperBis, macdArr, barSec)
    return buyPts, sellPts


def is_green(main_pt, upper_pts, atr, time_tol):
    """判定 T 的 1买/1卖 是否与紧邻上级 U 的买卖点共振（绿色）。"""
    if main_pt["type"] not in ("1买", "1卖"):
        return False
    if not upper_pts:
        return False
    tol = max(atr * 0.2, 0.05)
    for up in upper_pts:
        if abs(main_pt["time"] - up["time"]) <= time_tol and abs(main_pt["price"] - up["price"]) <= tol:
            return True
    return False


# ---------- 交易状态机 ----------
class Position:
    """持仓。lots 每手记录 {dir, entry, stop}。"""

    def __init__(self):
        self.lots = []

    @property
    def direction(self):
        if not self.lots:
            return 0
        return self.lots[0]["dir"]


def open_lot(pos, direction, price, stop, sig_type, trades):
    pos.lots.append({"dir": direction, "entry": price, "stop": stop, "type": sig_type})
    trades.append({"action": "开多" if direction > 0 else "开空", "price": price, "type": sig_type})


def close_all(pos, price, trades, reason=""):
    for lot in list(pos.lots):
        pnl = (price - lot["entry"]) * lot["dir"]
        trades.append({"action": "平多" if lot["dir"] > 0 else "平空", "price": price, "pnl": pnl, "reason": reason})
    pos.lots.clear()


def mark_to_market(pos, close):
    return sum((close - lot["entry"]) * lot["dir"] for lot in pos.lots)


def check_stop(bar, pos, trades):
    to_close = []
    for idx, lot in enumerate(pos.lots):
        if lot["dir"] > 0 and bar["low"] <= lot["stop"]:
            to_close.append((idx, lot["stop"]))
        elif lot["dir"] < 0 and bar["high"] >= lot["stop"]:
            to_close.append((idx, lot["stop"]))
    for idx, price in reversed(to_close):
        lot = pos.lots[idx]
        pnl = (price - lot["entry"]) * lot["dir"]
        trades.append({"action": "止损平多" if lot["dir"] > 0 else "止损平空", "price": price, "pnl": pnl})
        del pos.lots[idx]


def fmt_time(ts):
    from datetime import datetime
    dt = datetime.fromtimestamp(ts)
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"


# ---------- 单个周期独立回测 ----------
def run_one_period(res, upper_res, bars_self, bars_upper):
    main_sec = core.intervalSecOf(res)
    upper_sec = core.intervalSecOf(upper_res)
    time_tol = upper_sec

    pos = Position()
    confirmed = set()
    pending = None
    trades = []
    equity = []
    cur_atr = 0.0
    total = len(bars_self)

    for i, bar in enumerate(bars_self):
        if i % 1000 == 0:
            print(f"  [{res}m] {i}/{total}", flush=True)
        close_time = bar["time"] + main_sec

        # 1. 成交上一根确认的待成交信号（本根开盘价）
        if pending is not None:
            apply_signal(pending, bar["open"], cur_atr, pos, trades)
            pending = None

        # 2. 止损检查
        if pos.lots:
            check_stop(bar, pos, trades)

        # 3. 算本周期与紧邻上级的笔+买卖点（截至当前）
        win_self = bars_self[: i + 1]
        win_upper = [b for b in bars_upper if b["time"] + upper_sec <= close_time]

        upper_bis = None
        upper_macd = None
        buy_upper, sell_upper = [], []
        if len(win_upper) >= 3:
            upper_bis, _, upper_macd = compute_bis(upper_res, win_upper)
            buy_upper, sell_upper = compute_points(upper_res, upper_bis, None, upper_macd)
        upper_pts = buy_upper + sell_upper

        # 区间套强制对齐：主周期笔端点对齐紧邻上级笔端点（优先级最高）
        locked_pivots = core.lockedPivotsOf(upper_bis)
        self_bis, atr, self_macd = compute_bis(res, win_self, locked_pivots)
        # 区间套强制对齐：把主周期笔拐点对齐到紧邻上级笔拐点（上级底/顶=本级底/顶）
        # 第4参 win_self：幽灵端点防御——上级极值在主周期K线中不存在（跨周期数据源
        # 聚合差异）时跳过对齐，保留主周期真实极值
        if upper_bis:
            self_bis = core.alignBiToUpper(self_bis, upper_bis, upper_sec, win_self)
        cur_atr = atr

        buy_self, sell_self = compute_points(res, self_bis, upper_bis, self_macd)

        # 4. 滞后确认 + 绿色判定 → 生成信号
        cutoff = bar["time"] - CONFIRM_BARS * main_sec
        newest = None
        for p in buy_self + sell_self:
            key = (p["type"], p["time"], round(p["price"], 6))
            if key in confirmed:
                continue
            if p["time"] <= cutoff:
                confirmed.add(key)
                if p["type"] not in ("1买", "1卖"):
                    continue  # 本规则下 2/3 类不参与
                green = is_green(p, upper_pts, atr, time_tol)
                if newest is None or p["time"] > newest["time"]:
                    newest = {"type": p["type"], "time": p["time"], "price": p["price"], "green": green}

        if newest is not None:
            pending = newest

        # 5. 记录权益
        realized = sum(t.get("pnl", 0.0) for t in trades)
        equity.append((bar["time"], realized + mark_to_market(pos, bar["close"])))

    return trades, equity, bars_self


def apply_signal(sig, price, atr, pos, trades):
    """执行信号：只有绿色才开仓，平仓不看颜色。"""
    t = sig["type"]
    green = sig.get("green", False)
    stop_n = STOP_N * atr if atr > 0 else 0.0
    d = pos.direction

    if t == "1买":
        if green:
            if d > 0:
                return  # 已持多，忽略
            if d < 0:
                close_all(pos, price, trades, reason="绿1买反手")
            open_lot(pos, +1, price, price - stop_n, t, trades)
        else:
            if d < 0:
                close_all(pos, price, trades, reason="红1买平空")
    elif t == "1卖":
        if green:
            if d < 0:
                return
            if d > 0:
                close_all(pos, price, trades, reason="绿1卖反手")
            open_lot(pos, -1, price, price + stop_n, t, trades)
        else:
            if d > 0:
                close_all(pos, price, trades, reason="红1卖平多")


# ---------- 报告 ----------
def compute_stats(trades):
    closed = [t for t in trades if "pnl" in t]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    stats = {"trades": len(trades), "closed": len(closed)}
    if closed:
        stats["win_rate"] = len(wins) / len(closed) * 100
        stats["total_pnl"] = sum(t["pnl"] for t in closed)
        stats["avg_win"] = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        stats["avg_loss"] = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        if wins and losses:
            stats["pf"] = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        else:
            stats["pf"] = float("inf") if wins else 0.0
    else:
        stats["win_rate"] = 0.0
        stats["total_pnl"] = 0.0
        stats["avg_win"] = 0.0
        stats["avg_loss"] = 0.0
        stats["pf"] = 0.0
    return stats


def compute_max_dd(equity):
    if not equity:
        return 0.0
    peak = -1e18
    max_dd = 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return max_dd


def report_period(res, bars, trades, equity):
    stats = compute_stats(trades)
    dd = compute_max_dd(equity)
    print(f"----- {res} 分钟周期 -----")
    print(f"  数据: {fmt_time(bars[0]['time'])} ~ {fmt_time(bars[-1]['time'])}（{len(bars)} 根）")
    print(f"  成交 {stats['trades']} 笔 | 平仓 {stats['closed']} 笔 | 胜率 {stats['win_rate']:.1f}%")
    print(f"  总盈亏 {stats['total_pnl']:+.2f} 点 | 平均盈利 {stats['avg_win']:+.2f} | 平均亏损 {stats['avg_loss']:+.2f} | 盈亏比 {stats['pf']:.2f}")
    print(f"  最大回撤 {dd:.2f} 点")
    for t in trades[:20]:
        line = f"    {t['action']} @ {t['price']:.2f}"
        if "pnl" in t:
            line += f"  (盈亏 {t['pnl']:+.2f})"
        if "reason" in t:
            line += f"  [{t['reason']}]"
        if "type" in t:
            line += f"  信号:{t['type']}"
        print(line)
    return stats


def main():
    args = sys.argv[1:]
    run_res = [r for r in args if r in TRADE_RES] if args else TRADE_RES

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    bars_by_res = {res: sorted(data[res], key=lambda x: x["time"]) for res in ["3", "15", "60", "240", "D"]}

    print(f"===== 缠论策略多周期并行回测（绿色共振开仓，无链式区间套）=====")
    print(f"交易周期: {', '.join(run_res)} | 规则: 只有绿色 1买/1卖 开仓，平仓不看颜色，无加仓，止损 N×ATR(N={STOP_N})")
    print()

    all_trades = []
    agg = {"trades": 0, "closed": 0, "wins": 0, "total_pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0, "max_dd": 0.0}

    for res in run_res:
        upper = UPPER_OF[res]
        bars_self = bars_by_res[res]
        bars_upper = bars_by_res[upper]
        print(f"[{res}m] 开始回测（上级 {upper}）...", flush=True)
        trades, equity, bars = run_one_period(res, upper, bars_self, bars_upper)
        stats = report_period(res, bars, trades, equity)
        print()
        all_trades.extend(trades)
        agg["trades"] += stats["trades"]
        agg["closed"] += stats["closed"]
        agg["wins"] += len([t for t in trades if t.get("pnl", 0.0) > 0])
        agg["total_pnl"] += stats["total_pnl"]
        agg["gross_win"] += sum(t.get("pnl", 0.0) for t in trades if t.get("pnl", 0.0) > 0)
        agg["gross_loss"] += sum(t.get("pnl", 0.0) for t in trades if t.get("pnl", 0.0) <= 0)
        agg["max_dd"] += compute_max_dd(equity)

    print("===== 多周期汇总（各周期独立账户简单加总）=====")
    print(f"  总成交 {agg['trades']} 笔 | 总平仓 {agg['closed']} 笔")
    if agg["closed"]:
        print(f"  综合胜率 {agg['wins']/agg['closed']*100:.1f}% | 总盈亏 {agg['total_pnl']:+.2f} 点")
        if agg["gross_loss"]:
            print(f"  盈亏比(Profit Factor) {agg['gross_win']/abs(agg['gross_loss']):.2f}")
    print(f"  最大回撤合计 {agg['max_dd']:.2f} 点")
    print("  注：独立账户假设——各周期各自满仓一手，未做资金合并/仓位叠加，仅用于信号可行性评估。")


if __name__ == "__main__":
    main()
