# -*- coding: utf-8 -*-
"""支阻区间（OHLC 高低点聚类）—— SPEC 方法唯一算法源。

已确认高低点 → 价格聚类 → ATR 扩展区间 → 有效性筛选 → 就近选取。
纯函数模块：只吃已收盘K线切片（时间递增，dict 至少含 time/high/low/close），
无跨拍隐藏状态 → 前缀稳定（对任意前缀 bars[:t] 的计算结果 ≡ 当时在线计算），
无未来数据。回测（BacktestEngine 逐拍切片重算）与实盘（同引擎同路径）共用。

与 sr_flip 的关系：srMode="zones" 时整系统切换到本模块（密集区/黄金分割/BOLL
全部停用）；候选格式由 zone_candidates() 适配，关键语义：
  kind = "support"(低点聚类) / "resistance"(高点聚类)——由聚类来源决定，
        与当前价位置无关（价格跌进支撑区间正是买点，不能按位置丢方向性）。
  price = 按 kind 的远侧边界（support→lower / resistance→upper）——
        mark_entry.stop_ref_of / live_trader.provisional_sl 沿 price 做侧向判断，
        该语义使止损自然落在命中区间外侧（多单=下沿−滑点，空单=上沿+滑点），
        现有代码零改动即正确。
  进场闸门（mark_entry.near_zone，背驰点+区间）：背驰点价 ∈ 方向匹配区间
  [lower−near, upper+near]（多头=支撑区间、空头=压力区间）。
"""
from __future__ import annotations

# ------------------------------------------------------------
# 起始默认参数（SPEC：可复现起始值，尚未按品种调优）
# ------------------------------------------------------------
ZONE_DEFAULTS = {
    "zoneLookbackBars": 300,   # 回溯根数（每周期各自计数，只看最近 N 根已收盘K线）
    "zonePivotBars": 3,        # 拐点确认：左右各 span 根；第 i 根拐点 i+span 收盘后可用
    "zoneClusterAtr": 0.5,     # 聚类容差 × 当前 ATR：合并后组内跨度上限
    "zonePadAtr": 0.15,        # 扩展系数 × 当前 ATR：区间 = [组内最低−k, 组内最高+k]
    "zoneEventGap": 7,         # 事件间隔（根）：相邻不足 7 根的拐点合并为一次事件
    "zoneMinEvents": 2,        # 最少独立事件：证据不足的区间不入选
    "zoneInvalidBuf": 0.25,    # 失效缓冲 × 当时 ATR：连续两根收盘越界各超缓冲 → 失效
    "zoneAtrLen": 14,          # ATR 长度（Wilder 平滑）
    "zoneMaxPerSide": 0,       # 每侧保留数（0=不限，全部有效区间进池）
}
MIN_BARS = 30                 # 数据下限：不足不出区间，不强行补齐


def _num(v, d):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return d
    return v


def zone_kwargs_of(cfg, defaults=None):
    """cfg（页面/品种桶字符串或数值）→ 本模块参数 dict（缺失走默认，非法回退默认）。"""
    d = dict(ZONE_DEFAULTS if defaults is None else defaults)
    ints = ("zoneLookbackBars", "zonePivotBars", "zoneEventGap",
            "zoneMinEvents", "zoneAtrLen", "zoneMaxPerSide")
    floats = ("zoneClusterAtr", "zonePadAtr", "zoneInvalidBuf")
    for k in ints:
        if cfg.get(k) not in (None, ""):
            try:
                d[k] = int(float(cfg[k]))
            except (TypeError, ValueError):
                pass
    for k in floats:
        if cfg.get(k) not in (None, ""):
            d[k] = _num(cfg[k], d[k])
    return d


