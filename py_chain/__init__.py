# -*- coding: utf-8 -*-
"""
py_chain — Python 化缠论回测链路包

运行依赖链：画笔 → 标记买卖点 → 支阻位 → 交易计划 → 进出场。
提供纯函数算法模块（chan_core / mark_buy_sell / sr_flip / trading_plan / mark_entry）、
数据加载（data_loader）、点状回测引擎（backtest）、图表回画（tv_draw）与 CLI 编排（main）。

用法：
    python -m py_chain.main --from 2026-07-02 --draw
"""
