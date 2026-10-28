# -*- coding: utf-8 -*-
"""参数中心：分模块参数的 schema / 校验 / 持久化。

模块清单（与 web/params.html 页签一一对应，顺序对齐工作台流程；归属两大类见
module_registry——基础公用组件 vs 交易策略，2026-09-29 模块分层）：
  【基础公用组件】
  chan   画笔（成笔相关 CHAN_CFG 子集 + 小周期绘制窗口 windowDays*；内部 id 仍为 chan，
         避免改 API / 落盘键）
  zs     画中枢（各周期是否绘制、每周期最近保留个数）
  points 标记买卖点（邻近合并/保留数/中枢容差 + divergeDurRatio）
  【交易策略 · 缠论V1（chan_v1；策略页签组，未来新策略在此追加）】
  sr     支阻位（整份 cfg 按品种存于 modules.sr，不在 PARAM_MODULES / 无 schema；
           2026-09-29 起归属策略组——支阻参数按策略经「方案列表单选生效」选定：
           modules.sr.<品种>.preset 标记来源方案名，生效=方案 cfg 复制入品种桶）
  plan   交易计划（震荡判定阈值 RANGE_DEFAULTS + 顺势参考周期 trendRes）
  entry  标记进出场（near/lots/slip_* 与出场门槛 + 进场扩展 CHAN_CFG）
  【交易策略 · 强分型均线V1（fxma_v1；引擎 fx_ma.FxMaEngine，与缠论V1并行）】
  fxma   强分型均线V1（进出场周期/买卖点类别多选 + 强分型/均线分离条件开关 + 均线对 +
         强分型落差 + 点有效期 + 止盈止损点数 + 互斥范围；spec 第4位 "multi" = 多选逗号串类型）

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
from .fx_ma import FXMA_DEFAULTS, ENTRY_RES_OPTIONS as FXMA_RES_OPTIONS

# 持久化文件（原子写：tempfile + os.replace，与 webapp._presets_save 同模式）
PARAMS_FILE = os.path.join(os.path.dirname(__file__), "web", "module_params.json")
# 模块加载时实际使用的参数存储路径（导出隔离守卫：测试把 PARAMS_FILE 重定向到
# 临时存储时不写真实导出目录，防止临时桶覆盖画笔读取的 chan_cfg_<品种>.json）
_DEFAULT_PARAMS_FILE = os.path.abspath(PARAMS_FILE)
_lock = threading.Lock()

# 画中枢默认：2026-10-10 把当时五品种已保存的值收成全局默认（只画 4 小时，最近 5 个）
ZS_PERIOD_KEYS = (("D", "drawD", "keepD"), ("240", "draw240", "keep240"),
                  ("60", "draw60", "keep60"), ("15", "draw15", "keep15"),
                  ("3", "draw3", "keep3"))
ZS_DEFAULTS = {
    "drawD": False, "draw240": True, "draw60": False, "draw15": False, "draw3": False,
    "keepD": 10, "keep240": 5, "keep60": 5, "keep15": 10, "keep3": 10,
}

# CHAN_CFG 键按工作台步骤归属（算法默认值仍在 chan_core.CHAN_CFG_DEFAULTS）
CHAN_BI_KEYS = (
    "gapFilter", "wickMarkOn", "wickRatio", "wickMinLen", "wickMinRange", "fractalSideRealWick",
    "endInnerRecoverOn",
    "innerExtremeGuardOn",
    "wideBarOn",
    "wideBarPointsD", "wideBarPoints240", "wideBarPoints60", "wideBarPoints15", "wideBarPoints3",
    "nearDoubleFixed",
    "nearDouble3", "nearDouble15", "nearDouble60", "nearDouble240", "nearDoubleD",
    "nearDoubleShiftLocked",
    "absorbMarkFast",
    "synthIntrabarBars", "pointEnoughForming",
    "windowDays3", "windowDays15", "windowDays30S",
)
# secondDivergeGate 已随 2026-10-14 规则v2 移除（背驰不再参与买卖点判定，仅收笔用）；
# class2ZsTol 已停用（类2买/类2卖「同一个中枢」改按时间归属，find* 签名保留兼容）
POINTS_CHAN_KEYS = ("divergeDurRatio",)
ENTRY_CHAN_KEYS = (
    "sinkFallback", "sinkFallbackRearm", "pointMaxAttempts", "pointMaxAttemptsSeg",
    "macdDivergeBodyOn", "macdDivergeTol",
    "expectBiEnough", "expectBiMinBars", "divergeConfirm", "macdZeroTol",
    "strong2BuyMacdOn", "pointClasses",
    "entryMacdShrink", "stopEntryBarFloor",
    "anchorWinBars", "anchorWinPts", "anchorWinTrailOn",
    "divergeReferByZs", "sinkSkipLevel", "srSideGateOn",
    "divergeSlopeOn", "divergeAreaShrinkOn", "divergeAreaShrinkPct",
)
# pointClasses 旧值别名（2026-10-24 同日三次口径：三类/四类族也拆强弱 3s/3w/…/4xw；
# 二次口径曾拆二类/类二 2s/2w/2xs/2xw）。normalize 保存时迁移；mark_entry 运行期
# 也认旧值（已导出 chan_cfg json/回测快照零迁移）。
POINT_CLASSES_LEGACY = {"2": ("2s", "2w"), "2x": ("2xs", "2xw"),
                        "3": ("3s", "3w"), "3x": ("3xs", "3xw"),
                        "4": ("4s", "4w"), "4x": ("4xs", "4xw")}
# 旧 module_params.json 把上述键都写在 modules.chan 下；读入时迁到 points/entry
_LEGACY_CHAN_TO_POINTS = set(POINTS_CHAN_KEYS)
_LEGACY_CHAN_TO_ENTRY = set(ENTRY_CHAN_KEYS)

# 改这些模块会拼合写回 CHAN_CFG，任务运行中禁止保存
CHAN_CFG_MODULES = frozenset(("chan", "points", "entry"))

# ---- 双周期链（2026-10-23）----
# 链A=30S-3-15-60-240，链B=1-5-30-240。节点勾选=该周期作为开单周期（检测周期）；
# 不勾=仅结构参考（仍加载：笔/买卖点/支阻照算，不在该周期开单，类似 D 的角色）。
# 最细周期（链A 的 30S、链B 的 1）勾=加载参与（背驰下沉最深层）、不勾=不加载
# （上一级自动失去检测周期资格——filterDetectPeriods 既有规则）。
# D 不进链配置：引擎始终隐式加载作结构参考（regime/趋势锚），从不开单。
CHAIN_A_NODES = ("240", "60", "15", "3", "30S")   # 30S=细层开关（仅回测可用，MT5 无 30S）
CHAIN_B_NODES = ("240", "30", "5", "1")           # 1=细层开关
CHAIN_DEFAULTS = {
    "chainAOn": True,
    "chainA": "60,15,3",    # 240 默认不勾（仅结构参考；检测周期=60/15）
    "chainBOn": False,      # 默认关：未拉 1/5/30 周期数据时回测不因缺数据失败
    "chainB": "30,5,1",     # 与 "1,5,30" 集合相同；2026-10-25 起默认取用户参数页顺序
}

# schema：label/desc 前端展示用；type/min/max 由默认值类型与下表范围决定（校验用）。
# 未列 min/max 的布尔键只做类型校验。
PARAM_MODULES = {
    "chan": {
        "category": "base",
        "title": "画笔",
        "params": {
            "gapFilter": ("跳空成笔阈值", "相邻K线缺口 ≥ 该值×ATR 时强制独立成笔", 0.0, 5.0),
            "wickMarkOn": ("长影线修复",
                           "长影线插针压平与端点候选价总开关；关闭后不做任何长影线处理",
                           None, None),
            "wickRatio": ("长影线比例阈值", "影线占整根K线振幅 ≥ 该比例视为插针（不参与区间竞争）", 0.0, 1.0),
            "wickMinLen": ("长影线长度下限",
                           "影线绝对长度下限（品种报价单位价差，如黄金 0.5=0.5 美元）；"
                           "影线长度 ≥ 该值才判插针", 0.0, 500.0),
            "wickMinRange": ("长影线最小价差",
                             "整根K线价差（最高-最低）须大于该值（报价单位绝对价差）才判插针压平，"
                             "窄幅K线不处理。0=不限", 0.0, 500.0),
            "fractalSideRealWick": ("分型邻侧影线真实价",
                                    "分型左右邻比较用压平前真实影线价（高点 max(high,_wickHigh)、"
                                    "低点 min(low,_origLow)），被压平的邻侧影线不再误杀中间分型；"
                                    "中心K仍用压平结构价。区间套锁定（上级笔端点）优先于近等后移，"
                                    "上级已定端点不因此开关移动。2026-10-14 起默认开"
                                    "（用户规则：长影压平不能影响笔的高点归属）", None, None),
            "endInnerRecoverOn": ("端点内部极值恢复",
                                  "终点分型之前笔内部被包含合并吞掉的更极端真低/真高恢复为笔端点"
                                  "价/时（笔端点=笔区间真实极值）；只改端点，不动合并结构与分型。"
                                  "关闭后只认终点分型中心及其后的极值",
                                  None, None),
            "innerExtremeGuardOn": ("笔内极值守卫",
                                    "阶段二替换/顶替产生的新配对笔内藏着超近等容差（影线与实体双超）"
                                    "的更极值时，落地该极值为端点（五处挂钩：同型更极端替换/push起点侧/"
                                    "前顶前底作废/区间极值版/间隔不足顶替；例：15m 2026-09-04 下跌笔"
                                    "终点 12:00@4465.49 内藏 14:45@4460.15 → 延伸到 14:45）。锁定端点"
                                    "（区间套优先）与跳空锁定笔不参与；近等族实体容差内的取后不受影响。"
                                    "关闭=2026-10-24 旧口径逐位回退",
                                    None, None),
            "wideBarOn": ("单根长K豁免开关",
                          "「顶底分形不能包含」单根长K豁免总开关。关闭后所有周期一律不豁免"
                          "（各周期点数不再生效）", None, None),
            "wideBarPointsD": ("日线",
                               "单根K线振幅（最高-最低）≥ 该点数时不参与分型终点侧三根的反向贯穿检查；0=不豁免",
                               0.0, 500.0),
            "wideBarPoints240": ("4小时",
                                 "单根K线振幅（最高-最低）≥ 该点数时不参与分型终点侧三根的反向贯穿检查；0=不豁免",
                                 0.0, 500.0),
            "wideBarPoints60": ("1小时",
                                "单根K线振幅（最高-最低）≥ 该点数时不参与分型终点侧三根的反向贯穿检查；0=不豁免",
                                0.0, 500.0),
            "wideBarPoints15": ("15分钟",
                                "单根K线振幅（最高-最低）≥ 该点数时不参与分型终点侧三根的反向贯穿检查；0=不豁免",
                                0.0, 500.0),
            "wideBarPoints3": ("3分钟",
                               "单根K线振幅（最高-最低）≥ 该点数时不参与分型终点侧三根的反向贯穿检查；0=不豁免",
                               0.0, 500.0),
            "nearDoubleFixed": ("近等双顶固定容差",
                                "近等双顶/双底比较容差（报价单位绝对价差，如黄金 2.0=2 美元）；"
                                "中间真实回调深度闸门同用该值", 0.0, None),
            "nearDouble3": ("近等双顶·3分钟",
                            "该周期笔启用近等双顶/双底平台取后顶/后底", None, None),
            "nearDouble15": ("近等双顶·15分钟",
                             "该周期笔启用近等双顶/双底平台取后顶/后底", None, None),
            "nearDouble60": ("近等双顶·1小时",
                             "该周期笔启用近等双顶/双底平台取后顶/后底", None, None),
            "nearDouble240": ("近等双顶·4小时",
                              "该周期笔启用近等双顶/双底平台取后顶/后底", None, None),
            "nearDoubleD": ("近等双顶·日线",
                            "该周期笔启用近等双顶/双底平台取后顶/后底", None, None),
            "nearDoubleShiftLocked": ("近等取后让位锁定",
                                      "开=锁定端点（上级笔端点）唯一允许的移动方式为近等双顶/双底"
                                      "平台取后（闸门照常：影线/实体比较、差≤容差、回调够深、"
                                      "无相接成笔），需对应周期近等双顶开关开启；开启后下级端点"
                                      "可能不再与上级端点重合。默认关", None, None),
            "absorbMarkFast": ("吸收成笔即标",
                               "开=死顶/死底近等吸收只追加端点、笔起点原位不动，起点被区间套"
                               "锁定（上级笔端点）不再阻断吸收——笔当场成立、2买/2卖点即时"
                               "可标（k 与被吸收分型锁定仍排除）；关=旧口径（起点锁定即排除，"
                               "标记要等锁集变动）。默认开", None, None),
            "synthIntrabarBars": ("盘中合成K（15m/1h/4h）",
                                  "回测每拍用3分钟已收K聚合15m/1h/4h进行中K（O=bin首开、"
                                  "H/L=运行极值、C=最新收）临时注入链路，高周期结构盘中即见"
                                  "当根K变化；关闭则只认已收K", None, None),
            "pointEnoughForming": ("够笔只计成笔可能",
                                   "形成中段的够笔计数只数到极值块，反向确认（首根抬低点/抬高点K）"
                                   "后的K不计入；关闭则数到当下", None, None),
            "windowDays3": ("绘制窗口·3分钟",
                            "3分钟周期只绘制并只加载最近 N 天的笔（起点=最新K线时间前推 N 天）；"
                            "1小时/4小时/日线全量绘制", 1, 365),
            "windowDays15": ("绘制窗口·15分钟",
                             "15分钟周期只绘制最近 N 天的笔（取数仍全量供上级校准）", 1, 365),
            "windowDays30S": ("绘制窗口·30秒",
                              "30秒级别（只计算落盘不绘制）只取最近 N 天，供「以下级别背驰」"
                              "检测；密度是3分钟的6倍，窗口宜小", 1, 365),
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
            "keep": ("每周期保留标记数", "每周期买卖点标记总数上限（买+卖合并取最近N）", 1, 100),
            "thirdZsTol": ("3类进中枢容差(点)", "3买/类3买/4买/类4买/3卖/类3卖/4卖/类4卖允许进入中枢的绝对点数（0=严格）", 0.0, 1000.0),
            "divergeDurRatio": ("背驰时长可比上限", "两段时长比超过该值时面积项不计入背驰判据（收笔判定用，买卖点判定已不用背驰）", 1, 100),
            # nearAtrRatio 已随 2026-10-14 规则v3 移除（真1买/真1卖 邻近合并删除）
            # secondDivergeGate 已随 2026-10-14 规则v2 移除（背驰彻底退出买卖点判定）；
            # class2ZsTol 已停用（类2买/类2卖「同一个中枢」改按时间归属）；
            # anchorUndecidedSkip/anchorUndecidedMinBars 已淘汰（2026-10-10 提前入列）
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
            "exit_min_merged": ("出场成笔预期门槛", "形成段合并后 ≥ 该值根K视为够笔（检测/背驰周期够笔触发；过高低点止盈不用此门槛）", 2, 50),
            "realtime_min_bars": ("当下制够笔K线数", "检测周期形成段从起点所在合并块起达到该块数才评估（含起点；默认5）", 1, 100),
            "zs_exit_weak_ratio": ("出中枢力度衰减比例", "离开笔幅度 < 进入笔幅度×该值 视为力度变弱（1Buy/1Sell 条件）", 0.1, 5.0),
            # 出场方式（2026-10-08 可配置化：开关+平仓比例；至少启用一种，update 跨键校验）
            # 平仓比例基数=总仓位（初始开仓手数），非剩余仓位：手数=⌊总手数×比例%⌋
            # 整数（不足1手平1手），100=全平
            # 锁定盈利（2026-10-11）优先级最高：两个数都可配，比例锁的是盈利点数不是手数
            "exitLockOn": ("锁定盈利", "仅缠论V1（强分型均线V1不使用），出场优先级最高。最大盈利超过门槛后武装，价格回撤到只剩「最大盈利×锁定比例」时全平剩余仓位；本拍创出的新高下一拍才参与回撤判定", None, None),
            "exitLockPts": ("锁定盈利门槛", "最大盈利超过该点数（绝对价格）后武装", 0.1, 10000.0),
            "exitLockPct": ("锁定盈利比例", "武装后至少锁定最大盈利的百分比（50=最大盈利的一半；不是平仓手数比例）", 1, 100),
            "exitStopSrOn": ("支阻止损", "初始止损位（支阻位±滑点/进场K线外推/最大止损兜底）盘中穿越 → 离场", None, None),
            "exitStopSrPct": ("支阻止损平仓比例", "触发时平总仓位（初始手数）的百分比；100=全平，部分平仓后剩余由其它方式了结", 1, 100),
            "exitStopBeOn": ("保本止损", "TP1保本迁移（背驰周期首笔有利方向笔完成→止损上移保本位）后穿越保本位 → 离场", None, None),
            "exitStopBePct": ("保本止损平仓比例", "触发时平总仓位（初始手数）的百分比；100=全平", 1, 100),
            "exitStopBeMode": ("保本位模式", "extreme=极值±滑点：保本位=进场成交K线极值±保本滑点（被打到至少亏滑点+进场K线逆向波幅）；entry=0亏损（默认）：保本位=进场价（保本滑点不参与；穿越后下一开盘成交，跳空仍可能略有出入）；bi=笔端点±滑点：背驰周期首笔有利方向笔确认当拍保本位=该笔起点±保本滑点（滑点取开仓时快照；成笔前无值，止损暂留支阻位，成笔那拍迁移）", ("extreme", "entry", "bi"), None),
            "exitHalfMrOn": ("背驰周期够笔", "背驰周期向有利方向走出5块合并K（预期口径，不等确认笔）或首笔有利方向确认笔完成（TP1同事件源），不限顺势 → 平指定比例，剩余止损移保本位；与「检测周期够笔」同一张卡", None, None),
            "exitHalfMrPct": ("背驰周期够笔平仓比例", "触发时平总仓位（初始手数）的百分比；默认50", 1, 100),
            "exitHalfOn": ("检测周期够笔", "检测周期向有利方向走出5块合并K（预期口径，锚点=末笔终点块与进场块较晚者，不等确认笔）或末笔翻为有利方向确认笔，不限顺势 → 平指定比例，剩余止损移保本位", None, None),
            "exitHalfPct": ("检测周期够笔平仓比例", "触发时平总仓位（初始手数）的百分比；默认50", 1, 100),
            "exitCloseOn": ("过高低点止盈", "有利方向笔破前一同向笔端点（不分顺势逆势；预期口径=当根收盘K极值越过即触发，不等确认笔）→ 平指定比例", None, None),
            "exitClosePct": ("过高低点止盈平仓比例", "触发时平总仓位（初始手数）的百分比；100=全平", 1, 100),
            "exitExpectOn": ("出场预期口径（严进宽出）", "开=检测/背驰周期够笔认「向有利方向走出5块合并K」（锚点=末笔终点块与进场块较晚者）、过高低点认「当根收盘K极值越过前一同向笔端点」，均不等确认笔；关=等确认笔。进场侧结构判定不受影响", None, None),
            "exitTrailOn": ("跟踪止盈", "保本迁移（TP1/够笔类置保本位）之后才接管：参照点出现→止损一次性上移到点价∓跟踪止盈滑点（只上移，保本前的参照点不追溯）；上移后的止损被穿越 → 离场记跟踪止盈；保本之前穿越只认支阻/最大止损。参照由「跟踪止盈参照」选择", None, None),
            "exitTrailPct": ("跟踪止盈平仓比例", "触发时平总仓位（初始手数）的百分比；100=全平。部分平仓 latch 后剩余手数由最大止损硬上限兜底全平（记支阻止损）", 1, 100),
            "exitTrailSrc": ("跟踪止盈参照", "detect=检测周期已破前低/前高的有利方向笔起点（空头=破前低的下跌笔高点，多头=破前高的上涨笔低点；够笔未破前低/前高不算；保本后价格破坏该结构点记跟踪止盈）；mark=背驰周期信号流同向3类点（3Buy/Buy/…）", ("detect", "mark"), None),
            "exitTrailSlip": ("跟踪止盈滑点", "止损上移到参照点价后向不利侧偏移的点数（同 fxma tpTrailSlipPts；仅跟踪止盈启用时生效）", 0.0, 100.0),
            "exitTouchOn": ("价格线触价成交", "仅缠论V1。开=价格线类出场（支阻止损/保本/跟踪止盈/最大止损兜底/锁定盈利）盘中触及线价即在触发根按线价即时成交（同根多线被穿按线位从高到低逐线成交；实盘按同一候选源把第一条会全平的线挂成券商SL）；关=收盘判定+下一根开盘成交（旧口径）。够笔/过高低点等结构类出场不受影响", None, None),
            "sinkFallback": ("M1下沉链回退", "下沉停止级无候选时沿链向上一级重评", None, None),
            "sinkFallbackRearm": ("M1终局后重置去重", "同向持仓终局后重置段去重（同段可再进一次）；不清同一点尝试计数", None, None),
            "pointMaxAttempts": ("同一点最大尝试次数",
                                 "同一买卖点（同检测周期+类别+锚点时间）终生最多实际开仓次数："
                                 "成交建仓才计数（被同向互斥压掉未成交的信号不消耗次数），"
                                 "用满后该点不再布防触发（拒绝码 point_attempt_max）；"
                                 "终局重置只清段去重、不清此计数；0=不限（旧口径）；"
                                 "默认2=初始一次+重试一次",
                                 0, 20),
            "pointMaxAttemptsSeg": ("同一段内最大开仓次数",
                                    "同一上级段内同类买卖点（键=检测周期+类别+上级段起点）最多实际"
                                    "开仓次数：锚点随新极值漂移不重置预算（段延伸不换键），上级段终结"
                                    "反向新笔起点才重置；不同类别（2买/3买/强弱）各自独立预算互不"
                                    "挤占；计数口径同「同一点最大尝试次数」（成交才计、终局不回血），"
                                    "用满拒绝码 point_attempt_seg；0=不限；默认2",
                                    0, 20),
            "macdDivergeBodyOn": ("MACD背驰容差", "与容差同一张卡，在进场闸门。开=影线未过时再比笔端点K实体（默认开）；关=只比影线", None, None),
            "macdDivergeTol": ("背驰容差", "MACD背驰固定容差（绝对价差，点）。影线或实体未严格更极端时，价差落在 0 到该值内算近等。0=不放宽。开关关着仍用于影线", 0.0, 100.0),
            "expectBiEnough": ("M4预期够笔", "允许预期段在合并K线够笔后进场；结构预期本身在普通分型确认后立即参与", None, None),
            "expectBiMinBars": ("M4够笔K线数", "预期够笔的本级合并K线块数门槛（含起点所在块）", 1, 100),
            "divergeConfirm": ("M4背驰确认后成交", "开启=分型右邻K收盘后的下一根开盘成交；关闭=当下成交（2026-10-25 起默认开，此前默认关）", None, None),
            "entryMacdShrink": ("进场MACD柱缩闸", "开启=背驰级别上一根已收K线柱状体(|MACD|)较前一根缩小才出信号（确认背驰且柱缩，下一根开盘进场）", None, None),
            "stopEntryBarFloor": ("进场K线止损下限", "开启=止损至少在进场背驰周期K线极值外侧加滑点处（运行中外推、收盘冻结，与支阻位止损取更宽者）", None, None),
            "anchorWinBars": ("锚点窗口K线数", "锚点（买卖点）所在检测周期K线起 N 根内允许触发——当拍与背驰点所在K都须落窗；1=旧单K规则（2026-10-21 重定义）", 1, 10),
            "anchorWinPts": ("锚点窗口距极值点数", "触发价（当拍现价与背驰点价）距窗口极值（买侧最低/卖侧最高=锚点价与窗口内实际极值取小/大）的最大点数；0=不限价距只看时间窗", 0.0, 1000.0),
            "anchorWinTrailOn": ("锚点窗口随新极值重开", "开启=窗口内创新高/新低时时间窗锚K=窗口极值所在K线（连续新高不产生新压低2卖锚时窗口不再卡死，9-8 12:00 案例）；关闭=固定锚K起算（2026-10-21 口径）。价距基准两口径均随极值漂移", None, None),
            "srSideGateOn": ("fib/BOLL按方向取侧",
                             "近支阻闸门与止损参考只认信号同侧的 fib/BOLL 位：做多取 SUP"
                             "（最近上涨笔回撤支撑/布林下轨与下侧中轨）、做空取 RES"
                             "（最近下跌笔反弹阻力/布林上轨与上侧中轨）；密集区与人工位"
                             "不按侧过滤。关闭=两侧皆可命中（旧口径，2026-10-25 前）",
                             None, None),
            "macdZeroTol": ("黄白线容差", "强2类买卖点黄白线容差：开闸后强二买 DIF > -该值才进（0=严格 0 轴）。1买/2卖的 0 轴判定仍用该值", 0.0, 100.0),
            "strong2BuyMacdOn": ("强2类买卖点黄白线", "与容差同一张卡，在进场闸门。开=强二买开单要求 DIF 高于 -容差；关=强二买不看黄白线（默认）", None, None),
            "pointClasses": ("买卖点类别",
                             "勾选=该类买卖点可作进场锚点开单（买卖两侧对称：勾「强三类」=强3买+强3卖）。"
                             "强弱全族拆分：二类族=1买后反弹过/未过前高；3/4族=点后同向腿背驰"
                             "（弱3=2类后上推腿对1类后反弹腿背驰、弱4=3类后上推腿对2类后上推腿"
                             "背驰，卖侧镜像，判不出即强）。强→2Buy/3Buy/Buy 等基础键、"
                             "弱→2BuyBear/3BuyBear/BuyBear 等 Bear 键。"
                             "不勾=最新锚点为该类时本拍跳过不开单、不回溯更早的点"
                             "（浅过滤，锚点选取本身不变）。默认五项 1/2s/2xs/3s/4s"
                             "（2026-10-25 起；此前默认全选）",
                             ("1", "2s", "2w", "2xs", "2xw", "3s", "3w", "3xs", "3xw",
                              "4s", "4w", "4xs", "4xw"), "multi"),
            "divergeReferByZs": ("背驰参照按中枢",
                                 "背驰参照笔=入中枢段（跳过当前段之前中枢内部/之后的同向笔，"
                                 "背驰=入中枢段vs出中枢段，中枢内部振荡段不参与比较）；"
                                 "关=取紧邻前一同向笔", None, None),
            "sinkSkipLevel": ("跨级下沉",
                              "下沉链次级展开<3笔时跳过该级继续向下找有展开的级别判背驰"
                              "（如60→3直沉，markRes=3）；关=在本级判定", None, None),
            "divergeSlopeOn": ("进场背驰·斜率判据",
                               "开=背驰先比两笔斜率（|端点价差|÷时长），当前笔<参照笔即判背驰；"
                               "斜率没变小再看面积判据。与面积判据全关=旧三项OR口径"
                               "（面积变小 或 黄白线抬高/压低 或 单根柱变矮，A/B 基准）。"
                               "作用于当下制/确认制/强分型均线V1背驰触发三链"
                               "（收笔/买卖点识别不受影响）",
                               None, None),
            "divergeAreaShrinkOn": ("进场背驰·面积判据",
                                    "开=斜率没变小时再比柱状体面积，需明显变小（当前段<参照段×"
                                    "「面积明显变小阈值」%）；两段时长比过大时面积项自动不可用"
                                    "（只剩斜率判据）",
                                    None, None),
            "divergeAreaShrinkPct": ("进场背驰·面积明显变小阈值(%)",
                                     "当前段柱状体面积须小于参照段×该百分比才判背驰："
                                     "50=缩到一半以下；100=任意变小即可；0=面积判据恒不过",
                                     0.0, 100.0),
        },
    },
    "chain": {
        "category": "strategy",
        "title": "周期链",
        "params": {
            "chainAOn": ("链A总开关",
                         "30秒-3分钟-15分钟-60分钟-4小时周期链；关闭后链A 不加载不产生信号。"
                         "回测与实盘共用（实盘 MT5 无 30 秒行情，链A 勾选 30S 时实盘拒启）",
                         None, None),
            "chainA": ("链A开单周期",
                       "勾选=该周期作为开单周期（检测周期）；不勾=仅结构参考（仍加载笔/买卖点/"
                       "支阻，不在该周期开单）。30S 为细层开关：勾=加载 30 秒参与背驰下沉"
                       "（仅回测可用），不勾=不加载（此时 3 分钟无下级、不作检测周期）",
                       CHAIN_A_NODES, "multi"),
            "chainBOn": ("链B总开关",
                         "1分钟-5分钟-30分钟-4小时周期链；关闭后链B 不加载不产生信号。"
                         "启用前须先在基础数据页拉取 1/5/30 周期K线（回测读本地存储，"
                         "缺周期直接报错）",
                         None, None),
            "chainB": ("链B开单周期",
                       "勾选=该周期作为开单周期（检测周期）；不勾=仅结构参考。1分钟为细层开关："
                       "勾=加载 1 分钟参与背驰下沉，不勾=不加载（此时 5 分钟无下级、"
                       "不作检测周期）。两链同时启用时各自独立引擎实例并行、持仓互不影响",
                       CHAIN_B_NODES, "multi"),
        },
    },
    "plan": {
        "category": "strategy",
        "title": "交易计划",
        # 2026-10-16 起只剩震荡/顺势两组（开单链路直连买卖点，策略路由参数下架；
        # 键序 = 参数页 2 类分组（震荡 / 顺势；params.html PLAN_GROUPS 同步维护））
        "params": {
            # ---- 震荡 ----
            "rangeRes": ("震荡判定参考周期", "4小时/日线=参考周期判震荡→更低周期观望，参考周期及以上只作锚不交易；关闭=无震荡闸，直接走买卖点",
                         ("", "240", "D"), None),
            "rangeBoundOn": ("横盘判定", "A 类震荡判定（K线窄带+笔端点+涨跌交替）；参考周期开=只判参考周期、更低周期听门；本开关关闭=跳过此类",
                             None, None),
            "rangeBarN": ("震荡窗口K线数", "最近N根K线的区间判定窗口", 5, 500),
            "rangeBiN": ("震荡最近笔数", "最近N笔端点极差与涨跌交替判定", 2, 50),
            "rangeKMult": ("K线区间阈值(×ATR)", "窗口高低差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBiMult": ("笔端点阈值(×ATR)", "笔端点极差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBreakMult": ("突破跳过阈值(×ATR)", "末笔端点越过窗口另一端 >该值×ATR 视为突破（跳过震荡判定）", 0.0, 10.0),
            "rangeZsOn": ("中枢内判定", "B 类震荡判定（未离开中枢且现价在箱内）；参考周期开=只判参考周期、更低周期听门；本开关关闭=跳过此类",
                          None, None),
            # ---- 顺势 ----
            "trendRes": ("顺势参考周期", "参考周期方向过滤更低周期进场（关闭/4小时/日线；规则见本页下方说明）",
                         ("", "240", "D"), None),
            "trendRebound": ("方向相位判定", "锚点（1卖/2\\3卖及买侧镜像）确立后按 够笔+中枢边界/前低前高+角度强弱 分相位定方向，含观望态（闩锁优先）；关=确立方向锁到闩锁/新点。回测与图表 JS 均生效",
                         None, None),
            "reboundNearPts": ("相位近位容差(点)", "形成段极值距中枢上/下沿或前低/前高 ≤ 该值（绝对点数）视为附近（调大等效常判附近）",
                         0.0, 1000.0),
            "reboundAngleRef": ("相位角度45°基准(点/根)", "当前笔平均每根幅度 > 该值 = 角度>45°（强），≤ 为弱；下跌角度与反弹/回调力度同口径",
                         0.1, 100.0),
        },
    },
    "fxma": {
        "category": "strategy",
        "title": "强分型均线V1",
        "params": {
            "entryRes": ("进出场周期", "多选并行产生信号（逗号分隔；30S 仅回测可用——MT5 实盘行情由 M1 重采样无法生成 30S）",
                         FXMA_RES_OPTIONS, "multi"),
            "pointClasses": ("买卖点类别", "只交易所选类别的缠论买卖点：1买/1卖→1类，2买/2卖→2类，类2买/类2卖→类二（2x），3买/3卖→3类，类3买/类3卖→类三（3x）；4类不交易",
                             ("1", "2", "2x", "3", "3x"), "multi"),
            "entryPick1": ("一类点·条件满足数", "1买/1卖触发需启用进场条件（均线分离/收盘站线/强分型）中至少满足N个；"
                                        "生效N=min(N,启用条件数)；必选条件不通过则不触发；次级别背驰开启时条件组合不参与",
                         ("1", "2", "3"), None),
            "entryPick2": ("二类点·条件满足数", "2买/2卖触发需启用条件中至少满足N个；生效N=min(N,启用条件数)", ("1", "2", "3"), None),
            "entryPick2x": ("类二点·条件满足数", "类2买/类2卖触发需启用条件中至少满足N个；生效N=min(N,启用条件数)", ("1", "2", "3"), None),
            "entryPick3": ("三类点·条件满足数", "3买/3卖触发需启用条件中至少满足N个；生效N=min(N,启用条件数)", ("1", "2", "3"), None),
            "entryPick3x": ("类三点·条件满足数", "类3买/类3卖触发需启用条件中至少满足N个；生效N=min(N,启用条件数)", ("1", "2", "3"), None),
            "maOn": ("均线分离条件", "开启=参与条件判定（必选/可选见「条件性质」）；"
                                  "关闭=停用，不计票不拦截", None, None),
            "maReq": ("条件性质", "必选=均线分离不通过则该类买卖点不触发；可选=仅参与满足数计票（未通过只影响票数）。需均线分离条件开启时生效",
                       ("required", "optional"), None),
            "maType": ("均线类型", "收盘价简单/指数均线（按进出场周期K线计算）", ("SMA", "EMA"), None),
            "maFast1": ("一类点快线", "1类买卖点用的均线对（快线周期）", 1, 500),
            "maSlow1": ("一类点慢线", "1类买卖点用的均线对（慢线周期）", 2, 500),
            "maFast2": ("二三类点快线", "2/3类买卖点共用的均线对（快线周期）", 1, 500),
            "maSlow2": ("二三类点慢线", "2/3类买卖点共用的均线对（慢线周期）", 2, 500),
            "crossMinPts": ("均线上下穿确认点数", "交叉成立须快线与慢线间距≥该点数（如8均线下穿20均线2个点=快线低于慢线≥2点）；0=任意落差",
                            0.0, 100.0),
            "maStandOn": ("收盘站线条件", "开=参与条件判定（必选/可选见「条件性质」）——买点收盘站上（严格大于）、卖点站下（严格小于）指定均线；"
                                      "关闭=停用，不计票不拦截", None, None),
            "maStandReq": ("条件性质", "必选=收盘站线不通过则该类买卖点不触发；可选=仅参与满足数计票。需收盘站线条件开启时生效",
                           ("required", "optional"), None),
            "maStand1": ("一类点站线均线", "1类买卖点站线判定的均线周期（SMA/EMA 同均线类型，按进出场周期收盘价）", 1, 500),
            "maStand2": ("二三类点站线均线", "2/3类买卖点站线判定的均线周期（口径同上）", 1, 500),
            "fibNearOn": ("黄金分割附近条件", "独立硬门槛（开启即必须通过，不参与条件计票；仅2/3类点，1类点豁免）：摆动段=前一同侧买卖点价格→其后至本点前的本周期K线极值，本点价格距任一档位≤容差点数才触发（判定随点固定）；"
                                       "关闭=停用", None, None),
            "fibLevels": ("黄金分割档位", "回撤比例多选（各档 0<r<1）：本点价格靠近任一所选档位即通过，引擎按逗号串解析", ("0.236", "0.382", "0.5", "0.618", "0.786"), "multi"),
            "fibNearPts": ("黄金分割容差(点)", "本点价格与最近档位价位的绝对价差上限（点）", 0.0, 100.0),
            "upperDirOn": ("上级周期同向条件", "开=进出场周期的上级周期（30S→3→15→60→240）当前笔方向须与信号同向（买=上涨笔/卖=下跌笔；全部类别生效，独立硬门槛不参与计票）；"
                                      "关闭=跳过该条件", None, None),
            "divLowerOn": ("次级别背驰条件", "开=次级别同向背驰为唯一触发条件，与条件组合互斥（强分型/均线分离/收盘站线/满足数不参与，背驰不过则等待、不回退计票；黄金分割/上级同向若开启仍拦一道）。背驰判定同缠论V1 lowerDiverge（双判据背驰+区间套下沉链，落在次级别还是次次级别由结构决定）；窗口粘性：背驰段时间≥点时间−「背驰窗口」根本周期K线且≤评估拍（防未来）即通过，之后持续有效直到点作废（3分钟周期须加载30秒级别）。"
                                     "关闭=走条件组合（三选N计票）", None, None),
            "divLowerWinBars": ("背驰窗口(根)", "候选背驰点允许的最早时间=点时间−N根本周期K线；点之后、评估拍之前出现的背驰始终有效（无未来函数）。"
                                "调大可放行更多点，过大则该条件趋近失效",
                           1, 1000),
            "strongFxOn": ("强分型条件", "开启=参与条件判定（必选/可选见「条件性质」）；关闭=停用，不计票不拦截；"
                                    "三条件（均线分离/收盘站线/强分型）全停=买卖点属所选类别且未失效当拍收盘即触发", None, None),
            "strongFxReq": ("条件性质", "必选=强分型不通过则该类买卖点不触发；可选=仅参与满足数计票。需强分型条件开启时生效",
                            ("required", "optional"), None),
            "strongFxMinPts": ("强分型实体最小落差", "0=顶分型右肩收盘<左肩开盘即可；>0=须至少低/高该点数才算强分型",
                               0.0, 100.0),
            "pointValidBars": ("点有效期(根)", "买卖点出现后，强分型+均线分离须在 N 根K线内齐备，超时该点作废；0=不限（直到反向点出现）",
                               0, 1000),
            "pointValidPts": ("点有效期(值)", "评估K线盘中价距点极值的最大距离（买：最高价−买点最低价；卖：卖点最高价−最低价），"
                                       "超距该拍不评估、等待价格回到范围内（点不作废）；0=不限",
                              0.0, 10000.0),
            "stopPts": ("止损点数", "多：进场价−N；空：进场价+N（绝对点数）。最小周期K线盘中价触及即按该止损价成交（与实盘 MT5 SL 同口径，不等收盘确认）", 0.1, 1000.0),
            "tpPts": ("止盈点数", "多：进场价+N；空：进场价−N（绝对点数）。盘中价触及即按该止盈价成交（同止损口径）；仅止盈方式=固定点数时生效", 0.1, 1000.0),
            "tpMode": ("止盈方式", "points=固定点数：满仓一笔、触价全平；structure=结构组合（默认）：每笔开半仓（可开仓手数÷2），同向容量制叠加（作用域内总手数≤可开仓手数，最多两笔；同一粗类 1/2/3 持仓期间不叠加，该类平仓后可再开）——1/2类入场一半到前一点位主动止盈+另一半走跟踪止损（3/类3/4/类4同向点出现止损只上移），3类入场只主动止盈",
                       ("points", "structure"), None),
            "tpNearPts": ("到点容差(点)", "主动止盈价=前一点位价格向入场侧偏移N点（提前止盈），盘中触及即按该价成交；0=精确触及。目标位=入场点之前最近的反向买卖点（多头前卖点/空头前买点；在亏损侧或不存在则本笔不设主动止盈）",
                          0.0, 1000.0),
            "tpTrailSlipPts": ("提损滑点(点)", "结构跟踪止损：止损上移到 3/类3/4/类4 同向点极值价后再向不利侧偏移N点（多头−N/空头+N），只上移不下移",
                               0.0, 1000.0),
            "sameBarPriority": ("同根双触优先级", "同一根最小周期K线盘中同时触及止损与止盈时按哪个成交（stop=保守）", ("stop", "tp"), None),
            "mutexScope": ("同向互斥范围", "global=同向全局一笔未终局持仓（同缠论V1）；perPeriod=每个进出场周期独立互斥",
                           ("global", "perPeriod"), None),
            "lots": ("默认开仓手数", "回测卡/实盘配置显式指定手数时以配置为准；盈亏 = 价差 × 方向 × 手数 × 合约乘数(1手=0.01标准手)",
                     0.01, 100.0),
        },
    },
}


def _chan_defaults_subset(keys):
    """从 CHAN_CFG_DEFAULTS 按键序取子集。"""
    src = chan_core.CHAN_CFG_DEFAULTS
    return {k: src[k] for k in keys}


def defaults_of(module, symbol=None):
    """模块默认值（活取各算法模块常量，保证与代码默认不漂移）。
    symbol 传入时，进出场手数/最大止损改用该品种默认；未单列的品种仍用全局常量。
    不传 symbol 时返回全局默认（schema 与未列品种用）。"""
    if module == "chan":
        base = _chan_defaults_subset(CHAN_BI_KEYS)
    elif module == "points":
        base = {
            "keep": mark_buy_sell.KEEP,
            "thirdZsTol": mark_buy_sell.THIRD_ZS_TOL,
            **_chan_defaults_subset(POINTS_CHAN_KEYS),
        }
    elif module == "zs":
        base = dict(ZS_DEFAULTS)
    elif module == "entry":
        base = {
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
            **mark_entry.EXIT_MODE_DEFAULTS,
            **_chan_defaults_subset(ENTRY_CHAN_KEYS),
        }
        if symbol:
            sid = symbol_id(symbol)
            if sid:
                lots = mark_entry.DEFAULT_LOTS_BY_SYMBOL.get(sid)
                if lots is not None:
                    base["lots"] = lots
                slip = mark_entry.DEFAULT_SLIP_FALLBACK_BY_SYMBOL.get(sid)
                if slip is not None:
                    base["slip_fallback"] = slip
    elif module == "plan":
        # 2026-10-16 起交易计划只剩震荡（rangeRes+震荡阈值）/顺势（trendRes+相位）两组
        # 参数——策略路由参数（prevHighNearPts/secondNearPts/thirdStrongTrend/
        # weakTierByOrigin）随 strategyOf/classifySecond 删除而下架
        base = {**trading_plan.RANGE_DEFAULTS, "trendRes": trading_plan.TREND_RES,
                "rangeRes": trading_plan.RANGE_RES,
                "trendRebound": trading_plan.TREND_REBOUND,
                "reboundNearPts": trading_plan.REBOUND_NEAR_PTS,
                "reboundAngleRef": trading_plan.REBOUND_ANGLE_REF}
    elif module == "fxma":
        base = dict(FXMA_DEFAULTS)
    elif module == "chain":
        base = dict(CHAIN_DEFAULTS)
    else:
        raise ValueError(f"未知参数模块：{module}")
    return base


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
    """解析品种桶 id。空/未传 → 黄金页（XAUUSD）；未列入五品种 → None（走代码默认）。
    经纪商后缀（XAUUSDm）归到同一品种桶，参数中心仍用 XAUUSD。"""
    if not symbol:
        return "XAUUSD"
    suf = mark_entry.symbol_suffix(symbol)
    if suf in SYMBOLS:
        return suf
    for known in SYMBOLS:
        if suf.startswith(known):
            return known
    return None


symbol_id = entry_symbol_id  # 别名：全模块按品种取桶


def lots_of(ep, symbol):
    """按品种取进场手数。ep 为 effective("entry", symbol) / effective_all(symbol)["entry"]。"""
    return ep.get("lots", mark_entry.DEFAULT_LOTS)


def engine_module_params(pm):
    """effective_all(symbol) → BacktestEngine.module_params 映射（webapp 三模式 Worker 与
    实盘 live_trader 共用；2026-09-23 自 webapp._engine_module_params 上移，避免复制漂移）。
    调用方须先按品种解析 pm（各模块已是该品种桶）。"""
    ep = pm["entry"]
    out = {"plan": pm["plan"], "marks": pm["points"],
           "trendRes": pm["plan"]["trendRes"],
           "exit_min_merged": ep["exit_min_merged"],
           "realtime_min_bars": ep["realtime_min_bars"],
           "zs_exit_weak_ratio": ep["zs_exit_weak_ratio"],
           # 双周期链（2026-10-23）：整桶透传（引擎构造侧由 chain_specs 消费推导
           # periods/open_res；进 module_params 供 journal/header 快照记录）
           "chain": pm.get("chain") or dict(CHAIN_DEFAULTS)}
    # 出场方式开关/比例（2026-10-08；键名同名直通，缺键由引擎 EXIT_MODE_DEFAULTS 兜底）
    for k in mark_entry.EXIT_MODE_DEFAULTS:
        if k in ep:
            out[k] = ep[k]
    return out


def chain_specs(pm):
    """effective_all(symbol) → 启用链规格列表（回测双链循环/实盘链进程共用，
    2026-10-23 双周期链）。

    每条启用链返回 {"id": "A"|"B", "periods": 加载周期列表, "open_res": 开单周期集,
    "detect": 有效检测周期列表}：
    - periods = D（隐式结构参考头）+ 链全节点（未勾选的非最细节点仍加载=仅参考；
      最细节点 30S/1 按勾选加载），按 intervalSecOf 从大到小排序；
    - open_res = 勾选节点 ∩ 可开单节点（最细节点永不开单）；
    - detect = detectPeriodsOf(periods, open_res, periods)（须有已加载下级 ∩ 开单勾选）。
    两链全关 → []。
    """
    from .chan_core import intervalSecOf
    from .mark_entry import detectPeriodsOf
    ch = pm.get("chain") or dict(CHAIN_DEFAULTS)

    def _spec(cid, nodes, finest, raw):
        checked = {s.strip().upper() for s in str(raw or "").split(",") if s.strip()}
        opens = [p for p in nodes if p != finest and p in checked]
        loading = ["D"] + [p for p in nodes if p != finest]
        if finest in checked:
            loading.append(finest)
        loading.sort(key=lambda r: -(intervalSecOf(r) or 0))
        return {"id": cid, "periods": loading, "open_res": opens,
                "detect": detectPeriodsOf(loading, opens, loading)}

    specs = []
    if ch.get("chainAOn", CHAIN_DEFAULTS["chainAOn"]):
        specs.append(_spec("A", CHAIN_A_NODES, "30S", ch.get("chainA")))
    if ch.get("chainBOn", CHAIN_DEFAULTS["chainBOn"]):
        specs.append(_spec("B", CHAIN_B_NODES, "1", ch.get("chainB")))
    return specs


def fxma_engine_kwargs(pm, lots_override=None, **override):
    """effective_all(symbol) → FxMaEngine 构造 kwargs（回测卡/实盘分发点共用）。

    lots_override：回测卡/实盘配置显式手数（优先于参数中心 fxma.lots 默认）。
    marks_params：买卖点容差（points 品种桶）由 FxMaEngine 消费——见 override。
    """
    from .fx_ma import FxMaEngine
    kw = FxMaEngine.kwargs_from_params(pm["fxma"])
    if lots_override:
        kw["lots"] = lots_override
    kw["marks_params"] = {"class2ZsTol": pm["points"].get("class2ZsTol", 0.0),
                          "thirdZsTol": pm["points"].get("thirdZsTol", 0.0)}
    kw.update(override)
    return kw


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


def _is_multi(module, key):
    """spec 第4位 == "multi" → 多选逗号串类型（choices 在第3位）。"""
    meta = PARAM_MODULES[module]["params"][key]
    return len(meta) > 3 and meta[3] == "multi"


def _parse_multi_str(value, choices, key):
    """多选逗号串 → 去重保序规范串；逐元素校验 ∈ choices，至少一项。"""
    if not isinstance(value, str):
        raise ValueError(f"{key} 须为逗号分隔串（可选：{','.join(choices)}）")
    items, seen = [], set()
    for v in (s.strip() for s in value.split(",")):
        if not v:
            continue
        if v not in choices:
            raise ValueError(f"{key} 含非法值 {v!r}（可选：{','.join(choices)}）")
        if v not in seen:
            seen.add(v)
            items.append(v)
    if not items:
        raise ValueError(f"{key} 至少选择一项（可选：{','.join(choices)}）")
    return ",".join(items)


def schema_of(module):
    """前端渲染用 schema：每键 label/desc/type/min/max/default（str 枚举另带 choices）。"""
    defaults = defaults_of(module)
    spec = PARAM_MODULES[module]["params"]
    out = {}
    for key, meta in spec.items():
        typ = "multi" if _is_multi(module, key) else _type_of(defaults[key])
        item = {"label": meta[0], "desc": meta[1], "type": typ,
                "min": None if typ in ("str", "multi") else meta[2],
                "max": None if typ in ("str", "multi") else meta[3],
                "default": defaults[key]}
        if typ in ("str", "multi"):
            item["choices"] = list(meta[2])  # spec 第3位 = 允许值列表（枚举/多选）
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
        if _is_multi(module, key):
            choices = PARAM_MODULES[module]["params"][key][2]
            if key == "pointClasses" and isinstance(value, str):
                # 旧值迁移（POINT_CLASSES_LEGACY）：2→2s,2w；2x→2xs,2xw（保输入序）
                value = ",".join(i for s in value.split(",")
                                 for i in POINT_CLASSES_LEGACY.get(s.strip(), (s.strip(),)))
            out[key] = _parse_multi_str(value, choices, key)
            continue
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


def _migrate_fxma_store(mod):
    """modules.fxma：常规品种桶清洗（已删键 fibReq/divLowerReq 按 defaults 键集
    自动丢弃）+ entryPickN 旧值 4/5 钳到 3（条件组合五选N→三选N，2026-10-09，
    防穿透后撞引擎 1..3 校验）。"""
    out = _migrate_symbol_store("fxma", mod)
    for bucket in out.values():
        for k in ("entryPick1", "entryPick2", "entryPick2x", "entryPick3", "entryPick3x"):
            if bucket.get(k) in ("4", "5"):
                bucket[k] = "3"
    return out


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
        elif name == "fxma":
            out[name] = _migrate_fxma_store(mod)
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
    全部 PARAM_MODULES 按品种取桶：symbol 空=黄金页；未列品种=纯默认。
    进出场手数/最大止损的默认值按品种取。"""
    defaults = defaults_of(module, symbol)
    ov = _symbol_overrides(_load(), module, symbol)
    return {k: ov.get(k, defaults[k]) for k in PARAM_MODULES[module]["params"]}


