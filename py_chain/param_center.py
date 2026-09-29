# -*- coding: utf-8 -*-
"""参数中心：分模块参数的 schema / 校验 / 持久化。

模块清单（与 web/params.html 页签一一对应，顺序对齐工作台流程；归属两大类见
module_registry——基础公用组件 vs 交易策略，2026-09-29 模块分层）：
  【基础公用组件】
  chan   画笔（成笔相关 CHAN_CFG 子集；内部 id 仍为 chan，避免改 API / 落盘键）
  zs     画中枢（各周期是否绘制、每周期最近保留个数）
  points 标记买卖点（邻近合并/保留数/中枢容差 + divergeDurRatio）
  【交易策略 · 缠论V1（chan_v1；策略页签组，未来新策略在此追加）】
  sr     支阻位（整份 cfg 按品种存于 modules.sr，不在 PARAM_MODULES / 无 schema；
           2026-09-29 起归属策略组——支阻参数按策略经「方案列表单选生效」选定：
           modules.sr.<品种>.preset 标记来源方案名，生效=方案 cfg 复制入品种桶）
  plan   交易计划（震荡判定阈值 RANGE_DEFAULTS + 顺势参考周期 trendRes）
  entry  标记进出场（near/lots/slip_* 与出场门槛 + 进场扩展 CHAN_CFG）

存储 web/module_params.json：
  PARAM_MODULES → **只存与代码默认不同的键（overrides-only）**，按五品种分桶；
  modules.sr → 整份支阻 cfg（调用方已 normalize），按品种分桶。
代码默认值升级后不被旧存量静默覆盖，UI 可据此高亮"已修改"。
默认值单一来源 = 各算法模块常量（CHAN_CFG_DEFAULTS / RANGE_DEFAULTS 等），schema 调用时活取。

生效链路：
  chan/points/entry 中含 CHAN_CFG 键的模块 → 保存后立即 chan_core.apply_cfg(chan_cfg_effective(symbol))
  zs/plan → 调用方（webapp Worker / analysis_service / main）读取
           effective_all(symbol) 后以显式形参传入（BacktestEngine.module_params 等）。
  全部 PARAM_MODULES + sr 按五品种分桶（XAUUSD/XAGUSD/USOIL/BTCUSD/NAS100），任务侧按 symbol 取桶。
"""

import json
import os
import tempfile
import threading
import time

from . import chan_core
from . import mark_buy_sell
from . import mark_entry
from . import trading_plan

# 持久化文件（原子写：tempfile + os.replace，与 webapp._presets_save 同模式）
PARAMS_FILE = os.path.join(os.path.dirname(__file__), "web", "module_params.json")
_lock = threading.Lock()

# 画中枢默认：与历史 CLI 一致，默认只画 1 小时；每周期最近保留个数
ZS_PERIOD_KEYS = (("D", "drawD", "keepD"), ("240", "draw240", "keep240"),
                  ("60", "draw60", "keep60"), ("15", "draw15", "keep15"),
                  ("3", "draw3", "keep3"))
ZS_DEFAULTS = {
    "drawD": False, "draw240": False, "draw60": True, "draw15": False, "draw3": False,
    "keepD": 10, "keep240": 10, "keep60": 10, "keep15": 10, "keep3": 10,
}

# CHAN_CFG 键按工作台步骤归属（算法默认值仍在 chan_core.CHAN_CFG_DEFAULTS）
CHAN_BI_KEYS = (
    "gapFilter", "wickRatio", "wickAtrK", "wideBarPoints",
    "nearDoubleAtrK", "nearDoublePct", "nearDoubleFixed",
    "nearDoubleLowerRelax", "nearDoubleLowerRatio",
    "nearDouble3", "nearDouble15", "nearDouble60", "nearDouble240", "nearDoubleD",
    "synthIntrabarBars", "pointEnoughForming",
    "debug",
)
POINTS_CHAN_KEYS = ("divergeDurRatio", "anchorUndecidedSkip", "anchorUndecidedMinBars")
ENTRY_CHAN_KEYS = (
    "sinkFallback", "sinkFallbackRearm",
    "nearEqualAtrK", "nearEqualPct",
    "expectBiEnough", "expectBiMinBars", "divergeConfirm", "macdZeroTol",
    "entryMacdShrink", "stopEntryBarFloor",
    "divergeReferByZs", "sinkSkipLevel",
)
# 旧 module_params.json 把上述键都写在 modules.chan 下；读入时迁到 points/entry
_LEGACY_CHAN_TO_POINTS = set(POINTS_CHAN_KEYS)
_LEGACY_CHAN_TO_ENTRY = set(ENTRY_CHAN_KEYS)

# 改这些模块会拼合写回 CHAN_CFG，任务运行中禁止保存
CHAN_CFG_MODULES = frozenset(("chan", "points", "entry"))

