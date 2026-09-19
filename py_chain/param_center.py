# -*- coding: utf-8 -*-
"""参数中心：分模块参数的 schema / 校验 / 持久化。

模块清单（与 web/params.html 页签一一对应，顺序对齐工作台六步；支阻位走 sr_presets.json）：
  chan   画笔（成笔相关 CHAN_CFG 子集；内部 id 仍为 chan，避免改 API / 落盘键）
  zs     画中枢（各周期是否绘制、每周期最近保留个数）
  points 标记买卖点（邻近合并/保留数/中枢容差 + divergeDurRatio）
  entry  标记进出场（near/lots/slip_* 与出场门槛 + 进场扩展 CHAN_CFG）
  plan   交易计划（震荡判定阈值 RANGE_DEFAULTS + 顺势参考周期 trendRes）

存储 web/module_params.json，**只存与代码默认不同的键（overrides-only）**：
代码默认值升级后不被旧存量静默覆盖，UI 可据此高亮"已修改"。
默认值单一来源 = 各算法模块常量（CHAN_CFG_DEFAULTS / RANGE_DEFAULTS 等），schema 调用时活取。

生效链路：
  chan/points/entry 中含 CHAN_CFG 键的模块 → 保存后立即 chan_core.apply_cfg(chan_cfg_effective())
  zs/plan → 调用方（webapp Worker / analysis_service / main）读取
           effective_all() 后以显式形参传入（BacktestEngine.module_params 等）。
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
    "gapFilter", "wickRatio", "wickAtrK",
    "nearDoubleAtrK", "nearDoublePct", "nearDoubleLowerRelax", "nearDoubleLowerRatio",
    "debug",
)
POINTS_CHAN_KEYS = ("divergeDurRatio",)
ENTRY_CHAN_KEYS = (
    "sinkFallback", "sinkFallbackRearm",
    "nearEqualAtrK", "nearEqualPct",
    "expectBiEnough", "expectBiMinBars", "divergeConfirm", "macdZeroTol",
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
        "title": "画笔",
        "params": {
            "gapFilter": ("跳空成笔阈值", "相邻K线缺口 ≥ 该值×ATR 时强制独立成笔", 0.0, 5.0),
            "wickRatio": ("长影线比例阈值", "影线占整根K线振幅 ≥ 该比例视为插针（不参与区间竞争）", 0.0, 1.0),
            "wickAtrK": ("长影线长度下限", "影线绝对长度下限 = 该值×TR均值（窄幅小K线免疫）", 0.0, 5.0),
            "nearDoubleAtrK": ("近等双顶容差ATR", "近等双顶/双底平台价差容差（×ATR）", 0.0, 5.0),
            "nearDoublePct": ("近等双顶价差比例", "近等双顶/双底价差下限（价格比例）", 0.0, 0.05),
            "nearDoubleLowerRelax": ("60m双动能容差倍数", "仅60m：15m双动能确认时近等容差放宽倍数", 1.0, 10.0),
            "nearDoubleLowerRatio": ("15m动能衰减比例", "15m柱峰值与DIF幅度须衰减至前段该比例以内", 0.0, 1.0),
            "debug": ("调试打印", "buildBi/买卖点识别过程打印", None, None),
        },
    },
    "zs": {
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
        "title": "标记买卖点",
        "params": {
            "nearAtrRatio": ("邻近合并阈值(×ATR)", "1买与2买价差 ≤ 该值×ATR 时合并为「真1买」", 0.0, 5.0),
            "keep": ("每周期保留标记数", "每周期买卖点标记总数上限（买+卖合并取最近N）", 1, 100),
            "class2ZsTol": ("类2破中枢容差(点)", "类2买/类2卖允许越过中枢边界的绝对点数（0=严格）", 0.0, 1000.0),
            "thirdZsTol": ("3类进中枢容差(点)", "3买/类3买/3卖/类3卖允许进入中枢的绝对点数（0=严格）", 0.0, 1000.0),
            "divergeDurRatio": ("背驰时长可比上限", "两段时长比超过该值时面积项不计入背驰判据", 1, 100),
        },
    },
    "entry": {
        "title": "标记进出场",
        "params": {
            "near": ("近支阻阈值", "背驰点价与支阻位价差 ≤ 该值视为接近（绝对价差，不乘ATR）", 0.1, 1000.0),
            "lots": ("进场手数", "盈亏 = 价格差 × 方向 × 手数", 1, 100),
            "slip_stop": ("止损滑点", "正确侧支阻位外侧偏移（绝对价格）；有效值 = 该值 + ATR系数×ATR(14,背驰周期)", 0.1, 100.0),
            "slip_stop_atr_k": ("止损滑点ATR系数", "有效止损滑点 = 止损滑点 + 该值×ATR(14,背驰周期)；0=关闭", 0.0, 10.0),
            "slip_fallback": ("兜底止损滑点", "无正确侧支阻位时 止损=进场价±该值；有效值 = 该值 + ATR系数×ATR(14,背驰周期)", 0.1, 1000.0),
            "slip_fallback_atr_k": ("兜底滑点ATR系数", "有效兜底滑点 = 兜底止损滑点 + 该值×ATR(14,背驰周期)；0=关闭", 0.0, 10.0),
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
            "macdZeroTol": ("2买卖0轴容差", "2买 DIF > -该值 / 2卖 DIF < +该值 视为动能还在（0=严格 0 轴）", 0.0, 100.0),
        },
    },
    "plan": {
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
                "reboundAngleRef": trading_plan.REBOUND_ANGLE_REF}
    raise ValueError(f"未知参数模块：{module}")


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


def _load():
    """读取持久化 overrides（结构/未知键损坏时静默降级为空，不阻塞启动）。
    兼容旧文件：modules.chan 里已迁出的键归入 points/entry（目标模块已有同键时不覆盖）。
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
        defaults = defaults_of(name)
        # 未知键/已从默认值漂移的旧键丢弃；类型异常静默忽略（文件可手改）
        clean = {}
        for k, v in mod.items():
            if _accept_override(defaults, k, v):
                clean[k] = v
        out[name] = clean
    # 旧 chan 桶：已迁出键 → points / entry（目标已有显式 override 时保留目标）
    legacy = modules.get("chan")
    if isinstance(legacy, dict):
        for dest, keys in (("points", _LEGACY_CHAN_TO_POINTS),
                           ("entry", _LEGACY_CHAN_TO_ENTRY)):
            defaults = defaults_of(dest)
            bucket = out.setdefault(dest, {})
            for k in keys:
                if k in bucket:
                    continue
                v = legacy.get(k)
                if _accept_override(defaults, k, v):
                    bucket[k] = v
    return out