def effective_all(symbol=None):
    """全模块有效值（同一 symbol）。"""
    return {name: effective(name, symbol) for name in PARAM_MODULES}


def snapshot_of(pm):
    """effective_all 结果 → 对比快照（回测起跑时冻结进 cfg["param_snapshot"]，供
    bt_runs 参数对比，2026-10-28）。各桶键序稳定=桶 schema 序；另带 "_titles" 内嵌
    各桶中文标题——前端自描述分组展示，无需再拉 schema。不动入参。"""
    out = {name: dict(vals) for name, vals in pm.items()}
    out["_titles"] = {name: PARAM_MODULES[name]["title"] for name in pm}
    return out


def effective_snapshot(symbol=None):
    """全模块有效值快照（按品种取桶）。"""
    return snapshot_of(effective_all(symbol))


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


# 画笔参数中心导出目录（chan_bi.js 启动时自动读取，实现「参数中心=画笔参数唯一配置点」）
_CHAN_CFG_EXPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cursor", "cache")


def _export_chan_cfg_files():
    """把各品种有效 CHAN_CFG 导出到 .cursor/cache/chan_cfg_<品种>.json。

    chan/points/entry 任一模块保存后调用：有 override 桶的品种各写一份
    chan_cfg_effective(<sid>)；桶清空的品种同步删除导出文件（防陈旧值）。
    画笔脚本（chan_bi.js）未显式传 --chan-cfg 时按当前品种自动读取——参数
    统一在参数中心调，无需命令行。非五品种无桶 → 无文件 → 画笔用代码默认
    （与 chan_cfg_effective「未列品种走默认」语义一致）。导出失败不阻断保存。"""
    if os.path.abspath(PARAMS_FILE) != _DEFAULT_PARAMS_FILE:
        return  # 存储被测试重定向：跳过真实导出，防临时桶污染画笔配置
    overrides = _load()
    sids = set()
    for mod in CHAN_CFG_MODULES:
        by = overrides.get(mod) or {}
        sids.update(sid for sid, ov in by.items() if isinstance(ov, dict) and ov)
    try:
        os.makedirs(_CHAN_CFG_EXPORT_DIR, exist_ok=True)
        for name in os.listdir(_CHAN_CFG_EXPORT_DIR):
            if name.startswith("chan_cfg_") and name.endswith(".json"):
                sid = name[len("chan_cfg_"):-len(".json")]
                if sid not in sids:
                    try:
                        os.unlink(os.path.join(_CHAN_CFG_EXPORT_DIR, name))
                    except OSError:
                        pass
        for sid in sorted(sids):
            payload = {
                "symbol": sid,
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "cfg": chan_cfg_effective(sid),
            }
            fp = os.path.join(_CHAN_CFG_EXPORT_DIR, "chan_cfg_%s.json" % sid)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            os.replace(tmp, fp)
    except OSError:
        pass


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
        sid_defaults = defaults_of(name, sid)
        ov = by.get(sid) or {}
        by_symbol[sid] = {
            "label": label, "code": code, "overrides": ov,
            "defaults": sid_defaults,
            "effective": {k: ov.get(k, sid_defaults[k]) for k in spec["params"]},
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
        saved_at = _read_saved_at()
        sid = _require_symbol(symbol)
        defaults = defaults_of(module, sid)
        merged = dict((overrides.get(module) or {}).get(sid, {}))
        merged.update(clean)
        if (module == "entry"
                and not any(merged.get(k, defaults.get(k))
                            for k in mark_entry.EXIT_MODE_ON_KEYS)):
            raise ValueError("出场方式至少启用一种（锁定盈利/支阻止损/保本止损/背驰周期够笔/"
                             "检测周期够笔/过高低点止盈/跟踪止盈）")
        if module == "chain":
            # 双周期链跨键校验：至少一条链启用；每条启用链的勾选须产生 ≥1 检测周期
            # （链A 勾 3 须同时勾 30S——3 分钟需要 30 秒作下级；链B 勾 5 须同时勾 1）
            eff = dict(CHAIN_DEFAULTS)
            eff.update(merged)
            specs = chain_specs({"chain": eff})
            if not specs:
                raise ValueError("两条周期链均未启用（链A/链B 至少开一条）")
            for sp in specs:
                if not sp["detect"]:
                    raise ValueError(
                        f"链{sp['id']} 勾选的开单周期未产生任何检测周期"
                        f"（勾选 3 须同时勾 30S、勾选 5 须同时勾 1，且至少一个非最细周期被勾选）")
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
        _export_chan_cfg_files()
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
        _export_chan_cfg_files()
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
