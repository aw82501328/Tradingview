# -*- coding: utf-8 -*-
"""
进出场逻辑（Python 移植版，与 .cursor/skills/mark-entry/scripts/mark_entry.js 对齐）

纯函数模块：2026-10-16 起直接消费 chan_core 买卖点（与图上 mark-buy-sell 标记同源，
compute_period_points）判定各周期当前进场状态——原「交易计划」文案中转已取消，
买卖双侧各自取最近笔端点锚（_side_anchor，原 trading_plan.predictPlan.matchSide 平移），
按 pointStrategyOf 直映射到 12 种进场策略键，校验该策略的进场条件后生成进场信号：
  - 买点（多头）= 红色向上箭头（arrow_up）
  - 卖点（空头）= 向下绿色箭头（arrow_down）
交易计划模块只保留震荡（compute_regime → range_watch 闸）与顺势（trend_state_of →
trend_filter 闸）两项判定，均默认关闭。

信号画在「背驰级别」（更低周期）。出场规则（止损 + 滑点/兜底 + 三档止盈）的状态机由
backtest.BacktestEngine 增量推进；本模块提供出场构件（与 mark_entry.js 对齐）：
  - stop_ref_of        方向感知止损参考位（支阻位 ± 滑点，最大止损硬上限 进场价±滑点，永不为 None）
  - trend_following_of 顺势/逆势判定（strategyKey ∈ 顺势键集合为顺势；计划方向已取消）
  - forming_seg_ready  检测周期形成段「合并后≥5根K成笔预期」判定（历史 TP2/逆势 TP3b
                       事件源；2026-10-10 逆势 TP3 改破前低参照后仅工具/测试用）
  - find_bi_event      笔事件查找（TP1/TP3a 事件源，返回含 startTime）

不连接 CDP、不绘图；回测链路通过 compute_entries 直接调用。
"""

import bisect
from collections import OrderedDict

from .chan_core import (
    calcATR, calcMACD, isBiDiverge, lowerResOf, buildZSByUpper, buildZS, intervalSecOf,
    fmtT, CHAN_CFG, biMacdMetrics, _areaDurComparable,
    mergeBars, markWickBars, mergedSegmentCount, structurePeriods, pointEligibleBis,
    findBuyPoints, findSellPoints, pointContext,
)
from .trading_plan import TREND_RES, RANGE_RES, trend_state_of

# 箭头颜色：买点（多头）红色、卖点（空头）绿色
BUY_COLOR = "#F23645"
SELL_COLOR = "#089981"

# 靠近支阻位阈值（绝对价差，不乘 ATR；2026-09-13 起 ×ATR 语义改为直接输入，
# 与三个滑点同单位——工作台/回测界面/JS CLI 同参同名同默认）
NEAR = 30.0

# ---- 出场参数（2026-09-09 出场阶梯重构；与 mark_entry.js / CLI / Web 回测界面同名） ----
# 进场手数（盈亏 = 价格差 × 方向 × lots × 合约乘数；2026-09-23 起统一 MT4/MT5 口径：
# 1 手 = 0.01 标准手，合约乘数见 SYMBOL_CONTRACTS）
DEFAULT_LOTS = 4
# 品种默认手数（参数中心未再覆盖时使用；2026-09-28 把当时已保存的手数收成默认）
DEFAULT_LOTS_BY_SYMBOL = {
    "XAGUSD": 2,
    "BTCUSD": 20,
    "NAS100": 40,
}
# 品种合约规模（每标准手；2026-09-23 统一 MT4/MT5 经纪商 Exness 型口径：1 回测手 = 0.01 标准手，
# 盈亏 = 价差 × 方向 × lots × 乘数，乘数 = 每标准手规模 × 0.01；未知品种缺省乘数 1.0 保持旧行为）
SYMBOL_CONTRACTS = {
    "XAUUSD": 100.0,   # OANDA:XAUUSD 100盎司/标准手 → 乘数 1.0（数字不变）
    "XAGUSD": 5000.0,  # OANDA:XAGUSD 5000盎司/标准手 → 乘数 50.0
    "USOIL": 1000.0,   # TVC:USOIL 1000桶/标准手（NYMEX CL 期货型） → 乘数 10.0
                       # （2026-09-23 修正：原按 100 桶设乘数 1.0，实单核对应为 1000 桶）
    "BTCUSD": 1.0,     # BITSTAMP:BTCUSD 1 BTC/标准手 → 乘数 0.01
    "NAS100": 1.0,     # FX:NAS100 1合约($1/点)/标准手 → 乘数 0.01
}


def symbol_suffix(symbol):
    """品种后缀："OANDA:XAUUSD"→"XAUUSD"（无冒号原样返回，统一大写；None→None）。"""
    if not symbol:
        return symbol
    return (symbol.split(":", 1)[-1] if ":" in symbol else symbol).upper()


def contract_mult_of(symbol):
    """合约乘数 = 每标准手规模 × 0.01（1 回测手 = 0.01 标准手）；未知/None → 1.0。
    纯函数不读任何配置，bt_batch 子进程可安全调用。
    经纪商后缀（Exness Standard 的 XAUUSDm）先归到已知品种再取规模。"""
    suf = symbol_suffix(symbol)
    size = SYMBOL_CONTRACTS.get(suf)
    if size is None and suf:
        for known, val in SYMBOL_CONTRACTS.items():
            if suf.startswith(known):
                size = val
                break
    return 1.0 if size is None else size * 0.01
# 止损位滑点（绝对价格）：正确侧支阻位外侧偏移（short 上方+ / long 下方−）
DEFAULT_SLIP_STOP = 3.0
# 最大止损（绝对价格）：止损离进场价最远不超过该值；支阻位更远时收到进场价±该值（键名仍为 slip_fallback）
DEFAULT_SLIP_FALLBACK = 10.0
# 品种默认最大止损（未列入的品种用上面的全局值；2026-09-28 白银已保存的 0.2 收成默认）
DEFAULT_SLIP_FALLBACK_BY_SYMBOL = {
    "XAGUSD": 0.2,
}
# 保本滑点：保本止损位 beStop = 进场成交K线极值 ± slip_be（short: high+ / long: low−）
DEFAULT_SLIP_BE = 3.0
# 滑点 ATR 系数（2026-09-19）：有效滑点 = 固定滑点 + 系数 × ATR(14, 背驰周期 markRes)；0=关闭
DEFAULT_SLIP_STOP_ATR_K = 0.0
DEFAULT_SLIP_FALLBACK_ATR_K = 0.0
DEFAULT_SLIP_BE_ATR_K = 0.0
# 形成段「成笔预期」门槛：合并后 ≥5 根K（chan_core.isValid gap>=4 同口径）
EXIT_MIN_MERGED = 5
# 出中枢力度衰减比例：离开笔 span < 进入笔 span × ratio 即力度变弱（参数中心可调）
ZS_EXIT_WEAK_RATIO = 1.0
# 顺势判定键集合（2026-10-16 起唯一口径：计划方向已取消，按 strategyKey 判定；
# waitLike2Buy/waitLike2Sell 已无映射入口，保留仅为集合完整性）
TREND_PLAN_DIRS = {"多头多", "空头空"}
TREND_STRATEGY_KEYS = {"wait2Buy", "waitBuy", "wait3Buy", "waitLike2Buy",
                       "wait2Sell", "waitSell", "wait3Sell", "waitLike2Sell"}

# ---- 出场方式配置（2026-10-08 出场方式可配置化；2026-10-10 跟踪止盈默认开、
# 过高低点止盈默认平 25%，与当时黄金参数页生效值对齐） ----
# 每种离场方式 = 启用开关 + 平仓百分比（占总仓位=初始开仓手数，非剩余仓位；手数恒
# 整数：⌊总手数×比例%⌋ 不足1手平1手，封顶剩余）；至少启用一种
# （param_center.normalize 跨键校验）。键名与参数中心/engine module_params 同名直通。
EXIT_STOP_SR_ON = True     # 支阻止损：初始止损位（支阻±滑点/进场K线外推/最大止损兜底）穿越
EXIT_STOP_SR_PCT = 100.0
EXIT_STOP_BE_ON = True     # 保本止损：TP1 保本迁移后保本位 beStop 穿越离场
EXIT_STOP_BE_PCT = 100.0
# 保本位模式（2026-10-10 双口径；2026-10-12 三口径）：extreme=进场成交K线极值±保本
# 滑点（原行为——beStop 天然在进场价亏一侧，被打到至少亏滑点+进场K线逆向波幅）；
# entry=0亏损（默认，2026-10-10 黄金参数页生效值：beStop=进场价，保本滑点不参与；
# 穿越后下一开盘成交，跳空仍可能略有出入）；bi=笔端点±滑点（2026-10-12：背驰周期
# 首笔有利方向笔确认当拍 beStop=该笔起点±保本滑点，成笔前无值——引擎惰性落位）
EXIT_STOP_BE_MODE = "entry"
EXIT_HALF_MR_ON = False    # 背驰周期够笔（2026-10-08 拆分）：背驰周期首笔有利方向笔完成
                           # （同 TP1 保本事件源，不限顺势）→ 平指定比例，剩余止损移保本位；
                           # 默认关 = 旧基线可比
EXIT_HALF_MR_PCT = 50.0
EXIT_HALF_ON = True        # 检测周期够笔（原 TP2 半平/够笔止盈）：检测周期有利方向够笔
                           # （确认口径不限顺势 2026-10-10；预期口径仅顺势）
EXIT_HALF_PCT = 50.0
EXIT_CLOSE_ON = True       # 过高低点止盈（原 TP3 全平）：破前高/前低（不分顺势逆势）
EXIT_CLOSE_PCT = 25.0
EXIT_TRAIL_ON = True       # 跟踪止盈：检测周期有利方向笔起点（结构点）被破坏→出场
EXIT_TRAIL_PCT = 25.0
EXIT_TRAIL_SLIP = 1.0      # 跟踪止盈滑点（点）：止损上移到参照点价∓该值（同 fxma tpTrailSlipPts）
# 跟踪止盈参照（2026-10-09 双模式 exitTrailSrc，默认 detect）；门控保本（同日）：
# 保本迁移（beDone：TP1/half/halfMr）前不接管止损、不提损，参照点自保当当拍起算
# 不追溯——防入场时段参照点抢在「够笔→保本」之前把止损提到进场价附近
# （2026-08-19 XAUUSD 案例）。止损类部分平仓 latch 后剩余手数由 maxLoss 兜底
# （advance_exit_decision，记 stopSr 永不 latch）。
# - detect=检测周期已破前低/前高的有利方向笔起点（2026-10-11）：够笔不够，
#   空头下跌笔须破前低、多头上涨笔须破前高，结构点才是该笔起点。保本后止损
#   收到该点∓滑点，价格破坏结构点记 trailStop。不用 3/类3/4/类4 买卖点。
#   trade#109：15m 3卖 18:00 @ 4157.98 不是结构；其后只有 20:15 高点 4171.42
#   的下跌破了前低，4146/4140 未破前低不算；4171.42 到 9-29 22:15 才被破坏。
# - mark=背驰周期信号流（旧口径）：markRes 信号流中同向 3类买卖点
#   （wait3Buy/waitBuy/wait3Sell/waitSell，含被同向互斥抑制的）。
EXIT_TRAIL_SRC = "detect"
EXIT_TRAIL_REF_KEYS = {"wait3Buy", "waitBuy", "wait3Sell", "waitSell"}
EXIT_EXPECT_ON = True      # 出场预期口径（严进宽出，2026-10-09）：检测周期够笔/顺势过
                           # 高低点用形成段预期触发（合并K够块即够笔、当根收盘K极值越过
                           # 前一同向笔端点即过前高），不等确认笔；False=确认笔旧口径。
                           # 进场侧结构判定不变（严进），仅出场放宽（宽出）。
# 锁定盈利（2026-10-11，出场优先级最高；仅缠论V1标记进出场）。
# 参数在参数中心 entry 模块，强分型均线V1（FxMaEngine）不读、不生效。
# 最大盈利超过门槛后，回撤到只剩指定比例的盈利即全平剩余。
# 两个数都可配；比例是盈利点数的比例，不是手数比例。
EXIT_LOCK_ON = True
EXIT_LOCK_PTS = 100.0      # 最大盈利门槛（点，绝对价格）
EXIT_LOCK_PCT = 50.0       # 至少锁定最大盈利的百分比
# 价格线类触价成交（2026-10-21，仅缠论V1；FxMaEngine 出场本就是触价口径）：
# 开=支阻止损/保本止损/跟踪止盈/最大止损兜底/锁定盈利「盘中触及线价即在触发根
# 按线价即时成交」（同根多线被穿按线位从高到低逐线成交；与实盘券商SL挂线同口径，
# live_trader 按 backtest.next_touch_lines 把第一条会全平的线挂成 MT5 SL）；
# 关=收盘判定+下一根开盘成交（旧口径，A/B 基准）。结构类出场（够笔/过高低点）
# 不受影响。成交方式开关，不列入 EXIT_MODE_ON_KEYS（同 exitExpectOn 先例）。
EXIT_TOUCH_ON = True
# 出场方式默认快照（BacktestEngine.exit_cfg 消费；_fill_pending 固化进 pos.exitCfg，
# advance_exit_decision 纯函数读 pos——三模式（回测/实时/实盘）同一份判定）
EXIT_MODE_DEFAULTS = {
    "exitLockOn": EXIT_LOCK_ON, "exitLockPts": EXIT_LOCK_PTS,
    "exitLockPct": EXIT_LOCK_PCT,
    "exitStopSrOn": EXIT_STOP_SR_ON, "exitStopSrPct": EXIT_STOP_SR_PCT,
    "exitStopBeOn": EXIT_STOP_BE_ON, "exitStopBePct": EXIT_STOP_BE_PCT,
    "exitStopBeMode": EXIT_STOP_BE_MODE,
    "exitHalfMrOn": EXIT_HALF_MR_ON, "exitHalfMrPct": EXIT_HALF_MR_PCT,
    "exitHalfOn": EXIT_HALF_ON, "exitHalfPct": EXIT_HALF_PCT,
    "exitCloseOn": EXIT_CLOSE_ON, "exitClosePct": EXIT_CLOSE_PCT,
    "exitTrailOn": EXIT_TRAIL_ON, "exitTrailPct": EXIT_TRAIL_PCT,
    "exitTrailSlip": EXIT_TRAIL_SLIP,
    "exitTrailSrc": EXIT_TRAIL_SRC,
    "exitExpectOn": EXIT_EXPECT_ON,
    "exitTouchOn": EXIT_TOUCH_ON,
}
# 出场方式开关键（「至少启用一种」校验用；exitTrailSlip / 锁定盈利的两个数值非开关不列入）
EXIT_MODE_ON_KEYS = ("exitLockOn", "exitStopSrOn", "exitStopBeOn", "exitHalfMrOn",
                     "exitHalfOn", "exitCloseOn", "exitTrailOn")


# ============================================================
# 背驰识别算法
# ============================================================


