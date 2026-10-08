# -*- coding: utf-8 -*-
"""
支阻位参数调试模块 · 服务编排层（供 /api/sr/* 端点后台线程调用）

职责：
  1. 数据覆盖判定 + 缓存优先取数（不足自动从 TradingView CDP 补拉，合并回写共享缓存）
  2. 各周期重建笔（backtest.build_bis）→ 引擎 compute_srflip（参数全透传）
  3. meta 派生（当前价/各周期ATR等，供页面展示与级别回填）

纯编排：不连 CDP 之外的东西、不绘图；绘图见 sr_draw.py。
级别键一律用 6 个规范化键：W / D / 240 / 60 / 15 / 3（1W→W、1D→D 在入口归一化，
避免破坏引擎 UPPER_OF / minTouchFor / periodNameOf 的键匹配）。
"""

import json
import time

from . import data_loader
from .backtest import build_bis
from .chan_core import intervalSecOf
from .sr_flip import (LEVEL_ORDER, DEFAULT_SR_TYPES, FIB_LEVELS,
                      BOLL_LENGTH, BOLL_MULT, RECENT_BI_COUNT,
                      TOUCH_WEIGHT, BARS_WEIGHT, SIDE_COUNT, MAX_PER_PERIOD,
                      MAX_DIST_ATR, CLUSTER_ATR,
                      RECENT_CLUSTER_ATR, _kindOf, compute_srflip)

# 级别（大 → 小，与 LEVEL_ORDER 相对顺序一致；不含 30S / 1W / 1D 别名键）
CANONICAL_LEVELS = ["W", "D", "240", "60", "15", "3"]
DEFAULT_LEVELS = ["D", "240", "60", "15", "3"]
DEFAULT_FROM = "2026-06-30"
# 页面 minTouch 矩阵默认（与引擎 _MIN_TOUCH_DEFAULT 一致；W 引擎无收录落底 4）
MIN_TOUCH_UI = {"W": 4, "D": 4, "240": 4, "60": 4, "15": 3, "3": 8}
MIN_BARS_OK = 6   # 单周期可建笔的最少K线数（缓存覆盖判定下限）
# 「时点+回溯」（SR 控制台）：回溯加载下限与跨度换算
LOOKBACK_BARS_DEFAULT = 300   # 「向前K线根数」缺省（normalize 层与页面输入框一致）
LOOKBACK_FLOOR_BARS = 500     # fib/BOLL 加载下限：取 max(N, 500)，N 很小时参照段仍有深度
LOOKBACK_SPAN_PAD = 1.5       # 交易日→日历日换算（周末 ~5/7，1.5 留假日余量）
LOOKBACK_PAD_SEC = 2 * 86400  # 固定再垫 2 天
HEAD_TOL_SEC = 4 * 86400      # 起点容差：fetch_from 常落在周末/假日闭市段，下一根真实
                              # K线可能晚 1~3 天——首根晚于起点不超过此值即视为已覆盖


def fetch_from_map(periods, as_of_ts=None, lookback_bars=None, now_ts=None):
    """逐周期拉取起点（「时点+回溯」取数计划）。
    anchor = as_of_ts（时点，含当日）或 now（时点空=当下，同构无特例）；
    n = max(lookback_bars, LOOKBACK_FLOOR_BARS)——密集区只看 N 根，fib/BOLL/现价用
    全部已加载窗口，加载深度须保底；span 按日历日 1.5 倍换算 + 2 天垫量。
    @returns { 周期: UTC 时间戳 }（各周期 N 根的实际时间跨度不同，不能共用单一起点：
              D 的 300 根 ≈ 14 个月，3 的 300 根 ≈ 15 小时）"""
    anchor = int(as_of_ts) if as_of_ts else int(now_ts if now_ts is not None else time.time())
    n = max(int(lookback_bars or 0), LOOKBACK_FLOOR_BARS)
    out = {}
    for p in periods:
        sec = intervalSecOf(p) or 0
        out[p] = int(anchor - (n * sec * LOOKBACK_SPAN_PAD + LOOKBACK_PAD_SEC)) if sec > 0 else 0
    return out


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


