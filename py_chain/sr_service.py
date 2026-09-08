# -*- coding: utf-8 -*-
"""
支阻位参数调试模块 · 服务编排层（供 /api/sr/* 端点后台线程调用）

职责：
  1. 数据覆盖判定 + 缓存优先取数（不足自动从 TradingView CDP 补拉，合并回写共享缓存）
  2. 各周期重建笔（backtest.build_bis）→ 引擎 compute_srflip（参数全透传）
  3. meta 派生（当前价/各周期ATR/合并容差等，供页面展示合并语义与级别回填）

纯编排：不连 CDP 之外的东西、不绘图；绘图见 sr_draw.py。
级别键一律用 6 个规范化键：W / D / 240 / 60 / 15 / 3（1W→W、1D→D 在入口归一化，
避免破坏引擎 UPPER_OF / minTouchFor / periodNameOf 的键匹配）。
"""

import json

from . import data_loader
from .backtest import build_bis
from .sr_flip import (LEVEL_ORDER, DEFAULT_SR_TYPES, FIB_LEVELS,
                      BOLL_LENGTH, BOLL_MULT, RECENT_BI_COUNT,
                      TOUCH_WEIGHT, BARS_WEIGHT, SIDE_COUNT, MAX_PER_PERIOD,
                      MAX_DIST_ATR, CLUSTER_ATR, MERGE_ATR,
                      RECENT_CLUSTER_ATR, _kindOf, compute_srflip)

# 级别（大 → 小，与 LEVEL_ORDER 相对顺序一致；不含 30S / 1W / 1D 别名键）
CANONICAL_LEVELS = ["W", "D", "240", "60", "15", "3"]
DEFAULT_LEVELS = ["D", "240", "60", "15", "3"]
# 页面 minTouch 矩阵默认（与引擎 _MIN_TOUCH_DEFAULT 一致；W 引擎无收录落底 4）
MIN_TOUCH_UI = {"W": 4, "D": 4, "240": 4, "60": 4, "15": 3, "3": 8}
MIN_BARS_OK = 6   # 单周期可建笔的最少K线数（缓存覆盖判定下限）


def normalize_periods(periods):
    """规范化级别列表：1W→W、1D→D、去重、按 LEVEL_ORDER 大→小排序。
    @raises ValueError 白名单外的值（含 30S、非法字符串）"""
    alias = {"1W": "W", "1D": "D", "4H": "240", "1H": "60"}
    out, seen = [], set()
    for p in periods or []:
        s = str(p).strip().upper()
        s = alias.get(s, s)
        if s not in CANONICAL_LEVELS:
            raise ValueError(f"不支持的级别：{p}")
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return sorted(out, key=lambda x: LEVEL_ORDER.index(x))


# 缓存「可用」的最少时间跨度（秒）：数据源深度往往够不到用户填的起始日期
# （如 3m 对 OANDA 仅 ~2 个月），跨度足够即视为「可用全量」，避免每次计算都重拉。
SPAN_OK_SEC = 7 * 86400


def _ts_short(ts):
    """UTC 时间戳 → 本地(UTC+8) "MM-DD HH:MM"（与页面展示口径一致）。"""
    import time as _t
    return _t.strftime("%m-%d %H:%M", _t.gmtime(int(ts) + 8 * 3600))


def coverage(bars_by_period, periods, from_ts):
    """缓存覆盖判定：每周期返回 'ok' | 'partial' | 'missing' | 'short'。
    ok      = 首根 <= from_ts（覆盖所填起始日期）且 >= MIN_BARS_OK 根
    partial = 首根晚于 from_ts，但时间跨度 >= SPAN_OK_SEC（数据源深度边界，
              早于该窗口的历史不可得，按可得全量使用；不触发补拉）
    short   = 有数据但既未覆盖 from_ts、跨度又不足（疑似残缺缓存，需补拉）
    missing = 无该周期键"""
    out = {}
    for res in periods:
        bars = bars_by_period.get(res) or []
        if not bars:
            out[res] = "missing"
        elif len(bars) < MIN_BARS_OK:
            out[res] = "short"
        elif bars[0]["time"] <= from_ts:
            out[res] = "ok"
        elif bars[-1]["time"] - bars[0]["time"] >= SPAN_OK_SEC:
            out[res] = "partial"
        else:
            out[res] = "short"
    return out