def _zsBeforeSeg(bis, segStart, barSec):
    """segStart 之前紧邻的最后一个中枢（该级 bis 自身构成，buildZS；
    进入笔终点（enterEndTime）早于段起点才算「段之前」——startTime 含左外扩不可用）。"""
    try:
        zss = buildZS(bis, barSec)
    except Exception:
        return None
    best = None
    for z in (zss or []):
        enter = z.get("enterEndTime", z.get("startTime"))
        if enter is not None and enter < segStart:
            best = z  # zss 升序——取最后一个进入笔早于段起点的
    return best


def pickDivergeRefer(bis, cur, barSec, parentBi=None):
    """背驰参照笔选择（确认制 findDivergePoints / 当下制 _evalRealtimeNode 共用）。
    现状口径：向前最近同向已完成笔，跳过幅度 < 当前段 50% 的次级别回调。
    B 开关（CHAN_CFG.divergeReferByZs，2026-09-26 用户规则——背驰=入中枢段 vs 出中枢段，
    中枢内部振荡段不参与比较）：取 cur 之前紧邻的最后一个中枢，跳过中枢内部/之后的
    同向笔（cand.endTime > zs.enterEndTime），参照回退到结束于进入笔终点处的入中枢段
    （buildZS 的 startTime 已左外扩 5 根K、非笔边界，进入笔终点以 enterEndTime 为准）。
    parentBi（当下制规则 2）：参照跨出所属上级笔 → 停止（不回退更早）。
    @returns 参照笔 dict 或 None"""
    zs = _zsBeforeSeg(bis, cur["startTime"], barSec) if CHAN_CFG.get("divergeReferByZs") else None
    zsEnter = (zs.get("enterEndTime") if zs is not None else None)
    if zsEnter is None and zs is not None:
        zsEnter = zs["startTime"]
    fallback = None  # 容器内最近合格同向段（ZS 回退越界时的兜底参照）
    for j in range(bis.index(cur) - 1, -1, -1):
        cand = bis[j]
        if cand["type"] != cur["type"]:
            continue
        if cand["span"] < cur["span"] * 0.5:
            continue  # 跳过幅度不足的次级别回调
        if parentBi is not None and cand["startTime"] < parentBi["startTime"] - (barSec or 0):
            break  # 参照跨出候选段所属上级笔（规则 2，不回退更早）
        if zsEnter is not None and cand["endTime"] > zsEnter:
            # 中枢内部/之后的段不参与比较 → 优先回退到入中枢段；但若入中枢段与
            # 候选段反向（或已被容器边界截断），ZS 回退会一路推到容器外作废——
            # 2026-10-10 用户口径修正：此时退回容器内最近合格同向段作参照
            # （如 60m wait2BuyBear 10-8 16:00 案例：出中枢末段 vs 中枢构成大段）。
            fallback = cand  # 保留最外侧（容器起点侧）合格同向段——通常即进中枢大段
            continue
        return cand
    return fallback


def findDivergePoints(bis, macdArr, barSec=None):
    """识别某周期的背驰点（做多=底背驰，做空=顶背驰）。
    参考 chan_core.findBuyPoints/findSellPoints 的候选逻辑，但不做区间套/锚定：
      - 底背驰：下跌笔创新低 + MACD 背驰（绿柱面积变小 或 DIF低点抬高）
      - 顶背驰：上涨笔创新高 + MACD 背驰（红柱面积变小 或 DIF高点变低）
    参照笔 = 向前最近同向笔（跳过幅度 < 当前 50% 的次级别回调；B 开关下按
    中枢回退到入中枢段，见 pickDivergeRefer——barSec 为 None 时 B 规则不可用）。
    @returns [{ time, price, direction }] direction='long'（做多）|'short'（做空）
    """
    bis = pointEligibleBis(bis)
    if not bis or len(bis) < 3:
        return []
    points = []

    # 做多（底背驰）：下跌笔创新低 + 背驰
    downIdx = [i for i, b in enumerate(bis) if b["type"] == "down"]
    for k in range(1, len(downIdx)):
        cur = bis[downIdx[k]]
        refer = pickDivergeRefer(bis, cur, barSec)
        if refer is not None and cur["endPrice"] < refer["endPrice"] and isBiDiverge(cur, refer, macdArr):
            points.append({"time": cur["endTime"], "price": cur["endPrice"],
                           "direction": "long", "referStart": refer["startTime"]})

    # 做空（顶背驰）：上涨笔创新高 + 背驰
    upIdx = [i for i, b in enumerate(bis) if b["type"] == "up"]
    for k in range(1, len(upIdx)):
        cur = bis[upIdx[k]]
        refer = pickDivergeRefer(bis, cur, barSec)
        if refer is not None and cur["endPrice"] > refer["endPrice"] and isBiDiverge(cur, refer, macdArr):
            points.append({"time": cur["endTime"], "price": cur["endPrice"],
                           "direction": "short", "referStart": refer["startTime"]})

    return points


# ============================================================
# 区间套下沉判定（SPEC_divergence_chanset.md 规则 1/2/3）
# ============================================================
#
# 规则 1（展开下沉）：候选顶/底 P，从检测周期 X 的「以 P 为终点的笔」开始——该级笔内部，
#   若次一级别存在 ≥3 笔结构（段间不跨该级上级笔边界，含形成中延伸段计 1 笔）且末段
#   终点即 P → 下沉到次级别，重复；次级别展开不足 3 笔 → 停止下沉，在本级判定。
# 规则 2（跨级禁止）：候选背驰段的参照笔必须与候选段同处于其所属上级笔内部；
#   参照属更早的上级笔（跨级比较）→ 候选无效。
# 规则 3（归属）：markRes = 下沉停止的那一级。下沉上限 = 检测周期 X（不越过 X 向上归属）。


def _upperResOf(periodData, res):
    """periodData 中 res 的最近上级（sec 更大的最小者，须有笔数据）；无则 None。"""
    sec = intervalSecOf(res) or 0
    up = None
    for r, pd in periodData.items():
        s = intervalSecOf(r) or 0
        if s > sec and (up is None or s < intervalSecOf(up)) and pd and pd.get("bis"):
            up = r
    return up


def levelsBelow(periodData, X):
    """检测周期 X 之下的**连续**低级别链（逐级映射 lowerResOf：60→15→3；3 之下挂 30S）。
    某级无笔数据（<3 笔）则链在该级截断——区间套逐级下沉、不可跳级
    （60 与 3 之间缺 15 时链止于 60，不得由 60 直接下到 3）。"""
    chain = []
    cur = X
    while True:
        nxt = lowerResOf(cur)
        if nxt is None and str(cur) == "3":
            nxt = "30S"  # 3 分钟之下挂 30 秒（--with-30s 时加载）
        if nxt is None:
            break
        pd = periodData.get(nxt)
        if not pd or not pd.get("bis") or len(pd["bis"]) < 3:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def filterDetectPeriods(periods):
    """检测周期 = 自身之下存在**已加载**更低级别的周期（不含 D/30S）。
    下一级映射与 levelsBelow 一致：lowerResOf（240→60→15→3），3 之下挂 30S。
    30S 未加载时 3 之下无级别 → 3 不作为检测周期（最小检测 15m，背驰最深 3m），
    避免 realtime 模式下沉链为空、背驰落在检测周期自身（det=3/div=3）。"""
    loaded = {str(p).upper() for p in periods}
    out = []
    for p in periods:
        pu = str(p).upper()
        if pu in ("D", "30S"):
            continue
        nxt = lowerResOf(pu)
        if nxt is None and pu == "3":
            nxt = "30S"
        if nxt is None or nxt not in loaded:
            continue
        out.append(p)
    return out


def biEndingAt(bis, pTime, tol, wantType):
    """找「以 pTime 为终点」的笔：从尾部向前找第一根 endTime 与 pTime 相差 ≤ tol
    且方向匹配的笔（不同级别端点时间有不超过 1 根本级 bar 的偏移，区间套同一结构点）。"""
    for i in range(len(bis) - 1, -1, -1):
        b = bis[i]
        if wantType is not None and b["type"] != wantType:
            continue
        if abs(b["endTime"] - pTime) <= tol:
            return b
    return None


def _upperContainingBi(periodData, res, bi):
    """找 res 最近上级中包含笔 bi 的上级笔（bi 区间落在上级笔区间内，容差 1 根上级 bar；
    末笔视为开放段 +∞——形成中的上级笔，与 buildZSByUpper open_last 口径一致）。"""
    up = _upperResOf(periodData, res)
    if up is None:
        return None
    tol = intervalSecOf(up) or 0
    ub = periodData[up]["bis"]
    for i, u in enumerate(ub):
        last_open = (i == len(ub) - 1)
        if u["startTime"] - tol <= bi["startTime"] and (u["endTime"] + tol >= bi["endTime"] or last_open):
            return u
    return None


def _bisect_ge_key(bis, key, t):
    """二分：笔列表按 key 字段（startTime/endTime）升序，返回首个 bis[key] >= t 的索引。
    30S 级笔列表可达数万根，从索引 0 线性扫描会随窗口平方级放大。"""
    lo, hi = 0, len(bis)
    while lo < hi:
        mid = (lo + hi) // 2
        if bis[mid][key] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_gt_key(bis, key, t):
    """二分：返回首个 bis[key] > t 的索引（key 升序）。"""
    lo, hi = 0, len(bis)
    while lo < hi:
        mid = (lo + hi) // 2
        if bis[mid][key] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _cover_bar_index(times, t):
    """t 所在大周期K线的索引（times=该周期K线开盘时间升序数组）。
    早于首根返回 None；晚于末根按末根计（times 未见进行中K线时的覆盖语义）。
    2026-10-18 锚点K线窗口闸用：买卖点锚所在K线 = 小周期背驰触发窗口。"""
    if not times or t is None:
        return None
    i = bisect.bisect_right(times, t) - 1
    return None if i < 0 else i


def _anchor_in_window(idx, anchorIdx, winBars):
    """锚点窗口落窗判定（2026-10-21 重定义）：idx 所在K须在锚K起 winBars 根内
    （含锚K自身；锚K之前仍拒）。winBars=1 即旧单K规则。"""
    return idx is not None and anchorIdx is not None and 0 <= idx - anchorIdx < winBars


def _finest_res_with_bars(periodData):
    """periodData 中有K线数据的周期里取最细一级。锚点窗口价距基准须用细周期
    已收K：粗周期已收前缀不含进行中K线（锚K常在进行中，其低/高点不可见——
    同 JS barTimesAll 必须用未过滤全量的教训）。"""
    best, best_sec = None, None
    for r, d in (periodData or {}).items():
        if not (d or {}).get("bars"):
            continue
        sec = intervalSecOf(r) or 0
        if best is None or sec < best_sec:
            best, best_sec = r, sec
    return best


def _win_extreme_ref(periodData, fromT, direction, anchorPrice, anchorTime=None):
    """锚点窗口价距基准（买侧 lowRef/卖侧 highRef）：锚K开盘时刻 fromT 起细周期
    已收K极值与锚点价取小（买）/取大（卖），随新低/新高漂移。无K线数据降级
    =锚点价。bars 为已收前缀，从尾部反向扫到 fromT 即止。
    2026-10-22 起返回 (极值价, 极值时间)：极值时间=刷新极值的细周期K时间（锚点价
    兜底时=anchorTime）——trailing 锚K窗口（anchorWinTrailOn）用极值所在K重开时间窗。
    并列极值取先到者（min/max 与严格比较同语义）。"""
    res = _finest_res_with_bars(periodData)
    bars = ((periodData or {}).get(res) or {}).get("bars") if res else None
    ref = anchorPrice
    refT = anchorTime
    for b in reversed(bars or []):
        if (b.get("time") or 0) < fromT:
            break
        v = b.get("low", b.get("close")) if direction == "long" else b.get("high", b.get("close"))
        if (v < ref) if direction == "long" else (v > ref):
            ref = v
            refT = b.get("time")
    return ref, refT


def _beat_price(periodData):
    """当拍现价 = 最细周期最后一根已收K收盘（与当拍 MACD 同源；无数据返回 None，
    调用方跳过价距检查）。"""
    res = _finest_res_with_bars(periodData)
    bars = ((periodData or {}).get(res) or {}).get("bars") if res else None
    return bars[-1].get("close") if bars else None


def _expansionCount(bisL, parentBi, endT, tolStart, tolEnd):
    """数 lower 级笔在上级笔 parentBi 内部的展开笔数：startTime ≥ parentBi.startTime - tolStart
    且 endTime ≤ endT + tolEnd（段间不跨上级笔边界；含形成中延伸段计 1 笔）。
    startTime 升序 → 二分定位下界，只扫窗口内元素。"""
    lo_t = parentBi["startTime"] - tolStart
    i = _bisect_ge_key(bisL, "startTime", lo_t)
    hi_t = endT + tolEnd
    n = len(bisL)
    cnt = 0
    while i < n and bisL[i]["startTime"] <= hi_t:
        cnt += 1
        i += 1
    return cnt


def _virtualBi(afterBi, pTime):
    """虚拟形成笔：X 末段端点已过（如 60m 平台顶 4464.23 后的近等后顶 4461.7，
    图表最终结构经「近等双顶取后顶」并入同一笔；当下时刻该替换尚未确认）——
    以「末段端点 → P」的开放段作为容器参与展开计数（SPEC v1 口径：上级笔未闭合，
    用延伸中的上级笔 + 已闭合段计数）。"""
    return {"startTime": afterBi["endTime"], "endTime": pTime,
            "type": "down" if afterBi["type"] == "up" else "up",
            "startPrice": afterBi["endPrice"], "endPrice": None}


def _mergedFor(pd, res, tCut):
    """Closed merged prefix only; raw time arrays cannot prove merged sufficiency."""
    if pd.get("merged") is not None:
        return pd["merged"]
    sec = intervalSecOf(res) or 0
    raw = [b for b in (pd.get("bars") or []) if tCut is None or b["time"] + sec <= tCut]
    return mergeBars(markWickBars(raw))


def counterMoveQualifies(times, lastBi, tCut, enabled=None, merged=None, barSec=0):
    """预期够笔：从末笔极值所在合并块起计数，包含起点块；只接收已收盘前缀。

    times 保留兼容调用签名，但不再用原始K时间推断够笔；缺少合并数据返回 False。
    """
    if enabled is None:
        enabled = CHAN_CFG.get("expectBiEnough")
    if not enabled:
        return False
    return mergedSegmentCount(merged, lastBi["endTime"], barSec) >= int(CHAN_CFG.get("expectBiMinBars", 5) or 5)