def coverage(bars_by_period, periods, from_ts, records=None):
    """缓存覆盖判定：每周期返回 'ok' | 'partial' | 'missing' | 'short'。
    from_ts 兼容 int 或 {周期: int}（「时点+回溯」的逐周期拉取起点）。
    ok      = 首根 <= 起点（覆盖所需回溯深度）且 >= MIN_BARS_OK 根
    partial = 首根晚于起点但深度已探明：records（market 缓存整条记录）里该周期
              fetchFrom（实际持有最早K线）或 cdpProbedTo（已向 CDP 请求过的最早
              起点）任一 <= 起点 = 数据源只给到这里（早于此的历史不可得，按可得
              全量使用；不触发补拉）；records 缺省时沿用旧规则（跨度 >= SPAN_OK_SEC）
    short   = 有数据但既未覆盖起点、深度又未探明（疑似残缺缓存，需补拉）
    missing = 无该周期键"""
    out = {}
    for res in periods:
        ft = from_ts.get(res, 0) if isinstance(from_ts, dict) else from_ts
        bars = bars_by_period.get(res) or []
        if not bars:
            out[res] = "missing"
        elif len(bars) < MIN_BARS_OK:
            out[res] = "short"
        elif bars[0]["time"] <= ft:
            out[res] = "ok"
        elif records is not None:
            rec = records.get(res) or {}
            probed = min(rec.get("fetchFrom", float("inf")),
                         rec.get("cdpProbedTo", float("inf")))
            out[res] = "partial" if probed <= ft else "short"
        elif bars[-1]["time"] - bars[0]["time"] >= SPAN_OK_SEC:
            out[res] = "partial"
        else:
            out[res] = "short"
    return out


def ensure_data(periods, from_ts=None, log=None, refresh=False, symbol=None,
                fetch_froms=None, as_of_ts=None):
    """缓存优先取数：先读 bars_all_tf.json 判覆盖；refresh 或存在 missing/short 周期时，
    只对缺的周期经 CDP 补拉，与旧缓存合并后回写（保留 30S 等既有键）。
    partial（深度够不到起始日期但跨度充足）不触发补拉，日志说明后用可得全量。
    fetch_froms/as_of_ts：SR 控制台「时点+回溯」口径（逐周期起点 + 输出截上界），
    有 symbol 时透传给 ensure_symbol_data。
    @raises RuntimeError  某必需周期最终仍无K线
    @returns { 周期: [{time,open,high,low,close}] }（仅含请求周期，时间升序）
    """
    log = log or (lambda *a, **k: None)
    if symbol:
        return ensure_symbol_data(periods, from_ts, symbol, log, refresh,
                                  fetch_froms=fetch_froms, as_of_ts=as_of_ts)
    if from_ts is None and fetch_froms:
        # 旧共享缓存路径无逐周期概念，取最早起点兜底（防御：生产控制台恒走 symbol 分支）
        from_ts = min(fetch_froms.values())
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


def _db_bars(symbol, period, from_ts):
    """从本地存储（data/bars.db，基础数据页入库）读 [from_ts, 库末] 窗口K线。
    缺品种/窗口内无数据/库不可读 → []（调用方继续走 CDP）。"""
    from . import data_store
    try:
        head = data_store.query_bars(symbol, period, from_ts=from_ts, limit=1)
        total = head["total"]
        if not total:
            return []
        return data_store.query_bars(symbol, period, from_ts=from_ts,
                                     limit=total)["rows"]
    except Exception:
        return []