def ensure_data(periods, from_ts, log=None, refresh=False, symbol=None):
    """缓存优先取数：先读 bars_all_tf.json 判覆盖；refresh 或存在 missing/short 周期时，
    只对缺的周期经 CDP 补拉，与旧缓存合并后回写（保留 30S 等既有键）。
    partial（深度够不到起始日期但跨度充足）不触发补拉，日志说明后用可得全量。
    @raises RuntimeError  某必需周期最终仍无K线
    @returns { 周期: [{time,open,high,low,close}] }（仅含请求周期，时间升序）
    """
    log = log or (lambda *a, **k: None)
    cache_file = data_loader.CACHE_FILE
    cached = {}
    try:
        cached = data_loader.load_cached(cache_file)
    except (OSError, ValueError, json.JSONDecodeError):
        cached = {}
    cov = {} if refresh else coverage(cached, periods, from_ts)
    if refresh:
        # 强制刷新：全部请求周期都视为缺失（cov 置空不能用 cov[r] 走读缓存分支）
        missing = list(periods)
        partial = []
    else:
        missing = [r for r in periods if cov.get(r) in ("missing", "short")]
        partial = [r for r in periods if cov.get(r) == "partial"]
    if not missing:
        for r in periods:
            bars = cached[r]
            if cov[r] == "partial":
                log(f"读缓存 {r}：{len(bars)} 根，但数据源深度仅到 "
                    f"{_ts_short(bars[0]['time'])}（早于此窗口的历史不可得，按可得全量计算）")
            else:
                log(f"读缓存 {r}：{len(bars)} 根（已覆盖起始日期）")
        return {r: cached[r] for r in periods}
    reason = "强制刷新" if refresh else "缺失/残缺缓存（根数或跨度不足）"
    log(f"缓存未覆盖 {missing}（{reason}）→ 从 TradingView 补拉 ...")
    cfg = data_loader.CDPConfig(periods=missing)
    fetched = data_loader.fetch_bars(cfg=cfg, from_ts=from_ts, cache=False,
                                     symbol=symbol, log=log)
    merged = dict(cached)
    got = False
    for r in missing:
        if fetched.get(r):
            merged[r] = fetched[r]
            got = True
    if got:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False)
            log(f"已合并回写缓存 {cache_file}（保留其它既有键）")
        except OSError as e:
            log(f"缓存回写失败（忽略）：{e}")
    out = {}
    for r in periods:
        bars = merged.get(r)
        if not bars:
            raise RuntimeError(f"周期 {r} 未能获取K线（CDP 拉取失败或数据源无此周期）")
        out[r] = bars
    return out


def engine_kwargs_of(cfg):
    """把页面 cfg 映射为 compute_srflip 关键字参数（缺失键走引擎默认）。"""
    return {
        "clusterAtr": float(cfg.get("clusterAtr", CLUSTER_ATR)),
        "mergeAtr": float(cfg.get("mergeAtr", MERGE_ATR)),
        "recentClusterAtr": float(cfg.get("recentClusterAtr", RECENT_CLUSTER_ATR)),
        "maxDistAtr": float(cfg.get("maxDistAtr", MAX_DIST_ATR)),
        "maxPerPeriod": int(cfg.get("maxPerPeriod", MAX_PER_PERIOD)),
        "minTouchsIn": dict(cfg.get("minTouchs", {}) or {}),
        "srTypes": tuple(cfg.get("srTypes", DEFAULT_SR_TYPES)),
        "clusterParts": tuple(cfg.get("clusterParts", ("flip", "recent"))),
        "fibLevels": [float(x) for x in cfg.get("fibLevels", FIB_LEVELS)],
        "bollLength": int(cfg.get("bollLength", BOLL_LENGTH)),
        "bollMult": float(cfg.get("bollMult", BOLL_MULT)),
        "recentBiCount": int(cfg.get("recentBiCount", RECENT_BI_COUNT)),
        "touchWeight": float(cfg.get("touchWeight", TOUCH_WEIGHT)),
        "barsWeight": float(cfg.get("barsWeight", BARS_WEIGHT)),
        "sideCount": int(cfg.get("sideCount", SIDE_COUNT)),
        "mergeDetail": True,   # 调试页恒开成员追溯（明细表/RAW 叠加都依赖）
    }


