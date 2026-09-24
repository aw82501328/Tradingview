# -*- coding: utf-8 -*-
"""实盘编排层（主进程）：MT5 行情 → 引擎逐拍推进 → 事件镜像下单 → 对账收敛。

总原则（spec/plans/SPEC_live_exness_mt5.md）：
- 引擎=决策真相：backtest.py 零改动，只信 bar；券商 SL=生存保底。
- 镜像执行：信号拍立即市价单（≈引擎「下一开盘」口径）；TP1 保本/TP2 半仓/止损外推
  由持仓 dict 原地状态 diff 捕获后改单/平仓。
- **风控门只挡镜像侧，永不挡 step_to**：门控跳过下单 → trade 标记 shadow=1（引擎照常
  管理生命周期，对账豁免）；同向互斥的 suppressed 信号若已提前下单 → 立即市价平掉
  自愈（记录 undo_entry 偏差）。
- 重启恢复=确定性重放：execute=False 预热到 T_start → execute=True 重放到当前
  （replaying 标志抑制下单）→ 按 live_trades/live_orders 挂接 MT5 ticket → 对账。

用法：py -3.12 -m py_chain.live_trader [--shadow] [--once] [--audit] [--config 路径]
"""

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import chan_core, data_store, live_store, param_center
from .backtest import BacktestEngine, DEFAULT_PERIODS
from .mark_entry import (DEFAULT_SLIP_FALLBACK, DEFAULT_SLIP_STOP,
                         contract_mult_of)
from .mt5_broker import MT5Broker
from .mt5_feed import MT5Feed, DEFAULT_TAIL_M1

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "py_chain", "web", "live_config.json")

DEFAULT_LIVE_CONFIG = {
    "account": {"login": 0, "server": "", "require_mode": "demo"},
    "symbol": "XAUUSD", "db_symbol": "EXNESS:XAUUSD", "magic": 20260923, "lots": 2,
    "risk": {
        "max_volume_per_order": 0.02, "max_total_open_volume": 0.06,
        "max_positions": 2, "day_loss_halt_pct": 3.0, "equity_floor": 0,
        "max_spread_entry": 0.5, "hard_spread_alert": 1.5,
        "entry_session_block_srv": [["21:55", "22:15"]], "weekend": "drop",
        "orphan_policy": "close",
    },
    "broker": {"deviation_pts": 50, "order_retry": 3, "modify_retry": 3,
               "clamp_stops_level": True},
    "run": {"shadow": True, "poll_sec": 10, "replay_start_days_ago": 10,
            "warmup_days": 180, "kill_file": "data/LIVE_KILL",
            "arm_file": "data/LIVE_ARMED", "reconcile_min": 5,
            "param_drift": "refuse", "events_retention_days": 180},
    "notify": {"enabled": False, "channel": "telegram", "chat_id": "",
               "token_env": "LIVE_TG_TOKEN"},
}


# ---------------------------------------------------------------------------
# 配置与纯辅助
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None, cli_overrides=None):
    """默认值 ← live_config.json ← live_config.local.json ← CLI 覆盖。"""
    cfg = dict(DEFAULT_LIVE_CONFIG)
    for p in ([path] if path else [DEFAULT_CONFIG_PATH,
                                   DEFAULT_CONFIG_PATH.replace(".json", ".local.json")]):
        if p and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg = deep_merge(cfg, json.load(f))
    return deep_merge(cfg, cli_overrides or {})


def _abspath(p):
    return p if os.path.isabs(p) else os.path.join(REPO_ROOT, p)


def in_block_windows(srv_ts, windows):
    """服务器时刻是否落在阻断窗（"HH:MM"-"HH:MM"，支持跨午夜）。纯函数。

    srv_ts 为服务器时间 epoch（server_now 实测口径）：按 UTC 渲染即服务器墙上钟，
    不能用本机时区 fromtimestamp。"""
    hm = datetime.fromtimestamp(int(srv_ts), timezone.utc).strftime("%H:%M")
    for w in windows or []:
        if len(w) != 2:
            continue
        a, b = w
        if a <= b:
            if a <= hm <= b:
                return True
        elif hm >= a or hm <= b:      # 跨午夜（如 22:30-01:00）
            return True
    return False


def provisional_sl(direction, bid, ask, near_sr, slip=DEFAULT_SLIP_STOP,
                   fallback=DEFAULT_SLIP_FALLBACK):
    """信号拍临时 SL（引擎 stopRef 在成交拍才冻结）：近支阻 ± 滑点，错误侧兜底
    进场参考价 ± fallback（与 stop_ref_of 同口径）。"""
    if direction == "long":
        cand = (near_sr - slip) if near_sr is not None else None
        if cand is None or cand >= bid:
            cand = bid - fallback
        return cand
    cand = (near_sr + slip) if near_sr is not None else None
    if cand is None or cand <= ask:
        cand = ask + fallback
    return cand


