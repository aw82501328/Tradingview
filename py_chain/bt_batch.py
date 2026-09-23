# -*- coding: utf-8 -*-
"""批量回测子进程入口（Windows spawn）。

多品种并行（2026-09-23）：父进程（webapp.BacktestWorker._run_batch）把一切配置
预计算成纯 dict（chan_cfg / engine_kwargs / periods / 时间窗）传入，本模块只做
apply_cfg → load_store → BacktestEngine → run，事件经共享 mp.Queue 流式回传：

    {"kind": "log|progress|signal|trade|exit|suppressed|done|error", "symbol": ..., ...}

约束与要点：
- 模块级函数才可被 spawn pickle（target=bt_batch.run_symbol）。
- 子进程不 close 共享 Queue（父进程在全部子进程退出后统一 close/join_thread）。
- 引擎零改动：paused/stopped 直传 mp.Event（引擎只调 is_set()，接口兼容）。
- CHAN_CFG 是进程级全局参数 → 子进程各自 apply_cfg 即天然隔离。
- 顶层异常 → error 消息（无数据/窗口缺周期/引擎异常只影响本品种，不影响其他品种）。
"""

import time

from . import chan_core, data_store, mark_entry
from .backtest import BacktestEngine
from .chan_core import fmtT


def run_symbol(child_cfg, symbol, msg_q, pause_evt, stop_evt):
    """单品种 headless 全量回测。child_cfg 见 _run_batch：全部为可 pickle 的纯 dict。"""

    def _send(**msg):
        msg["symbol"] = symbol
        msg_q.put(msg)

    def _log(msg):
        _send(kind="log", msg=str(msg))

    try:
        t0 = time.time()
        chan_core.apply_cfg(child_cfg["chan_cfg"])
        periods = child_cfg["periods"]
        _log(f"取数：数据源=本地存储 symbol={symbol} periods={periods} "
             f"from_ts={child_cfg['data_from_ts']}"
             f"{' to_ts=' + str(child_cfg['to_ts']) if child_cfg.get('to_ts') else ''}")
        bars = data_store.load_store(symbol, periods=periods,
                                     from_ts=child_cfg["data_from_ts"],
                                     to_ts=child_cfg.get("to_ts"))
        for res in periods:
            n = len(bars.get(res, []) or [])
            if n:
                _log(f"  {res:>4}: {n} 根（{fmtT(bars[res][-1]['time'])} 止）")

        def _on_progress(i, total):
            if i % max(1, total // 100) == 0 or i == total:
                _send(kind="progress", current=i, total=total,
                      pct=round(100.0 * i / total, 1) if total else 0)

        # 合约乘数防御性重算（2026-09-23：1手=0.01标准手）——纯函数不读参数文件，
        # 不违子进程约束；手数已在父进程 _run_batch 循环按品种解析进 engine_kwargs
        kw = dict(child_cfg["engine_kwargs"])
        kw["contract_mult"] = mark_entry.contract_mult_of(symbol)
        engine = BacktestEngine(bars, **kw)
        _log(f"回测开始（最小周期 {engine.fine_res}，成交口径 {engine.fill_mode}，"
             f"信号模式 {'当下背驰' if engine.signal_mode == 'realtime' else '确认制'}，"
             f"背驰进场 {'分型确认后下一根开盘' if engine.diverge_confirm else '当下'}，"
             f"柱缩闸 {'开' if engine.entry_macd_shrink else '关'}，"
             f"止损下限 {'开' if engine.stop_entry_bar_floor else '关'}，"
             f"检测周期够笔 {'预期' if engine.expect_bi else '分型确认'}）...")
        result = engine.run(
            start_ts=child_cfg.get("start_ts"),
            log=_log,
            on_progress=_on_progress,
            on_signal=lambda s: _send(kind="signal", signal=dict(s)),
            on_trade=lambda tr: _send(kind="trade", trade=dict(tr)),
            on_exit=lambda tr: _send(kind="exit", trade=dict(tr)),
            on_suppressed=lambda s: _send(kind="suppressed", signal=dict(s)),
            paused=pause_evt,
            stopped=stop_evt)
        _send(kind="done",
              stats=result.get("stats") or {},
              open=[dict(tr) for tr in result.get("trades") or []
                    if tr.get("state") != "closed"],
              stopped=stop_evt.is_set(),
              duration=round(time.time() - t0, 1))
    except Exception as e:   # 无数据/窗口缺周期/引擎异常 → 本品种 error
        _send(kind="error", error=f"{type(e).__name__}: {e}")