# ------------------------------------------------------------
# 1. Wilder ATR（现值供聚类/扩展，序列供失效缓冲「当时 ATR」）
# ------------------------------------------------------------
def atr_wilder_series(bars, length=14):
    """Wilder 平滑 ATR 序列（与 bars 同长、同序）。

    种子期（i ≤ length）用可得 TR 的累计均值，其后标准 Wilder 递推
    atr = (prev×(length−1) + tr) / length。首元素为 0（无前收）。
    只用输入切片已知数据——前缀稳定。"""
    n = len(bars)
    out = [0.0] * n
    if n < 2:
        return out
    acc = 0.0
    prev = 0.0
    for i in range(1, n):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        if i <= length:
            acc += tr
            prev = acc / i
        else:
            prev = (prev * (length - 1) + tr) / length
        out[i] = prev
    return out


# ------------------------------------------------------------
# 2. 已确认拐点（i+span 收盘后可用；末 span 根不作拐点）
# ------------------------------------------------------------
def extract_pivots(bars, span=3):
    """已确认局部低/高点。

    低点：low ≤ 左 span 根的 low 且 严格 < 右 span 根的 low（高点对称：
    ≥ 左、严格 > 右）——同价平台保留靠后拐点。只扫 i ∈ [span, n-span)：
    第 i 根上的拐点在第 i+span 根收盘后才可用，末 span 根永不作拐点
    （无未来数据）。"""
    n = len(bars)
    lows, highs = [], []
    for i in range(span, n - span):
        lo_i, hi_i = bars[i]["low"], bars[i]["high"]
        is_low = is_high = True
        for j in range(i - span, i):
            if bars[j]["low"] < lo_i:
                is_low = False
            if bars[j]["high"] > hi_i:
                is_high = False
        for j in range(i + 1, i + 1 + span):
            if bars[j]["low"] <= lo_i:
                is_low = False
            if bars[j]["high"] >= hi_i:
                is_high = False
        if is_low:
            lows.append({"i": i, "time": bars[i]["time"], "price": lo_i})
        if is_high:
            highs.append({"i": i, "time": bars[i]["time"], "price": hi_i})
    return {"low": lows, "high": highs}


# ------------------------------------------------------------
# 3. 聚类：反复合并价格跨度最小的相邻组
# ------------------------------------------------------------
def cluster_pivots(pivots, tol):
    """相近拐点聚成组：每个拐点先成一组，反复合并「合并后跨度最小」的相邻组
    （按价格升序相邻），要求合并后 max−min ≤ tol；跨度平票优先合并价格
    较低的组。确定性输出（组内按价格升序，组间按价格升序）。"""
    groups = [[p] for p in sorted(pivots, key=lambda p: p["price"])]
    if tol <= 0 or len(groups) < 2:
        return groups

    def key_of(idx):
        prices = [p["price"] for p in groups[idx]] + [p["price"] for p in groups[idx + 1]]
        return (max(prices) - min(prices), min(prices), idx)

    while len(groups) > 1:
        best = min(range(len(groups) - 1), key=key_of)
        if key_of(best)[0] > tol:
            break
        groups[best:best + 2] = [groups[best] + groups[best + 1]]
    return groups


def events_of(pivots, gap=7):
    """独立事件：按 bar 序相邻 <gap 根链式合并为一次事件（事件时间=链末拐点）。
    @returns [ [pivot,...] ] 事件链列表（按时间升序，链内 pivot 按 i 升序）"""
    if not pivots:
        return []
    pts = sorted(pivots, key=lambda p: p["i"])
    events, cur = [], [pts[0]]
    for a, b in zip(pts, pts[1:]):
        if b["i"] - a["i"] >= gap:
            events.append(cur)
            cur = [b]
        else:
            cur.append(b)
    events.append(cur)
    return events


