# -*- coding: utf-8 -*-
"""模块分层注册表：基础公用组件 vs 交易策略（唯一分层来源）。

两大类（2026-09-29 梳理，见 SPEC.md「模块分层架构」）：
  基础公用组件  BASE_STAGES    画笔/画中枢/标记买卖点/支阻位——所有策略共用的结构计算
  交易策略      STRATEGIES     交易计划+标记进出场为同一策略的两步；当前唯一策略「缠论V1」

本模块为纯常量 + 纯函数，不导入包内其他模块（避免 param_center ↔ analysis_service
循环依赖）；analysis_service / webapp / live_trader / 前端 catalog 均以此为归属口径。

新增策略五步接入（详见 SPEC.md）：实现策略（引擎实现 step_to 接口 + 工作台 JS 技能）
→ 本表加条目 → analysis-catalog.json 加模块条目 + analysis_service.SCRIPTS 映射
→ param_center.PARAM_MODULES 加参数模块 → 引擎分发点按 engine 标识构建。
"""

# 基础公用组件（工作台流程顺序；所有策略共用）
BASE_STAGES = ["bi", "zs", "points"]

# 基础组件内部依赖（策略模块可依赖其中任意项）
BASE_DEPENDENCIES = {"bi": [], "zs": ["bi"], "points": ["bi", "zs"]}

# 交易策略注册表：id → 策略定义
#   title         界面显示名（工作台流程组标题 / 策略下拉选项）
#   stages        工作台流程步骤（按执行顺序；含策略内部依赖，如 entry 依赖 plan）
#   dependencies  策略步骤的依赖（可引用 BASE_STAGES 与策略内前序步骤）
#   param_modules 参数中心归属本策略的模块页签（param_center.PARAM_MODULES 键 + sr）
#   engine        回测/实盘引擎链路标识（当前唯一实现 "chan"=BacktestEngine 现链路；
#                 第二策略到来时在 webapp/live_trader 入口层按此标识分发）
# 注：支阻位(sr)自 2026-09-29 第二轮调整起归属策略（用户口径：支阻参数按策略选方案生效），
# 与 plan/entry 同组；基础组件只留结构计算（笔/中枢/买卖点）。
STRATEGIES = {
    "chan_v1": {
        "title": "缠论V1",
        "stages": ["sr", "plan", "entry"],
        "dependencies": {"sr": ["bi"], "plan": ["bi"], "entry": ["bi", "sr", "plan"]},
        "param_modules": ["sr", "plan", "entry"],
        "engine": "chan",
    },
    # 强分型均线V1（2026-09-30 接入）：缠论买卖点 + 强分型 + 均线分离 + 固定点数止损止盈。
    # 无支阻/计划步骤（不消费）；工作台单步 fxma_entry 依赖基础组件 points 的笔结构。
    # 引擎 fx_ma = fx_ma.FxMaEngine（step_to 接口同 chan，回测/实盘分发点按此构建）。
    "fxma_v1": {
        "title": "强分型均线V1",
        "stages": ["fxma_entry"],
        "dependencies": {"fxma_entry": ["points"]},
        "param_modules": ["fxma"],
        "engine": "fx_ma",
    },
}

DEFAULT_STRATEGY = "chan_v1"


def strategy_ids():
    """全部已注册策略 id。"""
    return list(STRATEGIES)


def strategy_of(strategy_id):
    """取策略定义；未知/空 → ValueError（调用方走各自 400/拒启路径）。"""
    sid = (strategy_id or "").strip() or DEFAULT_STRATEGY
    if sid not in STRATEGIES:
        raise ValueError(f"未知交易策略：{sid}（可选：{', '.join(STRATEGIES)}）")
    return STRATEGIES[sid]


def normalize_strategy(strategy_id):
    """策略 id 规范化：空 → 默认策略；未知 → ValueError。"""
    sid = (strategy_id or "").strip() or DEFAULT_STRATEGY
    strategy_of(sid)
    return sid


def order_of(strategy_id=None):
    """完整流程顺序 = 基础组件 + 指定策略步骤（None/空 = 默认策略）。"""
    strategy = strategy_of(strategy_id)
    return BASE_STAGES + list(strategy["stages"])


def dependencies_of(strategy_id=None):
    """合并依赖表：基础组件内部依赖 + 策略步骤依赖（键集 = order_of）。"""
    strategy = strategy_of(strategy_id)
    merged = dict(BASE_DEPENDENCIES)
    for stage in strategy["stages"]:
        deps = [d for d in strategy["dependencies"].get(stage, []) if d in merged or d in strategy["stages"]]
        merged[stage] = deps
    return merged