def _sinkChainRealtimeNodes(periodData, X, wantDir, tCut=None, periodTimes=None,
                            expectBiEnabled=None):
    """当下制下沉链（逐级节点版，供 realtimeLowerDiverge 回退用；判定与原实现逐行等价）：
    返回节点列表 [ {res, F, parentBi} ... ]——
      - 节点 0 = X 自身（F = X 末段，parentBi = X 的上级包含笔，仅停止级=X 时用作 containment）；
      - 其后每级 = 成功下沉一级（段内展开 ≥3），parentBi = 该级段所在容器（上级笔/虚拟形成笔）。
    链不通（X 末段方向不符且预期够笔不成立）返回 []。
    容器判定：① 两级共享当下极值 → 上级末段；② 下级末段越过上级端点 → 虚拟开放段；
    预期够笔模式（末笔反向、tCut/periodTimes 给定且 counterMoveQualifies 通过）：以
    「末笔端点 → 下级末段当下极值」的虚拟形成笔为承载笔下沉（仅首个下沉层；下级末段
    未越过末笔端点 → 断链），后续层级恢复正常容器判定。"""
    wantType = "down" if wantDir == "long" else "up"
    bisX = (periodData.get(X) or {}).get("bis") or []
    if not bisX:
        return []
    if bisX[-1].get("_forming"):
        if expectBiEnabled is False or (expectBiEnabled is None and not CHAN_CFG.get("expectBiEnough")):
            return []
        if mergedSegmentCount(_mergedFor(periodData[X], X, tCut), bisX[-1]["startTime"], intervalSecOf(X)) < int(CHAN_CFG.get("expectBiMinBars", 5) or 5):
            return []
    expectMode = bisX[-1]["type"] != wantType
    if expectMode:
        timesX = (periodTimes or {}).get(X)
        if (tCut is None or not timesX
                or not counterMoveQualifies(timesX, bisX[-1], tCut, enabled=expectBiEnabled,
                                            merged=_mergedFor(periodData[X], X, tCut), barSec=intervalSecOf(X))):
            return []  # 末笔反向且反向段未预期成笔 → 无链（保持旧口径的短路行为）
    nodes = [{"res": X, "F": bisX[-1], "parentBi": None}]
    C, B_C = X, bisX[-1]
    for L in levelsBelow(periodData, X):
        secC = intervalSecOf(C) or 0
        secL = intervalSecOf(L) or 0
        bisL = periodData[L]["bis"]
        F_L = bisL[-1]  # L 的形成中段（已延伸到当下极值）
        if F_L["type"] != wantType or F_L["endTime"] <= B_C["startTime"]:
            if CHAN_CFG.get("sinkSkipLevel"):
                # D 跨级下沉：本级末段非候选（方向不符——如 15m 已确认顶、形成段翻 down，
                # 而 60m 容器仍是 up 延伸——或早于容器起点）→ 跳过该级继续向下
                continue
            break  # L 末段非候选段（方向不符/早于容器起点）
        if expectMode:
            if F_L["endTime"] <= B_C["endTime"]:
                break  # 下级末段未越过末笔端点（反向段尚未走出本级端点）→ 断链
            container = _virtualBi(B_C, F_L["endTime"])  # 预期段承载笔：末笔端点 → 当下极值
        elif abs(F_L["endTime"] - B_C["endTime"]) <= secC:
            container = B_C                       # ① 两级共享当下极值
        elif F_L["endTime"] > B_C["endTime"] + secC:
            if (CHAN_CFG.get("sinkSkipLevel")
                    and ((B_C["type"] == "up" and F_L["endPrice"] is not None
                          and F_L["endPrice"] > B_C["endPrice"])
                         or (B_C["type"] == "down" and F_L["endPrice"] is not None
                             and F_L["endPrice"] < B_C["endPrice"]))):
                # D 同向延续：低级别极值已越过容器端点价（60m 端点 02:00 只是粒度滞后，
                # 4168.65>4167.59 是同一条上涨的延续）→ 容器 = 整条容器段延伸到当下极值，
                # 保留段内历史（否则规则2 会把 00:15→02:36 的入中枢段截在容器外）
                container = {"startTime": B_C["startTime"], "endTime": F_L["endTime"],
                             "type": B_C["type"], "startPrice": B_C["startPrice"],
                             "endPrice": F_L["endPrice"]}
            else:
                container = _virtualBi(B_C, F_L["endTime"])  # ② X 端点已过 → 虚拟开放段
        elif CHAN_CFG.get("sinkSkipLevel"):
            # D 跨级下沉：本级末段端点落后容器极值（含糊区，如合成K盘中视图里 15m 末段
            # 停在 02:30 而容器极值已到 06:03）→ 跳过该级继续向下找端点新鲜的级别
            continue
        else:
            break
        cnt = _expansionCount(bisL, container, F_L["endTime"], secC, secL)
        if cnt >= 3:
            nodes.append({"res": L, "F": F_L, "parentBi": container})
            C, B_C = L, F_L
            expectMode = False  # 预期承载笔只用于首个下沉层，其后恢复正常容器判定
        elif CHAN_CFG.get("sinkSkipLevel"):
            # D 跨级下沉（2026-09-26 用户确认）：本级展开不足 3 笔（如 15m 上整个反弹
            # 是一笔、微观回踩被合并包含吞掉）→ 跳过该级，以当前容器继续向下找有展开
            # 的级别判背驰（60→3 直沉；markRes=3、参照 containment 仍用容器笔）
            continue
        else:
            break
    # 从未下沉（停止级 = X）：参照 containment 用 X 的上级包含笔（开放末笔）
    nodes[0]["parentBi"] = _upperContainingBi(periodData, X, bisX[-1])
    return nodes


def sinkChainRealtime(periodData, X, wantDir):
    """当下制下沉链：P = 候选顶/底（随下沉逐级发现的各级末段当下极值）。
    X 级承载笔 B_X 两种形态：
      ① 末段终点 ≈ P（通常：X 末段与下级末段共享当下极值）→ B_X = X 末段；
      ② 下级末段越过 X 末段端点（X 端点已过、后续反向结构未确认为笔，案例 A）→
         B_X = 虚拟形成笔（X 末段端点 → P 开放段），段内展开 ≥3 同样下沉。
    @returns (stopRes, parentBi)：stopRes=下沉停止级（可为 X 自身）；parentBi=停止级段
    的所属上级笔（规则2 参照 containment 窗口）；链不通返回 (None, None)。"""
    nodes = _sinkChainRealtimeNodes(periodData, X, wantDir)
    if not nodes:
        return None, None
    if len(nodes) == 1:
        return nodes[0]["res"], nodes[0]["parentBi"]
    return nodes[-1]["res"], nodes[-1]["parentBi"]


def sinkChainConfirm(periodData, X, pTime, pDir):
    """确认制下沉链：P = 已完成背驰笔端点。X 级承载笔 B_X：
      ① X 级存在「以 P 为终点」的笔（任意位置，含末段延伸笔）→ 该笔；
      ② P 晚于 X 末段端点（X 端点已过、后续反向结构未确认，案例 A 同型）→ 虚拟形成笔；
      其余（P 早于末段端点且无精确匹配）→ 无链（候选无效）。
    @returns (stopRes, parentBi)，语义同 sinkChainRealtime；stopRes=None 表示无链。"""
    wantType = "up" if pDir == "short" else "down"
    bisX = (periodData.get(X) or {}).get("bis") or []
    if not bisX:
        return None, None
    secX = intervalSecOf(X) or 0
    B_C = biEndingAt(bisX, pTime, secX, wantType)
    if B_C is None:
        if pTime > bisX[-1]["endTime"] + secX:
            B_C = _virtualBi(bisX[-1], pTime)  # ② 虚拟形成笔（开放段）
        else:
            return None, None
    C = X
    parentBi = None
    for L in levelsBelow(periodData, X):
        secC = intervalSecOf(C) or 0
        secL = intervalSecOf(L) or 0
        bisL = periodData[L]["bis"]
        F_L = biEndingAt(bisL, pTime, secL, wantType)
        if F_L is None:
            break
        cnt = _expansionCount(bisL, B_C, F_L["endTime"], secC, secL)
        if cnt >= 3:
            parentBi, C, B_C = B_C, L, F_L
        else:
            break
    if parentBi is None and C == X:
        parentBi = _upperContainingBi(periodData, X, B_C)
    return C, parentBi


# ============================================================
# 买卖点 → 进场策略直映射（2026-10-16 取消交易计划文案中转）
# ============================================================

# 买/卖点族（与原 trading_plan.predictPlan 双侧锚同族划分）
BUY_FAMILY = ("1买", "2买", "类2买", "3买", "类3买", "4买", "类4买")
SELL_FAMILY = ("1卖", "2卖", "类2卖", "3卖", "类3卖", "4卖", "类4卖")
_FAMILY_CLS = {"2买": "二买", "类2买": "类2买", "2卖": "二卖", "类2卖": "类2卖"}


def point_cls_of(point):
    """2族强弱档（原 trading_plan.cls_of 的 strong 二分）：强二买/弱二买/强类2买…；
    strong=None（首段未走完）按弱先行；非2族（1类/3类族）返回 None。"""
    if "strong" in point and point["type"] in _FAMILY_CLS:
        return ("强" if point.get("strong") else "弱") + _FAMILY_CLS[point["type"]]
    return None


def pointStrategyOf(point):
    """买卖点 type+strong → 进场策略键直映射（用户规则，2026-10-16；替代原
    entryStrategyOf(计划文案)——计划层 strategyOf 三档路由/未定型滚动翻档/
    跨侧 fallback/形成中1类特殊分支均已取消）：
      1买→wait1Buy、1卖→wait1Sell（直接档：形成中与已确认统一，点本身即信号）；
      2买/类2买 strong→wait2Buy、弱/未定型→wait2BuyBear（卖侧镜像）；
      3买→wait3Buy、3卖→wait3Sell；类3买/4买/类4买→waitBuy、卖侧→waitSell。
    waitLike2Buy/waitLike2Sell 无映射入口（键定义与 strategyExtraOk 校验保留，
    eval 比对兼容）。
    @returns None 或 { key, direction, label, pointCls }"""
    t_ = point.get("type")
    if t_ == "1买":
        row = {"key": "wait1Buy", "direction": "long", "label": "等待一买"}
    elif t_ == "1卖":
        row = {"key": "wait1Sell", "direction": "short", "label": "等待一卖"}
    elif t_ in ("2买", "类2买"):
        row = ({"key": "wait2Buy", "direction": "long", "label": "等待回调后做2买"}
               if point.get("strong")
               else {"key": "wait2BuyBear", "direction": "long", "label": "等待低点附近的2买"})
    elif t_ in ("2卖", "类2卖"):
        row = ({"key": "wait2Sell", "direction": "short", "label": "等待反弹后做2卖"}
               if point.get("strong")
               else {"key": "wait2SellBear", "direction": "short", "label": "等待高点附近的2卖"})
    elif t_ == "3买":
        row = {"key": "wait3Buy", "direction": "long", "label": "等待回调后3买点"}
    elif t_ == "3卖":
        row = {"key": "wait3Sell", "direction": "short", "label": "等待反弹后3卖点"}
    elif t_ in ("类3买", "4买", "类4买"):
        row = {"key": "waitBuy", "direction": "long", "label": "等待回调后买点"}
    elif t_ in ("类3卖", "4卖", "类4卖"):
        row = {"key": "waitSell", "direction": "short", "label": "等待反弹后卖点"}
    else:
        return None
    row["pointCls"] = point_cls_of(point)
    return row


# _side_anchor 的族索引缓存（2026-10-16 性能等价改造）：键=points 行对象身份，
# 持行强引用防 id 复用；行对象经 compute_period_points 的 work_cache 跨步复用时
# 索引直接命中，避免每步重建全量点表。窗口内仍按原 all_ 顺序遍历，返回值与
# 原双层线性扫逐位一致。
_SIDE_IX = OrderedDict()


def _side_index(points, family):
    ent = _SIDE_IX.get(id(points))
    if ent is not None and ent[0] is points:
        ix = ent[1].get(family)
        if ix is not None:
            return ix
    else:
        ix = None
        if len(_SIDE_IX) >= 8:
            _SIDE_IX.clear()
        ent = (points, {})
        _SIDE_IX[id(points)] = ent
    all_ = list(points.get("buy") or []) + list(points.get("sell") or [])
    fam = [p for p in all_ if p["type"] in family]
    order = sorted(range(len(fam)), key=lambda i: fam[i]["time"])
    ftimes = [fam[i]["time"] for i in order]
    ix = (fam, ftimes, order)
    ent[1][family] = ix
    return ix


def _side_anchor(bis, points, family, tolSec, tolPrice):
    """买卖单侧最新锚（原 trading_plan.predictPlan.matchSide 平移，2026-10-16）：
    从最后一笔终点逐笔向前扫描，返回第一个落在笔端点上的 family 族买卖点。
    容差与原口径一致：时间 ≤ tolSec（本周期 1 bar）、价格 ≤ max(ATR×0.2, 0.05）。
    实现（2026-10-16 性能等价改造）：族预过滤 + 点表按时间二分取 ±tolSec 窗，
    窗口内按原 all_ 顺序取首个价格命中——与原全量嵌套线性扫同值。
    @returns (point, bi) 或 (None, None)。"""
    fam, ftimes, order = _side_index(points, family)
    for j in range(len(bis) - 1, -1, -1):
        bi = bis[j]
        if bi is None:
            continue
        et = bi["endTime"]
        lo = bisect.bisect_left(ftimes, et - tolSec)
        hi = bisect.bisect_right(ftimes, et + tolSec)
        if lo >= hi:
            continue
        for i in sorted(order[lo:hi]):
            p = fam[i]
            if abs(p["price"] - bi["endPrice"]) <= tolPrice:
                return p, bi
    return None, None


def side_candidates(bis, points, atr, barSec):
    """买卖双侧锚点（2026-10-10 双侧独立口径保留）：买侧与卖侧各自取最近笔端点锚，
    并行评估、互不影响（多空可同时持仓）。@returns [point, ...] 0~2 个。"""
    tolSec = barSec
    tolPrice = max((atr or 0) * 0.2, 0.05)
    cands = []
    for family in (BUY_FAMILY, SELL_FAMILY):
        p, _bi = _side_anchor(bis, points, family, tolSec, tolPrice)
        if p is not None:
            cands.append(p)
    return cands