def config_hash(pm, cfg):
    """参数指纹：module_params overrides 原文 + 解析后的实盘配置。漂移即拒自动恢复。"""
    mp_path = os.path.join(REPO_ROOT, "py_chain", "web", "module_params.json")
    mp_raw = ""
    if os.path.exists(mp_path):
        with open(mp_path, encoding="utf-8") as f:
            mp_raw = f.read()
    h = hashlib.sha256()
    h.update(mp_raw.encode("utf-8"))
    h.update(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# ReplayFeed：bars.db 回放（测试/演练，接口与 MT5Feed 对齐）
# ---------------------------------------------------------------------------

class ReplayFeed:
    """按虚拟时钟推进的行情源：tail() 每次把游标推进 step_sec，返回各周期尾部 bars
    （时间 ≤ 游标，含「进行中」末根——与 MT5Feed.tail 语义一致）。"""

    def __init__(self, db_symbol, periods, from_ts, to_ts, step_sec=180, tail_n=60,
                 back_days=120, fine_res="3"):
        self.db_symbol = db_symbol
        self.periods = tuple(periods)
        self.fine_res = fine_res
        self.cursor = int(from_ts)
        self.to_ts = int(to_ts)
        self.step = int(step_sec)
        self.tail_n = tail_n
        # 只载回放窗+回看缓冲（tail 需要各周期末尾 N 根；不拉全史防内存爆炸）
        self._bars = data_store.load_store(db_symbol, self.periods,
                                           int(from_ts) - back_days * 86400, int(to_ts))
        # 时间索引（bisect 定位，避免每拍全列表过滤）
        self._times = {res: [b["time"] for b in bs] for res, bs in self._bars.items()}

    def connect(self):
        return {"replay": True, "from": self.cursor, "to": self.to_ts}

    def close(self):
        pass

    def current_time(self):
        return self.cursor

    def done(self):
        return self.cursor >= self.to_ts

    def tail(self, n_m1=DEFAULT_TAIL_M1, store=False):
        """推进游标并返回各周期尾部 bars：全部周期给已收 bar；fine 周期额外附
        「进行中」bar（平的开盘价——取库中下一根已收 bar 的开盘，供引擎
        fill_at_open_bar 同拍成交；bar 收盘后真实值经 override 修正）。"""
        import bisect
        self.cursor = min(self.cursor + self.step, self.to_ts)
        out = {}
        for res, times in self._times.items():
            i = bisect.bisect_right(times, self.cursor)
            bars = list(self._bars[res][max(0, i - self.tail_n):i])
            if res == self.fine_res and i < len(self._bars[res]) \
                    and self._bars[res][i]["time"] == self.cursor:
                nxt = self._bars[res][i]
                bars.append({"time": nxt["time"], "open": nxt["open"],
                             "high": nxt["open"], "low": nxt["open"],
                             "close": nxt["open"]})
            out[res] = bars
        return out

    def history(self, min_depth_days=365):
        return {}   # 回放数据已在（bar 库直读）

    def staleness_sec(self):
        return 0


# ---------------------------------------------------------------------------
# LiveTrader
# ---------------------------------------------------------------------------

class LiveTrader:
    def __init__(self, cfg, broker, feed, log=None, accept_param_drift=False):
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.log = log or (lambda *a, **k: print(*a))
        self.accept_param_drift = accept_param_drift
        self.session = None
        self.engine = None
        self.replaying = False
        self._stopped = False
        self._pm = None
        self._open = {}            # tradeNo -> engine trade dict（原地变更，供 diff）
        self._snap = {}            # tradeNo -> 状态快照 {beDone,halfDone,stopRef,n_exits}
        self._be_fix = set()       # tradeNo：beStop 已按进场 bar 真实极值校正
        self._pending = []         # 信号拍已下单、等待成交拍挂接的记录
        self._detached = set()     # tradeNo：引擎有仓券商无（宕机期被打掉等）→ 不再镜像
        self._halted_day = None    # 日亏熔断生效日 "YYYY-MM-DD"
        self._last_reconcile = 0.0
        self._last_prune = 0.0
        self._day_guard = None     # 内存缓存（避免每拍读库）
        self._last_ckpt = (0, 0.0) # (fine_last, wall_ts)：fine 未推进且 <60s 跳过写库
        self.pre_tick_hook = None  # 测试钩子：每轮 tick 开头调用（忽略异常）

    # -- 启动守卫序列 ---------------------------------------------------------

    def start(self, once=False):
        c = self.cfg
        rc = c["run"]
        kill = _abspath(rc["kill_file"])
        if os.path.exists(kill):
            self.log(f"[live] 检测到 kill 文件 {kill}，拒绝启动（删除后可重启）")
            return
        live_store.ensure_tables()
        # ① 账号守卫（含实盘双条件）
        require_mode = c["account"]["require_mode"]
        arm = _abspath(rc.get("arm_file") or "data/LIVE_ARMED")
        if require_mode == "real" and not os.path.exists(arm):
            raise RuntimeError(f"实盘开闸双条件：require_mode=real 但 {arm} 不存在，拒启")
        acc = self.broker.connect(c["account"]["login"] or None,
                                  c["account"]["server"] or None, require_mode)
        self.log(f"[live] 账号守卫通过：login={acc['login']} server={acc['server']} "
                 f"mode={acc['trade_mode']} margin={acc['margin_mode']}")
        # ② 规格/手数校验
        spec = self.broker.spec()
        vol = c["lots"] * 0.01
        if vol < spec["volume_min"] or vol > spec["volume_max"]:
            raise RuntimeError(f"手数越界：lots={c['lots']}（{vol} 手）不在 "
                               f"[{spec['volume_min']},{spec['volume_max']}]")
        if c["lots"] % 2 != 0 and not c["risk"].get("allow_odd_lots"):
            raise RuntimeError(f"lots={c['lots']} 为奇数：TP2 半仓（{vol / 2:.3f} 手）"
                               f"低于最小手数，需 half→全平降级才可运行"
                               f"（risk.allow_odd_lots=true 显式接受）")
        if vol / 2 < spec["volume_min"] and not c["risk"].get("allow_odd_lots"):
            raise RuntimeError(f"半仓 {vol / 2:.3f} 手 < volume_min {spec['volume_min']}")
        # ③ 数据源
        finfo = self.feed.connect()
        self.log(f"[live] 行情源就绪：{finfo}")
        # ④ 参数中心（进程内全局 CHAN_CFG 与引擎 module_params 同 webapp 口径）
        self._pm = param_center.effective_all(c.get("symbol"))
        chan_core.apply_cfg(param_center.chan_cfg_effective(c.get("symbol")))
        # ⑤ 会话与参数漂移守卫
        h = config_hash(self._pm, self.cfg)
        st = live_store.load_state("session")
        if st and st.get("config_hash") != h:
            if rc["param_drift"] == "refuse" and not self.accept_param_drift:
                raise RuntimeError(
                    f"参数漂移守卫：session config_hash={st.get('config_hash')} ≠ 当前 "
                    f"{h}。重放会产生与券商持仓对不上的另一组交易；"
                    f"--accept-param-drift 显式放行（将以新参数重建、不挂接旧仓）")
            self.log("[live] 参数漂移已放行：重置会话")
            st = None
        if st:
            self.session = st["id"]
            t_start = int(st["T_start"])
            self.log(f"[live] 恢复会话 {self.session}（T_start="
                     f"{datetime.fromtimestamp(t_start, timezone.utc)}，确定性重放）")
            self._build_engine(t_start, rc["warmup_days"])
            self._restore(t_start)
        else:
            self.session = f"S{int(time.time())}"
            t_start = self.feed.current_time()
            self._build_engine(t_start, rc["warmup_days"])
            init = self.engine.step_to(t_start)      # 预热：忽略历史信号（LiveMonitor 同款）
            if init:
                self.log(f"[live] 预热完成：忽略初始历史信号 {len(init)} 个")
            live_store.save_state("session", {
                "id": self.session, "started_at": int(time.time()), "T_start": t_start,
                "config_hash": h, "symbol": c["symbol"], "lots": c["lots"],
                "shadow": bool(c["run"]["shadow"])})
            live_store.log_event(self.session, "session_start", {
                "account": {k: acc[k] for k in ("login", "server", "trade_mode")},
                "spec": spec, "config_hash": h, "shadow": c["run"]["shadow"]})
        self._refresh_day_guard()
        self.reconcile()
        self.log(f"[live] 就绪：session={self.session} shadow={c['run']['shadow']} "
                 f"lots={c['lots']}（{vol} 手）")
        if once:
            self.tick()
            return
        self.run_forever()

    def _build_engine(self, t_start, warmup_days):
        """从 bars.db 载入 [T_start-预热窗, 当前] 构建引擎（当前=feed.current_time，
        重放恢复时含 T_start~现在的全部已入库 bar——重放即由它们驱动）。"""
        to_ts = self.feed.current_time()
        from_ts = t_start - int(warmup_days) * 86400
        periods = [p for p in DEFAULT_PERIODS]
        bars = data_store.load_store(self.cfg["db_symbol"], periods, from_ts, to_ts)
        if not bars.get("3"):
            # 首启：EXNESS:XAUUSD 库为空 → 深拉（MT5Feed 路径；ReplayFeed 数据已在）
            self.feed.history(min_depth_days=int(warmup_days))
            bars = data_store.load_store(self.cfg["db_symbol"], periods, from_ts, to_ts)
            if not bars.get("3"):
                raise RuntimeError("行情预热失败：3m 无数据")
        n3 = len(bars["3"])
        self.log(f"[live] 预载 {self.cfg['db_symbol']}：3m×{n3}（"
                 f"{datetime.fromtimestamp(bars['3'][0]['time'], timezone.utc)} ~ "
                 f"{datetime.fromtimestamp(bars['3'][-1]['time'], timezone.utc)}）")
        self.engine = BacktestEngine(
            bars, periods=periods, warmup_bars=0, lots=self.cfg["lots"],
            contract_mult=contract_mult_of(self.cfg["symbol"]),
            module_params=param_center.engine_module_params(self._pm),
            fill_at_open_bar=True,   # 逐拍同拍成交（见 backtest.py 注释；批量口径不变）
            fine_res="3")            # 显式固定 fine=3m：OANDA 补深 240/D 后各周期数据
                                    # 跨度不齐，自动探测会误选 240 作时间轴（2026-09-24
                                    # shadow 实测踩中：fine_last 卡在 240 形成桶）

    def _restore(self, t_start):
        """确定性重放恢复：预热到 T_start → execute=True 重放到当前（抑制下单）。"""
        self.engine.step_to(t_start)                    # execute=False 预热
        self.replaying = True
        try:
            r = self.engine.step_to(self.feed.current_time(), execute=True)
            self._consume(r)
        finally:
            self.replaying = False
        self._flush_close_pending()
        # 挂接：重放出的 open trade ↔ live_trades 既有行 ↔ MT5 ticket
        for no, t in self._open.items():
            row = live_store.get_trade(self.session, no)
            if row is None:
                self._alert("restore_missing_row", f"重放 trade#{no} 无镜像行（异常）")
                continue
            if row["position_ticket"]:
                self.log(f"[live] 挂接 trade#{no} → ticket {row['position_ticket']}"
                         f"（state={row['state']} shadow={row['shadow']}）")
            if row["state"] == "detached":
                self._detached.add(no)

    def _flush_close_pending(self):
        """重放结束后补平「宕机窗口内引擎终局」的券商仓（close_pending 行）。

        真实语义：宕机期引擎按 bar 决策了出场，券商仓实际还在 → 重启此刻市价补平，
        成交价与引擎出场价的差异记入 deviation。失败留 close_pending，reconcile 的
        catchup_close 路径兜底重试。"""
        for row in live_store.open_trades(self.session):
            if row["state"] != "close_pending" or not row["position_ticket"]:
                continue
            no = row["engine_trade_no"]
            r = self.broker.close_position(row["position_ticket"],
                                           comment=f"restore_{row.get('exit_type')}")
            live_store.log_order(self.session, "full_close", engine_trade_no=no,
                                 direction=row["direction"],
                                 volume=row["volume_left"],
                                 position_ticket=row["position_ticket"], ok=int(r.ok),
                                 retcode=r.retcode, retcomment="restore_catchup",
                                 deal_price=r.deal_price, deal_volume=r.deal_volume)
            if not r.ok:
                self._alert("restore_close_failed",
                            f"trade#{no} 补平失败 retcode={r.retcode}，reconcile 兜底")
                continue
            dev = None
            if r.deal_price is not None and row.get("engine_pnl") is not None:
                eng_exit = None
                try:
                    eng_exit = (json.loads(row.get("engine_json") or "{}")
                                .get("exitPrice"))
                except Exception:
                    pass
                dev = round(r.deal_price - eng_exit, 3) if eng_exit is not None else None
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "state": "closed", "volume_left": 0.0,
                                     "exit_price": r.deal_price, "deviation": dev})
            self.log(f"[live] trade#{no} 重启补平 @ {r.deal_price}（deviation={dev}）")

    # -- 主循环 ---------------------------------------------------------------

    def run_forever(self):
        rc = self.cfg["run"]
        self.log(f"[live] 进入主循环：poll={rc['poll_sec']}s（Ctrl+C 优雅退出，不平仓）")
        while not self._stopped:
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.log(f"[live] tick 异常（继续轮询）：{e}")
                self._alert("tick_error", str(e))
            time.sleep(float(rc["poll_sec"]))

    def stop(self):
        self._stopped = True

    def tick(self):
        rc = self.cfg["run"]
        if os.path.exists(_abspath(rc["kill_file"])):
            self._stopped = True
            self._alert("kill", f"kill 文件出现，停止新开仓并退出（存量仓位保留，"
                                f"券商侧 SL 兜底）")
            return
        if self.pre_tick_hook:      # 测试钩子（如 MockBroker 报价跟随回放行情）
            try:
                self.pre_tick_hook()
            except Exception as e:
                self.log(f"[live] pre_tick_hook 异常（忽略）：{e}")
        tail = self.feed.tail()
        last_ts = max((bs[-1]["time"] for bs in tail.values() if bs), default=0)
        if not last_ts:
            return
        for res, bars in tail.items():
            fresh = self._fresh_bars(res, bars)
            if fresh:
                self.engine.append_bars(res, fresh)
        r = self.engine.step_to(last_ts, execute=True)
        self._consume(r)
        self._refresh_day_guard()
        self._checkpoint()
        now = time.time()
        if now - self._last_reconcile >= rc["reconcile_min"] * 60:
            self.reconcile()
            self._last_reconcile = now
        if now - self._last_prune >= 86400:
            n = live_store.prune_events(rc.get("events_retention_days", 180))
            if n:
                self.log(f"[live] 清理过期事件 {n} 行")
            self._last_prune = now
        # 行情新鲜度（会话内 >300s 告警 + 停新开仓由门控的 halted 联动）
        st = self.feed.staleness_sec()
        if st is not None and st > 300:
            self._alert("stale_feed", f"行情滞后 {st}s")

    def _fresh_bars(self, res, bars):
        """过滤并入前的 bar 列表（防 no-op 覆盖风暴）：

        append_bars 对「时间==末根」一律按 override 处理并登记整周期重放——回放源
        每拍回喂已收的末根（值未变）会触发全周期重放 + 引擎回退保护清持仓，实盘轮询
        同值 bar 也同理。放行规则：新时间戳 bar 恒放行；同时间仅 fine 周期且值有变化
        放行（进行中 bar 更新/收盘真实值覆盖）；其余周期只收已收 bar（cut 语义本就
        排除进行中 bar，喂形成桶只会白付重放开销）。"""
        times = self.engine._times.get(res) or []
        last_t = times[-1] if times else None
        if last_t is None:
            return list(bars)
        bl = self.engine.bars[res]["_list"]
        fine = str(self.engine.fine_res)
        out = []
        for b in bars:
            if b["time"] > last_t:
                out.append(b)
            elif b["time"] == last_t and str(res) == fine and bl[-1] != b:
                out.append(b)
        return out

    # -- 事件消费 -------------------------------------------------------------

    def _consume(self, r):
        for s in r.get("signals") or []:
            self._on_signal(s)
        for s in r.get("suppressed") or []:
            self._on_suppressed(s)
        for t in r.get("fills") or []:
            self._on_fill(t)
        for t in r.get("exits") or []:
            self._on_exit(t)
        self._diff_states()

    def _identity(self, d):
        return (d.get("direction"), d.get("strategyKey"), d.get("signalTime", d.get("time")))

    def _pop_pending(self, identity):
        for i, p in enumerate(self._pending):
            if p["identity"] == identity:
                return self._pending.pop(i)
        return None

    def _on_signal(self, s):
        """信号拍：记录事件 + 立即市价单（≈引擎「下一开盘」口径）。"""
        kind = "replay_signal" if self.replaying else "signal"
        live_store.log_event(self.session, kind, s, symbol=self.cfg["symbol"])
        if self.replaying:
            return
        identity = self._identity(s)
        d = s["direction"]
        # 风控门（只挡镜像侧）：被挡 → 成交拍落 shadow
        ok, reason = self._entry_gate(d)
        if not ok:
            self._pending.append({"identity": identity, "status": "blocked",
                                  "reason": reason})
            live_store.log_order(self.session, "entry", engine_trade_no=None,
                                 direction=d, volume=self.cfg["lots"] * 0.01,
                                 price=s.get("price"), sl=s.get("nearSr"),
                                 ok=0, retcode=0, retcomment=f"gate:{reason}", shadow=1)
            self._alert("gate_blocked", f"进场被门控拦截（shadow）：{reason} {d} "
                                        f"{s['strategyKey']}")
            return
        bid, ask = self.broker.quote()
        sl = provisional_sl(d, bid, ask, s.get("nearSr"))
        sl = self.broker.normalize_price(sl)
        vol = self.broker.normalize_volume(self.cfg["lots"] * 0.01)
        comment = f"chai_{d}_{s['time']}"
        r = None
        for _ in range(max(1, self.cfg["broker"]["order_retry"])):
            r = self.broker.market_order(d, vol, sl=sl, comment=comment)
            if r.ok:
                break
        if r.ok:
            oid = live_store.log_order(self.session, "entry", direction=d, volume=vol,
                                       price=s.get("price"), sl=sl,
                                       position_ticket=r.position_ticket,
                                       ok=1, retcode=r.retcode, retcomment=r.retcomment,
                                       deal_price=r.deal_price, deal_volume=r.deal_volume,
                                       raw=r.raw)
            self._pending.append({"identity": identity, "status": "ordered",
                                  "order_row_id": oid,
                                  "position_ticket": r.position_ticket,
                                  "deal_price": r.deal_price, "deal_volume": r.deal_volume,
                                  "sl": sl})
            self.log(f"[live] 进场单已发：{d} {vol} 手 SL={sl} ticket={r.position_ticket} "
                     f"成交={r.deal_price}")
        else:
            self._pending.append({"identity": identity, "status": "failed",
                                  "retcode": r.retcode})
            live_store.log_order(self.session, "entry", direction=d, volume=vol,
                                 ok=0, retcode=r.retcode, retcomment=r.retcomment, raw=r.raw)
            self._alert("order_failed", f"进场单失败 retcode={r.retcode}（成交拍将落 shadow）")

    def _on_fill(self, t):
        """成交拍：挂接引擎 trade ↔ 既有订单，SL 对齐到引擎冻结 stopRef。"""
        no = t["tradeNo"]
        live_store.log_event(self.session,
                             "replay_fill" if self.replaying else "fill",
                             {k: t.get(k) for k in ("tradeNo", "direction", "strategyKey",
                                                    "entryTime", "entryPrice", "stopRef",
                                                    "beStop", "lots")},
                             symbol=self.cfg["symbol"])
        self._open[no] = t
        self._snap[no] = self._snapshot(t)
        if not self._fill_on_forming(t):
            self._be_fix.add(no)   # 成交 bar 已是真实值（重放/补拍路径）→ 无需校正
        identity = self._identity(t)
        p = self._pop_pending(identity)   # 重放恢复时 pending 为空 → p=None（只挂接不动券商）
        row = live_store.get_trade(self.session, no)
        vol = round(self.cfg["lots"] * 0.01, 2)
        if row is None:
            shadow = 0 if (p and p.get("status") == "ordered") else 1
            row = live_store.upsert_trade({
                "session": self.session, "engine_trade_no": no,
                "direction": t["direction"], "entry_time": t["entryTime"],
                "entry_signal_time": t["signalTime"], "entry_price": t["entryPrice"],
                "position_ticket": (p or {}).get("position_ticket"),
                "volume_open": vol, "volume_left": vol,
                "engine_stop": t["stopRef"],
                "broker_sl": (p or {}).get("sl") if p and p.get("status") == "ordered" else None,
                "shadow": shadow, "state": "open",
                "engine_json": self._engine_json(t)})
        # 状态迁移恢复（be/half 可能发生在宕机窗口）
        if row["state"] not in ("closed", "detached"):
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "be_done": int(bool(t["beDone"])),
                                     "half_done": int(bool(t["halfDone"])),
                                     "engine_stop": t["stopRef"],
                                     "engine_json": self._engine_json(t)})
        if self.replaying:
            return
        if p is None:
            return   # 重放/异常：不动券商
        if p.get("order_row_id"):
            live_store.update_order_trade_no(p["order_row_id"], no)   # 进场单回填挂接
        if p["status"] == "failed":
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "shadow": 1})
            self._alert("fill_shadow", f"trade#{no} 引擎成交但订单失败 → shadow")
            return
        if p["status"] == "blocked":
            return   # 门控拦截：行已 shadow
        # SL 对齐：临时 SL → 引擎冻结 stopRef
        if t["stopRef"] is not None and p.get("sl") is not None:
            eps = (self.broker.spec()["point"] or 0.01)
            if abs(p["sl"] - t["stopRef"]) > eps:
                m = self.broker.modify_sl(p["position_ticket"], t["stopRef"],
                                          clamp=self.cfg["broker"]["clamp_stops_level"],
                                          retry=self.cfg["broker"]["modify_retry"])
                live_store.log_order(self.session, "sl_modify", engine_trade_no=no,
                                     direction=t["direction"], sl=t["stopRef"],
                                     position_ticket=p["position_ticket"], ok=int(m.ok),
                                     retcode=m.retcode, retcomment=m.retcomment, raw=m.raw)
                if m.ok:
                    live_store.upsert_trade({"session": self.session,
                                             "engine_trade_no": no, "broker_sl": t["stopRef"]})

    def _on_suppressed(self, s):
        """同向互斥被压制的信号：若信号拍已提前下单 → 立即市价平掉自愈。"""
        live_store.log_event(self.session,
                             "replay_suppressed" if self.replaying else "suppressed",
                             s, symbol=self.cfg["symbol"])
        if self.replaying:
            return
        identity = self._identity(s)
        p = self._pop_pending(identity)
        if p and p.get("status") == "ordered" and p.get("position_ticket"):
            r = self.broker.close_position(p["position_ticket"],
                                           comment="undo_suppressed")
            live_store.log_order(self.session, "undo_entry", direction=s["direction"],
                                 position_ticket=p["position_ticket"], ok=int(r.ok),
                                 retcode=r.retcode, retcomment=r.retcomment,
                                 deal_price=r.deal_price, deal_volume=r.deal_volume)
            self._alert("undo_entry", f"引擎互斥压制但券商已进场 → 立即平掉 "
                                      f"ticket={p['position_ticket']}（偏差="
                                      f"{r.deal_price}）")

    def _on_exit(self, t):
        """终局出场：平掉券商剩余仓 + 收口镜像行。"""
        no = t["tradeNo"]
        live_store.log_event(self.session,
                             "replay_exit" if self.replaying else "exit",
                             {k: t.get(k) for k in ("tradeNo", "direction", "exitType",
                                                    "exitTime", "exitPrice", "pnl")},
                             symbol=self.cfg["symbol"])
        row = live_store.get_trade(self.session, no)
        self._open.pop(no, None)
        self._snap.pop(no, None)
        self._detached.discard(no)
        if row is None:
            return
        if self.replaying and row["position_ticket"] and not row["shadow"] \
                and row["state"] not in ("closed", "detached"):
            # 宕机窗口内引擎终局的仓：券商侧仓还在 → 挂 close_pending，
            # 重放结束后立即补平（真实价成交），失败由 reconcile 兜底补平
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "state": "close_pending",
                                     "exit_type": t.get("exitType"),
                                     "exit_time": t.get("exitTime"),
                                     "engine_pnl": t.get("pnl"),
                                     "engine_json": self._engine_json(t)})
            return
        if self.replaying or row["shadow"] or row["state"] in ("closed", "detached"):
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "state": "closed" if row["state"] != "detached" else "detached",
                                     "exit_type": t.get("exitType"),
                                     "exit_time": t.get("exitTime"),
                                     "exit_price": t.get("exitPrice"),
                                     "engine_pnl": t.get("pnl"),
                                     "engine_json": self._engine_json(t)})
            return
        if row["position_ticket"]:
            r = self.broker.close_position(row["position_ticket"],
                                           comment=f"exit_{t.get('exitType')}")
            live_store.log_order(self.session, "full_close", engine_trade_no=no,
                                 direction=t["direction"],
                                 volume=row["volume_left"], position_ticket=row["position_ticket"],
                                 ok=int(r.ok), retcode=r.retcode, retcomment=r.retcomment,
                                 deal_price=r.deal_price, deal_volume=r.deal_volume, raw=r.raw)
            if not r.ok:
                self._alert("close_failed", f"trade#{no} 终局平仓失败 "
                                            f"retcode={r.retcode}，待 reconcile 重试")
                live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                         "exit_type": t.get("exitType"),
                                         "engine_pnl": t.get("pnl")})
                return
            broker_pnl, dev = self._broker_pnl(row, r, t.get("exitPrice"))
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "state": "closed", "volume_left": 0.0,
                                     "exit_type": t.get("exitType"),
                                     "exit_time": t.get("exitTime"),
                                     "exit_price": r.deal_price or t.get("exitPrice"),
                                     "engine_pnl": t.get("pnl"),
                                     "broker_pnl": broker_pnl, "deviation": dev,
                                     "engine_json": self._engine_json(t)})
            self.log(f"[live] trade#{no} 终局平仓 {t.get('exitType')} 券商价="
                     f"{r.deal_price} 引擎价={t.get('exitPrice')} 偏差={dev}")
        else:
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "state": "closed", "exit_type": t.get("exitType"),
                                     "exit_time": t.get("exitTime"),
                                     "exit_price": t.get("exitPrice"),
                                     "engine_pnl": t.get("pnl"),
                                     "engine_json": self._engine_json(t)})

    def _fill_on_forming(self, t):
        """成交是否发生在进行中 bar 上（fill_at_open_bar 同拍成交）：进场 bar 仍是
        fine 末根（其后无更晚 bar）即视为进行中——MT5 实况的部分值形成桶与回放源的
        平开盘桶都成立。重放/批量路径的成交 bar 后面已有更晚 bar（已收真实值）。"""
        import bisect
        fine = self.engine.fine_res
        times = self.engine._times.get(fine) or []
        idx = bisect.bisect_left(times, t.get("entryTime") or 0)
        if idx >= len(times) or times[idx] != t.get("entryTime"):
            return False
        return idx == len(times) - 1     # 仍是末根 → 进行中

    def _maybe_fix_bestop(self, no, t):
        """beStop 收盘校正（fill_at_open_bar 的配套，批量等价）：

        同拍成交时进场 bar 极值未定，引擎按 开盘价±保本滑点 冻结 beStop；bar 收盘
        真实值经 override 并入（不再是末根）后，按 进场bar真实极值±同保本滑点 重算
        （滑点量从 beStop 与开盘价差推导——fill 时 beStop=open∓eff 恒成立）。校正
        写入引擎 trade dict（原地），与 run() 批量口径一致。"""
        import bisect
        if t.get("entryTime") is None or t.get("beStop") is None:
            self._be_fix.add(no)
            return
        fine = self.engine.fine_res
        times = self.engine._times.get(fine) or []
        idx = bisect.bisect_left(times, t["entryTime"])
        if idx >= len(times) or times[idx] != t["entryTime"]:
            return                       # bar 尚未并入，下拍再试
        if idx == len(times) - 1:
            return                       # 仍是末根（进行中），等收盘真实值
        bar = self.engine.bars[fine]["_list"][idx]
        eff = abs(t["beStop"] - bar["open"])
        t["beStop"] = (bar["low"] - eff) if t["direction"] == "long" \
            else (bar["high"] + eff)
        self._be_fix.add(no)
        live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                 "engine_json": self._engine_json(t)})
        self.log(f"[live] trade#{no} beStop 收盘校正 → {t['beStop']:.2f}")

    def _diff_states(self):
        """持仓原地状态 diff：TP1 保本改SL / TP2 半平 / 止损外推改SL。
        无变化不读库（每拍 O(open)内存比较，动作时才落库/下单）。"""
        spec = self.broker.spec()
        eps = spec["point"] or 0.01
        for no, t in list(self._open.items()):
            if no in self._detached:
                continue
            if no not in self._be_fix:
                self._maybe_fix_bestop(no, t)
            snap = self._snap.get(no) or self._snapshot(t)
            eff_stop = t["beStop"] if t["beDone"] else t["stopRef"]
            stop_changed = abs((eff_stop or 0) - (snap.get("eff_stop") or 0)) > eps
            half_changed = t["halfDone"] and not snap.get("halfDone")
            if stop_changed or half_changed:
                row = live_store.get_trade(self.session, no)
                if row and row["state"] in ("closed", "detached"):
                    self._snap[no] = self._snapshot(t)
                    continue
                if stop_changed:
                    self._modify_stop(no, t, row, eff_stop,
                                      reason="be" if t["beDone"] else "extrapolate")
                if half_changed:
                    self._half_close(no, t, row)
            self._snap[no] = self._snapshot(t)

    def _modify_stop(self, no, t, row, sl, reason):
        if row is None or row["shadow"] or not row["position_ticket"]:
            if row is not None:
                live_store.upsert_trade({"session": self.session,
                                         "engine_trade_no": no, "engine_stop": sl})
            return
        m = self.broker.modify_sl(row["position_ticket"], sl,
                                  clamp=self.cfg["broker"]["clamp_stops_level"],
                                  retry=self.cfg["broker"]["modify_retry"])
        live_store.log_order(self.session, "sl_modify", engine_trade_no=no,
                             direction=t["direction"], sl=sl,
                             position_ticket=row["position_ticket"], ok=int(m.ok),
                             retcode=m.retcode, retcomment=f"{reason}:{m.retcomment}",
                             raw=m.raw)
        if m.ok:
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "engine_stop": sl, "broker_sl": sl,
                                     "be_done": int(bool(t["beDone"]))})
            self.log(f"[live] trade#{no} SL→{sl}（{reason}）")
        else:
            self._alert("modify_failed", f"trade#{no} 改SL失败（{reason}）retcode="
                                         f"{m.retcode}，旧 SL 仍在场，下拍重试")

    def _half_close(self, no, t, row):
        if row is None or row["shadow"] or not row["position_ticket"]:
            if row is not None:
                live_store.upsert_trade({"session": self.session,
                                         "engine_trade_no": no, "half_done": 1})
            return
        left = row["volume_left"] or row["volume_open"]
        half = round(left / 2, 2)
        vmin = self.broker.spec()["volume_min"]
        if half < vmin:   # 奇数手降级：全平
            r = self.broker.close_position(row["position_ticket"], comment="half_degrade_full")
            live_store.log_order(self.session, "full_close", engine_trade_no=no,
                                 direction=t["direction"], volume=left,
                                 position_ticket=row["position_ticket"], ok=int(r.ok),
                                 retcode=r.retcode, retcomment="half_degrade_full",
                                 deal_price=r.deal_price, deal_volume=r.deal_volume)
            self._alert("half_degrade", f"trade#{no} 半仓 {half} < {vmin} → 全平降级")
            if r.ok:
                live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                         "half_done": 1, "volume_left": 0.0,
                                         "state": "closed"})
                self._open.pop(no, None)
                self._snap.pop(no, None)
            return
        r = self.broker.close_position(row["position_ticket"], volume=half,
                                       comment="tp2_half")
        live_store.log_order(self.session, "half_close", engine_trade_no=no,
                             direction=t["direction"], volume=half,
                             position_ticket=row["position_ticket"], ok=int(r.ok),
                             retcode=r.retcode, retcomment=r.retcomment,
                             deal_price=r.deal_price, deal_volume=r.deal_volume, raw=r.raw)
        if r.ok:
            live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                     "half_done": 1,
                                     "volume_left": round(left - (r.deal_volume or half), 2)})
            self.log(f"[live] trade#{no} TP2 半平 {half} 手 @ {r.deal_price}")
        else:
            self._alert("half_close_failed", f"trade#{no} 半平失败 retcode={r.retcode}，"
                                             f"待 reconcile 收敛")

    # -- 风控门（只挡镜像侧） ---------------------------------------------------

    def _entry_gate(self, direction):
        rc, rk = self.cfg["run"], self.cfg["risk"]
        if rc["shadow"]:
            return False, "shadow模式"
        if self._halted_day:
            return False, f"日亏熔断({self._halted_day})"
        spread = self.broker.spread()
        if spread > rk["max_spread_entry"]:
            return False, f"点差{spread:.2f}>{rk['max_spread_entry']}"
        srv = self._server_now()
        if srv is not None and in_block_windows(srv, rk.get("entry_session_block_srv")):
            return False, "时段阻断"
        vol_new = self.cfg["lots"] * 0.01
        if vol_new > rk["max_volume_per_order"] + 1e-9:
            return False, f"单笔手数{vol_new}>{rk['max_volume_per_order']}"
        rows = [r for r in live_store.open_trades(self.session) if not r["shadow"]]
        open_vol = sum(r["volume_left"] or 0 for r in rows)
        if open_vol + vol_new > rk["max_total_open_volume"] + 1e-9:
            return False, f"总敞口{open_vol + vol_new:.2f}>{rk['max_total_open_volume']}"
        if len(rows) + 1 > rk["max_positions"]:
            return False, f"持仓数{len(rows) + 1}>{rk['max_positions']}"
        eq = self.broker.account_equity()
        if rk.get("equity_floor") and eq is not None and eq < rk["equity_floor"]:
            return False, f"权益{eq}<{rk['equity_floor']}"
        return True, ""

    def _server_now(self):
        try:
            return self.broker.server_now()
        except AttributeError:
            return None

    def _refresh_day_guard(self):
        rk = self.cfg["risk"]
        if not rk.get("day_loss_halt_pct"):
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day_guard is None or self._day_guard.get("date") != today:
            g = live_store.load_state("day_guard")
            if not g or g.get("date") != today:
                g = {"date": today, "start_equity": self.broker.account_equity()}
                live_store.save_state("day_guard", g)
            self._day_guard = g
            self._halted_day = None
        eq = self.broker.account_equity()
        g = self._day_guard
        if self._halted_day == today or eq is None or not g.get("start_equity"):
            return
        dd = (g["start_equity"] - eq) / g["start_equity"] * 100
        if dd >= rk["day_loss_halt_pct"]:
            self._halted_day = today
            self._alert("day_loss_halt", f"当日权益回撤 {dd:.2f}% ≥ "
                                         f"{rk['day_loss_halt_pct']}%，停新开仓（存量管理到终局）")

    # -- 对账 -----------------------------------------------------------------

    def reconcile(self):
        """引擎持仓（live_trades 镜像）vs 券商持仓收敛。纯判定在 reconcile_plan。"""
        rows = live_store.open_trades(self.session)
        pos = {(p["ticket"]): p for p in self.broker.positions()}
        actions = reconcile_plan(rows, pos, {no: t for no, t in self._open.items()},
                                 detached=self._detached,
                                 orphan_policy=self.cfg["risk"].get("orphan_policy", "close"))
        for a in actions:
            kind = a["action"]
            no = a.get("engine_trade_no")
            if kind == "alert_mismatch":
                self._alert("recon_mismatch", a["detail"])
            elif kind == "catchup_close":
                r = self.broker.close_position(a["ticket"], comment="recon_catchup")
                live_store.log_order(self.session, "full_close", engine_trade_no=no,
                                     position_ticket=a["ticket"], ok=int(r.ok),
                                     retcode=r.retcode, retcomment="recon_catchup",
                                     deal_price=r.deal_price, deal_volume=r.deal_volume)
                if r.ok:
                    live_store.upsert_trade({"session": self.session,
                                             "engine_trade_no": no, "state": "closed",
                                             "volume_left": 0.0})
                self._alert("recon_catchup", f"trade#{no} 引擎已平券商未平 → 补平")
            elif kind == "detach":
                self._detached.add(no)
                live_store.upsert_trade({"session": self.session, "engine_trade_no": no,
                                         "state": "detached"})
                self._alert("recon_detach", f"trade#{no} 券商侧仓已不存在（宕机期 SL 打掉/"
                                            f"手动平）→ 脱钩不再镜像，引擎按自身 bar 继续")
            elif kind == "orphan_close":
                r = self.broker.close_position(a["ticket"], comment="recon_orphan")
                live_store.log_order(self.session, "close_all", engine_trade_no=None,
                                     position_ticket=a["ticket"], ok=int(r.ok),
                                     retcode=r.retcode, retcomment="recon_orphan",
                                     deal_price=r.deal_price, deal_volume=r.deal_volume)
                self._alert("recon_orphan", f"孤儿仓 ticket={a['ticket']} → 平掉")
            elif kind == "alert_missing_row":
                self._alert("recon_missing_row", a["detail"])
        if actions:
            live_store.log_event(self.session, "recon",
                                 {"n_actions": len(actions),
                                  "actions": [{k: a.get(k) for k in ("action",
                                                                     "engine_trade_no",
                                                                     "ticket")}
                                              for a in actions]},
                                 symbol=self.cfg["symbol"])

    # -- 检查点/告警 ------------------------------------------------------------

    def _checkpoint(self):
        fine_ts = 0
        try:
            ts = self.engine.bars.get(self.engine.fine_res, {}).get("_times")
            fine_ts = ts[-1] if ts else 0
        except Exception:
            pass
        # 节流：fine 未推进且距上次写库 <60s 跳过（轮询 10s×6 次只写 1 次）
        now = time.time()
        if fine_ts == self._last_ckpt[0] and now - self._last_ckpt[1] < 60:
            return
        self._last_ckpt = (fine_ts, now)
        live_store.save_state("heartbeat", {"ts": int(now), "fine_last": fine_ts})
        live_store.save_state("engine_cursor", {
            "session": self.session,
            "T_start": (live_store.load_state("session", {}) or {}).get("T_start"),
            "fine_last": fine_ts})

    def _alert(self, kind, text):
        self.log(f"[live][alert] {kind}: {text}")
        live_store.log_event(self.session, "alert", {"kind": kind, "text": text},
                             symbol=self.cfg["symbol"])
        self._notify(f"[{self.cfg['symbol']}] {kind}: {text}")

    def _notify(self, text):
        n = self.cfg.get("notify") or {}
        if not n.get("enabled"):
            return
        token = os.environ.get(n.get("token_env") or "LIVE_TG_TOKEN", "")
        chat = n.get("chat_id")
        if not token or not chat:
            return
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode(),
                timeout=5)
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            self.log(f"[live] 通知发送失败（忽略）：{e}")

    # -- 辅助 -----------------------------------------------------------------

    @staticmethod
    def _snapshot(t):
        return {"beDone": bool(t.get("beDone")), "halfDone": bool(t.get("halfDone")),
                "stopRef": t.get("stopRef"),
                "eff_stop": t.get("beStop") if t.get("beDone") else t.get("stopRef"),
                "n_exits": len(t.get("exits") or []),
                "state": t.get("state")}

    @staticmethod
    def _engine_json(t):
        try:
            return json.dumps(t, ensure_ascii=False, default=str)
        except Exception:
            return None

    def _broker_pnl(self, row, close_result, engine_exit_price=None):
        """券商口径盈亏 = 全部平仓 deal 的 (价-进场)×方向×量×合约乘数（近似不含库存费）。
        deviation = 终局平仓 deal 价 − 引擎终局价（滑点观测）。"""
        mult = contract_mult_of(self.cfg["symbol"])
        d = 1 if row["direction"] == "long" else -1
        entry = row["entry_price"]
        deals = [o for o in live_store.orders_of(self.session, row["engine_trade_no"])
                 if o["action"] in ("half_close", "full_close", "undo_entry")
                 and o.get("ok") and o.get("deal_price")]
        pnl = sum((o["deal_price"] - entry) * d * (o["deal_volume"] or 0) * 100 * mult
                  for o in deals)
        dev = None
        if close_result.deal_price is not None and engine_exit_price is not None:
            dev = round(close_result.deal_price - engine_exit_price, 3)
        return round(pnl, 2), dev