def build_chain_result(bars_by_period, cfg, log=None):
    """重建笔 → compute_srflip → meta。
    @returns (result, meta)；result 键：periods/merged/drawnByPeriod/currentPrice/periodAtrs
    """
    log = log or (lambda *a, **k: None)
    periods = list(cfg["periods"])
    log("重建各周期笔 ...")
    bis_by_period = build_bis(bars_by_period, periods=periods)
    for res in periods:
        log(f"  {res:>4}: {len(bis_by_period.get(res) or [])} 笔"
            f"（K线 {len(bars_by_period[res])} 根）")
    kw = engine_kwargs_of(cfg)
    log(f"计算支阻位（{','.join(kw['srTypes'])}；合并容差×最小ATR 参数={kw['mergeAtr']}）...")
    result = compute_srflip(bis_by_period, bars_by_period, periods, **kw)
    meta = build_meta(result, bars_by_period, bis_by_period, cfg, kw)
    return result, meta


def build_meta(result, bars_by_period, bis_by_period, cfg, kw=None):
    """派生展示用元信息：当前价、各周期 ATR、合并容差（与引擎 717-720 口径一致）、
    bar/笔计数、覆盖判定。"""
    kw = kw or engine_kwargs_of(cfg)
    periodAtrs = result["periodAtrs"]
    # 引擎合并容差 = mergeAtr × 最小「有候选周期」ATR（口径同 compute_srflip）
    atrValues = [periodAtrs[r] for r in cfg["periods"]
                 if result["periods"].get(r) and r in periodAtrs]
    minAtr = min(atrValues) if atrValues else 0.0
    periods_cov = coverage(bars_by_period, cfg["periods"], cfg.get("from_ts", 0))
    return {
        "current_price": result["currentPrice"],
        "per_level_atr": {r: periodAtrs[r] for r in cfg["periods"] if r in periodAtrs},
        "bar_counts": {r: len(bars_by_period.get(r) or []) for r in cfg["periods"]},
        "bi_counts": {r: len(bis_by_period.get(r) or []) for r in cfg["periods"]},
        "min_atr": round(minAtr, 6),
        "merge_tol": round(kw["mergeAtr"] * minAtr, 6),
        "coverage": periods_cov,
    }


def main_lines(result):
    """drawnByPeriod → 每显示周期画线数据（含合并项全字段，供行点击/成员查询）。
    @returns { 显示周期: [line,...] }，line 带 breakTime/price/label/level/sources 等引擎原字段。"""
    return {str(L): list(lines) for L, lines in (result.get("drawnByPeriod") or {}).items()}


def raw_pool_lines(result, maxDistAtr=MAX_DIST_ATR):
    """合并前原始候选 RAW 线（调试叠加用）：与合并灰线同一继承口径，同框对照。

    对每个显示周期 L（drawnByPeriod 的键），叠加 来源级别 ≥ L（即比 L 粗或同级，
    LEVEL_ORDER.index(R) <= index(L)）的各来源周期原始候选（result["periods"]：
    密集区已按 maxPerPeriod 截断 + fib + boll，天然有上限）。
    原始候选再多条最终也只会合成 ≤2×sideCount 条灰线，这里保留全部——
    同价位多来源重叠正是「该价被跨级测试、合并成一 条」的可视依据。

    距离过滤（防喧宾夺主）：只保留距 currentPrice ≤ maxDistAtr×该来源周期ATR 的
    候选（与 drawnByPeriod 选取的距离上限同口径；来源无 ATR 或无限价时不限）。
    想扩大可见范围就调大页面「选取距离上限(maxDistAtr)」。

    @returns { L: [ {time, price, kind, type, source} ] }（price 升序；成员追溯
    merged.members 仍在明细表展开用，不在这里重复画）
    """
    drawn = result.get("drawnByPeriod") or {}
    periods = result.get("periods") or {}
    periodAtrs = result.get("periodAtrs") or {}
    current = result.get("currentPrice")
    out = {}
    for L in drawn:
        li = LEVEL_ORDER.index(L)
        items = []
        for R, cands in periods.items():
            try:
                ri = LEVEL_ORDER.index(R)
            except ValueError:
                continue
            if ri > li:
                continue  # R 比 L 更细：不继承到本图（与灰线口径一致）
            atr = periodAtrs.get(R)
            for f in cands:
                price = float(f["price"])
                if current is not None and atr:
                    if abs(price - current) > maxDistAtr * atr:
                        continue
                items.append({"time": int(f.get("breakTime") or 0), "price": price,
                              "kind": _kindOf(f), "type": f.get("type") or "",
                              "source": R})
        items.sort(key=lambda x: x["price"])
        out[L] = items
    return out