# ------------------------------------------------------------
# 4. 有效性：失效清零 + 失效后 ≥minEvents 次新事件
# ------------------------------------------------------------
def _zone_validity(kind, lower, upper, group, bars, atrs, buf_k=0.25, gap=7):
    """扫描失效并统计失效后独立事件数。

    失效：连续两根收盘越界且各自超过当时 0.25×ATR 缓冲（支撑=低于下界、
    压力=高于上界）。发生失效后之前的事件不再计入；只有最后一次失效之后
    重新形成的事件才作有效证据。区间边界按本次计算的当前 ATR 扩展
    （SPEC 以当前 ATR 为价格尺度；失效缓冲用「当时」ATR）。
    @returns (有效事件数, 事件链列表, 最后失效 bar 序号或 None)"""
    n = len(bars)
    pts = sorted(group, key=lambda p: p["i"])
    first_i = pts[0]["i"]
    below = kind == "low"
    last_inv = None
    run = 0
    for i in range(first_i + 1, n):
        c = bars[i]["close"]
        buf = buf_k * (atrs[i] or 0.0)
        if (below and c < lower - buf) or (not below and c > upper + buf):
            run += 1
        else:
            run = 0
        if run >= 2:
            last_inv = i
            run = 0  # 只记最后一次失效；继续扫后续
    events = events_of(pts, gap)
    eff = [e for e in events if e[-1]["i"] > (last_inv if last_inv is not None else -1)]
    return len(eff), events, last_inv


# ------------------------------------------------------------
# 5. 单周期主函数：zones_of_period
# ------------------------------------------------------------
def zones_of_period(bars, params=None):
    """一个周期一个时点的支阻区间（只吃本切片，无未来数据）。

    @param bars   已收盘K线切片（时间递增，dict: time/high/low/close）
    @param params zone_kwargs_of 的输出（或 None 走默认）
    @returns {
        as_of, close, atr,
        zones: [ {kind, lower, upper, eventCount, pivotCount,
                  firstTouch, lastTouch, lastEventTime, lastInvalidTime} ],
        support / resistance / inside_zone: 最近命中区间或 None（SPEC 第4步）,
    }     数据不足（<30根）或 ATR≤0 → zones=[]、选取全 None，不强行补齐。"""
    p = zone_kwargs_of({}, defaults=params) if params else dict(ZONE_DEFAULTS)
    n = len(bars)
    empty = {"as_of": bars[-1]["time"] if n else None, "close": bars[-1]["close"] if n else None,
             "atr": 0.0, "zones": [], "support": None, "resistance": None, "inside_zone": None}
    lookback = int(p["zoneLookbackBars"] or 0)
    if lookback > 0 and n > lookback:
        bars = bars[-lookback:]
        n = lookback
    if n < MIN_BARS:
        return empty
    atrs = atr_wilder_series(bars, int(p["zoneAtrLen"]))
    atr_now = atrs[-1]
    if atr_now <= 0:
        return empty

    pivots = extract_pivots(bars, int(p["zonePivotBars"]))
    pad = p["zonePadAtr"] * atr_now
    zones = []
    for kind in ("low", "high"):
        for grp in cluster_pivots(pivots[kind], p["zoneClusterAtr"] * atr_now):
            lo_raw = min(q["price"] for q in grp)
            hi_raw = max(q["price"] for q in grp)
            lower, upper = lo_raw - pad, hi_raw + pad
            eff, events, last_inv = _zone_validity(
                kind, lower, upper, grp, bars, atrs,
                buf_k=p["zoneInvalidBuf"], gap=int(p["zoneEventGap"]))
            if eff < int(p["zoneMinEvents"]):
                continue
            zones.append({
                "kind": "support" if kind == "low" else "resistance",
                "lower": lower, "upper": upper,
                "eventCount": eff, "pivotCount": len(grp),
                "firstTouch": grp[0]["time"] if grp else None,
                "lastTouch": grp[-1]["time"] if grp else None,
                "lastEventTime": events[-1][-1]["time"] if events else None,
                "lastInvalidTime": bars[last_inv]["time"] if last_inv is not None else None,
            })
    # 每侧保留数截断（0=不限）：按事件数降序、最近事件时间降序（证据强优先）
    max_side = int(p["zoneMaxPerSide"] or 0)
    if max_side > 0:
        for kind in ("support", "resistance"):
            side = [z for z in zones if z["kind"] == kind]
            if len(side) > max_side:
                keep = set(id(z) for z in sorted(
                    side, key=lambda z: (z["eventCount"], z["lastEventTime"] or 0),
                    reverse=True)[:max_side])
                zones = [z for z in zones if z["kind"] != kind or id(z) in keep]
    out = dict(empty, zones=zones, atr=atr_now)
    out["as_of"], out["close"] = bars[-1]["time"], bars[-1]["close"]
    out.update(_select_nearest(zones, out["close"], atr_now))
    return out