def compute_period_points(periodBis, periods, periodMacd=None, work_cache=None,
                          class2ZsTol=0.0, thirdZsTol=0.0):
    """逐周期计算买卖点原始输出（chan_core.findBuyPoints/findSellPoints，与图上
    mark-buy-sell 标记同源；2026-10-16 起作为进场候选的直接来源，替代交易计划锚）。
    上级周期取法与 trend_state_of 同口径：periodBis 中比本周期大一级的最小周期。
    @param work_cache 可选：{(res): (key, row)} 引擎级缓存——本级合格笔视图与上级笔
                      指纹未变时直接复用（与 compute_regime 同一缓存 dict；2026-10-16
                      起键不再含 MACD 长度：买卖点判定不依赖 macd）。
    @returns { res: {"buy": [...], "sell": [...]} }（无数据周期缺省）"""
    from .sr_flip import _bis_fingerprint
    periodBis = periodBis or {}
    periodMacd = periodMacd or {}
    out = {}
    for res in periods:
        bis = periodBis.get(res) or []
        if len(bis) < 3:
            continue
        # 全量笔视图（2026-10-21 统一点源：与 fxma _fx_all_points / 图上标记层
        # compute_all_marks 同口径）。旧「最近 60 笔」窗口（原计划窗口遗留）曾使
        # 老于窗口的上级笔端点投影退化为大级别端点、1买锚后抬低序列/上级分解中枢
        # 被截断——缠论V1 与 fxma 同周期点集因此分歧；project_first_points 的档位
        # 二分与顶点缓存（2026-10-16/10-20 性能等价改造）已无长列表扫描问题，
        # 重算频次仍由 work_cache 指纹闸住（全量指纹不再随窗口头滑动，只随真实结构变化）。
        sec = intervalSecOf(res) or 60
        best = None
        for r in periodBis:
            s = intervalSecOf(r) or 0
            if s > sec and (best is None or s < (intervalSecOf(best) or 0)):
                best = r
        upper = periodBis.get(best) if best is not None else None
        macdArr = periodMacd.get(res) or []
        # 缓存键=真实输出依赖（2026-10-16 性能收紧）：买卖点输出只由合格笔视图与
        # 上级笔决定——macdArr 不参与判定（findBuyPoints 文档），len(macdArr) 删除后
        # 该周期每根收盘不再无谓失效；本级指纹改对 pointEligibleBis 视图取，形成中
        # 未够笔的延伸不改输出。上级仍对全量取（形成中上级笔经中枢 upperEnd 标签
        # 参与输出，不可收紧）。命中/未命中重算的结果均与旧键逐位一致。
        key = ("points", res, _bis_fingerprint(pointEligibleBis(bis)),
               _bis_fingerprint(upper), class2ZsTol, thirdZsTol)
        if work_cache is not None:
            ent = work_cache.get(("points", res))
            if ent is not None and ent[0] == key:
                out[res] = ent[1]
                continue
        upperMacd = periodMacd.get(best) if best is not None else None
        try:
            ctx = pointContext(bis, upper or [], sec)
            buy = findBuyPoints(bis, upper or [], macdArr, sec, class2ZsTol, thirdZsTol,
                                upperMacd=upperMacd, _ctx=ctx)
            sell = findSellPoints(bis, upper or [], macdArr, sec, class2ZsTol, thirdZsTol,
                                  upperMacd=upperMacd, _ctx=ctx)
        except Exception:
            buy, sell = [], []
        row = {"buy": buy, "sell": sell}
        out[res] = row
        if work_cache is not None:
            work_cache[("points", res)] = (key, row)
    return out


# ============================================================
# 进场策略的条件判定（纯函数；2026-09-25 起 10 键 + 2026-10-09 弱档分流 2 键 = 12 键，
# 专属条件 wait1/wait2 系 + wait2BuyBear/wait2SellBear 抬低/压低）
# ============================================================


def lastBiOk(bis, wantType):
    """够笔：最后一笔是否为预期方向（空头→up 反弹、多头→down 回调）。"""
    if not bis or len(bis) == 0:
        return False
    return bis[-1]["type"] == wantType and (not bis[-1].get("_forming") or
        (CHAN_CFG.get("expectBiEnough") and bis[-1].get("mergedCount", 0) >= int(CHAN_CFG.get("expectBiMinBars", 5) or 5)))


def brokePrevLow(bis):
    """下跌段破前底：最近完成的一笔 down 笔终点，跌破更早最近同向 down 笔终点（创新低）。"""
    if not bis or len(bis) < 2:
        return False
    lastDownIdx = -1
    for i in range(len(bis) - 1, -1, -1):
        if bis[i]["type"] == "down":
            lastDownIdx = i
            break
    if lastDownIdx <= 0:
        return False
    prevLow = float("inf")
    for i in range(lastDownIdx - 1, -1, -1):
        if bis[i]["type"] != "down":
            continue
        prevLow = bis[i]["endPrice"]
        break
    if prevLow == float("inf"):
        return False
    return bis[lastDownIdx]["endPrice"] < prevLow


def brokePrevHigh(bis):
    """上涨段过前高：最近完成的一笔 up 笔终点，突破更早最近同向 up 笔终点（创新高）。"""
    if not bis or len(bis) < 2:
        return False
    lastUpIdx = -1
    for i in range(len(bis) - 1, -1, -1):
        if bis[i]["type"] == "up":
            lastUpIdx = i
            break
    if lastUpIdx <= 0:
        return False
    prevHigh = float("-inf")
    for i in range(lastUpIdx - 1, -1, -1):
        if bis[i]["type"] != "up":
            continue
        prevHigh = bis[i]["endPrice"]
        break
    if prevHigh == float("-inf"):
        return False
    return bis[lastUpIdx]["endPrice"] > prevHigh


def macdBelowZero(macdArr):
    """MACD 当前未破 0 轴太多（dif < +macdZeroTol，默认容差 5）：下0轴后反弹不过0轴太多，
    空头动能还在（原严格口径 dif < 0，tol=0 回退；2026-09-16）。"""
    if not macdArr or len(macdArr) == 0:
        return False
    tol = float(CHAN_CFG.get("macdZeroTol", 5.0) or 0.0)
    return macdArr[-1]["dif"] < tol


def macdAboveZero(macdArr):
    """MACD 当前未破 0 轴太多（dif > -macdZeroTol，默认容差 5）：上0轴后回调不破0轴太多，
    多头动能还在（原严格口径 dif > 0，tol=0 回退；2026-09-16）。"""
    if not macdArr or len(macdArr) == 0:
        return False
    tol = float(CHAN_CFG.get("macdZeroTol", 5.0) or 0.0)
    return macdArr[-1]["dif"] > -tol


def zsExitWeak(bis, upperBis, macdArr, barSec, ratio=1.0, wantDir="short"):
    """出中枢的力度变弱（buildZSByUpper 取最后一个中枢）：
    离开中枢的笔相对进入中枢的笔 isBiDiverge 为 true，或离开笔 span < 进入笔 span × ratio。

    性能：realtime 每根 fine 收盘可能反复进入 wait1Buy/wait1Sell；按笔快照缓存
    buildZSByUpper 结果（末笔延伸即失效），输出与未缓存路径逐位一致。"""
    zss = _zs_by_upper_cached(bis, upperBis, barSec)
    if not zss:
        return False
    last = zss[-1]
    if last is None:
        return False
    enter = next((b for b in bis if b["endTime"] == last["enterEndTime"]), None)
    if enter is None:
        return False
    # 离开笔 = exitTime 对应的笔（exitStartTime 为离开笔起点时间，原笔对象时间戳精确匹配）
    exitBi = next((b for b in bis if b["startTime"] == last["exitStartTime"]), None) \
        if last.get("exitTime") is not None else None
    if exitBi is None:
        return False  # 中枢未离开（仍在延伸），无「出中枢」力度可言
    # 期望的离开方向过滤：一卖应向上离开中枢、一买应向下离开中枢
    if wantDir == "short" and exitBi["type"] != "up":
        return False
    if wantDir == "long" and exitBi["type"] != "down":
        return False
    # 力度变弱：离开笔相对进入笔 MACD 背驰 或 离开笔幅度小于进入笔幅度 × ratio
    if isBiDiverge(exitBi, enter, macdArr):
        return True
    if exitBi["span"] < enter["span"] * (ratio or 1.0):
        return True
    return False


# buildZSByUpper 快照缓存：键含下级/上级笔数量与末笔端点；末笔延伸即 miss。
_zsByUpperCache = {}
_ZS_CACHE_MAX = 256


def _zs_by_upper_cached(bis, upperBis, barSec):
    """按笔快照缓存 buildZSByUpper；列表内容未变时复用同一结果。"""
    upperBis = upperBis or []
    lb = bis[-1] if bis else None
    ub = upperBis[-1] if upperBis else None
    key = (
        id(bis), len(bis) if bis else 0,
        lb.get("startTime") if lb else None,
        lb.get("endTime") if lb else None,
        lb.get("endPrice") if lb else None,
        id(upperBis), len(upperBis),
        ub.get("startTime") if ub else None,
        ub.get("endTime") if ub else None,
        barSec or 0,
    )
    ent = _zsByUpperCache.get(key)
    if ent is not None and ent[0] is bis and ent[1] is upperBis:
        return ent[2]
    try:
        zss = buildZSByUpper(bis, upperBis, barSec)
    except Exception:
        zss = []
    if len(_zsByUpperCache) >= _ZS_CACHE_MAX:
        _zsByUpperCache.clear()
    _zsByUpperCache[key] = (bis, upperBis, zss)
    return zss


def lowerDiverge(periodData, X, wantDir):
    """以下级别出现背驰（区间套下沉版，SPEC_divergence_chanset 规则 1/2/3）：
    收集所有更低周期中方向匹配的背驰点后逐个做下沉链校验，仅保留
    「候选级别 == 该点下沉停止级」且「参照笔与候选段同处其所属上级笔内部」的候选；
    按时间**降序**返回（最新的在前）。
    调用方（evaluateEntry）依次尝试候选并做支阻位校验，失败回退次新——过滤后所见
    候选已全部下沉合法，天然不会把高级别候选替换成跨级 3m/30S 微点。
    @returns [ { res, point:{time,price,direction,referStart} }, ... ] 按 point.time 降序
    """
    xSec = intervalSecOf(X) or float("inf")
    cands = []
    for res, pd in periodData.items():
        sec = intervalSecOf(res) or 0
        if sec >= xSec:
            continue  # 只取更低级别
        if not pd or not pd.get("bis") or len(pd["bis"]) < 3:
            continue
        try:
            pts = findDivergePoints(pd["bis"], pd.get("macdArr"), barSec=sec or None)
        except Exception:
            continue
        for p in pts:
            if p["direction"] != wantDir:
                continue
            cands.append({"res": res, "point": p})
    filtered = []
    for c in cands:
        stopRes, parentBi = sinkChainConfirm(periodData, X, c["point"]["time"], c["point"]["direction"])
        if stopRes != c["res"]:
            continue  # 规则 1/3：该点的下沉停止级不是候选级别（其结构已在停止级或更高级判定）
        if parentBi is not None:
            tol = intervalSecOf(c["res"]) or 0
            if (c["point"].get("referStart") or 0) < parentBi["startTime"] - tol:
                continue  # 规则 2：参照跨出候选段所属上级笔 → 候选无效
        filtered.append(c)
    filtered.sort(key=lambda c: c["point"]["time"], reverse=True)  # 最新在前
    return filtered


def sr_of_detect(srLevels, res):
    """只留检测周期自己的支阻位。

    3/15/60/240 同一条规则：检测周期是哪一级，近支阻和止损就只用那一级。
    背驰级别、上一级、下一级的价位都不参与。无 level 的条目不参与。
    """
    if not srLevels or res is None:
        return []
    key = str(res)
    return [sr for sr in srLevels
            if isinstance(sr, dict) and str(sr.get("level")) == key]


# 1类买卖点策略键（wait1Buy=空头结构等一买 / wait1Sell=多头结构等一卖）：
# 黄金分割支阻对其不生效（2026-10-10）。fib 位由非一类买卖点锚定派生（sr_flip
# FIB_BUY_TYPES/FIB_SELL_TYPES 白名单，与 fxma fibNearOn「仅2/3类点判定，1类点
# 豁免」同语义）：1类点创新低/高时旧摆动段的回撤档不再有支撑/压力语义——不参与
# 「支阻位附近」闸门，也不参与止损重选（backtest sr_pick 同口径剔除）。
CLASS1_STRATEGY_KEYS = ("wait1Buy", "wait1Sell")


def sr_drop_fib_for_class1(srLevels, strategyKey):
    """1类策略（wait1Buy/wait1Sell）的支阻池剔除黄金分割候选；其余键原样返回。

    fib 候选带 fib=True 标记（srcType 恒 "fib"，该标记永不清理）；非1类键或空池
    直接返回原列表（零拷贝）。"""
    if strategyKey not in CLASS1_STRATEGY_KEYS or not srLevels:
        return srLevels
    return [sr for sr in srLevels
            if not (isinstance(sr, dict) and sr.get("fib"))]


def nearSr(price, srLevels, nearTol):
    """在支阻位附近：背驰点价与任一 srLevels 支阻位价差 ≤ nearTol。
    调用方须先用 sr_of_detect 收成检测周期。
    @returns None 或 { sr, dist } 最近命中的支阻位
    """
    if not srLevels or len(srLevels) == 0:
        return None
    best = None
    for sr in srLevels:
        d = abs(sr["price"] - price)
        if d <= nearTol and (best is None or d < best["dist"]):
            best = {"sr": sr, "dist": d}
    return best


def nearest_sr(price, srLevels):
    """最近的支阻位（不限方向/阈值——拒绝日志「距支阻位 X 超过 near Y」叙事用）。
    区间候选（kind=support/resistance）按区间感知距离：价在区间内=0（取较近边界
    作叙事价），区间外=到近边界距离；点位候选同旧口径 |价差|。
    @returns (price, dist) 或 None（无支阻位）"""
    best = None
    for sr in (srLevels or []):
        if isinstance(sr, dict) and sr.get("kind") in ("support", "resistance"):
            lo, up = sr.get("lower"), sr.get("upper")
            if lo is None or up is None:
                continue
            if lo <= price <= up:
                p = lo if price - lo <= up - price else up
                d = 0.0
            elif price < lo:
                p, d = lo, lo - price
            else:
                p, d = up, price - up
        else:
            p = sr.get("price") if isinstance(sr, dict) else sr
            if p is None:
                continue
            d = abs(p - price)
        if best is None or d < best[1]:
            best = (p, d)
    return best


def near_zone(price, srLevels, direction, nearTol):
    """区间感知支阻闸门（srMode="zones" 时替换 nearSr 的「背驰点+区间」语义）：
    - 区间候选（kind=support/resistance）：方向匹配——多头只看支撑区间、空头只看
      压力区间（角色由聚类来源决定，与现价位置无关）；背驰点价 ∈
      [lower−nearTol, upper+nearTol] 命中，区间内距离=0、区间外按到近边界距离。
    - 点位候选（无 kind，如人工位）：|价差| ≤ nearTol（与 nearSr 完全同口径）。
      levels 模式无区间候选 → 行为与 nearSr 逐位一致。
    @returns None 或 { sr, dist } 最近命中；命中区间的 sr["price"]=远侧边界
             （support→lower / resistance→upper），信号 nearSr 与止损参考沿它
             自动落区间外侧（stop_ref_of / provisional_sl 零改动即正确）。"""
    if not srLevels:
        return None
    want = "support" if direction == "long" else "resistance"
    best = None
    for sr in srLevels:
        if not isinstance(sr, dict):
            continue
        kind = sr.get("kind")
        if kind in ("support", "resistance"):
            if kind != want:
                continue
            lo, up = sr.get("lower"), sr.get("upper")
            if lo is None or up is None:
                continue
            d = 0 if lo <= price <= up else min(abs(price - lo), abs(price - up))
        else:
            p = sr.get("price")
            if p is None:
                continue
            d = abs(p - price)
        if d <= nearTol and (best is None or d < best["dist"]):
            best = {"sr": sr, "dist": d}
    return best