def ensure_symbol_data(periods, from_ts, symbol, log, refresh=False,
                       fetch_froms=None, as_of_ts=None):
    """SR UI uses the same verified, symbol-separated cache as calibration.

    The legacy shared cache has no provenance and is left untouched. Consumers
    without a symbol keep their existing path above.

    两种取数口径：
    - fetch_froms=None（旧，调参快照等）：窗口起点 from_ts，输出 = [from_ts, 最新]。
    - fetch_froms={周期: ts}（SR 控制台「时点+回溯」）：逐周期按各自起点判覆盖/补拉
      （market 记录带 fetchFrom 戳区分「真缺」与「数据源已到头」），缓存存
      [fetch_from, 最新]（CDP 总是拉到最新），输出 = [回溯起点, as_of_ts]（有界窗口，
      不随缓存深度膨胀）。补深顺序：本地存储（bars.db，深且快，覆盖回溯窗+时点即免
      CDP）→ CDP 实时拉取（小周期实时图深度有限，3m 约 2 个月）。时点前仍无数据的
      周期**跳过并警告**（不再让整品种失败）；全部周期不可得才报错。
    """
    from .sr_tune import Store, digest
    store = Store()
    symbol = symbol.strip().upper()
    cached, recs = {}, {}
    for period in periods:
        try:
            item = store.get("market", digest({"symbol": symbol, "period": period}))
            if item.get("symbol") == symbol and item.get("period") == period:
                cached[period] = item["bars"]
                recs[period] = item
        except ValueError:
            pass
    if fetch_froms is None:
        # 旧口径：单一起点，输出下滤 >= from_ts（调参快照等既有调用零变化）
        cov = coverage(cached, periods, from_ts)
        missing = [p for p in periods if refresh or cov[p] in ("missing", "short")]
        if missing:
            log(f"读取带品种校验的行情 {symbol} / {','.join(missing)} ...")
            fetched = data_loader.fetch_bars(cfg=data_loader.CDPConfig(periods=missing),
                                             from_ts=from_ts, cache=False, symbol=symbol,
                                             log=log, verify_symbol=True)
            for period in missing:
                if not fetched.get(period):
                    raise RuntimeError(f"未能获取 {symbol}/{period} 的行情")
                cached[period] = data_loader._dedup_sorted(fetched[period])
                store.put("market", digest({"symbol": symbol, "period": period}),
                          {"symbol": symbol, "period": period, "bars": cached[period]})
        out = {p: [b for b in cached[p] if b["time"] >= from_ts] for p in periods}
        for period, bars in out.items():
            if len(bars) < MIN_BARS_OK:
                raise RuntimeError(f"{symbol}/{period} 起始日期之后的K线不足")
            log(f"{period}: {len(bars)} 根（品种已校验；窗口始于 {_ts_short(bars[0]['time'])}）")
        return out
    # 「时点+回溯」：需要补深的周期 = 缓存首根晚于回溯起点（含 2 根容差）或无缓存。
    # 补深顺序：本地存储（bars.db，sqlite 秒级且深，每次都可廉价复询）→ CDP 实时拉取
    # （小周期实时图深度有限，3m≈2个月；cdpProbedTo 戳记录已探到多深，源到头不重拉）。
    # market 记录戳口径：fetchFrom=实际持有的最早K线（诚实深度）；cdpProbedTo=已向
    # CDP 请求过的最早起点（防 CDP 到头后每次计算都白拉几分钟）。
    need = []
    for p in periods:
        first = cached[p][0]["time"] if cached.get(p) else None
        if refresh or first is None or first > fetch_froms[p] + HEAD_TOL_SEC:
            need.append(p)
    if need and not refresh:
        still = []
        for p in need:
            ft = fetch_froms[p]
            db_rows = _db_bars(symbol, p, ft)
            if not db_rows:
                still.append(p)
                continue
            merged = data_loader._dedup_sorted((cached.get(p) or []) + db_rows)
            sec = intervalSecOf(p) or 0
            # 起点容差：周末/假日闭市段不算深度不足
            head_ok = merged[0]["time"] <= ft + HEAD_TOL_SEC
            tail_ok = (as_of_ts is not None
                       and merged[-1]["time"] >= as_of_ts - 3 * sec)
            if head_ok and tail_ok:
                cached[p] = merged
                store.put("market", digest({"symbol": symbol, "period": p}),
                          {"symbol": symbol, "period": p, "bars": merged,
                           "fetchFrom": merged[0]["time"], "fetchedAt": int(time.time())})
                log(f"{p}: 本地存储补深 {len(db_rows)} 根"
                    f"（{_ts_short(merged[0]['time'])} -> {_ts_short(merged[-1]['time'])}），免 CDP 拉取")
                continue
            # db 只够一部分（起点不够早/尾不够新）：增深保留，继续交 CDP
            if len(merged) > len(cached.get(p) or []):
                cached[p] = merged
            still.append(p)
        need = still
    # CDP 只对「未探到该深度」的周期发起（cdpProbedTo 戳，取更深者）；源已到头的周期
    # 不重拉（分钟级深拉很贵），靠截窗/跳过降级——刷新按钮强制重拉可越过此戳
    cdp_list = [p for p in need
                if refresh
                or (recs.get(p) or {}).get("cdpProbedTo") is None
                or (recs.get(p) or {}).get("cdpProbedTo") > fetch_froms[p]]
    if cdp_list:
        log(f"读取带品种校验的行情 {symbol} / {','.join(cdp_list)} ...")
        fetched = data_loader.fetch_bars(cfg=data_loader.CDPConfig(periods=cdp_list),
                                         from_ts={p: fetch_froms[p] for p in cdp_list},
                                         cache=False, symbol=symbol,
                                         log=log, verify_symbol=True)
        for period in cdp_list:
            if not fetched.get(period):
                raise RuntimeError(f"未能获取 {symbol}/{period} 的行情")
            merged = data_loader._dedup_sorted((cached.get(period) or []) + fetched[period])
            cached[period] = merged
            old_probed = (recs.get(period) or {}).get("cdpProbedTo")
            probed = fetch_froms[period] if old_probed is None \
                else min(old_probed, fetch_froms[period])
            store.put("market", digest({"symbol": symbol, "period": period}),
                      {"symbol": symbol, "period": period, "bars": merged,
                       "fetchFrom": merged[0]["time"], "cdpProbedTo": probed,
                       "fetchedAt": int(time.time())})
            log(f"{period}: 拉取 {len(fetched[period])} 根，合并后 {len(merged)} 根"
                f"（源深度至 {_ts_short(merged[0]['time'])}）")
    elif need:
        log(f"{'/'.join(need)}：CDP 已探到源深度尽头（本次不重拉；如需强拉点「刷新数据并计算」）")
    # 截窗输出：[回溯起点(容差2根), 时点]——窗口有界（≈ max(N,500)×1.5 根 + 2 天）。
    # 缓存会因历史深拉越并越深，不限下界则重算窗口与耗时随缓存无限膨胀。
    # 某周期时点前无数据 → 跳过并警告（不再整品种失败）；全部不可得才报错。
    out, skipped = {}, []
    for p in periods:
        sec = intervalSecOf(p) or 0
        lo = fetch_froms[p] - 2 * sec
        bars = [b for b in cached.get(p, [])
                if b["time"] >= lo and (as_of_ts is None or b["time"] <= as_of_ts)]
        if len(bars) < MIN_BARS_OK:
            skipped.append(p)
            continue
        out[p] = bars
        tag = "" if bars[0]["time"] <= fetch_froms[p] + HEAD_TOL_SEC \
            else "（数据源深度不足回溯窗口，按可得全量计算）"
        log(f"{p}: {len(bars)} 根（{_ts_short(bars[0]['time'])} -> "
            f"{_ts_short(bars[-1]['time'])}）{tag}")
    if skipped:
        log(f"⚠ 跳过 {'/'.join(skipped)}：时点之前的K线不可得"
            f"（本地存储与数据源深度均不足），其余周期照算")
        if not out:
            raise RuntimeError(f"{symbol} 全部周期（{'/'.join(skipped)}）时点之前的K线均不可得，"
                               f"请改晚时点或先在基础数据页拉取入库")
    return out


