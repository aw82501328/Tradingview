# -*- coding: utf-8 -*-
"""
编排入口：取数 → 全链路 → 回测 → 回画 → 打印统计

用法：
    python -m py_chain.main --symbol OANDA:XAUUSD --periods D,240,60,15,3 --from 2026-07-02
    python -m py_chain.main --use-cache --no-draw          # 读缓存、只回测不画图

链路顺序（与 JS 实时画图一致）：画笔(buildBi) → 买卖点(compute_all_marks)
→ 支阻位(compute_srflip) → 交易计划(compute_plan) → 进出场(compute_entries)。
回测完成后只回画「实际成交」的进场箭头：做多=红向上、做空=绿向下（见 tv_draw）。
"""

import argparse
import calendar
import datetime
import sys

from .data_loader import CDPConfig, load_bars
from .backtest import build_bis, run_backtest, summarize
from .mark_buy_sell import compute_all_marks
from .sr_flip import compute_srflip
from .trading_plan import compute_plan
from .mark_entry import compute_entries
from .tv_draw import draw_trades
from .chan_core import intervalSecOf, fmtT

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]


def parse_from(s):
    """'YYYY-MM-DD' → UTC 时间戳（当天 00:00 UTC）。"""
    y, m, d = (int(x) for x in s.split("-"))
    return calendar.timegm(datetime.datetime(y, m, d).timetuple())


def build_full_chain(bars_by_period, periods, with_marks=True):
    """全链路（对整段数据一次性计算），返回各阶段结果。

    30S（--with-30s 追加）只参与 bis 计算与进出场（compute_entries），
    不进 marks/sr/plan——与 JS 端语义一致（mark-buy-sell/mark-sr-flip/trading-plan
    的周期不含 30S，sr_flip 的 LEVEL_ORDER 也未收录 30S）。
    """
    core = [p for p in periods if str(p).upper() != "30S"]
    bis = build_bis(bars_by_period, periods)
    marks = {}
    if with_marks:
        marks = compute_all_marks(bis, bars_by_period, core, fromTs=None)
    sr = compute_srflip(bis, bars_by_period, core)
    plan = compute_plan(bis, bars_by_period, core)
    srLevels = (sr or {}).get("merged") or []
    entries = compute_entries(bis, bars_by_period, plan, srLevels,
                              detectPeriods=[p for p in core if p != "D"],
                              with_30s=any(str(p).upper() == "30S" for p in periods))
    return {"bis": bis, "marks": marks, "sr": sr, "plan": plan, "entries": entries}


def print_chain(chain, periods):
    """打印全链路各周期摘要。"""
    plan = chain["plan"]
    print("\n===== 全链路摘要（整段数据实时态）=====")
    for res in periods:
        p = plan.get(res)
        if not p:
            continue
        bis = chain["bis"].get(res, [])
        marks = chain["marks"].get(res, [])
        nMark = len(marks)
        nBuy = sum(1 for m in marks if "买" in m["label"])
        nSell = sum(1 for m in marks if "卖" in m["label"])
        print(f"[{res:>4}] 笔 {len(bis):>3} 买卖点 {nMark:>3}（多{nBuy}/空{nSell}）"
              f" 计划方向={p['direction']} 策略={p['strategy']} "
              f"({p.get('pointDesc', '')})")
    entries = chain["entries"]
    total = sum(len(v) for v in entries.values())
    if total:
        print(f"全链路进场信号：{total} 个")
        for res, sigs in sorted(entries.items(), key=lambda kv: intervalSecOf(kv[0]) or 0):
            for s in sigs:
                d = "多" if s["direction"] == "long" else "空"
                print(f"  {res:>4}  {d} {s['strategyKey']:<12} "
                      f"@ {fmtT(s['time'])} {s['price']:.2f} 近支阻 {s['nearSr']:.2f}")
    else:
        print("全链路进场信号：无")