def strategyExtraOk(key, bis, upperBis, macdArr, barSec, zs_exit_weak_ratio=ZS_EXIT_WEAK_RATIO,
                    point_cls=None):
    """各策略专属条件（原 evaluateEntry 第 2 步抽取为独立函数，确认制/当下制共用）。
    zs_exit_weak_ratio：出中枢力度衰减比例（参数中心可调，默认 ZS_EXIT_WEAK_RATIO）。
    point_cls：计划锚点档位（强二买/1买等同文案「等待回调后做2买」，开单条件按档区分）。
    @returns None（全部通过）或 失败原因字符串"""
    if key == "wait2Sell":
        if not brokePrevLow(bis):
            return "下跌段未破前底"
        if not macdBelowZero(macdArr):
            return "MACD 未下0轴或反弹过0轴"
    elif key == "wait2Buy":
        # 强二买（2026-10-12）：取消过前高；0 轴改为 strong2BuyMacdOn，默认关。
        # 1买及其他仍走原两道（过前高 + DIF > -macdZeroTol）。
        if point_cls == "强二买":
            if CHAN_CFG.get("strong2BuyMacdOn") and not macdAboveZero(macdArr):
                return "MACD 未上0轴或回调破0轴"
        else:
            if not brokePrevHigh(bis):
                return "上涨段未过前高"
            if not macdAboveZero(macdArr):
                return "MACD 未上0轴或回调破0轴"
    elif key == "wait1Sell":
        if not brokePrevHigh(bis):
            return "未够笔且过高点"
        if not zsExitWeak(bis, upperBis, macdArr, barSec, zs_exit_weak_ratio, "short"):
            return "出中枢力度未变弱"
    elif key == "wait1Buy":
        if not brokePrevLow(bis):
            return "未够笔且过低点"
        if not zsExitWeak(bis, upperBis, macdArr, barSec, zs_exit_weak_ratio, "long"):
            return "出中枢力度未变弱"
    elif key == "wait2BuyBear":
        # 空头结构2买（2026-10-09 弱档原始点分流）：只买抬低回调——末下跌笔终点
        # 不破前一根下跌笔终点；破前低属1买语义，由 空头多/等一买 档负责。
        if brokePrevLow(bis):
            return "回调破前低，非抬低2买"
    elif key == "wait2SellBear":
        # 多头结构2卖（对称）：只卖压低反弹——末上涨笔终点不过前一根上涨笔终点。
        if brokePrevHigh(bis):
            return "反弹过前高，非压低2卖"
    # waitBuy / waitSell / wait3Buy / wait3Sell / waitLike2Buy / waitLike2Sell：
    # 仅需够笔 + 以下级别背驰 + 支阻位附近（无专属条件）
    return None


def evaluateEntry(ctx, strategy):
    """校验某个进场策略的全部条件（在检测周期 X 上）。
    公共条件：够笔 + 以下级别背驰 + 在支阻位附近；按策略附加专属条件。
    @returns { ok, reason?, markRes?, point?, nearSr? }，ok=True 时 markRes=背驰所在更低周期、
             point=背驰点、nearSr=命中支阻位价格
    """
    res = ctx["res"]
    bis = ctx["bis"]
    upperBis = ctx.get("upperBis")
    macdArr = ctx.get("macdArr")
    atr = ctx.get("atr", 0)
    barSec = ctx.get("barSec", 0)
    near = ctx.get("near", NEAR)
    key = strategy["key"]
    direction = strategy["direction"]
    # 1类键（wait1Buy/wait1Sell）豁免黄金分割支阻（闸门口径，2026-10-10）
    srLevels = sr_drop_fib_for_class1(sr_of_detect(ctx.get("srLevels"), res), key)
    periodData = ctx.get("periodData")
    wantType = "up" if direction == "short" else "down"  # 空头等反弹(up)，多头等回调(down)
    divergeDir = direction  # 空头→顶背驰(short)，多头→底背驰(long)

    # ⓪ 锚点窗口（2026-10-18 用户规则，2026-10-21 重定义参数化；确认制镜像）：
    # 时间窗=锚K起 anchorWinBars 根检测周期K（旧口径=1），背驰点所在K须落窗；
    # 价距窗=背驰点价距窗口极值 ≤ anchorWinPts（0=不限），极值=锚点价与锚K→
    # 背驰点各已收K极值取小（买侧）/取大（卖侧）。trailing 重开（2026-10-22
    # anchorWinTrailOn 默认开，与当下制主闸同口径）：窗口内创新高/新低时，
    # 时间窗锚K=极值所在检测周期K；False=固定锚K旧口径。
    # 确认制不查评估拍（其信号本身即历史背驰点），只约束背驰点落窗；
    # 锚点由 compute_entries 逐候选传入 ctx["anchorPoint"]，缺省（旧调用方/
    # 最小 fixture）时降级放行。
    barsX = ((periodData or {}).get(res) or {}).get("bars") or []
    timesX = [b.get("time") for b in barsX]
    anchorPt = ctx.get("anchorPoint")
    anchorWinIdx = _cover_bar_index(timesX, anchorPt.get("time")) if anchorPt is not None else None
    winBars = int(CHAN_CFG.get("anchorWinBars", 2) or 2)
    winPts = float(CHAN_CFG.get("anchorWinPts", 10.0) or 0.0)
    trailOn = bool(CHAN_CFG.get("anchorWinTrailOn", True))

    # 1. 够笔
    if not lastBiOk(bis, wantType):
        return {"ok": False,
                "reason": f"最后一笔为 {bis[-1]['type'] if bis else '?'}，需 {wantType}（反弹/回调不够笔）"}

    # 2. 各策略专属条件（与当下制共用 strategyExtraOk）
    extraReason = strategyExtraOk(key, bis, upperBis, macdArr, barSec,
                                  ctx.get("zs_exit_weak_ratio", ZS_EXIT_WEAK_RATIO),
                                  point_cls=ctx.get("pointCls"))
    if extraReason is not None:
        return {"ok": False, "reason": extraReason}

    # 3+4. 以下级别背驰候选（按时间降序）依次做支阻位校验，失败回退次新点：
    #      策略专属条件不依赖背驰点（第2步已过），只需重试「支阻位附近」。
    #      （--with-30s 后 30S 高频背驰点常抢占原级别点，且其微观极值价常远离支阻位——
    #       无回退时信号彻底消失：旧点被抢占丢弃、新点校验被拒，两边都不出箭头。）
    cands = lowerDiverge(periodData, res, divergeDir)
    if not cands:
        return {"ok": False, "reason": "以下级别无匹配方向背驰"}
    if timesX and anchorWinIdx is not None:
        def _anchor_win_ok(c):
            i = _cover_bar_index(timesX, c["point"]["time"])
            if i is None:
                return False
            # 窗口极值（价距基准）：锚点价与锚K→背驰点各已收K极值取小/大；
            # refJ=极值所在检测周期K（trailing 锚K；并列极值取先到者，与
            # min/max 逐位一致）
            ref = anchorPt.get("price")
            refJ = anchorWinIdx
            for j in range(anchorWinIdx, i + 1):
                b = barsX[j]
                v = b.get("low", b.get("close")) if direction == "long" else b.get("high", b.get("close"))
                if (v < ref) if direction == "long" else (v > ref):
                    ref = v
                    refJ = j
            if not _anchor_in_window(i, refJ if trailOn else anchorWinIdx, winBars):
                return False
            if winPts > 0:
                dist = (c["point"]["price"] - ref) if direction == "long" else (ref - c["point"]["price"])
                if dist > winPts:
                    return False
            return True
        cands = [c for c in cands if _anchor_win_ok(c)]
        if not cands:
            return {"ok": False,
                    "reason": "以下级别背驰点均不在锚点窗口内（锚K起 N 根检测周期K内且距窗口极值 ≤ 点数）"}

    nearTol = near  # 绝对价差（2026-09-13 起不乘 ATR）
    for c in cands:
        # 区间感知闸门：levels 模式（无区间候选）与 nearSr 逐位一致；
        # zones 模式=背驰点价落入方向匹配区间（多头=支撑区间 / 空头=压力区间）
        hit = near_zone(c["point"]["price"], srLevels, direction, nearTol)
        if hit is not None:
            return {"ok": True, "markRes": c["res"], "point": c["point"], "nearSr": hit["sr"]["price"]}
    return {"ok": False, "reason": "以下级别背驰点均远离支阻位"}


# ============================================================
# 当下背驰（实时判断）：形成中段创新低/新高 + MACD 当拍对比，无需反向笔确认
# ============================================================

# 形成中段够笔门槛：从起点所在合并块开始至少5块，包含起点块。
REALTIME_MIN_BARS = 5


def _barsSince(times, t0, tCut):
    """times（升序）中 (t0, tCut] 覆盖的K线数，供形成中段长度门槛。"""
    if not times:
        return 0
    i0 = bisect.bisect_left(times, t0)
    i1 = bisect.bisect_right(times, tCut)
    return i1 - i0


def _macdDivergeTol():
    """MACD背驰固定容差（绝对价差，默认 0）。0=不放宽，只有严格更极端才过。"""
    return float(CHAN_CFG.get("macdDivergeTol", 0.0) or 0.0)


def _priceExtreme(cur, ref, want_lower, tol):
    """极值比较。严格更极端返回 strict；未严格突破但价差落在 [0, tol] 返回 near；否则 None。
    做多 want_lower 看更低，做空看更高。tol<=0 时没有近等档。"""
    if want_lower:
        if cur < ref:
            return "strict"
        gap = cur - ref
    else:
        if cur > ref:
            return "strict"
        gap = ref - cur
    if tol > 0 and 0 <= gap <= tol:
        return "near"
    return None


def _endpointBody(bars, bi, want_lower):
    """笔端点 K 的实体价：底 = min(open, close)，顶 = max(open, close)。
    取本级 bars 里 time == endTime 的那根（进行中段含盘中合成 K）。对不上开收盘返回 None。"""
    if not bars or not bi or bi.get("endTime") is None:
        return None
    t = bi["endTime"]
    i = bisect.bisect_left(bars, t, key=lambda b: b.get("time"))
    if i >= len(bars) or bars[i].get("time") != t:
        return None
    o, c = bars[i].get("open"), bars[i].get("close")
    if o is None or c is None:
        return None
    return min(o, c) if want_lower else max(o, c)


def _biDivergeStrictAll(bi, refer, macdArr):
    """M2 近等带内护栏：背驰判据 AND（isBiDiverge 2026-10-01 起为 DIF+面积 AND，
    此护栏在其之上再加柱高项）——近等（未严格创新极值）的候选要求动能全面衰竭，
    防普通双底误判为背驰。"""
    cur = biMacdMetrics(bi, macdArr)
    ref = biMacdMetrics(refer, macdArr)
    if cur is None or ref is None:
        return False
    if bi["type"] == "down":
        return ((not _areaDurComparable(bi, refer) or cur["greenArea"] < ref["greenArea"])
                and cur["difLow"] > ref["difLow"]
                and cur["greenMax"] < ref["greenMax"])
    return ((not _areaDurComparable(bi, refer) or cur["redArea"] < ref["redArea"])
            and cur["difHigh"] < ref["difHigh"]
            and cur["redMax"] < ref["redMax"])


def _evalRealtimeNode(periodData, node, wantDir, tCut, periodTimes, minBars):
    """对下沉链单个节点（某级形成中段）评当下背驰候选（原停止级逻辑的逐级抽出版）。
    @returns (候选或None, 拒绝代码或None, 拒绝ctx)——候选命中时 (cand, None, None)；
    拒绝代码见 bt_journal.GATE_LABELS（交易日志「为什么没开单」叙事源）。"""
    res, F, parentBi = node["res"], node["F"], node["parentBi"]
    pd = periodData.get(res) or {}
    bis = pd.get("bis") or []
    if len(bis) < 2:
        return None, "few_bis", {"res": res, "n": len(bis), "segStart": F.get("startTime")}
    wantType = "down" if wantDir == "long" else "up"
    if F["type"] != wantType:
        return None, "wrong_type", {"res": res, "type": F["type"], "want": wantType,
                                    "segStart": F.get("startTime")}
    times = (periodTimes or {}).get(res)
    if not times:
        times = [b["time"] for b in (pd.get("bars") or [])]
    cnt = mergedSegmentCount(_mergedFor(pd, res, tCut), F["startTime"], intervalSecOf(res))
    if cnt < minBars:
        return None, "seg_short", {"res": res, "count": cnt, "need": minBars,
                                   "segStart": F.get("startTime")}
    # 参照笔：向前最近同向已完成笔（不含形成中段），跳过幅度不足的次级别回调；
    # 规则 2：参照须与 F 同处上级笔内部（更早的参照只会更靠外，直接无效）；
    # B 开关（divergeReferByZs）：中枢内部段不参与比较，参照回退到入中枢段
    refer = pickDivergeRefer(bis, F, intervalSecOf(res), parentBi=parentBi)
    if refer is None:
        return None, "no_refer", {"res": res, "segStart": F.get("startTime")}
    # 先比笔端点影线（endPrice）。严格更极端直接过；未严格突破时价差 ∈ [0, tol] 算近等。
    # 影线两档都不过、且 macdDivergeBodyOn（默认开）时，再比同一根端点 K 的实体。
    want_lower = wantDir == "long"
    tol = _macdDivergeTol()
    kind = _priceExtreme(F["endPrice"], refer["endPrice"], want_lower, tol)
    body_cur = body_ref = None
    body_on = bool(CHAN_CFG.get("macdDivergeBodyOn"))
    if kind is None and body_on:
        bars = pd.get("bars") or []
        body_cur = _endpointBody(bars, F, want_lower)
        body_ref = _endpointBody(bars, refer, want_lower)
        if body_cur is not None and body_ref is not None:
            kind = _priceExtreme(body_cur, body_ref, want_lower, tol)
    if kind is None:
        ctx = {
            "res": res, "extreme": F["endPrice"], "refExtreme": refer["endPrice"],
            "tol": tol, "refTime": refer.get("endTime"), "segStart": F.get("startTime")}
        if body_on:
            ctx["bodyExtreme"] = body_cur
            ctx["bodyRef"] = body_ref
        return None, "no_new_extreme", ctx
    strictNew = kind == "strict"
    # 当下对比 MACD：窗口取 [refer.startTime, F.endTime] 的切片（避免全量数组线性扫）
    macdArr = pd.get("macdArr") or []
    macdT = pd.get("macdTimes") or [m["time"] for m in macdArr]
    if not macdT:
        return None, "macd_no_diverge", {"res": res, "segStart": F.get("startTime")}
    lo = bisect.bisect_left(macdT, refer["startTime"])
    hi = bisect.bisect_right(macdT, F["endTime"])
    if not isBiDiverge(F, refer, macdArr[lo:hi]):
        return None, "macd_no_diverge", {"res": res, "segStart": F.get("startTime")}
    if not strictNew and not _biDivergeStrictAll(F, refer, macdArr[lo:hi]):
        return None, "near_equal_strict_fail", {"res": res, "segStart": F.get("startTime")}
    out = {"res": res,
           "point": {"time": F["endTime"], "price": F["endPrice"],
                     "direction": wantDir},
           "segStart": F["startTime"]}
    if not strictNew:
        out["nearEqual"] = True
    return out, None, None