# schema：label/desc 前端展示用；type/min/max 由默认值类型与下表范围决定（校验用）。
# 未列 min/max 的布尔键只做类型校验。
PARAM_MODULES = {
    "chan": {
        "category": "base",
        "title": "画笔",
        "params": {
            "gapFilter": ("跳空成笔阈值", "相邻K线缺口 ≥ 该值×ATR 时强制独立成笔", 0.0, 5.0),
            "wickRatio": ("长影线比例阈值", "影线占整根K线振幅 ≥ 该比例视为插针（不参与区间竞争）", 0.0, 1.0),
            "wickAtrK": ("长影线长度下限", "影线绝对长度下限 = 该值×TR均值（窄幅小K线免疫）", 0.0, 5.0),
            "wideBarPoints": ("15分钟大振幅豁免",
                              "仅15分钟：单根K线振幅（最高-最低）≥ 该点数时，不参与分型终点侧三根的反向贯穿检查。"
                              "0 表示不豁免", 0.0, 500.0),
            "nearDoubleAtrK": ("近等双顶容差ATR", "近等双顶/双底平台价差容差（×ATR）", 0.0, 5.0),
            "nearDoublePct": ("近等双顶价差比例", "近等双顶/双底价差下限（价格比例）", 0.0, 0.05),
            "nearDoubleFixed": ("近等双顶固定容差",
                                "固定价差容差（品种报价单位绝对价差，如黄金 0.1=0.1 美元）；"
                                "总容差 = max(ATR×容差ATR, 价格×价差比例, 该值)——并集取最大、"
                                "只增不减；0=不启用（默认，与原行为一致）", 0.0, None),
            "nearDoubleLowerRelax": ("60m双动能容差倍数", "仅60m：15m双动能确认时近等容差放宽倍数", 1.0, 10.0),
            "nearDoubleLowerRatio": ("15m动能衰减比例", "15m柱峰值与DIF幅度须衰减至前段该比例以内", 0.0, 1.0),
            "nearDouble3": ("近等双顶·3分钟",
                            "3分钟笔是否启用「近等双顶/双底平台取后顶/后底」（默认关）", None, None),
            "nearDouble15": ("近等双顶·15分钟",
                             "15分钟笔是否启用（默认关；全周期开启曾实测 61 次触发、"
                             "微观结构大面积重排）", None, None),
            "nearDouble60": ("近等双顶·1小时",
                             "1小时笔是否启用（默认开；价差超阈值时可由15m双动能补充确认）", None, None),
            "nearDouble240": ("近等双顶·4小时", "4小时笔是否启用（默认开）", None, None),
            "nearDoubleD": ("近等双顶·日线", "日线笔是否启用（默认开）", None, None),
            "synthIntrabarBars": ("盘中合成K（15m/1h/4h）",
                                  "回测每拍用3分钟已收K聚合15m/1h/4h进行中K（O=bin首开、"
                                  "H/L=运行极值、C=最新收）临时注入链路——高周期结构盘中"
                                  "即见当根K变化（默认关=只认已收K，原行为）", None, None),
            "pointEnoughForming": ("够笔只计成笔可能",
                                   "形成中段承载买卖点的够笔计数只数到极值块——反向确认"
                                   "（首根抬低点/抬高点K）后的K不属于本段不计入（默认关="
                                   "数到当下，原行为）", None, None),
            "debug": ("调试打印", "buildBi/买卖点识别过程打印", None, None),
        },
    },
    "zs": {
        "category": "base",
        "title": "画中枢",
        "params": {
            "drawD": ("日线画中枢", "是否在日线周期绘制中枢矩形", None, None),
            "keepD": ("日线最近个数", "日线只保留时间上最近 N 个中枢（按进入笔终点）", 1, 100),
            "draw240": ("4小时画中枢", "是否在4小时周期绘制中枢矩形", None, None),
            "keep240": ("4小时最近个数", "4小时只保留时间上最近 N 个中枢", 1, 100),
            "draw60": ("1小时画中枢", "是否在1小时周期绘制中枢矩形", None, None),
            "keep60": ("1小时最近个数", "1小时只保留时间上最近 N 个中枢", 1, 100),
            "draw15": ("15分钟画中枢", "是否在15分钟周期绘制中枢矩形", None, None),
            "keep15": ("15分钟最近个数", "15分钟只保留时间上最近 N 个中枢", 1, 100),
            "draw3": ("3分钟画中枢", "是否在3分钟周期绘制中枢矩形", None, None),
            "keep3": ("3分钟最近个数", "3分钟只保留时间上最近 N 个中枢", 1, 100),
        },
    },
    "points": {
        "category": "base",
        "title": "标记买卖点",
        "params": {
            "nearAtrRatio": ("邻近合并阈值(×ATR)", "1买与2买价差 ≤ 该值×ATR 时合并为「真1买」", 0.0, 5.0),
            "keep": ("每周期保留标记数", "每周期买卖点标记总数上限（买+卖合并取最近N）", 1, 100),
            "class2ZsTol": ("类2破中枢容差(点)", "类2买/类2卖允许越过中枢边界的绝对点数（0=严格）", 0.0, 1000.0),
            "thirdZsTol": ("3类进中枢容差(点)", "3买/类3买/4买/类4买/3卖/类3卖/4卖/类4卖允许进入中枢的绝对点数（0=严格）", 0.0, 1000.0),
            "divergeDurRatio": ("背驰时长可比上限", "两段时长比超过该值时面积项不计入背驰判据", 1, 100),
            "anchorUndecidedSkip": ("未定型点不接管",
                                    "2/3类点分类「未定型」（点后反向段不存在/未达根数）时不接管"
                                    "计划/顺势锚点、回退前一个定型点（默认开；关掉则出生即接管）", None, None),
            "anchorUndecidedMinBars": ("定型阈值(合并块)", "点后反向段的本级合并块数达到该值才算定型"
                                       "（1=分型即定型；2=右肩+1根确认，默认；5=与够笔同参）", 1, 100),
        },
    },
    "entry": {
        "category": "strategy",
        "title": "标记进出场",
        "params": {
            "near": ("近支阻阈值", "背驰点价与支阻位价差 ≤ 该值视为接近（绝对价差，不乘ATR）", 0.1, 1000.0),
            "lots": ("手数", "本品种进场手数;盈亏 = 价差 × 方向 × 手数 × 合约乘数(1手=0.01标准手)", 1, 100),
            "slip_stop": ("止损滑点", "正确侧支阻位外侧偏移（绝对价格）；有效值 = 该值 + ATR系数×ATR(14,背驰周期)", 0.1, 100.0),
            "slip_stop_atr_k": ("止损滑点ATR系数", "有效止损滑点 = 止损滑点 + 该值×ATR(14,背驰周期)；0=关闭", 0.0, 10.0),
            "slip_fallback": ("最大止损", "买点止损不低于进场价−该值，卖点不高于进场价+该值；支阻位更远时收到此价；有效值 = 该值 + ATR系数×ATR(14,背驰周期)", 0.1, 1000.0),
            "slip_fallback_atr_k": ("最大止损ATR系数", "有效最大止损 = 最大止损 + 该值×ATR(14,背驰周期)；0=关闭", 0.0, 10.0),
            "slip_be": ("保本滑点", "保本止损位 = 进场成交K线极值±该值；有效值 = 该值 + ATR系数×ATR(14,背驰周期)", 0.1, 100.0),
            "slip_be_atr_k": ("保本滑点ATR系数", "有效保本滑点 = 保本滑点 + 该值×ATR(14,背驰周期)；0=关闭", 0.0, 10.0),
            "exit_min_merged": ("出场成笔预期门槛", "形成段合并后 ≥ 该值根K视为成笔预期（TP2/逆势TP3b触发）", 2, 50),
            "realtime_min_bars": ("当下制够笔K线数", "检测周期形成段从起点所在合并块起达到该块数才评估（含起点；默认5）", 1, 100),
            "zs_exit_weak_ratio": ("出中枢力度衰减比例", "离开笔幅度 < 进入笔幅度×该值 视为力度变弱（wait1买/卖条件）", 0.1, 5.0),
            "sinkFallback": ("M1下沉链回退", "下沉停止级无候选时沿链向上一级重评", None, None),
            "sinkFallbackRearm": ("M1终局后重置去重", "同向持仓终局后重置段去重（同段可再进一次）", None, None),
            "nearEqualAtrK": ("M2近等容差ATR", "创新低/新高近等容差（×ATR；0=关闭，实测负贡献）", 0.0, 5.0),
            "nearEqualPct": ("M2近等容差比例", "近等容差价格比例（0=关闭）", 0.0, 0.05),
            "expectBiEnough": ("M4预期够笔", "允许预期段在合并K线够笔后进场；结构预期本身在普通分型确认后立即参与", None, None),
            "expectBiMinBars": ("M4够笔K线数", "预期够笔的本级合并K线块数门槛（含起点所在块）", 1, 100),
            "divergeConfirm": ("M4背驰确认后成交", "开启=分型右邻K收盘后的下一根开盘成交（默认当下）", None, None),
            "entryMacdShrink": ("进场MACD柱缩闸", "开启=背驰级别上一根已收K线柱状体(|MACD|)较前一根缩小才出信号（确认背驰且柱缩，下一根开盘进场）", None, None),
            "stopEntryBarFloor": ("进场K线止损下限", "开启=止损至少在进场背驰周期K线极值外侧加滑点处（运行中外推、收盘冻结，与支阻位止损取更宽者）", None, None),
            "macdZeroTol": ("2买卖0轴容差", "2买 DIF > -该值 / 2卖 DIF < +该值 视为动能还在（0=严格 0 轴）", 0.0, 100.0),
            "divergeReferByZs": ("背驰参照按中枢",
                                 "参照笔=入中枢段——跳过当前段之前紧邻中枢内部/之后的同向笔"
                                 "（背驰=入中枢段vs出中枢段，中枢内部振荡段不参与比较；"
                                 "默认关=紧邻前一同向笔，原行为）", None, None),
            "sinkSkipLevel": ("跨级下沉",
                              "下沉链次级展开<3笔时跳过该级继续向下找有展开的级别判背驰"
                              "（如60→3直沉，markRes=3；默认关=在本级判定，原行为）", None, None),
        },
    },
    "plan": {
        "category": "strategy",
        "title": "交易计划",
        "params": {
            "rangeBoundOn": ("①横盘判定", "参考周期横盘整理判定（K线窄带+笔端点+涨跌交替）；关闭后跳过此类", None, None),
            "rangeBarN": ("震荡窗口K线数", "最近N根K线的区间判定窗口", 5, 500),
            "rangeBiN": ("震荡最近笔数", "最近N笔端点极差与涨跌交替判定", 2, 50),
            "rangeKMult": ("K线区间阈值(×ATR)", "窗口高低差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBiMult": ("笔端点阈值(×ATR)", "笔端点极差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBreakMult": ("突破跳过阈值(×ATR)", "末笔端点越过窗口另一端 >该值×ATR 视为突破（跳过震荡判定）", 0.0, 10.0),
            "rangeZsOn": ("②中枢内判定", "参考周期未离开中枢且现价在箱内则判震荡；关闭后跳过此类", None, None),
            "trendRes": ("顺势参考周期", "参考周期方向过滤更低周期进场（关闭/4小时/日线；规则见本页下方说明）",
                         ("", "240", "D"), None),
            "rangeRes": ("震荡判定参考周期", "更低周期只看该周期结构判震荡；参考周期及以上只作锚不交易（必填：4小时/日线）",
                         ("240", "D"), None),
            "trendRebound": ("方向相位判定", "锚点（1卖/2\\3卖及买侧镜像）确立后按 够笔+中枢边界/前低前高+角度强弱 分相位定方向，含观望态（闩锁优先；仅回测/监控引擎，JS技能无此参数）",
                         None, None),
            "reboundNearPts": ("相位近位容差(点)", "形成段极值距中枢上/下沿或前低/前高 ≤ 该值（绝对点数）视为附近（调大等效常判附近）",
                         0.0, 1000.0),
            "reboundAngleRef": ("相位角度45°基准(点/根)", "当前笔平均每根幅度 > 该值 = 角度>45°（强），≤ 为弱；下跌角度与反弹/回调力度同口径",
                         0.1, 100.0),
            "prevHighNearPts": ("前高/前低附近容差(点)", "2买/2卖后首段上涨（下跌）终点距前高（前低）≤ 该值（绝对点数）视为附近 → 等回调后的类2买点/类2卖点",
                         0.0, 1000.0),
            "secondNearPts": ("回踩2买/2卖点容差(点)", "未过前高（前低）时，最近一笔回调（反弹）终点距2买（2卖）点价 ≤ 该值 视为回踩到位 → 等回调后的类2买点/类2卖点",
                         0.0, 1000.0),
            "thirdStrongTrend": ("③3类点强档顺势", "开=3买/类3买/4买/类4买（3卖/类3卖/4卖/类4卖）过前高不背驰时顺势「等待回调后的新买点/新卖点」（默认现状）；关=这些点一律弱档（多头空/空头多，等一卖/一买）",
                         None, None),
        },
    },
}


def _chan_defaults_subset(keys):
    """从 CHAN_CFG_DEFAULTS 按键序取子集。"""
    src = chan_core.CHAN_CFG_DEFAULTS
    return {k: src[k] for k in keys}


def defaults_of(module):
    """模块默认值（活取各算法模块常量，保证与代码默认不漂移）。"""
    if module == "chan":
        return _chan_defaults_subset(CHAN_BI_KEYS)
    if module == "points":
        return {
            "nearAtrRatio": mark_buy_sell.NEAR_ATR_RATIO,
            "keep": mark_buy_sell.KEEP,
            "class2ZsTol": mark_buy_sell.CLASS2_ZS_TOL,
            "thirdZsTol": mark_buy_sell.THIRD_ZS_TOL,
            **_chan_defaults_subset(POINTS_CHAN_KEYS),
        }
    if module == "zs":
        return dict(ZS_DEFAULTS)
    if module == "entry":
        return {
            "near": mark_entry.NEAR,
            "lots": mark_entry.DEFAULT_LOTS,
            "slip_stop": mark_entry.DEFAULT_SLIP_STOP,
            "slip_stop_atr_k": mark_entry.DEFAULT_SLIP_STOP_ATR_K,
            "slip_fallback": mark_entry.DEFAULT_SLIP_FALLBACK,
            "slip_fallback_atr_k": mark_entry.DEFAULT_SLIP_FALLBACK_ATR_K,
            "slip_be": mark_entry.DEFAULT_SLIP_BE,
            "slip_be_atr_k": mark_entry.DEFAULT_SLIP_BE_ATR_K,
            "exit_min_merged": mark_entry.EXIT_MIN_MERGED,
            "realtime_min_bars": mark_entry.REALTIME_MIN_BARS,
            "zs_exit_weak_ratio": mark_entry.ZS_EXIT_WEAK_RATIO,
            **_chan_defaults_subset(ENTRY_CHAN_KEYS),
        }
    if module == "plan":
        return {**trading_plan.RANGE_DEFAULTS, "trendRes": trading_plan.TREND_RES,
                "rangeRes": trading_plan.RANGE_RES,
                "trendRebound": trading_plan.TREND_REBOUND,
                "reboundNearPts": trading_plan.REBOUND_NEAR_PTS,
                "reboundAngleRef": trading_plan.REBOUND_ANGLE_REF,
                "prevHighNearPts": trading_plan.PREV_HIGH_NEAR_PTS,
                "secondNearPts": trading_plan.SECOND_NEAR_PTS,
                "thirdStrongTrend": trading_plan.THIRD_STRONG_TREND}
    raise ValueError(f"未知参数模块：{module}")


# 五品种分桶（2026-09-24）：与参数页子页签 / 回测多选品种一致；全 PARAM_MODULES + sr 共用
ENTRY_SYMBOLS = ("XAUUSD", "XAGUSD", "USOIL", "BTCUSD", "NAS100")
ENTRY_SYMBOL_META = (
    ("XAUUSD", "黄金", "OANDA:XAUUSD"),
    ("XAGUSD", "白银", "OANDA:XAGUSD"),
    ("USOIL", "原油", "TVC:USOIL"),
    ("BTCUSD", "BTC", "BITSTAMP:BTCUSD"),
    ("NAS100", "纳指", "FX:NAS100"),
)
SYMBOLS = ENTRY_SYMBOLS
SYMBOL_META = ENTRY_SYMBOL_META

# 旧扁平 lots_* 键 → 品种桶（读入时迁到该品种的 lots）
_LEGACY_LOT_KEYS = {
    "lots_xauusd": "XAUUSD",
    "lots_xagusd": "XAGUSD",
    "lots_usoil": "USOIL",
    "lots_btcusd": "BTCUSD",
    "lots_nas100": "NAS100",
}

# 支阻位：整份 cfg 按品种存；缺省走引擎（只写 periods/srTypes）
SR_PRESETS_FILE = os.path.join(os.path.dirname(__file__), "web", "sr_presets.json")
SR_DEFAULTS = {
    "srTypes": ["cluster", "boll"],
    "periods": ["D", "240", "60", "15", "3"],
}
# 运行时键不落盘（品种/时间窗由任务侧决定；as_of_ts=时点截断界，同属查看态）
# lookbackBars（向前K线根数，密集区限窗）不在此列：随品种桶落盘，但引擎路径经
# engine_kwargs_of 显式枚举映射，永不进 compute_srflip → 回测/live 语义不变
_SR_RUNTIME_KEYS = frozenset((
    "symbol", "symbols", "from", "from_ts", "as_of_ts", "to", "to_ts", "start_ts",
    "name", "saved_at",
))


def entry_symbol_id(symbol):
    """解析品种桶 id。空/未传 → 黄金页（XAUUSD）；未列入五品种 → None（走代码默认）。"""
    if not symbol:
        return "XAUUSD"
    suf = mark_entry.symbol_suffix(symbol)
    return suf if suf in SYMBOLS else None


symbol_id = entry_symbol_id  # 别名：全模块按品种取桶


def lots_of(ep, symbol):
    """按品种取进场手数。ep 为 effective("entry", symbol) / effective_all(symbol)["entry"]。"""
    return ep.get("lots", mark_entry.DEFAULT_LOTS)


def engine_module_params(pm):
    """effective_all(symbol) → BacktestEngine.module_params 映射（webapp 三模式 Worker 与
    实盘 live_trader 共用；2026-09-23 自 webapp._engine_module_params 上移，避免复制漂移）。
    调用方须先按品种解析 pm（各模块已是该品种桶）。"""
    ep = pm["entry"]
    return {"plan": pm["plan"], "marks": pm["points"],
            "trendRes": pm["plan"]["trendRes"],
            "exit_min_merged": ep["exit_min_merged"],
            "realtime_min_bars": ep["realtime_min_bars"],
            "zs_exit_weak_ratio": ep["zs_exit_weak_ratio"]}


def zs_draw_spec(zs_cfg=None):
    """从画中枢参数得到 CLI 用周期列表与每周期 keep。
    @returns (periods:list[str], keep_by_res:dict[str,int])
    """
    cfg = zs_cfg if zs_cfg is not None else effective("zs")
    periods = []
    keep_by_res = {}
    for res, draw_key, keep_key in ZS_PERIOD_KEYS:
        if cfg.get(draw_key):
            periods.append(res)
            keep_by_res[res] = max(1, int(cfg.get(keep_key) or 1))
    return periods, keep_by_res


def _type_of(default):
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, str):
        return "str"
    return "float"


def schema_of(module):
    """前端渲染用 schema：每键 label/desc/type/min/max/default（str 枚举另带 choices）。"""
    defaults = defaults_of(module)
    spec = PARAM_MODULES[module]["params"]
    out = {}
    for key, meta in spec.items():
        typ = _type_of(defaults[key])
        item = {"label": meta[0], "desc": meta[1], "type": typ,
                "min": None if typ == "str" else meta[2],
                "max": None if typ == "str" else meta[3],
                "default": defaults[key]}
        if typ == "str":
            item["choices"] = list(meta[2])  # spec 第3位 = 允许值列表（枚举）
        out[key] = item
    return out


def normalize(module, cfg):
    """校验并规范化提交值（类型转换 + 范围检查，非法直接 raise ValueError）。
    @returns 只含本模块合法键的规范化 dict（未知键报错，防拼写错误静默丢失）。
    """
    spec = PARAM_MODULES.get(module)
    if spec is None:
        raise ValueError(f"未知参数模块：{module}")
    defaults = defaults_of(module)
    out = {}
    for key, value in (cfg or {}).items():
        if key not in spec["params"]:
            raise ValueError(f"未知参数：{module}.{key}")
        dv = defaults[key]
        typ = _type_of(dv)
        if typ == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} 须为布尔值")
            out[key] = value
            continue
        if typ == "str":
            choices = PARAM_MODULES[module]["params"][key][2]
            if not isinstance(value, str) or value not in choices:
                raise ValueError(f"{key} 须为 {'/'.join(choices)} 之一")
            out[key] = value
            continue
        if isinstance(value, bool):
            raise ValueError(f"{key} 须为{'整数' if typ == 'int' else '数值'}")
        lo, hi = spec["params"][key][2], spec["params"][key][3]
        try:
            if typ == "int":
                if float(value) != int(float(value)):
                    raise ValueError
                value = int(float(value))
            else:
                value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 须为{'整数' if typ == 'int' else '数值'}")
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ValueError(f"{key} 须在 {lo} ~ {hi} 之间")
        out[key] = value
    return out