def engine_kwargs_of(cfg):
    """把页面 cfg 映射为 compute_srflip 关键字参数（缺失键走引擎默认）。

    2026-09-26 引擎同步（用户拍板）：lookbackBars → clusterLookbackBars（密集区
    限窗，0=不限=全前缀旧口径）；fibLastStroke 恒 True（fib=末段趋势侧口径——
    fib 在引擎默认 srTypes 里关闭，仅显式开启时生效）。回测/分析与控制台同口径；
    live 实盘不传 sr kwargs，仍为旧口径（例外，如需另做）——2026-10-08 起部分
    打破：支阻区间模式（mode=zones）经 symbol_sr_kwargs() 下发实盘（区间必须在
    实盘生效）；经典模式实盘仍不传（引擎默认口径，行为不变）。

    2026-10-08 支阻位模式：mode ∈ {"levels","zones"}（缺省 levels=经典支阻位，
    行为逐位不变）；zones → compute_srflip srMode="zones" 整系统切换为支阻区间
    （sr_zone 模块），zone* 九参数随桶透传。
    """
    from .sr_zone import ZONE_DEFAULTS
    mode = str(cfg.get("mode") or "levels").strip() or "levels"
    if mode not in ("levels", "zones"):
        raise ValueError(f"支阻位模式非法：{mode}（levels=经典支阻位 / zones=支阻区间）")
    def _zone(k, cast):
        v = cfg.get(k)
        if v in (None, ""):
            return ZONE_DEFAULTS[k]
        try:
            return cast(v)
        except (TypeError, ValueError):
            return ZONE_DEFAULTS[k]
    return {
        "clusterAtr": float(cfg.get("clusterAtr", CLUSTER_ATR)),
        "recentClusterAtr": float(cfg.get("recentClusterAtr", RECENT_CLUSTER_ATR)),
        "maxDistAtr": float(cfg.get("maxDistAtr", MAX_DIST_ATR)),
        "maxPerPeriod": int(cfg.get("maxPerPeriod", MAX_PER_PERIOD)),
        "minTouchsIn": dict(cfg.get("minTouchs", {}) or {}),
        "clusterParamsByPeriod": dict(cfg.get("clusterParamsByPeriod", {}) or {}),
        "srTypes": tuple(cfg.get("srTypes", DEFAULT_SR_TYPES)),
        "clusterParts": tuple(cfg.get("clusterParts", ("flip", "recent"))),
        "fibLevels": [float(x) for x in cfg.get("fibLevels", FIB_LEVELS)],
        "bollLength": int(cfg.get("bollLength", BOLL_LENGTH)),
        "bollMult": float(cfg.get("bollMult", BOLL_MULT)),
        "recentBiCount": int(cfg.get("recentBiCount", RECENT_BI_COUNT)),
        "touchWeight": float(cfg.get("touchWeight", TOUCH_WEIGHT)),
        "barsWeight": float(cfg.get("barsWeight", BARS_WEIGHT)),
        "sideCount": int(cfg.get("sideCount", SIDE_COUNT)),
        "manualLevels": dict(cfg.get("manualLevels") or {}),
        "clusterLookbackBars": int(cfg.get("lookbackBars", 0) or 0),
        "fibLastStroke": True,
        "srMode": mode,
        "zoneLookbackBars": _zone("zoneLookbackBars", int),
        "zonePivotBars": _zone("zonePivotBars", int),
        "zoneClusterAtr": _zone("zoneClusterAtr", float),
        "zonePadAtr": _zone("zonePadAtr", float),
        "zoneEventGap": _zone("zoneEventGap", int),
        "zoneMinEvents": _zone("zoneMinEvents", int),
        "zoneInvalidBuf": _zone("zoneInvalidBuf", float),
        "zoneAtrLen": _zone("zoneAtrLen", int),
        "zoneMaxPerSide": _zone("zoneMaxPerSide", int),
    }


