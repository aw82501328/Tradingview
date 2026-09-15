# -*- coding: utf-8 -*-
"""参数中心：分模块参数的 schema / 校验 / 持久化。

模块清单（与 web/params.html 页签一一对应；支阻位走 sr_presets.json 独立预设体系，不在此处）：
  chan   缠论核心 CHAN_CFG（chan_core.apply_cfg 进程内全局生效）
  points 买卖点（compute_all_marks 的 nearAtrRatio/keep）
  entry  进出场（near/lots/slip_* 与出场门槛）
  plan   交易计划（震荡判定阈值 RANGE_DEFAULTS）

存储 web/module_params.json，**只存与代码默认不同的键（overrides-only）**：
代码默认值升级后不被旧存量静默覆盖，UI 可据此高亮"已修改"。
默认值单一来源 = 各算法模块常量（CHAN_CFG_DEFAULTS / RANGE_DEFAULTS 等），schema 调用时活取。

生效链路：
  chan   → 保存后立即 chan_core.apply_cfg（进程内所有 Python 引擎路径自动生效）
  points/entry/plan → 调用方（webapp Worker / analysis_service / main）读取
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

# schema：label/desc 前端展示用；type/min/max 由默认值类型与下表范围决定（校验用）。
# 未列 min/max 的布尔键只做类型校验。
PARAM_MODULES = {
    "chan": {
        "title": "缠论核心",
        "params": {
            "gapFilter": ("跳空成笔阈值", "相邻K线缺口 ≥ 该值×ATR 时强制独立成笔", 0.0, 5.0),
            "wickRatio": ("长影线比例阈值", "影线占整根K线振幅 ≥ 该比例视为插针（不参与区间竞争）", 0.0, 1.0),
            "wickAtrK": ("长影线长度下限", "影线绝对长度下限 = 该值×TR均值（窄幅小K线免疫）", 0.0, 5.0),
            "divergeDurRatio": ("背驰时长可比上限", "两段时长比超过该值时面积项不计入背驰判据", 1, 100),
            "nearDoubleAtrK": ("近等双顶容差ATR", "近等双顶/双底平台价差容差（×ATR）", 0.0, 5.0),
            "nearDoublePct": ("近等双顶价差比例", "近等双顶/双底价差下限（价格比例）", 0.0, 0.05),
            "nearDoubleLowerRelax": ("60m双动能容差倍数", "仅60m：15m双动能确认时近等容差放宽倍数", 1.0, 10.0),
            "nearDoubleLowerRatio": ("15m动能衰减比例", "15m柱峰值与DIF幅度须衰减至前段该比例以内", 0.0, 1.0),
            "sinkFallback": ("M1下沉链回退", "下沉停止级无候选时沿链向上一级重评", None, None),
            "sinkFallbackRearm": ("M1终局后重置去重", "同向持仓终局后重置段去重（同段可再进一次）", None, None),
            "nearEqualAtrK": ("M2近等容差ATR", "创新低/新高近等容差（×ATR；0=关闭，实测负贡献）", 0.0, 5.0),
            "nearEqualPct": ("M2近等容差比例", "近等容差价格比例（0=关闭）", 0.0, 0.05),
            "expectBiEnough": ("M4预期够笔", "末笔反向且端点后够K线即视为回调/反弹中", None, None),
            "expectBiMinBars": ("M4够笔K线数", "预期够笔的本级K线数门槛", 1, 100),
            "divergeConfirm": ("M4背驰确认后成交", "开启=分型右邻K收盘后的下一根开盘成交（默认当下）", None, None),
            "debug": ("调试打印", "buildBi/买卖点识别过程打印", None, None),
        },
    },
    "points": {
        "title": "买卖点",
        "params": {
            "nearAtrRatio": ("邻近合并阈值(×ATR)", "1买与2买价差 ≤ 该值×ATR 时合并为「真1买」", 0.0, 5.0),
            "keep": ("每周期保留标记数", "每周期买卖点标记总数上限（买+卖合并取最近N）", 1, 100),
        },
    },
    "entry": {
        "title": "进出场",
        "params": {
            "near": ("近支阻阈值", "背驰点价与支阻位价差 ≤ 该值视为接近（绝对价差，不乘ATR）", 0.1, 1000.0),
            "lots": ("进场手数", "盈亏 = 价格差 × 方向 × 手数", 1, 100),
            "slip_stop": ("止损滑点", "正确侧支阻位外侧偏移（绝对价格）", 0.1, 100.0),
            "slip_fallback": ("兜底止损滑点", "无正确侧支阻位时 止损=进场价±该值", 0.1, 1000.0),
            "slip_be": ("保本滑点", "保本止损位 = 进场成交K线极值±该值", 0.1, 100.0),
            "exit_min_merged": ("出场成笔预期门槛", "形成段合并后 ≥ 该值根K视为成笔预期（TP2/逆势TP3b触发）", 2, 50),
            "realtime_min_bars": ("当下制够笔K线数", "检测周期形成段 ≥ 该值根原始K才评估（仅回测/监控引擎，JS技能无此参数）", 1, 100),
            "zs_exit_weak_ratio": ("出中枢力度衰减比例", "离开笔幅度 < 进入笔幅度×该值 视为力度变弱（wait1买/卖条件）", 0.1, 5.0),
        },
    },
    "plan": {
        "title": "交易计划",
        "params": {
            "rangeBarN": ("震荡窗口K线数", "最近N根K线的区间判定窗口", 5, 500),
            "rangeBiN": ("震荡最近笔数", "最近N笔端点极差与涨跌交替判定", 2, 50),
            "rangeKMult": ("K线区间阈值(×ATR)", "窗口高低差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBiMult": ("笔端点阈值(×ATR)", "笔端点极差 ≤ 该值×ATR 判震荡", 0.5, 50.0),
            "rangeBreakMult": ("突破跳过阈值(×ATR)", "末笔端点越过窗口另一端 >该值×ATR 视为突破（跳过震荡判定）", 0.0, 10.0),
        },
    },
}


def defaults_of(module):
    """模块默认值（活取各算法模块常量，保证与代码默认不漂移）。"""
    if module == "chan":
        return dict(chan_core.CHAN_CFG_DEFAULTS)
    if module == "points":
        return {"nearAtrRatio": mark_buy_sell.NEAR_ATR_RATIO, "keep": mark_buy_sell.KEEP}
    if module == "entry":
        return {
            "near": mark_entry.NEAR,
            "lots": mark_entry.DEFAULT_LOTS,
            "slip_stop": mark_entry.DEFAULT_SLIP_STOP,
            "slip_fallback": mark_entry.DEFAULT_SLIP_FALLBACK,
            "slip_be": mark_entry.DEFAULT_SLIP_BE,
            "exit_min_merged": mark_entry.EXIT_MIN_MERGED,
            "realtime_min_bars": mark_entry.REALTIME_MIN_BARS,
            "zs_exit_weak_ratio": mark_entry.ZS_EXIT_WEAK_RATIO,
        }
    if module == "plan":
        return dict(trading_plan.RANGE_DEFAULTS)
    raise ValueError(f"未知参数模块：{module}")


def _type_of(default):
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    return "float"


def schema_of(module):
    """前端渲染用 schema：每键 label/desc/type/min/max/default。"""
    defaults = defaults_of(module)
    spec = PARAM_MODULES[module]["params"]
    return {key: {"label": meta[0], "desc": meta[1], "type": _type_of(defaults[key]),
                  "min": meta[2], "max": meta[3], "default": defaults[key]}
            for key, meta in spec.items()}


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


def _load():
    """读取持久化 overrides（结构/未知键损坏时静默降级为空，不阻塞启动）。"""
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
            if k in defaults and isinstance(v, type(defaults[k])):
                clean[k] = v
        out[name] = clean
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
    chan 模块写盘后立即 apply_cfg（进程内全局生效）。
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
    if module == "chan":
        chan_core.apply_cfg(effective("chan"))
    return effective(module)


def reset(module):
    """恢复模块默认：清空 overrides 并写盘；chan 同步 reset_cfg。"""
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
    if module == "chan":
        chan_core.reset_cfg()
    return effective(module)