def _accept_override(defaults, k, v):
    """类型匹配时收下 override（bool 与 int 严格区分）。"""
    if k not in defaults:
        return False
    dv = defaults[k]
    if isinstance(dv, bool):
        return isinstance(v, bool)
    if isinstance(dv, int) and not isinstance(dv, bool):
        return isinstance(v, int) and not isinstance(v, bool)
    return isinstance(v, type(dv))


def _is_symbol_store(mod):
    """是否已是/像品种桶：任一品种键值为 dict，或非空键集 ⊆ 五品种。"""
    if not isinstance(mod, dict) or not mod:
        return False
    if any(k in SYMBOLS and isinstance(mod.get(k), dict) for k in SYMBOLS):
        return True
    return all(k in SYMBOLS for k in mod)


def _clean_module_bucket(module, raw):
    """清洗单个品种桶：只留 schema 内且类型匹配的键。"""
    defaults = defaults_of(module)
    return {k: v for k, v in (raw or {}).items() if _accept_override(defaults, k, v)}


def _migrate_symbol_store(module, mod):
    """chan/zs/points/plan：旧扁平 overrides → 五品种各一份；已是品种桶则按桶清洗。"""
    if not isinstance(mod, dict):
        return {}
    if _is_symbol_store(mod):
        out = {}
        for sid in SYMBOLS:
            raw = mod.get(sid) if isinstance(mod.get(sid), dict) else {}
            bucket = _clean_module_bucket(module, raw)
            if bucket:
                out[sid] = bucket
        return out
    defaults = defaults_of(module)
    shared = {k: v for k, v in mod.items() if _accept_override(defaults, k, v)}
    if not shared:
        return {}
    return {sid: dict(shared) for sid in SYMBOLS}