def realtimeLowerDiverge(periodData, X, wantDir, tCut,
                          periodTimes=None, minBars=REALTIME_MIN_BARS,
                          divergeConfirm=None, expectBiEnabled=None,
                          entryMacdShrink=None, reasons_out=None):
    """当下背驰（区间套下沉版，每根 fine 收盘调用）。

    形成中段 = 笔列表最后一笔——回测引擎的增量状态已用 extendLastBiFrom
    把它延伸到最新极值（endTime/endPrice = 当下极值），天然就是"正在走的这段"。

    下沉判定（SPEC_divergence_chanset 规则 1/2/3）：对 (X, wantDir) 先走下沉链
    （P = X 末段当前极值），只在「下沉停止级 S」产候选——
    S 级展开不足 3 笔的更低级别（如与其上级 15m 笔同笔的 3m 末段）不再产候选；
    S = X（次级别展开不足、下沉链一步未走）→ **不产信号**：背驰必须落在严格
    更低级别（markRes < periodX），在本级自身形成段上判背驰等于无低级别确认。

    M1 下沉链回退（CHAN_CFG.sinkFallback，SPEC_divergence_fallback）：停止级 S 无候选
    （方向/创新低/背驰任一不过）时，沿链向上一级（仍须严格 < X）用该级形成中段 +
    该级参照笔重评，首个命中级产候选并带 fallback=True——最细级别的"动能加速"
    不再一票否决大级别已成立的背驰（如 8-7 08:45：3m 加速但 15m 真背驰）。
    关闭时保持原行为（只在停止级评估）。

    候选条件（与确认制 findDivergePoints 同一套背驰标准，对象换成该级形成中段）：
      - 段方向匹配（做多→形成中下跌段 / 做空→形成中上涨段）；
      - 段长 ≥ minBars（够笔门槛，见 REALTIME_MIN_BARS）；
      - 创新低/新高：先比笔端点影线 endPrice（严格更极端，或价差 ∈ [0, macdDivergeTol]）；
        影线不过且 macdDivergeBodyOn（默认开）时再比该 K 实体
        （底 min(open,close) / 顶 max(open,close)）。仅落在容差内（未严格突破）
        要求三判据 AND（_biDivergeStrictAll），候选带 nearEqual=True；
      - isBiDiverge（当下对比，2026-10-01 起双判据 AND）：DIF低点抬高（多）/DIF高点变低（空）
        且（时长可比时）绿柱/红柱面积变小；时长比>3 时面积不计入，DIF 单判据兜底。
    参照笔 = 向前最近同向**已完成**笔（跳过幅度 < 当前段 50% 的次级别回调），
    且必须与候选段同处其所属上级笔内部（规则 2）——参照跨上级笔边界则候选无效。

    @param periodTimes   各周期K线时间数组（升序，二分用）；缺省时从 periodData[res].bars 现建
    @param reasons_out   可选 list：无候选时逐因append {"gate", "segStart", **ctx}
                         （交易日志拒绝叙事；None=不收集，热路径零开销）
    @returns 候选列表（≤1 条）[ { res, point:{time,price,direction}, segStart, fallback?, nearEqual? } ]
    """
    nodes = _sinkChainRealtimeNodes(periodData, X, wantDir, tCut=tCut,
                                    periodTimes=periodTimes,
                                    expectBiEnabled=expectBiEnabled)
    if not nodes or len(nodes) < 2:
        if reasons_out is not None:
            reasons_out.append({"gate": "sink_chain_short"})
        return []  # 链不通 / 停止级=检测周期自身 → 无严格更低级别背驰，不产信号
    if divergeConfirm is None:
        divergeConfirm = bool(CHAN_CFG.get("divergeConfirm"))
    if entryMacdShrink is None:
        entryMacdShrink = bool(CHAN_CFG.get("entryMacdShrink"))
    last = len(nodes) - 1
    order = range(last, 0, -1) if CHAN_CFG.get("sinkFallback") else [last]
    for depth in order:  # 停止级优先；M1 开启时逐级向粗回退（不含 X 自身）
        cand, code, rctx = _evalRealtimeNode(periodData, nodes[depth], wantDir, tCut,
                                             periodTimes, minBars)
        if cand is None:
            if reasons_out is not None:
                reasons_out.append({"gate": code, **(rctx or {})})
            continue
        # M4 分型确认进场（页面可选，默认当下）：极值K线的右邻K收盘（分型可见最早
        # 时刻的代理，未做合并包含严格校验）后本拍才出信号 → 下一根 fine 开盘成交。
        # 段延伸（极值后移）时确认时钟自动重置。
        if divergeConfirm:
            confirmAt = cand["point"]["time"] + 2 * (intervalSecOf(cand["res"]) or 0)
            if tCut < confirmAt:
                if reasons_out is not None:
                    reasons_out.append({"gate": "diverge_confirm_wait",
                                        "confirmAt": confirmAt,
                                        "res": cand["res"], "segStart": cand["segStart"]})
                return []
        # 进场MACD柱缩闸（CHAN_CFG.entryMacdShrink，2026-09-23）：背驰级别最近两根
        # 已收K线的柱状体（|macd|）变小才出信号——动能仍在放大时不进场，等收缩确认。
        # 上一根=决策拍最近已收的背驰级别K线（macdArr 仅含已收K线，无前视）；
        # 闸未过不消耗段去重键（fired.add 只在真正发出处），下一拍自动重评；
        # 不足两根无从判定 → 拦截（保守）。
        if entryMacdShrink:
            arr = (periodData.get(cand["res"]) or {}).get("macdArr") or []
            if (len(arr) < 2
                    or not abs(arr[-1]["macd"]) < abs(arr[-2]["macd"])):
                if reasons_out is not None:
                    reasons_out.append({
                        "gate": "macd_shrink_gate",
                        "last": round(abs(arr[-1]["macd"]), 4) if len(arr) >= 1 else None,
                        "prev": round(abs(arr[-2]["macd"]), 4) if len(arr) >= 2 else None,
                        "res": cand["res"], "segStart": cand["segStart"]})
                return []
        if depth < last:
            cand["fallback"] = True  # 非停止级命中 = 回退候选
        return [cand]
    return []