def print_stats(result):
    """打印回测统计。"""
    print("\n===== 回测统计 =====")
    for k, v in summarize(result).items():
        print(f"  {k}: {v}")
    trades = result["trades"]
    if trades:
        print("\n===== 成交明细（下一根开盘价成交，未实现出场）=====")
        for t in trades[:60]:
            d = "多" if t["direction"] == "long" else "空"
            print(f"  #{t['tradeNo']:>3} {t['markRes']:>4} {d} {t['strategyKey']:<12} "
                  f"信号@ {fmtT(t['signalTime'])} {t['signalPrice']:.2f} "
                  f"成交@ {fmtT(t['entryTime'])} {t['entryPrice']:.2f}")
        if len(trades) > 60:
            print(f"  ... 共 {len(trades)} 笔")


def main(argv=None):
    # Windows 控制台默认 GBK，统一转 UTF-8 输出，避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Python 化回测链路：取数→全链路→回测→回画→统计")
    ap.add_argument("--symbol", default=None, help="品种，如 OANDA:XAUUSD（先切换图表品种再取数）")
    ap.add_argument("--periods", default=",".join(DEFAULT_PERIODS), help="周期列表，默认 D,240,60,15,3")
    ap.add_argument("--with-30s", action="store_true", help="追加 30 秒级别（30S 只取最近3天数据，供 3 分钟的进场背驰检测）")
    ap.add_argument("--from", dest="from_date", default="2026-07-02", help="起始日期 YYYY-MM-DD（UTC）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 调试端口，默认 9222")
    ap.add_argument("--use-cache", action="store_true", help="优先读 bars_all_tf.json 缓存")
    ap.add_argument("--warmup", type=int, default=60, help="预热K线数，默认 60")
    ap.add_argument("--no-draw", action="store_true", help="不把成交箭头回画到图表")
    ap.add_argument("--no-marks", action="store_true", help="回测时跳过买卖点标记计算（更快）")
    args = ap.parse_args(argv)

    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    if args.with_30s and "30S" not in periods:
        periods.append("30S")
    from_ts = parse_from(args.from_date)

    # 1. 取数（CDP 或缓存）
    print(f"取数：symbol={args.symbol} periods={periods} from={args.from_date} "
          f"use_cache={args.use_cache}")
    bars_by_period = load_bars(periods=periods, from_ts=from_ts,
                               use_cache=args.use_cache, symbol=args.symbol)
    for res in periods:
        n = len(bars_by_period.get(res, []))
        if n:
            first = fmtT(bars_by_period[res][0]["time"])
            last = fmtT(bars_by_period[res][-1]["time"])
            print(f"  {res:>4}: {n} 根（{first} ~ {last}）")
        else:
            print(f"  {res:>4}: 无数据")

    # 2. 全链路（实时态摘要）
    chain = build_full_chain(bars_by_period, periods, with_marks=not args.no_marks)
    print_chain(chain, periods)

    # 3. 点状回测
    print("\n回测中（逐根K线重放整条链路）...")
    result = run_backtest(bars_by_period, periods=periods,
                          warmup_bars=args.warmup, with_marks=not args.no_marks,
                          log=lambda *a: print(*a) if a and a[0].startswith("回测") else None)

    # 4. 打印统计
    print_stats(result)

    # 5. 回画实际成交的进场箭头
    if not args.no_draw:
        trades = result["trades"]
        if not trades:
            print("\n无成交记录，跳过回画")
        else:
            print(f"\n回画 {len(trades)} 个实际成交进场箭头到 TradingView 图表...")
            r = draw_trades(trades, cfg=CDPConfig(port=args.port),
                            clear_first=True, log=lambda *a: print(*a))
            print(f"回画完成：已画 {r['drawn']}，清除旧标记 {r['cleared']}，失败 {r['errors']}")
    else:
        print("\n已跳过回画（--no-draw）")

    print("\n完成。")


if __name__ == "__main__":
    sys.exit(main())