def _clean_entry_bucket(raw):
    """清洗单个进出场品种桶。"""
    return _clean_module_bucket("entry", raw)


def _migrate_entry_store(mod):
    """旧扁平 entry → {品种: overrides}；新格式按五品种清洗。
    旧 lots_* 写入对应品种 lots；旧「默认手数 lots」不迁入五品种。
    旧全局键（near/滑点/CHAN_CFG 等）复制到五个品种桶（原先全品种共用）。"""
    if not isinstance(mod, dict):
        return {}
    if _is_symbol_store(mod):
        out = {}
        for sid in SYMBOLS:
            bucket = _clean_entry_bucket(mod.get(sid) if isinstance(mod.get(sid), dict) else {})
            if bucket:
                out[sid] = bucket
        return out
    defaults = defaults_of("entry")
    shared, lots_by = {}, {}
    for k, v in mod.items():
        if k in _LEGACY_LOT_KEYS:
            if _accept_override(defaults, "lots", v):
                lots_by[_LEGACY_LOT_KEYS[k]] = v
        elif k == "lots":
            continue
        elif _accept_override(defaults, k, v):
            shared[k] = v
    out = {}
    for sid in SYMBOLS:
        bucket = dict(shared)
        if sid in lots_by:
            bucket["lots"] = lots_by[sid]
        bucket = _clean_entry_bucket(bucket)
        if bucket:
            out[sid] = bucket
    return out