def evaluateRealtimeEntries(periodBis, periodMacd, periodAtr, periodPoints, srLevels,
                            detectPeriods, near=NEAR, tCut=None, fired=None,
                            periodTimes=None, periodMacdTimes=None, divergeConfirm=None,
                            expectBiEnabled=None, realtimeMinBars=None,
                            zsExitWeakRatio=None, trend_res=None, trend_state=None,
                            periodMerged=None, periodBars=None, firedIndex=None,
                            entryMacdShrink=None, range_res=None, range_gate=None,
                            on_reject=None):
    """当下模式进场评估（每根 fine 收盘调用，信号无需等反向笔确认）。

    候选来源（2026-10-16 起）：periodPoints 的 chan_core 买卖点——买卖双侧各自取
    最近笔端点锚（side_candidates，原 predictPlan.matchSide 平移）→ pointStrategyOf
    直映射策略键；原「交易计划」文案中转已取消，plan_watch 闸门随之删除。

    三条件与确认制同构，差异只在"何时评"与"②用什么评"：
      ① 够笔：检测周期最后一笔（引擎中=延伸中的形成段）方向匹配且段长 ≥ REALTIME_MIN_BARS；
         预期够笔（expectBiEnough 固定口径）：末笔方向相反但端点后 ≥ expectBiMinBars 根
         本级K线即视为回调/反弹中（信号带 expectBi=True，不等反向分型右邻收盘）；
      ② 当下背驰：realtimeLowerDiverge（区间套下沉到停止级，仅在该级的形成中段上
         判创新低/新高 + 当拍 MACD 对比，SPEC_divergence_chanset 规则 1/2/3）；
      ③ 支阻位附近：形成中段当前极值价 vs 检测周期支阻位（价差 ≤ near，绝对价差不乘 ATR）。
    策略专属条件与确认制共用（strategyExtraOk）。

    去重：fired 集合按 (periodX, strategyKey, markRes, 段起点时间)——每个形成段只发一次，
    段延伸（继续创新低）不重发；新段（新起点）重新评估。

    @param tCut              当前时刻（fine 收盘时间）；None 时取各周期数据末尾
    @param fired             去重集合（调用方跨拍持有，原地更新）
    @param firedIndex        可选：{(periodX, strategyKey, segStart)} 索引（fired 的 O(1)
                             预检视图，由调用方与 fired 同步维护；None 时回退 any() 线性扫描）
    @param periodTimes       各周期K线时间数组 { res: [times] }（二分用，可选）
    @param periodMacdTimes   各周期MACD时间数组 { res: [times] }（切片用，可选；
                             缺省时 realtimeLowerDiverge 内部现建）
    @param divergeConfirm    M4 背驰进场时机（None → 读 CHAN_CFG.divergeConfirm）：
                             True=极值K线右邻K收盘后才出信号（分型确认后下一根开盘进场）
    @param entryMacdShrink   进场MACD柱缩闸（None → 读 CHAN_CFG.entryMacdShrink）：
                             True=背驰级别最近两根已收K线柱状体（|macd|）变小才出信号
                             （上一根K确认背驰且柱缩，下一根开盘进场；闸未过不消耗
                             段去重键，下一拍自动重评）
    @param trend_res         顺势参考周期（None→TREND_RES，默认关闭；"240"/"D"=开启）。
                             参考周期及以上不作检测周期；方向与信号相反时跳过信号
                             （规则见 trading_plan.trend_direction）。
    @param trend_state       参考周期方向状态（trend_state_of 结果）。本函数无 bars
                             入参、无法自行计算，须由调用方传入（回测引擎在链路重算拍
                             算好复用；None = 不过滤，仅保留检测周期剔除）。
    @param range_res         震荡判定参考周期（None→RANGE_RES，默认关闭；"240"/"D"=开启）。
                             参考周期及以上不作检测周期（res_ge_range 结构性剔除）。
    @param range_gate        震荡 regime（compute_regime 结果，调用方预计算传入；
                             range=True → 本周期观望 range_watch；None/关闭 = 无震荡闸）。
    @param on_reject         可选 on_reject(gate, period, seg_start, strategy_key, ctx, t)：
                             每个静默 continue 闸门处上报（交易日志「为什么没开单」；
                             ctx 为结构化数字，bt_journal 按 (周期,策略,段,闸门) 去重落盘，
                             热路径只多一次调用+小 dict 构造）。
    @returns 新信号列表（flat），每项含 { periodX, markRes, time, price, direction,
             strategyKey, strategyLabel, signalNote, nearSr, pointType, pointStrong,
             realtime:True, segStart, fallback?, nearEqual?, expectBi,
             trendDirection?, trendReason? }
    """
    fired = fired if fired is not None else set()
    # 出场参数（参数中心可调；None → 模块常量默认）
    min_bars = realtimeMinBars if realtimeMinBars is not None else REALTIME_MIN_BARS
    zs_ratio = zsExitWeakRatio if zsExitWeakRatio is not None else ZS_EXIT_WEAK_RATIO
    if tCut is None:
        tCut = max((ts[-1] for ts in (periodTimes or {}).values() if ts), default=0)
    # 组装 periodData（bis/macdArr/atr/macdTimes）
    periodData = {}
    for res, bis in (periodBis or {}).items():
        if not bis:
            continue
        periodData[res] = {"bis": bis,
                           "macdArr": (periodMacd or {}).get(res) or [],
                           "macdTimes": (periodMacdTimes or {}).get(res),
                           "atr": (periodAtr or {}).get(res) or 0,
                           "bars": (periodBars or {}).get(res) or [],
                           "merged": (periodMerged or {}).get(res)}
    if not periodData:
        return []

    def upperResOf(res):
        sec = intervalSecOf(res) or 0
        best = None
        for r in periodData:
            s = intervalSecOf(r) or 0
            if s > sec and (best is None or s < intervalSecOf(best)):
                best = r
        return best

    out = []
    # 顺势参考周期（同 compute_entries；trend_state 须由调用方传入，见 docstring）
    trend_res = TREND_RES if trend_res is None else trend_res
    trend_sec = (intervalSecOf(trend_res) or 0) if trend_res else 0
    trend_dir = (trend_state or {}).get("dir") if trend_res else None
    trend_reason = (trend_state or {}).get("reason", "") if trend_res else ""
    # 震荡参考周期（compute_regime 的 regime；默认关闭）
    range_res = RANGE_RES if range_res is None else range_res
    range_sec = (intervalSecOf(range_res) or 0) if range_res else 0
    for X in (detectPeriods or []):
        def _rej(gate, seg_start=None, skey=None, ctx=None):
            if on_reject is not None:
                try:
                    on_reject(gate, X, seg_start, skey, ctx or {}, tCut)
                except Exception:
                    pass
        pd = periodData.get(X)
        if pd is None:
            continue
        if trend_sec and (intervalSecOf(X) or 0) >= trend_sec:
            _rej("res_ge_trend", ctx={"res": X, "trendRes": trend_res})
            continue  # 顺势参考周期及以上不作检测周期（只作方向锚，结构性剔除）
        if range_sec and (intervalSecOf(X) or 0) >= range_sec:
            _rej("res_ge_range", ctx={"res": X, "rangeRes": range_res})
            continue  # 震荡参考周期及以上不作检测周期（只作锚，结构性剔除；原计划口径）
        if range_res and range_gate is not None and range_gate.get("range"):
            _rej("range_watch", ctx={"rangeRes": range_gate.get("resName") or range_res,
                                     "reason": (range_gate.get("reason") or "")[:80]})
            continue  # 参考周期 regime 震荡/笔不足 → 本周期观望（原计划「震荡整理观望」）
        # 2026-10-10 买卖双侧独立（原 trading_plan.predictPlan 双锚口径保留）：
        # 买侧 + 卖侧各自取最近笔端点锚，每侧各自过闸（够笔/专属/背驰链/支阻/顺势
        # 过滤），互不影响、多空可同时持仓。
        cands = side_candidates(pd["bis"], (periodPoints or {}).get(X) or {},
                                pd.get("atr"), intervalSecOf(X) or 60)
        if not cands:
            _rej("no_point", ctx={})
            continue
        for point in cands:
            strategy = pointStrategyOf(point)
            if strategy is None:
                _rej("no_strategy_map", ctx={"pointType": point.get("type")})
                continue
            if trend_dir and trend_dir != strategy["direction"]:
                _rej("trend_filter", ctx={"trendDir": trend_dir,
                                          "need": strategy["direction"],
                                          "trendReason": trend_reason})
                continue  # 逆参考周期方向的信号跳过（顺势过滤，按侧独立过滤）
            key = strategy["key"]
            direction = strategy["direction"]
            wantType = "up" if direction == "short" else "down"  # 空头等反弹(up)，多头等回调(down)
            bis = pd["bis"]
            if not bis:
                _rej("no_bis", skey=key)
                continue
            times = (periodTimes or {}).get(X) or [b["time"] for b in (pd.get("bars") or [])]
            # ⓪ 锚点窗口（2026-10-18 用户规则；2026-10-21 重定义参数化）：时间窗=
            # 锚K起 anchorWinBars 根检测周期K（旧口径=1），当拍与背驰点所在K都须
            # 落窗、锚K之前仍拒；价距窗=触发价距窗口极值（买侧最低/卖侧最高=
            # 锚点价与锚K开盘起细周期已收K极值取小/大）≤ anchorWinPts（0=不限）。
            # 无永久闩锁：锚随新高/新低漂移或换新点时，每拍按当前锚现算——新锚
            # 所在K线自动重开窗。trailing 重开（2026-10-22 anchorWinTrailOn 默认开）：
            # 窗口内创新高/新低时，时间窗锚K=窗口极值所在K（价距基准本就随极值漂移，
            # 时间窗此前只认锚点固定时间戳——连续新高不可能产生新压低2卖锚，
            # 窗口卡死，9-8 12:00 案例）；极值仍落原锚K时与旧口径逐位一致；
            # False=2026-10-21 固定锚K口径（A/B 基准）。
            # times 缺失（无 periodTimes 且无 bars 的最小 fixture）时降级放行，
            # 不影响既有最小单测。
            winBars = int(CHAN_CFG.get("anchorWinBars", 2) or 2)
            winPts = float(CHAN_CFG.get("anchorWinPts", 10.0) or 0.0)
            trailOn = bool(CHAN_CFG.get("anchorWinTrailOn", True))
            anchorWinIdx = nowWinIdx = winAnchorIdx = None
            winRef = winExtT = None
            if times:
                anchorWinIdx = _cover_bar_index(times, point.get("time"))
                nowWinIdx = _cover_bar_index(times, tCut)
                if trailOn and anchorWinIdx is not None:
                    winRef, winExtT = _win_extreme_ref(periodData, times[anchorWinIdx],
                                                       direction, point.get("price"),
                                                       point.get("time"))
                    winAnchorIdx = _cover_bar_index(times, winExtT)
                    if winAnchorIdx is None or winAnchorIdx < anchorWinIdx:
                        winAnchorIdx = anchorWinIdx  # 极值时间早于锚K（不应发生）兜底
                else:
                    winAnchorIdx = anchorWinIdx
                if not _anchor_in_window(nowWinIdx, winAnchorIdx, winBars):
                    _rej("anchor_bar_window", skey=key,
                         ctx={"pointType": point.get("type"), "anchorTime": point.get("time"),
                              "nowBarTime": times[nowWinIdx] if nowWinIdx is not None else None,
                              "effAnchorTime": (times[winAnchorIdx]
                                                if winAnchorIdx is not None else None),
                              "extTime": winExtT, "extPrice": winRef})
                    continue
                # 价距（主闸=当拍现价）：细周期K线/现价缺失（最小 fixture）时跳过，
                # 次级仍以 winRef（含锚点价兜底）查背驰点价
                if winPts > 0:
                    if winRef is None:
                        winRef = _win_extreme_ref(periodData, times[anchorWinIdx],
                                                  direction, point.get("price"))[0]
                    nowPx = _beat_price(periodData)
                    if nowPx is not None:
                        dist = (nowPx - winRef) if direction == "long" else (winRef - nowPx)
                        if dist > winPts:
                            _rej("anchor_bar_window", skey=key,
                                 ctx={"pointType": point.get("type"),
                                      "anchorTime": point.get("time"),
                                      "nowBarTime": times[nowWinIdx],
                                      "price": nowPx, "winExt": winRef,
                                      "dist": round(dist, 4)})
                            continue
            # ① 够笔（当下制）：最后一笔=形成中段，方向匹配 + 段长门槛；
            #    预期够笔（expectBiEnabled，页面可选默认开）：末笔方向不符（回调/反弹的反向段
            #    尚未确认为笔）但末笔端点后已走出 ≥ expectBiMinBars 根本级K线 → 视为回调/反弹中，
            #    段起点改用末笔端点（分型确认固有 1 根K线滞后，不等右邻收盘）；
            #    False = 旧口径：末笔必须已是确认的反向笔。
            merged = _mergedFor(pd, X, tCut)
            forming = bis[-1].get("_forming", False)
            expectBi = forming or bis[-1]["type"] != wantType
            if forming:
                enabled = CHAN_CFG.get("expectBiEnough") if expectBiEnabled is None else expectBiEnabled
                if not enabled or bis[-1]["type"] != wantType:
                    _rej("forming_wrong_type", skey=key,
                         ctx={"lastType": bis[-1]["type"], "want": wantType})
                    continue
                segStart = bis[-1]["startTime"]
                eCnt = mergedSegmentCount(merged, segStart, intervalSecOf(X))
                eNeed = int(CHAN_CFG.get("expectBiMinBars", 5) or 5)
                if eCnt < eNeed:
                    _rej("expect_min_bars", seg_start=segStart, skey=key,
                         ctx={"count": eCnt, "need": eNeed})
                    continue
            elif expectBi:
                if not counterMoveQualifies(times, bis[-1], tCut, enabled=expectBiEnabled,
                                            merged=merged, barSec=intervalSecOf(X)):
                    _rej("counter_move_fail", seg_start=bis[-1]["endTime"], skey=key)
                    continue
                segStart = bis[-1]["endTime"]
            else:
                segStart = bis[-1]["startTime"]
            mCnt = mergedSegmentCount(merged, segStart, intervalSecOf(X))
            if mCnt < min_bars:
                _rej("min_bars", seg_start=segStart, skey=key, ctx={"count": mCnt, "need": min_bars})
                continue
            # 该 (periodX, strategyKey, segStart) 已在任一 markRes 发过 → 跳过重算背驰链
            # （形成段延伸不重发；多 markRes 同段在既有口径下也只会命中一次有效进场路径）
            if firedIndex is not None:
                if (X, key, segStart) in firedIndex:
                    _rej("fired_dedup", seg_start=segStart, skey=key)
                    continue
            elif any(f[0] == X and f[1] == key and f[3] == segStart for f in fired):
                _rej("fired_dedup", seg_start=segStart, skey=key)
                continue
            # 策略专属条件（与确认制共用；pointCls 由点 strong 直派生）
            upRes = upperResOf(X)
            upperBis = periodData[upRes]["bis"] if (upRes and upRes in periodData) else None
            extraReason = strategyExtraOk(key, bis, upperBis, pd["macdArr"], intervalSecOf(X),
                                          zs_ratio, point_cls=strategy.get("pointCls"))
            if extraReason is not None:
                _rej("strategy_extra", seg_start=segStart, skey=key, ctx={"reason": extraReason})
                continue
            # ② 当下背驰 + ③ 支阻位附近（候选级别从大到小，命中即出）
            # 注意循环变量用 hit：near 是本函数参数（绝对价差），曾用 near 接收返回 dict
            # 导致后续检测周期 nearTol 变 dict（float<=dict 崩溃，2026-09-13 修复）
            # 支阻只取检测周期 X，背驰级别的价位不参与靠近判定
            nearTol = near  # 绝对价差（不乘 ATR，恒 > 0）
            # 1类键（wait1Buy/wait1Sell）豁免黄金分割支阻（同确认制闸门口径，2026-10-10）
            srX = sr_drop_fib_for_class1(sr_of_detect(srLevels, X), key)
            rsn = [] if on_reject is not None else None
            cands = realtimeLowerDiverge(periodData, X, direction, tCut,
                                         periodTimes=periodTimes or {},
                                         divergeConfirm=divergeConfirm,
                                         expectBiEnabled=expectBiEnabled,
                                         entryMacdShrink=entryMacdShrink,
                                         reasons_out=rsn)
            if not cands and rsn:
                for r in rsn:
                    _rej(r.pop("gate"), seg_start=r.pop("segStart", None), skey=key, ctx=r)
            for c in cands:
                fkey = (X, key, c["res"], c["segStart"])
                if fkey in fired:
                    _rej("cand_fired", seg_start=c["segStart"], skey=key,
                         ctx={"markRes": c["res"]})
                    continue  # 该形成段已发过，段延伸不重发
                # 次级保险（锚点窗口）：当前拍在窗内、但背驰极值是窗口外更早K线的
                # 陈旧极值时同样拒绝——背驰点所在K须落在锚K起 N 根内（trailing 口径
                # =窗口极值所在K，主闸已算 winAnchorIdx），且背驰点价
                # 距窗口极值 ≤ anchorWinPts（winRef 主闸已算，含锚点价兜底）
                if times and not _anchor_in_window(_cover_bar_index(times, c["point"]["time"]),
                                                   winAnchorIdx, winBars):
                    _rej("anchor_bar_window", seg_start=c["segStart"], skey=key,
                         ctx={"markRes": c["res"], "divergeTime": c["point"]["time"],
                              "anchorTime": point.get("time")})
                    continue
                if times and winPts > 0:
                    ref = winRef if winRef is not None else point.get("price")
                    px = c["point"]["price"]
                    dist = (px - ref) if direction == "long" else (ref - px)
                    if dist > winPts:
                        _rej("anchor_bar_window", seg_start=c["segStart"], skey=key,
                             ctx={"markRes": c["res"], "divergeTime": c["point"]["time"],
                                  "anchorTime": point.get("time"),
                                  "price": px, "winExt": ref, "dist": round(dist, 4)})
                        continue
                # 区间感知闸门（同确认制 evaluateEntry：levels=逐位同 nearSr，
                # zones=背驰点价落入方向匹配区间）
                hit = near_zone(c["point"]["price"], srX, direction, nearTol)
                if hit is None:
                    near2 = nearest_sr(c["point"]["price"], srX)
                    _rej("near_sr_fail", seg_start=c["segStart"], skey=key,
                         ctx={"markRes": c["res"], "price": c["point"]["price"],
                              "srPrice": near2[0] if near2 else None,
                              "dist": round(near2[1], 4) if near2 else None,
                              "near": nearTol})
                    continue
                fired.add(fkey)
                # 索引与 fired 只在真正发出处同步维护（预检处绝不加——会把未发段误标已发）
                if firedIndex is not None:
                    firedIndex.add((fkey[0], fkey[1], fkey[3]))
                sig = {
                    "periodX": X,
                    "markRes": c["res"],
                    "time": c["point"]["time"],
                    "price": c["point"]["price"],
                    "direction": direction,
                    "strategyKey": key,
                    "strategyLabel": strategy["label"],
                    "signalNote": (
                        f"{strategy['label']}｜检测周期 {X} 锚点："
                        f"{point['type']}@{fmtT(point['time'])} {point['price']:.2f}"
                        f"｜背驰 {c['res']} @ {fmtT(c['point']['time'])} {c['point']['price']:.2f}"
                        f"｜近支阻位 {hit['sr']['price']:.2f}｜形成段自 {fmtT(c['segStart'])}"),
                    "nearSr": hit["sr"]["price"],
                    "pointType": point.get("type"),
                    "pointStrong": point.get("strong"),
                    "realtime": True,
                    "segStart": c["segStart"],
                    "fallback": bool(c.get("fallback", False)),   # M1 回退候选（非停止级命中）
                    "nearEqual": bool(c.get("nearEqual", False)), # M2 近等候选（未严格创新极值）
                    "expectBi": expectBi,                         # M4 检测周期走了预期够笔口径
                }
                # 观望态（dir=None 但 reason 非空）也要带注记；方向门控仍只看 dir
                if trend_dir or trend_reason:
                    sig["trendDirection"] = trend_dir
                    sig["trendReason"] = trend_reason
                out.append(sig)
    return out


# ============================================================
# 汇总计算（供回测链路调用）
# ============================================================

# 全部参与判定的周期（与 JS 一致）：检测周期 + 日线（仅作为 240 的上一级别笔）。
# with_30s=True 时追加 30S（3 分钟状态可用 30S 背驰产生进场信号，箭头画在 30S 级别）
ALL_RES = ["D", "240", "60", "15", "3"]
ALL_RES_WITH_30S = ["D", "240", "60", "15", "3", "30S"]


def compute_entries(periodBis, barsByPeriod, periodPoints, srLevels, detectPeriods,
                    near=NEAR, periodMacd=None, periodAtr=None, with_30s=False,
                    zs_exit_weak_ratio=ZS_EXIT_WEAK_RATIO,
                    trend_res=None, trend_state=None, trend_cfg=None,
                    range_res=None, range_gate=None, on_reject=None):
    """逐周期判定进场状态（直接消费买卖点，2026-10-16 起替代计划锚）→ 生成进场信号。

    @param periodBis     各周期笔 { 周期: [bis] }
    @param barsByPeriod  各周期原始K线 { 周期: [bars] }
    @param periodPoints  各周期买卖点 { 周期: {"buy": […], "sell": […]} }
                         （compute_period_points 结果；买卖双侧锚 → pointStrategyOf 直映射）
    @param srLevels      支阻位列表（srflip.merged 全量候选池，每项含 price/level）；
                         各检测周期只使用 level 等于该周期的条目
    @param detectPeriods 检测周期列表（从大到小，默认 240,60,15,3）
    @param near          靠近支阻位阈值（绝对价差，不乘 ATR）
    @param periodMacd    可选：各周期预计算 MACD { 周期: [macdArr] }
    @param periodAtr     可选：各周期预计算 ATR { 周期: atr }
    @param with_30s      启用 30 秒级别（ALL_RES 追加 30S，仍按数据存在性过滤）
    @param trend_res     顺势参考周期（None→TREND_RES，默认关闭；"240"/"D"=开启）。
                         参考周期及以上不作检测周期（只作方向锚，结构性剔除）；
                         参考周期方向与信号方向相反时跳过（规则见
                         trading_plan.trend_direction）。
    @param trend_state   调用方预计算的参考周期方向状态（trend_state_of 结果），
                         缺省时本函数内部计算（回测引擎在链路重算拍复用传入）。
    @param trend_cfg     顺势相位判定参数（plan 模块 trendRebound/reboundNearPts/
                         reboundAngleRef；缺省用 trading_plan 常量默认，仅自算
                         trend_state 路径消费）。
    @param range_res     震荡判定参考周期（None→RANGE_RES，默认关闭）；参考周期及以上
                         不作检测周期（res_ge_range 结构性剔除）。
    @param range_gate    震荡 regime（compute_regime 结果；range=True → 本周期观望）。
    @param on_reject     可选 on_reject(gate, period, seg_start, strategy_key, ctx, t)：
                         evaluateEntry 校验未过时上报（gate=eval_reason，ctx.reason=
                         中文失败原因；交易日志用，与当下制同签名）。
    @returns { 标记级别: [信号...] }，信号含 { periodX, time, price, direction, strategyKey,
             strategyLabel, signalNote, nearSr, color, markRes, pointType, pointStrong,
             trendDirection, trendReason }
    """
    periodBis = structurePeriods(periodBis, barsByPeriod)
    periodMacd = periodMacd or {}
    periodAtr = periodAtr or {}
    all_res = ALL_RES_WITH_30S if with_30s else ALL_RES
    periodData = {}
    for res in all_res:
        bis = periodBis.get(res, []) or []
        if not bis:
            continue
        bars = barsByPeriod.get(res, []) or []
        if not bars:
            continue
        atr = periodAtr.get(res)
        if atr is None:
            atr = calcATR(bars, 14)
        macdArr = periodMacd.get(res)
        if macdArr is None:
            macdArr = calcMACD(bars)
        periodData[res] = {
            "bis": bis,
            "bars": bars,
            "atr": atr,
            "macdArr": macdArr,
        }

    # 上一级别周期映射：240→D、60→240、15→60、3→15（取有数据的最小更大级别）
    def upperResOf(res):
        sec = intervalSecOf(res) or 0
        best = None
        for r in periodData:
            s = intervalSecOf(r) or 0
            if s > sec and (best is None or s < intervalSecOf(best)):
                best = r
        return best

    # 顺势参考周期方向（trading_plan.trend_state_of；trend_state 可由调用方在链路
    # 重算拍预计算传入复用）。trend_res 为空 = 关闭：参考周期照常检测、方向不过滤。
    trend_res = TREND_RES if trend_res is None else trend_res
    trend_sec = (intervalSecOf(trend_res) or 0) if trend_res else 0
    if trend_state is None and trend_res:
        trend_state = trend_state_of(periodBis, barsByPeriod, trend_res,
                                     periodMacd=periodMacd, cfg=trend_cfg)
    trend_dir = (trend_state or {}).get("dir") if trend_res else None
    trend_reason = (trend_state or {}).get("reason", "") if trend_res else ""
    # 震荡参考周期（compute_regime 的 regime；默认关闭）
    range_res = RANGE_RES if range_res is None else range_res
    range_sec = (intervalSecOf(range_res) or 0) if range_res else 0

    allEntries = {}
    for res in detectPeriods:
        pd = periodData.get(res)
        if pd is None:
            continue
        if trend_sec and (intervalSecOf(res) or 0) >= trend_sec:
            continue  # 顺势参考周期及以上不作检测周期（只作方向锚，结构性剔除）
        if range_sec and (intervalSecOf(res) or 0) >= range_sec:
            continue  # 震荡参考周期及以上不作检测周期（只作锚，结构性剔除；原计划口径）
        if range_res and range_gate is not None and range_gate.get("range"):
            continue  # 参考周期 regime 震荡/笔不足 → 本周期观望（原计划「震荡整理观望」）
        # 2026-10-10 买卖双侧独立口径保留：买侧 + 卖侧各自最近笔端点锚并行评估
        cands = side_candidates(pd["bis"], (periodPoints or {}).get(res) or {},
                                pd.get("atr"), intervalSecOf(res) or 60)
        if not cands:
            continue
        upRes = upperResOf(res)
        ctx = {
            "res": res,
            "bis": pd["bis"],
            "upperBis": periodData[upRes]["bis"] if (upRes and upRes in periodData) else None,
            "macdArr": pd["macdArr"],
            "atr": pd["atr"],
            "barSec": intervalSecOf(res),
            "near": near,
            "srLevels": srLevels,
            "periodData": periodData,
            "zs_exit_weak_ratio": zs_exit_weak_ratio,
        }
        for point in cands:
            strategy = pointStrategyOf(point)
            if strategy is None:
                continue
            if trend_dir and trend_dir != strategy["direction"]:
                continue  # 逆参考周期方向的信号跳过（顺势过滤，按侧独立过滤）
            ctx["pointCls"] = strategy.get("pointCls")
            ctx["anchorPoint"] = point  # 锚点K线窗口闸（2026-10-18）：背驰仅限锚点所在K线内
            evalRes = evaluateEntry(ctx, strategy)
            if not evalRes["ok"]:
                if on_reject is not None:
                    try:
                        t_approx = pd["bis"][-1]["endTime"] if pd.get("bis") else None
                        on_reject("eval_reason", res, None, strategy["key"],
                                  {"reason": evalRes.get("reason")}, t_approx)
                    except Exception:
                        pass
                continue
            # 命中：在背驰级别标记箭头
            sig = {
                "periodX": res,
                "time": evalRes["point"]["time"],
                "price": evalRes["point"]["price"],
                "direction": strategy["direction"],
                "strategyKey": strategy["key"],
                "strategyLabel": strategy["label"],
                "signalNote": (
                    f"{strategy['label']}｜检测周期 {res} 锚点："
                    f"{point['type']}@{fmtT(point['time'])} {point['price']:.2f}"
                    f"｜背驰 {evalRes['markRes']} @ {fmtT(evalRes['point']['time'])} "
                    f"{evalRes['point']['price']:.2f}｜近支阻位 {evalRes['nearSr']:.2f}"),
                "nearSr": evalRes["nearSr"],
                "color": BUY_COLOR if strategy["direction"] == "long" else SELL_COLOR,
                "markRes": evalRes["markRes"],
                "pointType": point.get("type"),
                "pointStrong": point.get("strong"),
            }
            # 观望态（dir=None 但 reason 非空）也要带注记；方向门控仍只看 dir
            if trend_dir or trend_reason:
                sig["trendDirection"] = trend_dir
                sig["trendReason"] = trend_reason
            allEntries.setdefault(evalRes["markRes"], []).append(sig)
    return allEntries


