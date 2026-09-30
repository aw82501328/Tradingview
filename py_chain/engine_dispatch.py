# -*- coding: utf-8 -*-
"""策略引擎分发点（module_registry 的 engine 标识 → 引擎类与取数周期）。

回测/回放/监控 Worker（webapp）与 MT5 实盘（live_trader._build_engine）共用，
保证「同一策略在任何入口用同一引擎」。新增策略在此追加 engine 分支即可
（注册表见 module_registry.STRATEGIES）。
"""

from . import module_registry
from .backtest import BacktestEngine
from .fx_ma import FxMaEngine, parse_multi, ENTRY_RES_OPTIONS

# fxma 取数周期：默认链（D/240/60/15/3，上级笔 30S→3→15→60→240 已覆盖 +
# MT5Feed DEFAULT_PERIODS 兼容）+ 30S（entryRes 选中才加）。与 FxMaEngine
# 内部 periods 保持同一口径——取数层多拉无害，少拉会缺上级笔。
DEFAULT_LOAD_PERIODS = ["D", "240", "60", "15", "3"]


def engine_class_of(strategy_id=None):
    """注册表 engine 标识 → 引擎类（未知标识抛 ValueError）。"""
    spec = module_registry.strategy_of(strategy_id)
    engine = spec.get("engine")
    if engine == "chan":
        return BacktestEngine
    if engine == "fx_ma":
        return FxMaEngine
    raise ValueError(f"策略「{spec['title']}」的引擎 {engine!r} 未接入分发点（engine_dispatch）")


def fxma_load_periods(entry_res):
    """fxma 取数周期（默认链 + 30S 选中才加；entry_res 为逗号串或序列）。"""
    sel = parse_multi(entry_res, ENTRY_RES_OPTIONS, "entryRes")
    return DEFAULT_LOAD_PERIODS + (["30S"] if "30S" in sel else [])
