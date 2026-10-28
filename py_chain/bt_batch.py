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

分段并行（2026-10-28·独立段口径快跑模式，用户拍板）：run_symbol_parallel 为
webapp 直接 spawn 的聚合入口——按 (链,段) fan-out 孙进程（run_seg_subproc）槽位
调度，聚合出与 run_symbol 同构的单条 done（webapp 中继零改动）。口径与链式顺序的
差异只在交易层：段边界持仓按段末收盘价 mark-to-market（不强平不交接）、同向互斥/
点尝试/背驰去重不跨段；结构预热与链式同口径（每段独立重建）。合并按固定任务序
（链主序×段时间序）与完成顺序无关=可复现。作参数摸索快跑；定稿请用链式口径复跑。
"""

import os
import time

from . import chan_core, data_store, mark_entry
from . import engine_dispatch
from .backtest import BacktestEngine, merge_run_stats
from .chan_core import fmtT


def run_chain(child_cfg, symbol, task, seg_slice, _send, _log, pause_evt, stop_evt,
              seg_base=None, chained=True, journal_register=True):
    """单链段循环主体（2026-10-28 从 run_symbol 抽出——run_symbol 顺序调用=行为
    逐位不变；run_seg_subproc 以单段切片 + chained=False 调用=分段并行独立段口径）。

    @param seg_slice  本进程要跑的段列表（顺序链式=全部段；并行=单段切片）
    @param seg_base   (全局起始段号, 全局段数)；缺省 (0, len(seg_slice))——journal
                      命名/进度分母/段完成文案用全局编号（并行单段保持全局位次）
    @param chained    True=段间 carry 交接（持仓/单号/计数连续，仅末段结算）；
                      False=独立段口径（每段 carry=None：段末各自 mark-to-market、
                      计数不跨段——与链式非逐位一致，见模块 docstring）
    @param journal_register False=BtJournal(register=False) 只建文件不注册 latest/
                      不清理（分段并行：注册收敛到聚合进程，见 bt_journal.register）
    @return {"stats", "open", "journals", "journalStrategies"}
    """
    from .backtest import exit_cfg_desc
    si0, n_all = seg_base or (0, len(seg_slice))
    cid = task.get("id")
    tag = f"[链{cid}] " if cid else ""
    periods = task["periods"]
    carry = None
    merged = None
    open_trades = []
    journals = []
    jstrs = []
    for li, seg in enumerate(seg_slice):
        if stop_evt.is_set():
            break
        g = si0 + li   # 全局段号（journal 命名/进度/文案）
        seg = seg or {}
        data_from_ts = seg.get("data_from_ts", child_cfg["data_from_ts"])
        seg_to = seg.get("to_ts", child_cfg.get("to_ts"))
        seg_start = seg.get("start_ts", child_cfg.get("start_ts"))
        stag = tag if n_all == 1 else f"{tag}段{g + 1}/{n_all} "
        _log(f"{stag}取数：数据源=本地存储 symbol={symbol} periods={periods} "
             f"from_ts={data_from_ts}"
             f"{' to_ts=' + str(seg_to) if seg_to else ''}")
        bars = data_store.load_store(symbol, periods=periods,
                                     from_ts=data_from_ts, to_ts=seg_to)
        for res in periods:
            n = len(bars.get(res, []) or [])
            if n:
                _log(f"  {res:>4}: {n} 根（{fmtT(bars[res][-1]['time'])} 止）")
        kw = dict(task["engine_kwargs"])
        kw["contract_mult"] = mark_entry.contract_mult_of(symbol)
        engine = BacktestEngine(bars, **kw)
        if g == 0:
            _log(f"{tag}回测开始（最小周期 {engine.fine_res}，成交口径 {engine.fill_mode}，"
                 f"信号模式 {'当下背驰' if engine.signal_mode == 'realtime' else '确认制'}，"
                 f"背驰进场 {'分型确认后下一根开盘' if engine.diverge_confirm else '当下'}，"
                 f"柱缩闸 {'开' if engine.entry_macd_shrink else '关'}，"
                 f"止损下限 {'开' if engine.stop_entry_bar_floor else '关'}，"
                 f"检测周期够笔 {'预期' if engine.expect_bi else '分型确认'}，"
                 f"出场 {exit_cfg_desc(engine.exit_cfg)}）...")
        chain_kw = {"chain": cid} if cid else {}
        # 段>1 时 journal 文件名带段后缀（同一 wall-clock 启动会撞名）；
        # 单段保持旧命名（chanA/chan），旧口径零变化
        jstr = ((f"chan{cid}" if cid else "chan") if n_all == 1 else
                (f"chan{cid}_s{g + 1}of{n_all}" if cid
                 else f"chan_s{g + 1}of{n_all}"))

        def _on_progress(i, total, _cid=cid, _g=g):
            if i % max(1, total // 100) == 0 or i == total:
                if n_all > 1:
                    pct = round(100.0 * (_g + i / total) / n_all, 1)
                else:
                    pct = round(100.0 * i / total, 1) if total else 0
                m = {"kind": "progress", "current": i, "total": total,
                     "pct": pct}
                if _cid:
                    m["chain"] = _cid
                _send(**m)

        run_kw = dict(start_ts=seg_start, log=_log, on_progress=_on_progress,
                      on_signal=lambda s: _send(kind="signal", signal=dict(s, **chain_kw)),
                      on_trade=lambda tr: _send(kind="trade", trade=dict(tr, **chain_kw)),
                      on_exit=lambda tr: _send(kind="exit", trade=dict(tr, **chain_kw)),
                      on_suppressed=lambda s: _send(kind="suppressed", signal=dict(s, **chain_kw)),
                      paused=pause_evt, stopped=stop_evt,
                      journal_symbol=symbol, journal_strategy=jstr,
                      carry=(carry if chained else None))
        if not journal_register:
            from .bt_journal import BtJournal
            run_kw.pop("journal_symbol")
            run_kw.pop("journal_strategy")
            run_kw["journal"] = BtJournal(strategy=jstr, symbol=symbol,
                                          register=False)
        result = engine.run(**run_kw)
        if chained:
            carry = result.get("carryState")
        jp = result.get("journal")
        if jp:
            journals.append(jp)
            jstrs.append(jstr)
            _log(f"{stag}交易日志：{jp}")
        st = result.get("stats") or {}
        merged = merge_run_stats(merged, st)
        n_open = sum(1 for tr in result.get("trades") or []
                     if tr.get("state") != "closed")
        suffix = ("交接下段继续管" if chained and g + 1 < n_all else
                  "按末段口径结算" if chained else "按段末结算（独立段口径）")
        _log(f"{stag}段完成：{st.get('steps', 0)} 步，信号 {st.get('signals', 0)}，"
             f"成交 {st.get('executed', 0)}，已平仓 {st.get('closed', 0)}，"
             f"未平仓 {n_open} 笔（{suffix}）")
        if (not chained) or g + 1 == n_all:
            open_trades.extend(dict(tr, **chain_kw)
                               for tr in result.get("trades") or []
                               if tr.get("state") != "closed")
    return {"stats": merged or {}, "open": open_trades,
            "journals": journals, "journalStrategies": jstrs}


def run_symbol(child_cfg, symbol, msg_q, pause_evt, stop_evt):
    """单品种 headless 全量回测。child_cfg 见 _run_batch：全部为可 pickle 的纯 dict。

    引擎按 child_cfg["strategy"] 分发（2026-09-30：fx_ma = 强分型均线V1）；
    缺省 None = 缠论V1（历史 child_cfg 无该键，行为不变）。

    双周期链（2026-10-23）：chan 传 child_cfg["chains"]=[{id,periods,engine_kwargs}]
    ——逐链独立引擎实例顺序跑（2026-10-28 起链内段循环主体在 run_chain，本函数
    行为不变）：持仓/信号/交易日志按链隔离（journal_strategy=chanA/chanB、事件带
    chain 字段），stats/open/journal 合并为单条 done；无 chains 键的旧 child_cfg
    （engine_kwargs+periods）走原单引擎路径，行为逐位不变。

    长窗自动分段（2026-10-27）：child_cfg["segments"]=[{data_from_ts,to_ts,start_ts}]
    时链内逐段顺序跑（每段独立装载数据+预热，段间 carry 交接完整 trades/互斥槽/
    点尝试与段级计数/当下背驰去重——持仓不断，只有末段末尾结算；journal 文件名带
    _sNofM 段后缀）。缺省 [整窗] 单段=旧口径逐位不变。"""

    def _send(**msg):
        msg["symbol"] = symbol
        msg_q.put(msg)

    def _log(msg):
        _send(kind="log", msg=str(msg))

    try:
        t0 = time.time()
        chan_core.apply_cfg(child_cfg["chan_cfg"])
        chains = child_cfg.get("chains")
        if chains:
            # 长窗自动分段（2026-10-27）：child_cfg["segments"] 由父进程按窗口预算
            # （>split_min_days 均分为 ≤split_seg_days 段，日界对齐；缺省 [None]=整窗
            # 单段，旧 child_cfg 行为逐位不变）。段间持仓连续：carry 携带前段完整
            # trades/互斥槽/点尝试与段级计数/当下背驰去重（见 BacktestEngine.run
            # carry 文档），只有末段末尾 mark-to-market——中间段 open 不进 done.open。
            segments = child_cfg.get("segments") or [None]
            merged = None
            open_trades = []
            journals = []
            for task in chains:
                if stop_evt.is_set():
                    break
                r = run_chain(child_cfg, symbol, task, segments, _send, _log,
                              pause_evt, stop_evt)
                merged = merge_run_stats(merged, r["stats"])
                open_trades.extend(r["open"])
                journals.extend(r["journals"])
            _send(kind="done",
                  stats=merged or {},
                  open=open_trades,
                  stopped=stop_evt.is_set(),
                  duration=round(time.time() - t0, 1),
                  journal=(";".join(journals) if journals else None))
            return

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
        engine_cls = engine_dispatch.engine_class_of(child_cfg.get("strategy"))
        engine = engine_cls(bars, **kw)
        if engine_cls is BacktestEngine:
            from .backtest import exit_cfg_desc
            _log(f"回测开始（最小周期 {engine.fine_res}，成交口径 {engine.fill_mode}，"
                 f"信号模式 {'当下背驰' if engine.signal_mode == 'realtime' else '确认制'}，"
                 f"背驰进场 {'分型确认后下一根开盘' if engine.diverge_confirm else '当下'}，"
                 f"柱缩闸 {'开' if engine.entry_macd_shrink else '关'}，"
                 f"止损下限 {'开' if engine.stop_entry_bar_floor else '关'}，"
                 f"检测周期够笔 {'预期' if engine.expect_bi else '分型确认'}，"
                 f"出场 {exit_cfg_desc(engine.exit_cfg)}）...")
        else:
            _log(f"回测开始（强分型均线V1：周期 {','.join(engine.entry_res)}，"
                 f"最小周期 {engine.fine_res}，止损{engine.stop_pts}/止盈{engine.tp_pts}点）...")
        result = engine.run(
            start_ts=child_cfg.get("start_ts"),
            log=_log,
            on_progress=_on_progress,
            on_signal=lambda s: _send(kind="signal", signal=dict(s)),
            on_trade=lambda tr: _send(kind="trade", trade=dict(tr)),
            on_exit=lambda tr: _send(kind="exit", trade=dict(tr)),
            on_suppressed=lambda s: _send(kind="suppressed", signal=dict(s)),
            paused=pause_evt,
            stopped=stop_evt,
            journal_symbol=symbol,
            journal_strategy=("fxma" if engine_cls is not BacktestEngine else "chan"))
        jp = result.get("journal")
        if jp:
            _log(f"交易日志：{jp}")
        _send(kind="done",
              stats=result.get("stats") or {},
              open=[dict(tr) for tr in result.get("trades") or []
                    if tr.get("state") != "closed"],
              stopped=stop_evt.is_set(),
              duration=round(time.time() - t0, 1),
              journal=jp)
    except Exception as e:   # 无数据/窗口缺周期/引擎异常 → 本品种 error
        _send(kind="error", error=f"{type(e).__name__}: {e}")


def run_seg_subproc(child_cfg, symbol, task, seg_index, msg_q, pause_evt, stop_evt):
    """分段并行（2026-10-28·独立段口径）：单 (链,段) 孙进程入口——run_chain 单段
    切片 chained=False（段末各自结算、计数不跨段）；done 带 chain/seg 字段由聚合
    进程（run_symbol_parallel）按固定任务序合并。journal register=False 只建文件
    （latest.json/清理收敛聚合进程）。progress 消息带 seg 供聚合归因。"""
    def _send(**msg):
        msg["symbol"] = symbol
        if msg.get("kind") == "progress":
            msg["seg"] = seg_index
        msg_q.put(msg)

    def _log(msg):
        _send(kind="log", msg=str(msg))

    t0 = time.time()
    try:
        chan_core.apply_cfg(child_cfg["chan_cfg"])
        segments = child_cfg.get("segments") or [None]
        r = run_chain(child_cfg, symbol, task, [segments[seg_index]],
                      _send, _log, pause_evt, stop_evt,
                      seg_base=(seg_index, len(segments)),
                      chained=False, journal_register=False)
        _send(kind="done", chain=task.get("id"), seg=seg_index,
              stats=r["stats"], open=r["open"], stopped=stop_evt.is_set(),
              duration=round(time.time() - t0, 1),
              journal=(";".join(r["journals"]) if r["journals"] else None),
              journalStrategies=r["journalStrategies"])
    except Exception as e:
        _send(kind="error", chain=task.get("id"), seg=seg_index,
              error=f"{type(e).__name__}: {e}")


def run_symbol_parallel(child_cfg, symbol, msg_q, pause_evt, stop_evt):
    """分段并行聚合入口（2026-10-28·独立段口径快跑模式）：webapp 直接 spawn 本函数
    （与 run_symbol 同签名/同 done 协议，单品种与批量中继零改动）；内部按 (链,段)
    fan-out 孙进程（run_seg_subproc）槽位调度，并行度 = min(任务数, CPU 核数)
    （环境变量 BT_SEG_PARALLEL 覆盖，0/缺省=自动）。

    口径（见模块 docstring）：与链式顺序结果非逐位一致——段边界持仓按段末 mark-to-
    market、计数不跨段；合并按固定任务序（链主序×段时间序）与完成顺序无关=可复现。

    孙进程 journal register=False；本进程收齐后 bt_journal.register 串行补注册
    （latest.json 读改写/清理收敛单侧，防多进程竞态）。任一孙进程 error/崩溃 →
    置 stop 中止其余 → 上报 error（对齐 run_symbol 顶层异常语义）。"""
    import multiprocessing as mp
    import queue as _queue
    from . import bt_journal

    def _send(**msg):
        msg["symbol"] = symbol
        msg_q.put(msg)

    def _log(msg):
        _send(kind="log", msg=str(msg))

    tasks = child_cfg.get("chains") or []
    segments = child_cfg.get("segments") or [None]
    jobs = [(ti, si) for ti in range(len(tasks)) for si in range(len(segments))]
    if len(jobs) < 2:
        return run_symbol(child_cfg, symbol, msg_q, pause_evt, stop_evt)

    def _ti_of(cid):
        for i, t in enumerate(tasks):
            if t.get("id") == cid:
                return i
        return 0

    t0 = time.time()
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    pause_e = ctx.Event()
    stop_e = ctx.Event()
    results = {}          # (ti, si) -> done payload
    pct_by_job = {}       # (ti, si) -> 最新全局 pct（整体进度=均值）
    procs = {}            # id(p) -> {"p":, "job":, "dying":, "stopAt":}
    pending = []
    error = None
    try:
        max_par = int(os.environ.get("BT_SEG_PARALLEL") or 0) or min(
            len(jobs), (os.cpu_count() or 4))
        _log(f"分段并行（独立段口径）：{len(tasks)} 链 × {len(segments)} 段 = {len(jobs)} 个"
             f"任务，并行度 {min(max_par, len(jobs))}（快跑模式：与链式口径有边界差异，"
             f"定稿请关闭本开关复跑）")
        pending = list(jobs)
        last_pct = -1.0
        while pending or procs:
            # 控制下发：父事件镜像到孙进程（与 webapp 中继同口径）
            if pause_evt.is_set():
                pause_e.set()
            else:
                pause_e.clear()
            if stop_evt.is_set():
                stop_e.set()
            # 槽位补充（整体出错/停止后不再新起）
            while pending and len(procs) < max_par and error is None \
                    and not stop_e.is_set():
                ti, si = pending.pop(0)
                p = ctx.Process(
                    target=run_seg_subproc,
                    args=(child_cfg, symbol, tasks[ti], si, q, pause_e, stop_e),
                    name=f"bt-seg-{symbol}-{tasks[ti].get('id') or 'A'}-s{si + 1}",
                    daemon=True)
                p.start()
                procs[id(p)] = {"p": p, "job": (ti, si), "dying": None, "stopAt": None}
                _log(f"段子进程已启动（PID {p.pid}，链{tasks[ti].get('id') or 'A'} "
                     f"段{si + 1}/{len(segments)}）")
            # 消息收发
            got = None
            try:
                got = q.get(timeout=0.2)
            except _queue.Empty:
                pass
            while got is not None:
                kind = got.get("kind")
                if kind == "done":
                    job = (_ti_of(got.get("chain")), int(got.get("seg") or 0))
                    results[job] = got
                    pct_by_job[job] = 100.0
                elif kind == "error":
                    error = (error or f"分段任务失败（链{got.get('chain') or 'A'} "
                                       f"段{int(got.get('seg') or 0) + 1}）："
                                       f"{got.get('error')}")
                    stop_e.set()
                elif kind == "log":
                    _log(got.get("msg", ""))
                elif kind == "progress":
                    job = (_ti_of(got.get("chain")), int(got.get("seg") or 0))
                    pct_by_job[job] = float(got.get("pct") or 0)
                else:
                    _send(**{k: v for k, v in got.items() if k != "seg"})
                if error is not None:
                    stop_e.set()
                try:
                    got = q.get_nowait()
                except _queue.Empty:
                    got = None
            overall = (round(sum(pct_by_job.values()) / len(jobs), 1)
                       if pct_by_job else 0.0)
            if abs(overall - last_pct) >= 0.5:
                _send(kind="progress", current=int(overall), total=100, pct=overall)
                last_pct = overall
            # 进程回收：崩溃兜底（无终结消息死亡=error，2s 等队列 flush）/ stop 5s 终杀
            now = time.time()
            for key in list(procs):
                ent = procs[key]
                p, job = ent["p"], ent["job"]
                if p.is_alive():
                    ent["dying"] = None
                    if stop_e.is_set():
                        if ent["stopAt"] is None:
                            ent["stopAt"] = now
                        elif now - ent["stopAt"] > 5:
                            p.terminate()
                    continue
                if job in results or error is not None:
                    p.join(2)
                    del procs[key]
                    continue
                if ent["dying"] is None:
                    ent["dying"] = now
                elif now - ent["dying"] > 2.0:
                    error = (error or f"段子进程异常退出（链{tasks[job[0]].get('id') or 'A'} "
                                       f"段{job[1] + 1}，exitcode={p.exitcode}）")
                    stop_e.set()
                    p.join(2)
                    del procs[key]
        # 聚合：固定任务序（链主序×段时间序）合并，与完成顺序无关
        if error is not None:
            _send(kind="error", error=error)
            return
        merged = None
        opens = []
        journals = []
        jregs = []
        for ti, si in jobs:
            m = results.get((ti, si))
            if m is None:
                continue   # stop 中断未跑/未完成的段
            merged = merge_run_stats(merged, m.get("stats") or {})
            opens.extend(m.get("open") or [])
            for jp, jstr in zip((m.get("journal") or "").split(";"),
                                m.get("journalStrategies") or []):
                if jp:
                    journals.append(jp)
                    jregs.append((jp, jstr))
        for jp, jstr in jregs:
            try:
                bt_journal.register(jp, jstr, symbol)
            except Exception:
                pass
        _send(kind="done",
              stats=merged or {},
              open=opens,
              stopped=bool(stop_evt.is_set() or stop_e.is_set()),
              duration=round(time.time() - t0, 1),
              journal=(";".join(journals) if journals else None))
    except Exception as e:
        _send(kind="error", error=f"{type(e).__name__}: {e}")
    finally:
        stop_e.set()
        for ent in procs.values():
            p = ent["p"]
            if p.is_alive():
                p.join(5)
                if p.is_alive():
                    p.terminate()
            p.join(2)
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