def reconcile_plan(rows, broker_pos, engine_open, detached=(), orphan_policy="close"):
    """对账判定（纯函数）：输入镜像行/券商持仓/引擎 open trade，产出动作表。

    动作：alert_mismatch / catchup_close / detach / orphan_close / alert_missing_row。
    ticket 归属判定含 shadow/detached 行（被任何行引用的仓不算孤儿）。"""
    actions = []
    by_ticket = dict(broker_pos)
    claimed = {r["position_ticket"] for r in rows if r.get("position_ticket")}
    for row in rows:
        no = row["engine_trade_no"]
        if no in detached or row["state"] == "detached":
            continue
        ticket = row["position_ticket"]
        eng_open = no in engine_open
        if row["shadow"] or not ticket:
            continue
        p = by_ticket.pop(ticket, None)
        if p is not None:
            if not eng_open:      # 引擎已终局但券商还有仓（平仓失败遗留）→ 补平
                actions.append({"action": "catchup_close", "engine_trade_no": no,
                                "ticket": ticket})
            else:
                exp = round(row["volume_left"] or row["volume_open"] or 0, 2)
                if abs((p["volume"] or 0) - exp) > 0.005:
                    actions.append({"action": "alert_mismatch",
                                    "detail": f"trade#{no} 量不齐：券商 {p['volume']} vs "
                                              f"镜像 {exp}", "engine_trade_no": no,
                                    "ticket": ticket})
        else:
            if eng_open:          # 引擎有仓、券商无（宕机期 SL 打掉/手动平）→ 脱钩
                actions.append({"action": "detach", "engine_trade_no": no, "ticket": ticket})
            # 引擎已平 + 券商已无 = 一致（终局平仓成功）
    # 孤儿仓：券商有、无任何镜像行引用（shadow/detached 引用也算已认领）
    for ticket in sorted(set(by_ticket) - claimed):
        if orphan_policy == "close":
            actions.append({"action": "orphan_close", "ticket": ticket})
        else:
            actions.append({"action": "alert_mismatch",
                            "detail": f"孤儿仓 ticket={ticket}（policy={orphan_policy}）"})
    # 引擎 open 但无镜像行（异常）
    row_nos = {r["engine_trade_no"] for r in rows}
    for no in engine_open:
        if no not in row_nos:
            actions.append({"action": "alert_missing_row",
                            "detail": f"引擎 open trade#{no} 无镜像行"})
    return actions