# ============================================================
# 出场规则（与 mark_entry.js 对齐：stopRefOf / findBiEvent / formingSegReady）
# ============================================================


def stop_ref_of(direction, entry_price, near_sr, sr_levels,
                slip_stop=DEFAULT_SLIP_STOP, slip_fallback=DEFAULT_SLIP_FALLBACK,
                atr=0.0, k_stop=0.0, k_fallback=0.0, source_out=None):
    """方向感知的止损参考位（含滑点偏移与最大止损硬上限，返回值永不为 None）：
    short 取进场价上方最近支阻位（阻力）+ slip_stop、long 取下方最近（支撑）− slip_stop。

    near_sr（进场校验按绝对价差最近命中的支阻位价，不分上下方）已在正确侧直接沿用；
    否则从 sr_levels 重选正确侧最近位（进场判定逻辑不变，仅供出场止损参考）。
    无正确侧位 → 止损 = 进场价 ± slip_fallback。
    最大止损硬上限：多单止损 ≥ 进场价−有效最大止损，空单止损 ≤ 进场价+有效最大止损
    （支阻位更远时收到此价，亏损不超过该滑点）。
    @param direction     "long" | "short"
    @param entry_price   进场价
    @param near_sr       信号自带的近支阻位价格（可为 None）
    @param sr_levels     支阻位列表（dict 含 "price"，或直接为价格数值）
    @param slip_stop     支阻位滑点（绝对价格，short + / long −）
    @param slip_fallback 最大止损（绝对价格；键名历史遗留）
    @param atr           背驰周期 ATR(14)（2026-09-19 ATR 分量，缺省 0 = 纯固定滑点）
    @param k_stop        止损滑点 ATR 系数：有效滑点 = slip_stop + k_stop×atr（0=关闭）
    @param k_fallback    最大止损 ATR 系数：有效最大止损 = slip_fallback + k_fallback×atr
    """
    atr = atr or 0.0
    slip_stop = slip_stop + k_stop * atr
    slip_fallback = slip_fallback + k_fallback * atr
    is_short = direction == "short"
    slip = slip_stop if is_short else -slip_stop
    # 最大止损价：多单不低于此价、空单不高于此价
    max_loss = entry_price + (slip_fallback if is_short else -slip_fallback)
    if near_sr is not None and (near_sr > entry_price if is_short else near_sr < entry_price):
        ref = near_sr + slip
        if source_out is not None:
            source_out.update({"src": "near_sr", "srPrice": near_sr})
    else:
        best = None
        for sr in (sr_levels or []):
            p = sr.get("price") if isinstance(sr, dict) else sr
            if p is None:
                continue
            if (p > entry_price) if is_short else (p < entry_price):
                d = abs(p - entry_price)
                if best is None or d < best[1]:
                    best = (p, d)
        ref = max_loss if best is None else best[0] + slip
        if source_out is not None:
            # near_sr=信号命中的近支阻位（沿用）/ sr_pick=正确侧重选位 / fallback=最大止损兜底
            source_out.update({"src": "fallback"} if best is None
                               else {"src": "sr_pick", "srPrice": best[0]})
    # 支阻位更远时收到最大止损价（多抬高 / 空压低）
    return min(ref, max_loss) if is_short else max(ref, max_loss)


def trend_following_of(plan_direction, strategy_key=None):
    """顺势/逆势判定（TP2 平一半 / TP3 分支门槛）：
    2026-10-16 起交易计划 direction 已取消，恒按 strategy_key 判定
    （wait2Buy/waitBuy/wait3Buy/waitLike2Buy 及卖侧对称 → 顺势；
    wait1Buy/wait1Sell/wait2BuyBear/wait2SellBear → 逆势）。
    plan_direction 形参保留仅为兼容旧调用方，传入即忽略。
    """
    if strategy_key is None and plan_direction:
        return plan_direction in TREND_PLAN_DIRS  # 旧口径兼容（无 strategyKey 时）
    return strategy_key in TREND_STRATEGY_KEYS


def forming_seg_ready(px_bis, px_merged_times, is_short, min_merged=EXIT_MIN_MERGED):
    """检测周期形成段「成笔预期」判定（历史 TP2/逆势 TP3b 事件源；2026-10-10 逆势
    TP3 统一改破前低参照后引擎不再调用，保留为计数口径文档与单测工具。锚点无
    进场块下限——进场前已走出的块会计入，事件源用途已由 enough_fav_bars 取代）：
    末笔为不利方向（short→up / long→down）且其后正在走的有利方向形成段，
    自末笔延伸终点所在合并块起，其后合并K线块数 ≥ min_merged−1（与 isValid
    「两分型间隔 gap>=4」同口径，含锚点块共 min_merged 块）。

    锚点按末笔 endTime 在 px_merged_times（合并块截止时间数组，升序）中二分定位——
    不能用 bis[-1]["endIdx"]：extendLastBiFrom 延伸时不更新 endIdx。
    末笔延伸（创新不利极值）时 endTime 推进、锚点右移、计数自动归零。
    @param px_bis          检测周期笔列表（末笔可为延伸中的形成笔）
    @param px_merged_times 检测周期合并K线块截止时间数组（升序，与引擎 _merged_times 同构）
    @param is_short        持仓方向是否空头
    @param min_merged      成笔预期门槛（默认 EXIT_MIN_MERGED=5）
    """
    if not px_bis or not px_merged_times:
        return False
    last = px_bis[-1]
    if last["type"] != ("up" if is_short else "down"):
        return False  # 末笔为有利方向 → 其后形成段为不利方向，不触发
    anchor = bisect.bisect_left(px_merged_times, last["endTime"])
    if anchor >= len(px_merged_times):
        return False
    return (len(px_merged_times) - 1) - anchor >= min_merged - 1


def enough_fav_bars(bis, merged_times, entry_time, is_short, min_merged=EXIT_MIN_MERGED):
    """够笔统一口径（2026-10-10 用户拍板）：向有利方向走出 min_merged 块合并K即
    够笔，不分顺势/逆势、不等确认笔。检测周期（half）与背驰周期（halfMr）共用。

    锚点 = 末笔延伸终点块 与 进场时刻所在块 的靠后者，自锚点块起（含）共
    min_merged 块即触发（与 forming_seg_ready 同计数，多一个进场块下限）：
    - 末笔锚点：末笔延伸（创新不利极值）时终点推进、锚点右移、计数归零——但
      延伸仅在周期K收盘提交，入场初段锚点滞留旧端点，会把末笔自身的块计入
      （2026-10-01 XAUUSD：08:27 进场、锚点滞留 02:00，03:00-07:00 下跌块被计入
      =入场拍假就绪，与逆势快速离场同拍抢触发）；
    - 进场块下限：锚点不早于进场时刻所在块——进场前已走出的块不计，进场拍
      计数归零起步，防刚进场即够笔。
    末笔已是确认有利方向（无形成段）时返回 False——走 lastBiOk / find_bi_event
    确认笔口径（先到先触发，halfDone/halfMrDone latch 一次性消费）。
    @param entry_time  进场时刻（秒；缺省 0=不设下限，退化为 forming_seg_ready 口径）
    """
    if not bis or not merged_times:
        return False
    fav = "down" if is_short else "up"
    if bis[-1]["type"] == fav:
        return False  # 末笔已是有利方向 → 无形成段，走确认笔口径
    anchor = bisect.bisect_left(merged_times, bis[-1]["endTime"])
    if entry_time:
        e = bisect.bisect_right(merged_times, entry_time) - 1
        if e > anchor:
            anchor = e
    if anchor >= len(merged_times):
        return False
    return (len(merged_times) - 1) - anchor >= min_merged - 1


def forming_break_ref(bis, is_short):
    """预期过前高/破前底参照（严进宽出 2026-10-09；2026-10-10 补末笔有利方向分路）：
    末笔为不利方向（其后正在走的有利方向形成段尚未确认成笔）时，返回最近一根已
    确认**同向（有利方向）**笔的端点——形成段运行极值越过该端点即视为「过高低点」，
    不等形成段收笔确认。

    末笔已是利好方向时分两路：该笔端点**尚未破**前一同向笔端点（更高低点/更低
    高点）→ 仍返回前一同向笔端点（价格越过即触发——有利笔确认后确认口径 tp3a 须等
    端点越过参照，而端点可能被 gapLocked 冻结〔跨周末跳空〕或延伸滞后，价格实际
    破位后迟迟不触发：2026-09-28 trade#109 实证 08:00 破 4254.58、12:00 才
    4196.81 平仓）；该笔端点**已破**前一同向端点 → 返回 None（破位事件发生在确认
    笔内，tp3a 当拍自会触发，不重复给参照）。
    @returns None | {price, time}（参照笔端点价/完成时间）
    """
    if not bis:
        return None
    fav = "down" if is_short else "up"
    if bis[-1]["type"] == fav:
        for j in range(len(bis) - 2, -1, -1):
            if bis[j]["type"] == fav:
                broke = (bis[-1]["endPrice"] < bis[j]["endPrice"]) if is_short \
                    else (bis[-1]["endPrice"] > bis[j]["endPrice"])
                if broke:
                    return None
                return {"price": bis[j]["endPrice"], "time": bis[j]["endTime"]}
        return None
    for j in range(len(bis) - 1, -1, -1):
        if bis[j]["type"] == fav:
            return {"price": bis[j]["endPrice"], "time": bis[j]["endTime"]}
    return None


def find_bi_event(bis, from_t, bi_type, require_post_start=False, break_prev=False):
    """找 from_t 之后首个完成的指定 type 笔（够笔/破高低点事件源）。

    @param bis               笔列表（按时间升序）
    @param from_t            起始时间（秒，不含等于）
    @param bi_type           "up" | "down"
    @param require_post_start True 时要求 startTime >= from_t（TP3 用：必须是进场后
                             开始的新反向笔，排除进场前已存在的同向笔——进场背驰点
                             本身常是「创新高/新低」笔）
    @param break_prev        True 时再要求端点破前一同向笔端点（up 过前高 / down 破前底）
    @returns None | {time, price, startTime, startPrice}（笔完成时间 endTime、端点价、
             起点时间/起点价——startPrice 供保本位 bi 口径取「有利方向笔起点」，
             2026-10-12 增量键，既有调用者不感知）
    """
    if not bis:
        return None
    # endTime 升序 → 二分跳过 from_t 之前的部分，只从首个 endTime > from_t 起向后找
    for i in range(_bisect_gt_key(bis, "endTime", from_t), len(bis)):
        b = bis[i]
        if b["type"] != bi_type:
            continue
        if require_post_start and b["startTime"] < from_t:
            continue
        if break_prev:
            j = i - 1
            while j >= 0 and bis[j]["type"] != bi_type:
                j -= 1
            if j < 0:
                continue
            broke = (b["endPrice"] > bis[j]["endPrice"]) if bi_type == "up" \
                else (b["endPrice"] < bis[j]["endPrice"])
            if not broke:
                continue
            # 被破的前一同向笔端点（TP3a 出场叙事/日志用；增量键，既有调用者不感知）
            return {"time": b["endTime"], "price": b["endPrice"],
                    "startTime": b["startTime"], "startPrice": b["startPrice"],
                    "refTime": bis[j]["endTime"], "refPrice": bis[j]["endPrice"]}
        return {"time": b["endTime"], "price": b["endPrice"],
                "startTime": b["startTime"], "startPrice": b["startPrice"]}
    return None
