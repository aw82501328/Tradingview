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
from .mark_entry import (compute_entries, filterDetectPeriods,
                         NEAR, ZS_EXIT_WEAK_RATIO)
from . import mark_entry
from .tv_draw import draw_trades
from .chan_core import intervalSecOf, fmtT

DEFAULT_PERIODS = ["D", "240", "60", "15", "3"]


def _load_sr_preset(name):
    """按名读取旧版支阻位预设 cfg（历史兼容：py_chain/web/sr_presets.json）。
    @raises SystemExit 预设不存在 / 文件不可读"""
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "sr_presets.json")
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"支阻位预设文件不可读：{path}（{e}）")
    names = []
    for p in entries if isinstance(entries, list) else []:
        if not isinstance(p, dict):
            continue
        names.append(str(p.get("name")))
        if p.get("name") == name and isinstance(p.get("cfg"), dict):
            return dict(p["cfg"])
    raise SystemExit(f"未找到支阻位预设「{name}」（可用：{names or '无'}）")


def _sr_cfg_for_symbol(symbol, preset_name=None):
    """优先读该品种 effective_sr；若仍传旧预设名且需要兼容则回退旧文件。"""
    from . import param_center
    cfg = dict(param_center.effective_sr(symbol))
    if preset_name:
        try:
            legacy = _load_sr_preset(preset_name)
            # 优先品种桶：桶已有非默认内容则保留；否则用旧预设名
            if cfg == param_center.SR_DEFAULTS:
                cfg = legacy
        except SystemExit:
            pass  # 旧名不可用时仍用品种桶
    return cfg


def parse_from(s):
    """'YYYY-MM-DD' → UTC 时间戳（当天 00:00 UTC）。"""
    y, m, d = (int(x) for x in s.split("-"))
    return calendar.timegm(datetime.datetime(y, m, d).timetuple())