# ---------------------------------------------------------------------------
# 审计（--audit：事件↔动作 1:1 校验）
# ---------------------------------------------------------------------------

def audit(session, log=print):
    """比对引擎事件（fill/suppressed/exit/be/half）与 live_orders 动作的 1:1 不变量。"""
    events = live_store.recent_events(session, n=100000)
    trades = live_store.all_trades(session)
    orders = live_store.orders_of(session)
    fills = [e for e in events if e["kind"] == "fill"]
    exits = [e for e in events if e["kind"] == "exit"]
    entry_orders = [o for o in orders if o["action"] == "entry"]
    close_orders = [o for o in orders if o["action"] in ("full_close", "undo_entry")]
    mismatches = []
    if len(fills) != len(trades):
        mismatches.append(f"fill事件{len(fills)} ≠ 镜像行{len(trades)}")
    shadow_rows = [t for t in trades if t["shadow"]]
    non_shadow = [t for t in trades if not t["shadow"]]
    if len(entry_orders) < len(non_shadow):
        mismatches.append(f"非shadow行{len(non_shadow)} > 进场单{len(entry_orders)}")
    for t in trades:
        if t["state"] == "closed" and not t["shadow"] and t["position_ticket"]:
            has_close = any(o["engine_trade_no"] == t["engine_trade_no"]
                            and o["action"] in ("full_close", "half_close")
                            for o in orders)
            if not has_close:
                mismatches.append(f"trade#{t['engine_trade_no']} closed 但无平仓单")
    report = {"session": session, "events": len(events), "fills": len(fills),
              "exits": len(exits), "trades": len(trades),
              "shadow_trades": len(shadow_rows), "orders": len(orders),
              "entry_orders": len(entry_orders), "close_orders": len(close_orders),
              "mismatches": mismatches, "ok": not mismatches}
    log(json.dumps(report, ensure_ascii=False, indent=2))
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="EXNESS MT5 实盘交易（live_trader）")
    ap.add_argument("--config", default=None, help="配置路径（默认 web/live_config.json）")
    ap.add_argument("--shadow", dest="shadow", action="store_true", default=None,
                    help="强制 shadow 模式（只记录不下单）")
    ap.add_argument("--no-shadow", dest="shadow", action="store_false",
                    help="强制真实下单（覆盖配置）")
    ap.add_argument("--once", action="store_true", help="只跑一轮 tick（联调用）")
    ap.add_argument("--audit", action="store_true", help="审计会话事件↔动作一致性")
    ap.add_argument("--accept-param-drift", action="store_true",
                    help="放行参数漂移（将以新参数重建会话、不挂接旧仓）")
    ap.add_argument("--kill-close", action="store_true",
                    help="平掉本 magic 全部持仓后退出（应急）")
    ap.add_argument("--feed", choices=["mt5", "replay"], default="mt5")
    ap.add_argument("--replay-from", type=int, default=None,
                    help="replay feed 起点（UTC epoch 秒，测试用）")
    ap.add_argument("--replay-to", type=int, default=None)
    ap.add_argument("--log-file", default=None, help="日志文件（默认 data/live_trader.log）")
    args = ap.parse_args(argv)

    if args.audit:
        sess = live_store.load_state("session", {})
        if not sess or not sess.get("id"):
            print("无会话")
            return
        audit(sess["id"])
        return

    cli = {}
    if args.shadow is not None:
        cli = {"run": {"shadow": args.shadow}}
    cfg = load_config(args.config, cli)
    log_path = _abspath(args.log_file or "data/live_trader.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    lf = open(log_path, "a", encoding="utf-8")

    def log(*a):
        line = " ".join(str(x) for x in a)
        print(line)
        lf.write(f"{datetime.now(timezone.utc).isoformat()} {line}\n")
        lf.flush()

    broker = MT5Broker(symbol=cfg["symbol"], magic=cfg["magic"],
                       deviation_pts=cfg["broker"]["deviation_pts"], log=log)
    if args.feed == "replay":
        assert args.replay_from and args.replay_to, "--replay-from/to 必填"
        feed = ReplayFeed(cfg["db_symbol"], DEFAULT_PERIODS,
                          args.replay_from, args.replay_to)
    else:
        feed = MT5Feed(symbol=cfg["symbol"], weekend=cfg["risk"]["weekend"],
                       db_symbol=cfg["db_symbol"], log=log)
    trader = LiveTrader(cfg, broker, feed, log=log,
                        accept_param_drift=args.accept_param_drift)
    if args.kill_close:
        broker.connect(cfg["account"]["login"] or None, cfg["account"]["server"] or None,
                       cfg["account"]["require_mode"])
        for p in broker.positions():
            r = broker.close_position(p["ticket"], comment="kill_close")
            log(f"[live] kill-close ticket={p['ticket']} vol={p['volume']} ok={r.ok}")
        return
    try:
        trader.start(once=args.once)
    finally:
        feed.close()
        broker.close()
        lf.close()


if __name__ == "__main__":
    main()