def _save(overrides, saved_at):
    payload = {"version": 1, "modules": {name: overrides.get(name, {})
                                         for name in PARAM_MODULES},
               "savedAt": saved_at}
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


def effective(module):
    """默认值 ∪ overrides（键序稳定：按 schema 顺序）。"""
    eff = dict(defaults_of(module))
    eff.update(_load().get(module, {}))
    return {k: eff[k] for k in PARAM_MODULES[module]["params"]}


def effective_all():
    return {name: effective(name) for name in PARAM_MODULES}


def chan_cfg_effective():
    """从画笔 / 标记买卖点 / 标记进出场拼出完整 CHAN_CFG（供 apply_cfg / --chan-cfg）。"""
    cfg = dict(chan_core.CHAN_CFG_DEFAULTS)
    overrides = _load()
    for keys, mod in ((CHAN_BI_KEYS, "chan"),
                      (POINTS_CHAN_KEYS, "points"),
                      (ENTRY_CHAN_KEYS, "entry")):
        ov = overrides.get(mod) or {}
        for k in keys:
            if k in ov:
                cfg[k] = ov[k]
    return cfg


def snapshot():
    """GET /api/params 响应体：每模块 schema/defaults/overrides/effective/savedAt。"""
    overrides = _load()
    saved_at_all = {}
    try:
        with open(PARAMS_FILE, encoding="utf-8") as f:
            saved_at_all = (json.load(f) or {}).get("savedAt") or {}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    out = {}
    for name, spec in PARAM_MODULES.items():
        defaults = defaults_of(name)
        ov = overrides.get(name, {})
        out[name] = {
            "title": spec["title"],
            "schema": schema_of(name),
            "defaults": defaults,
            "overrides": ov,
            "effective": {k: ov.get(k, defaults[k]) for k in spec["params"]},
            "savedAt": saved_at_all.get(name),
        }
    return out


def update(module, cfg):
    """保存模块参数：规范化 → 合并现有 overrides → 剔除等于默认值的键 → 原子写盘。
    含 CHAN_CFG 键的模块写盘后立即 apply_cfg(chan_cfg_effective())。
    @returns effective(module)
    """
    clean = normalize(module, cfg)
    with _lock:
        overrides = _load()
        defaults = defaults_of(module)
        merged = dict(overrides.get(module, {}))
        merged.update(clean)
        merged = {k: v for k, v in merged.items()
                  if k in defaults and v != defaults[k]}
        saved_at = {}
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                saved_at = (json.load(f) or {}).get("savedAt") or {}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        overrides[module] = merged
        saved_at[module] = time.time()
        _save(overrides, saved_at)
    if module in CHAN_CFG_MODULES:
        chan_core.apply_cfg(chan_cfg_effective())
    return effective(module)


def reset(module):
    """恢复模块默认：清空 overrides 并写盘；含 CHAN_CFG 键的模块重放拼合配置（不清全局）。"""
    if module not in PARAM_MODULES:
        raise ValueError(f"未知参数模块：{module}")
    with _lock:
        overrides = _load()
        overrides.pop(module, None)
        saved_at = {}
        try:
            with open(PARAMS_FILE, encoding="utf-8") as f:
                saved_at = (json.load(f) or {}).get("savedAt") or {}
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        saved_at.pop(module, None)
        _save(overrides, saved_at)
    if module in CHAN_CFG_MODULES:
        chan_core.apply_cfg(chan_cfg_effective())
    return effective(module)