def build_full_chain(bars_by_period, periods, with_marks=True, sr_types=None, fib_levels=None,
                     boll_length=None, boll_mult=None, module_params=None):
    """全链路（对整段数据一次性计算），返回各阶段结果。

    30S（--with-30s 追加）只参与 bis 计算与进出场（compute_entries），
    不进 marks/sr/plan——与 JS 端语义一致（mark-buy-sell/mark-sr-flip/trading-plan
    的周期不含 30S，sr_flip 的 LEVEL_ORDER 也未收录 30S）。
    sr_types/fib_levels 透传给 compute_srflip（None 用其默认 cluster+boll / 0.382,0.5,0.618）；
    boll_length/boll_mult 透传 BOLL 周期与标准差倍数（None 用默认 26/2）。
    module_params 为参数中心模块参数（marks/plan/entry；None 用各模块默认）。
    """
    mp = module_params or {}
    core = [p for p in periods if str(p).upper() != "30S"]
    bis = build_bis(bars_by_period, periods)
    marks = {}
    if with_marks:
        marks = compute_all_marks(bis, bars_by_period, core, fromTs=None,
                                  **(mp.get("marks") or {}))
    srKw = {}
    if sr_types is not None:
        srKw["srTypes"] = tuple(sr_types)
    if fib_levels is not None:
        srKw["fibLevels"] = fib_levels
    if boll_length is not None:
        srKw["bollLength"] = boll_length
    if boll_mult is not None:
        srKw["bollMult"] = boll_mult
    sr = compute_srflip(bis, bars_by_period, core, **srKw)
    # plan 模块的 trendRes（顺势参考周期）/ rangeRes（震荡判定参考周期）单独取出：
    # 不混入震荡阈值 cfg；trendRes "" = 关闭顺势过滤，rangeRes "" = 未配置（必填项，
    # 防御语义——compute_plan 全部周期观望）
    plan_mp = dict(mp.get("plan") or {})
    trend_res = plan_mp.pop("trendRes", None)
    range_res = plan_mp.pop("rangeRes", None)
    # 方向相位判定参数（2026-09-17；缺省 → trading_plan 常量默认，相位判定默认开启）
    trend_cfg = {k: plan_mp.pop(k) for k in
                 ("trendRebound", "reboundNearPts", "reboundAngleRef") if k in plan_mp}
    plan = compute_plan(bis, bars_by_period, core, cfg=plan_mp or None,
                        range_res=range_res)
    srLevels = (sr or {}).get("merged") or []
    ep = mp.get("entry") or {}
    entries = compute_entries(bis, bars_by_period, plan, srLevels,
                              detectPeriods=filterDetectPeriods(periods),
                              near=ep.get("near", NEAR),
                              with_30s=any(str(p).upper() == "30S" for p in periods),
                              zs_exit_weak_ratio=ep.get("zs_exit_weak_ratio", ZS_EXIT_WEAK_RATIO),
                              trend_res=trend_res,
                              trend_cfg=trend_cfg or None)
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
    ap.add_argument("--lead-days", dest="lead_days", type=int, default=0,
                    help="预热提前天数（>0 时起始日期=交易开始日：取数自动前移 N 天建状态，"
                         "交易起点前只推进状态不成交、空仓起步；0=现行为，用 --warmup 根数预热）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 调试端口，默认 9222")
    ap.add_argument("--use-cache", action="store_true", help="优先读 bars_all_tf.json 缓存")
    ap.add_argument("--warmup", type=int, default=60, help="预热K线数，默认 60")
    ap.add_argument("--no-draw", action="store_true", help="不把成交箭头回画到图表")
    ap.add_argument("--no-marks", action="store_true", help="回测时跳过买卖点标记计算（更快）")
    ap.add_argument("--sr-types", default=None,
                    help="支阻位类型开关，逗号分隔 cluster,boll（可选 cluster/fib/boll，默认 cluster,boll，与 JS --sr-types 一致）")
    ap.add_argument("--fib-levels", default=None,
                    help="黄金分割比率，逗号分隔（默认 0.382,0.5,0.618）")
    ap.add_argument("--boll-length", type=int, default=None,
                    help="BOLL SMA 周期（默认 26，已收盘K线口径）")
    ap.add_argument("--boll-mult", type=float, default=None,
                    help="BOLL 标准差倍数（默认 2）")
    ap.add_argument("--lots", type=int, default=None,
                    help="每笔进场手数（盈亏 = 价差 × 方向 × 手数 × 合约乘数，1手=0.01标准手）；"
                         "缺省按品种取参数中心值（品种行/默认行）")
    ap.add_argument("--slip-stop", type=float, default=None,
                    help="止损位滑点（绝对价格：正确侧支阻位外侧偏移）；缺省用参数中心值（默认 3）")
    ap.add_argument("--slip-fallback", type=float, default=None,
                    help="最大止损（买点止损不低于进场价−该值，卖点不高于进场价+该值；"
                         "支阻位更远时收到此价）；缺省用参数中心值（默认 10）")
    ap.add_argument("--slip-be", type=float, default=None,
                    help="保本滑点（beStop = 进场成交K线极值 ± 该值）；缺省用参数中心值（默认 3）")
    ap.add_argument("--slip-stop-atr-k", type=float, default=None,
                    help="止损滑点ATR系数（有效滑点=固定值+系数×ATR(14,背驰周期)，0=关闭）；缺省用参数中心值（默认 0）")
    ap.add_argument("--slip-fallback-atr-k", type=float, default=None,
                    help="最大止损ATR系数（同上口径）；缺省用参数中心值（默认 0）")
    ap.add_argument("--slip-be-atr-k", type=float, default=None,
                    help="保本滑点ATR系数（同上口径）；缺省用参数中心值（默认 0）")
    ap.add_argument("--near", type=float, default=None,
                    help="近支阻阈值（绝对价差，不乘 ATR）；缺省用参数中心值（默认 10）")
    ap.add_argument("--sr-preset", default=None,
                    help="支阻位：优先读该 --symbol 的参数中心品种桶；若仍传旧预设名可作兼容回退"
                         "（新任务不依赖预设名）")
    args = ap.parse_args(argv)

    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    if args.with_30s and "30S" not in periods:
        periods.append("30S")
    from_ts = parse_from(args.from_date)
    # 预热提前（--lead-days > 0）：起始日期=交易开始日——取数自动前移 N 天建状态，
    # 引擎 start_ts 前只推进状态不交易（空仓起步）；0=现行为（warmup 根数预热）
    lead_days = args.lead_days or 0
    data_from_ts = max(0, from_ts - lead_days * 86400) if lead_days > 0 else from_ts
    start_ts = from_ts if lead_days > 0 else None

    # 1. 取数（CDP 或缓存）
    print(f"取数：symbol={args.symbol} periods={periods} from={args.from_date} "
          f"use_cache={args.use_cache}"
          + (f" 预热提前 {lead_days} 天（数据起点前移）" if lead_days > 0 else ""))
    bars_by_period = load_bars(periods=periods, from_ts=data_from_ts,
                               use_cache=args.use_cache, symbol=args.symbol)
    for res in periods:
        n = len(bars_by_period.get(res, []))
        if n:
            first = fmtT(bars_by_period[res][0]["time"])
            last = fmtT(bars_by_period[res][-1]["time"])
            print(f"  {res:>4}: {n} 根（{first} ~ {last}）")
        else:
            print(f"  {res:>4}: 无数据")

    # 2. 全链路（实时态摘要）—— 参数中心（参数配置页）：CLI 显式值优先，缺省用参数中心值
    from . import param_center
    from .chan_core import apply_cfg
    pm = param_center.effective_all(args.symbol)
    apply_cfg(param_center.chan_cfg_effective(args.symbol))
    ep = pm["entry"]
    lots = args.lots if args.lots is not None else param_center.lots_of(ep, args.symbol)
    slip_stop = args.slip_stop if args.slip_stop is not None else ep["slip_stop"]
    slip_fallback = args.slip_fallback if args.slip_fallback is not None else ep["slip_fallback"]
    slip_be = args.slip_be if args.slip_be is not None else ep["slip_be"]
    slip_stop_atr_k = args.slip_stop_atr_k if args.slip_stop_atr_k is not None else ep["slip_stop_atr_k"]
    slip_fallback_atr_k = args.slip_fallback_atr_k if args.slip_fallback_atr_k is not None else ep["slip_fallback_atr_k"]
    slip_be_atr_k = args.slip_be_atr_k if args.slip_be_atr_k is not None else ep["slip_be_atr_k"]
    near = args.near if args.near is not None else ep["near"]
    module_params = {"plan": pm["plan"], "marks": pm["points"],
                     "exit_min_merged": ep["exit_min_merged"],
                     "realtime_min_bars": ep["realtime_min_bars"],
                     "zs_exit_weak_ratio": ep["zs_exit_weak_ratio"],
                     "entry": ep}
    sr_types = [t.strip().lower() for t in args.sr_types.split(",") if t.strip()] if args.sr_types else None
    fib_levels = ([float(x.strip()) for x in args.fib_levels.split(",") if x.strip()]
                  if args.fib_levels else None)
    chain = build_full_chain(bars_by_period, periods, with_marks=not args.no_marks,
                             sr_types=sr_types, fib_levels=fib_levels,
                             boll_length=args.boll_length, boll_mult=args.boll_mult,
                             module_params=module_params)
    print_chain(chain, periods)

    # 3. 点状回测（支阻：优先该品种参数中心桶 → normalize → engine_kwargs）
    from .webapp import ControlApp
    from .sr_service import engine_kwargs_of
    preset_cfg = _sr_cfg_for_symbol(args.symbol, args.sr_preset)
    preset_cfg.pop("symbols", None)
    preset_cfg.update(periods=periods, symbol=args.symbol,
                      **{"from": args.from_date})
    preset_cfg = ControlApp.normalize_sr_cfg(preset_cfg)
    sr_kwargs = engine_kwargs_of(preset_cfg)
    print(f"支阻位（品种桶）已载入：srTypes={preset_cfg['srTypes']}"
          + (f" 人工周期={list(preset_cfg.get('manualLevels') or {})}" if preset_cfg.get("manualLevels") else ""))
    print("\n回测中（逐根K线重放整条链路）...")
    result = run_backtest(bars_by_period, periods=periods,
                          warmup_bars=args.warmup, with_marks=not args.no_marks,
                          start_ts=start_ts,
                          sr_types=sr_types, fib_levels=fib_levels,
                          boll_length=args.boll_length, boll_mult=args.boll_mult,
                          lots=lots,
                          contract_mult=mark_entry.contract_mult_of(args.symbol),
                          slip_stop=slip_stop,
                          slip_fallback=slip_fallback, slip_be=slip_be,
                          slip_stop_atr_k=slip_stop_atr_k,
                          slip_fallback_atr_k=slip_fallback_atr_k,
                          slip_be_atr_k=slip_be_atr_k,
                          near=near, sr_kwargs=sr_kwargs, module_params=module_params,
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