def _strip_sr_runtime(cfg):
    """剔除品种/时间窗等运行时键，保留算法键。"""
    return {k: v for k, v in (cfg or {}).items() if k not in _SR_RUNTIME_KEYS}


def _seed_sr_from_presets():
    """无 modules.sr 时：读 sr_presets.json 第一份预设 cfg，复制到五品种；损坏则用 SR_DEFAULTS。"""
    try:
        with open(SR_PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            cfg = data[0].get("cfg")
            if isinstance(cfg, dict):
                cleaned = _strip_sr_runtime(cfg)
                if cleaned:
                    return {sid: dict(cleaned) for sid in SYMBOLS}
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        pass
    return {sid: dict(SR_DEFAULTS) for sid in SYMBOLS}


def _migrate_sr_store(mod):
    """modules.sr：已是品种桶则清洗；若是旧扁平整份 cfg 则复制到五品种。"""
    if not isinstance(mod, dict):
        return {}
    if _is_symbol_store(mod):
        out = {}
        for sid in SYMBOLS:
            raw = mod.get(sid)
            if isinstance(raw, dict):
                cleaned = _strip_sr_runtime(raw)
                if cleaned:
                    out[sid] = cleaned
        return out
    cleaned = _strip_sr_runtime(mod)
    if not cleaned:
        return {}
    return {sid: dict(cleaned) for sid in SYMBOLS}


def _load():
    """读取持久化（结构/未知键损坏时静默降级为空，不阻塞启动）。
    迁移：PARAM_MODULES 按品种分桶；旧 chan 已迁出键 → points/entry 各品种桶；
    modules.sr 缺失时从 sr_presets.json 第一份预设播种。
    """
    try:
        with open(PARAMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    modules = data.get("modules") if isinstance(data, dict) else None
    if not isinstance(modules, dict):
        return {}
    out = {}
    for name in PARAM_MODULES:
        mod = modules.get(name)
        if not isinstance(mod, dict):
            continue
        if name == "entry":
            out[name] = _migrate_entry_store(mod)
        else:
            out[name] = _migrate_symbol_store(name, mod)
    # 旧 chan 桶：已迁出键 → points / entry 各品种桶（目标已有显式 override 时保留目标）
    legacy = modules.get("chan")
    if isinstance(legacy, dict):
        defaults_p = defaults_of("points")
        by_p = out.setdefault("points", {})
        for sid in SYMBOLS:
            bucket = by_p.setdefault(sid, {})
            for k in _LEGACY_CHAN_TO_POINTS:
                if k in bucket:
                    continue
                v = legacy.get(k)
                if _accept_override(defaults_p, k, v):
                    bucket[k] = v
            if not bucket:
                by_p.pop(sid, None)
        defaults_e = defaults_of("entry")
        by_e = out.setdefault("entry", {})
        for sid in SYMBOLS:
            bucket = by_e.setdefault(sid, {})
            for k in _LEGACY_CHAN_TO_ENTRY:
                if k in bucket:
                    continue
                v = legacy.get(k)
                if _accept_override(defaults_e, k, v):
                    bucket[k] = v
            if not bucket:
                by_e.pop(sid, None)
    # 支阻位：缺失则从预设播种
    raw_sr = modules.get("sr")
    if not isinstance(raw_sr, dict):
        out["sr"] = _seed_sr_from_presets()
    else:
        out["sr"] = _migrate_sr_store(raw_sr)
    return out


def _save(overrides, saved_at):
    """原子写盘：PARAM_MODULES 与 modules.sr 一并写出。"""
    modules = {name: overrides.get(name, {}) for name in PARAM_MODULES}
    modules["sr"] = overrides.get("sr", {})
    payload = {"version": 1, "modules": modules, "savedAt": saved_at}
    os.makedirs(os.path.dirname(PARAMS_FILE), exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(PARAMS_FILE),
                                         suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp_path, PARAMS_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def _symbol_overrides(overrides, module, symbol):
    """取出某模块某品种的 override 桶；未列品种返回空（走代码默认）。"""
    sid = symbol_id(symbol)
    if sid is None:
        return {}
    by = overrides.get(module) or {}
    ov = by.get(sid)
    return ov if isinstance(ov, dict) else {}


def effective(module, symbol=None):
    """默认值 ∪ overrides（键序稳定：按 schema 顺序）。
    全部 PARAM_MODULES 按品种取桶：symbol 空=黄金页；未列品种=纯默认。"""
    defaults = defaults_of(module)
    ov = _symbol_overrides(_load(), module, symbol)
    return {k: ov.get(k, defaults[k]) for k in PARAM_MODULES[module]["params"]}


def effective_all(symbol=None):
    """全模块有效值（同一 symbol）。"""
    return {name: effective(name, symbol) for name in PARAM_MODULES}


def chan_cfg_effective(symbol=None):
    """从画笔 / 标记买卖点 / 标记进出场拼出完整 CHAN_CFG（供 apply_cfg / --chan-cfg）。
    chan/points/entry 均取该品种桶（空=黄金页）。"""
    cfg = dict(chan_core.CHAN_CFG_DEFAULTS)
    overrides = _load()
    for keys, mod in ((CHAN_BI_KEYS, "chan"), (POINTS_CHAN_KEYS, "points"),
                      (ENTRY_CHAN_KEYS, "entry")):
        ov = _symbol_overrides(overrides, mod, symbol)
        for k in keys:
            if k in ov:
                cfg[k] = ov[k]
    return cfg


def effective_sr(symbol=None):
    """支阻位有效 cfg：有存量返回整份；空 symbol=黄金页；未列品种 / 无存量 → SR_DEFAULTS。

    品种桶内的 preset 键（生效方案标记，set_sr_preset 写入）为溯源元数据，
    不属于算法参数——返回前剔除，运行时消费方（分析 configure / 回测 kwargs）零感知。"""
    sid = symbol_id(symbol)
    if sid is None:
        return dict(SR_DEFAULTS)
    ov = (_load().get("sr") or {}).get(sid)
    if isinstance(ov, dict) and ov:
        out = dict(ov)
        out.pop("preset", None)
        return out
    return dict(SR_DEFAULTS)


def _find_sr_preset(name):
    """按名查全局支阻方案（web/sr_presets.json），返回 cfg dict；无/损坏 → None。"""
    try:
        with open(SR_PRESETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for p in data:
                if isinstance(p, dict) and p.get("name") == name:
                    cfg = p.get("cfg")
                    return cfg if isinstance(cfg, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def set_sr_preset(symbol, name):
    """选定/取消该品种的生效支阻方案（单选；复制语义，2026-09-29）。

    name 非空：方案 cfg（剥离运行时键）复制入品种桶并标记来源名——该方案即此品种
    策略的生效支阻参数（方案后续改动不自动同步，重新生效即再复制）；
    方案不存在 → ValueError。name 空/None：仅清除标记，桶参数保留已复制内容。
    @returns effective_sr(symbol)
    """
    sid = _require_symbol(symbol)
    name = str(name or "").strip()
    with _lock:
        overrides = _load()
        saved_at = _read_saved_at()
        by = dict(overrides.get("sr") or {})
        cur = dict(by.get(sid) or {}) if isinstance(by.get(sid), dict) else {}
        if name:
            preset_cfg = _find_sr_preset(name)
            if preset_cfg is None:
                raise ValueError(f"支阻方案「{name}」不存在（已删除或改名）")
            clean = _strip_sr_runtime(preset_cfg)
            if not clean:
                raise ValueError(f"支阻方案「{name}」无有效参数")
            clean["preset"] = name
            by[sid] = clean
        else:
            cur.pop("preset", None)
            if cur:
                by[sid] = cur
            else:
                by.pop(sid, None)
        overrides["sr"] = by
        mod_at = saved_at.get("sr")
        if not isinstance(mod_at, dict):
            mod_at = {}
        mod_at[sid] = time.time()
        saved_at["sr"] = mod_at
        _save(overrides, saved_at)
    return effective_sr(symbol)


def _snapshot_module(name, spec, overrides, saved_at_all):
    """单个 PARAM_MODULES 的 snapshot 块（含 bySymbol / symbols）。"""
    defaults = defaults_of(name)
    by = overrides.get(name) or {}
    mod_at = saved_at_all.get(name)
    if not isinstance(mod_at, dict):
        mod_at = {}
    by_symbol = {}
    for sid, label, code in SYMBOL_META:
        ov = by.get(sid) or {}
        by_symbol[sid] = {
            "label": label, "code": code, "overrides": ov,
            "effective": {k: ov.get(k, defaults[k]) for k in spec["params"]},
            "savedAt": mod_at.get(sid),
        }
    gold = by_symbol["XAUUSD"]
    return {
        "title": spec["title"], "category": spec.get("category", "base"),
        "schema": schema_of(name),
        "defaults": defaults, "bySymbol": by_symbol,
        "symbols": [{"id": s, "label": l, "code": c} for s, l, c in SYMBOL_META],
        "overrides": gold["overrides"], "effective": gold["effective"],
        "savedAt": gold["savedAt"],
    }


def _snapshot_sr(overrides, saved_at_all):
    """modules.sr 的 snapshot：同形 bySymbol；effective=完整 cfg；schema=null。

    归属交易策略组（2026-09-29 起）；preset=该品种生效方案名（UI 回显，无则 null）。"""
    by = overrides.get("sr") or {}
    mod_at = saved_at_all.get("sr")
    if not isinstance(mod_at, dict):
        mod_at = {}
    by_symbol = {}
    for sid, label, code in SYMBOL_META:
        ov = by.get(sid) if isinstance(by.get(sid), dict) else {}
        eff = dict(ov) if ov else dict(SR_DEFAULTS)
        by_symbol[sid] = {
            "label": label, "code": code, "overrides": ov,
            "effective": eff, "savedAt": mod_at.get(sid),
            "preset": ov.get("preset") or None,
        }
    gold = by_symbol["XAUUSD"]
    return {
        "title": "支阻位", "category": "strategy", "schema": None,
        "defaults": dict(SR_DEFAULTS), "bySymbol": by_symbol,
        "symbols": [{"id": s, "label": l, "code": c} for s, l, c in SYMBOL_META],
        "overrides": gold["overrides"], "effective": gold["effective"],
        "preset": gold["preset"],
        "savedAt": gold["savedAt"],
    }


def snapshot():
    """GET /api/params 响应体：每 PARAM_MODULES + sr 均带 bySymbol / symbols。"""
    overrides = _load()
    saved_at_all = _read_saved_at()
    out = {}
    for name, spec in PARAM_MODULES.items():
        out[name] = _snapshot_module(name, spec, overrides, saved_at_all)
    out["sr"] = _snapshot_sr(overrides, saved_at_all)
    return out


def _read_saved_at():
    try:
        with open(PARAMS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("savedAt") or {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _require_symbol(symbol):
    """解析并校验品种；未知则 raise。"""
    sid = symbol_id(symbol)
    if sid is None:
        raise ValueError("未知品种，参数仅支持黄金/白银/原油/BTC/纳指")
    return sid


def update(module, cfg, symbol=None):
    """保存模块参数：规范化 → 合并现有 overrides → 剔除等于默认值的键 → 原子写盘。
    全部模块按品种写入对应桶（symbol 空=黄金页；未知品种 raise）。
    含 CHAN_CFG 键的模块写盘后立即 apply_cfg(chan_cfg_effective(symbol))。
    @returns effective(module, symbol)
    """
    clean = normalize(module, cfg)
    with _lock:
        overrides = _load()
        defaults = defaults_of(module)
        saved_at = _read_saved_at()
        sid = _require_symbol(symbol)
        merged = dict((overrides.get(module) or {}).get(sid, {}))
        merged.update(clean)
        merged = {k: v for k, v in merged.items()
                  if k in defaults and v != defaults[k]}
        by = dict(overrides.get(module) or {})
        if merged:
            by[sid] = merged
        else:
            by.pop(sid, None)
        overrides[module] = by
        mod_at = saved_at.get(module)
        if not isinstance(mod_at, dict):
            mod_at = {}
        mod_at[sid] = time.time()
        saved_at[module] = mod_at
        _save(overrides, saved_at)
    if module in CHAN_CFG_MODULES:
        chan_core.apply_cfg(chan_cfg_effective(symbol))
    return effective(module, symbol)


def reset(module, symbol=None):
    """恢复模块默认：含 CHAN_CFG 键的模块重放拼合配置。
    有 symbol：只清该品种桶；无 symbol：清整模块全部品种。"""
    if module not in PARAM_MODULES:
        raise ValueError(f"未知参数模块：{module}")
    with _lock:
        overrides = _load()
        saved_at = _read_saved_at()
        if symbol:
            sid = _require_symbol(symbol)
            by = dict(overrides.get(module) or {})
            by.pop(sid, None)
            overrides[module] = by
            mod_at = saved_at.get(module)
            if isinstance(mod_at, dict):
                mod_at.pop(sid, None)
                saved_at[module] = mod_at
        else:
            overrides.pop(module, None)
            saved_at.pop(module, None)
        _save(overrides, saved_at)
    if module in CHAN_CFG_MODULES:
        chan_core.apply_cfg(chan_cfg_effective(symbol))
    return effective(module, symbol)


def update_sr(symbol, cfg):
    """保存某品种整份支阻 cfg（调用方已 normalize）；剔除运行时键后落盘。
    编辑器表单不含 preset 键——保留品种已选的生效方案标记（set_sr_preset 写入）。
    @returns effective_sr(symbol)
    """
    sid = _require_symbol(symbol)
    if not isinstance(cfg, dict):
        raise ValueError("cfg 须为 dict")
    clean = _strip_sr_runtime(cfg)
    with _lock:
        overrides = _load()
        saved_at = _read_saved_at()
        by = dict(overrides.get("sr") or {})
        old = by.get(sid) if isinstance(by.get(sid), dict) else {}
        if clean:
            if old.get("preset") and "preset" not in clean:
                clean["preset"] = old["preset"]
            by[sid] = clean
        else:
            by.pop(sid, None)
        overrides["sr"] = by
        mod_at = saved_at.get("sr")
        if not isinstance(mod_at, dict):
            mod_at = {}
        mod_at[sid] = time.time()
        saved_at["sr"] = mod_at
        _save(overrides, saved_at)
    return effective_sr(symbol)


def reset_sr(symbol=None):
    """恢复支阻默认：有 symbol 只清该品种；无 symbol 清整模块。
    @returns effective_sr(symbol)
    """
    with _lock:
        overrides = _load()
        saved_at = _read_saved_at()
        if symbol:
            sid = _require_symbol(symbol)
            by = dict(overrides.get("sr") or {})
            by.pop(sid, None)
            overrides["sr"] = by
            mod_at = saved_at.get("sr")
            if isinstance(mod_at, dict):
                mod_at.pop(sid, None)
                saved_at["sr"] = mod_at
        else:
            overrides.pop("sr", None)
            saved_at.pop("sr", None)
        _save(overrides, saved_at)
    return effective_sr(symbol)