def symbol_sr_kwargs(symbol=None):
    """实盘支阻参数：品种桶（param_center.effective_sr）→ compute_srflip kwargs。

    仅当支阻位模式=zones（支阻区间）时返回 kwargs——区间必须在实盘生效；
    levels（经典支阻位）返回 None=引擎默认口径，实盘行为与 2026-10-08 前逐位
    不变。读桶/映射异常一律 None（实盘不因参数页坏值起不来，引擎默认兜底）。"""
    try:
        from . import param_center
        cfg = param_center.effective_sr(symbol)
        if str(cfg.get("mode") or "levels").strip() != "zones":
            return None
        return engine_kwargs_of(cfg)
    except Exception:
        return None


def build_chain_result(bars_by_period, cfg, log=None, bis_by_period=None, engine_extra=None):
    """重建笔 → compute_srflip → meta。
    @param engine_extra 可选：追加给 compute_srflip 的关键字参数（在 engine_kwargs_of 之后
           覆盖，只进显式传入的调用方）。现役使用者：SR 调参页 bollIncludeLast=True
           （BOLL 含末根，与 TV 当前 bar 同拍；引擎/分析页/回测不传 → 已收盘口径不变）。
    @returns (result, meta)；result 键：periods/merged/drawnByPeriod/currentPrice/periodAtrs
    """
    log = log or (lambda *a, **k: None)
    periods = list(cfg["periods"])
    if bis_by_period is None:
        log("重建各周期笔 ...")
        bis_by_period = build_bis(bars_by_period, periods=periods)
    else:
        log("使用本轮画笔数据（不重算笔） ...")
        if any(r not in bis_by_period for r in periods):
            raise ValueError("本轮笔数据缺少支阻位所需周期")
    for res in periods:
        log(f"  {res:>4}: {len(bis_by_period.get(res) or [])} 笔"
            f"（K线 {len(bars_by_period[res])} 根）")
    kw = engine_kwargs_of(cfg)
    if engine_extra:
        kw.update(engine_extra)
    if kw.get("srMode") == "zones":
        log("计算支阻位（模式=支阻区间：OHLC 高低点聚类；旧类型与人工位停用；"
            f"回溯{kw.get('zoneLookbackBars')}根/聚类{kw.get('zoneClusterAtr')}×ATR"
            f"/扩展{kw.get('zonePadAtr')}×ATR；各周期独立成线，不合并）...")
    else:
        manual = list(kw.get("manualLevels") or {})
        log(f"计算支阻位（模式=经典支阻位：{','.join(kw['srTypes'])}；各周期独立成线，不合并"
            + (f"；人工输入周期：{'/'.join(manual)}" if manual else "") + "）...")
    result = compute_srflip(bis_by_period, bars_by_period, periods, **kw)
    meta = build_meta(result, bars_by_period, bis_by_period, cfg)
    return result, meta