def _select_nearest(zones, close, atr):
    """SPEC 第4步：按收盘价就近选取（仅供展示/结果输出；策略池吃全部有效区间）。
    支撑=上界低于 close 的 support 区间（距上界最近）；压力=下界高于 close 的
    resistance 区间（距下界最近）；所在区间=含 close 的区间。距离平票依次比
    事件数（多者优先）、最近事件时间（新者优先）。"""

    def best(cands, dist):
        if not cands:
            return None
        return sorted(cands, key=lambda z: (dist(z), -z["eventCount"],
                                            -(z["lastEventTime"] or 0)))[0]

    supports = [z for z in zones if z["kind"] == "support" and z["upper"] < close]
    resistances = [z for z in zones if z["kind"] == "resistance" and z["lower"] > close]
    insides = [z for z in zones if z["lower"] <= close <= z["upper"]]
    sup = best(supports, lambda z: close - z["upper"])
    res = best(resistances, lambda z: z["lower"] - close)
    ins = best(insides, lambda z: min(close - z["lower"], z["upper"] - close))
    for z, d in ((sup, close - sup["upper"] if sup else None),
                 (res, res["lower"] - close if res else None),
                 (ins, min(close - ins["lower"], ins["upper"] - close) if ins else None)):
        if z is not None:
            z["distanceAtr"] = round(d / atr, 4) if atr > 0 else None
    return {"support": sup, "resistance": res, "inside_zone": ins}


# ------------------------------------------------------------
# 6. 候选适配（供 sr_flip.compute_srflip srMode="zones" 调用）
# ------------------------------------------------------------
def zone_candidates(res, bars, params=None, work_cache=None):
    """一个周期的区间 → sr_flip 候选格式（merged 候选池/画线/策略闸门共用）。

    每候选：price=按 kind 的远侧边界、lower/upper/kind/type(SUP|RES)/
    srcType="zone"/level/touchCount=eventCount/firstTouch/lastTouch/
    eventCount/lastEventTime。work_cache 可选：未变周期（根数+末根时间+
    参数指纹相同）直接复用，输出与无缓存逐位一致。
    @returns (candidates, selection)  selection=zones_of_period 的 SPEC 第4步结果"""
    p = zone_kwargs_of({}, defaults=params) if params else dict(ZONE_DEFAULTS)
    n = len(bars)
    key = ("sr_zone", str(res), n, bars[-1]["time"] if n else None,
           tuple(sorted(p.items())))
    if work_cache is not None:
        ent = work_cache.get(("sr_zone", str(res)))
        if ent is not None and ent[0] == key:
            return ent[1], ent[2]
    result = zones_of_period(bars, p)
    cands = []
    for z in result["zones"]:
        cands.append({
            "price": z["lower"] if z["kind"] == "support" else z["upper"],
            "lower": z["lower"], "upper": z["upper"],
            "kind": z["kind"],
            "type": "SUP" if z["kind"] == "support" else "RES",
            "srcType": "zone",
            "touchCount": z["eventCount"],
            "firstTouch": z["firstTouch"], "lastTouch": z["lastTouch"],
            "eventCount": z["eventCount"], "lastEventTime": z["lastEventTime"],
        })
    if work_cache is not None:
        work_cache[("sr_zone", str(res))] = (key, cands,
                                             {"support": result["support"],
                                              "resistance": result["resistance"],
                                              "inside_zone": result["inside_zone"],
                                              "close": result["close"], "atr": result["atr"]})
    return cands, {"support": result["support"], "resistance": result["resistance"],
                   "inside_zone": result["inside_zone"],
                   "close": result["close"], "atr": result["atr"]}