def build_meta(result, bars_by_period, bis_by_period, cfg):
    """派生展示用元信息：当前价、各周期 ATR、bar/笔计数、覆盖判定。"""
    periodAtrs = result["periodAtrs"]
    # 覆盖判定锚：SR 控制台「时点+回溯」用逐周期起点；分析页等旧路径沿用 from_ts
    periods_cov = coverage(bars_by_period, cfg["periods"],
                           cfg.get("fetch_froms") or cfg.get("from_ts", 0))
    return {
        "current_price": result["currentPrice"],
        "per_level_atr": {r: periodAtrs[r] for r in cfg["periods"] if r in periodAtrs},
        "bar_counts": {r: len(bars_by_period.get(r) or []) for r in cfg["periods"]},
        "bi_counts": {r: len(bis_by_period.get(r) or []) for r in cfg["periods"]},
        "coverage": periods_cov,
        "skipped_periods": list(cfg.get("skipped_periods") or []),
    }


def main_lines(result):
    """drawnByPeriod → 每显示周期画线数据（含候选项全字段，供行点击）。
    @returns { 显示周期: [line,...] }，line 带 breakTime/price/label/level/srcType 等引擎原字段。"""
    return {str(L): list(lines) for L, lines in (result.get("drawnByPeriod") or {}).items()}


def raw_pool_lines(result, maxDistAtr=MAX_DIST_ATR):
    """全量候选 RAW 线（调试叠加用）：与灰线同一「各周期独立」口径，同框对照。

    对每个显示周期 L（drawnByPeriod 的键），叠加**仅 L 自身周期**的原始候选
    （result["periods"]：密集区已按 maxPerPeriod 截断 + fib + boll）。
    不合并、不继承其它周期线——灰线 = 本周期候选就近选取，RAW = 本周期候选全量，
    同框即可对照「哪些候选被选取」。

    距离过滤（防喧宾夺主）：只保留距 currentPrice ≤ maxDistAtr×该周期ATR 的
    候选（与 drawnByPeriod 选取的距离上限同口径；无 ATR 或无限价时不限）；
    **手动位豁免距离过滤**——人工价位「全部画出」不受距离上限，RAW 对照层同样全可见。
    想扩大可见范围就调大页面「选取距离上限(maxDistAtr)」。

    @returns { L: [ {time, price, kind, type, source} ] }（price 升序）
    """
    drawn = result.get("drawnByPeriod") or {}
    periods = result.get("periods") or {}
    periodAtrs = result.get("periodAtrs") or {}
    current = result.get("currentPrice")
    out = {}
    for L in drawn:
        items = []
        cands = periods.get(L) or []
        atr = periodAtrs.get(L)
        for f in cands:
            price = float(f["price"])
            if current is not None and atr and not f.get("manual"):
                if abs(price - current) > maxDistAtr * atr:
                    continue
            items.append({"time": int(f.get("breakTime") or 0), "price": price,
                          "kind": _kindOf(f), "type": f.get("type") or "",
                          "source": L})
        items.sort(key=lambda x: x["price"])
        out[L] = items
    return out
