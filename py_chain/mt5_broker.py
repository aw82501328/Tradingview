# -*- coding: utf-8 -*-
"""EXNESS MT5 执行层：下单/改SL/平仓/持仓/流水，接口窄化为可 mock 的纯方法。

约定（spec/plans/SPEC_live_exness_mt5.md）：
- 本文件与 mt5_feed.py 是全仓唯一允许 `import MetaTrader5` 的文件；
  MockBroker 与模块级纯函数在无终端环境（CI）可用。
- 账号守卫：login/server/trade_mode/margin_mode 逐项校验，netting 账户拒启
  （引擎允许多空并存，netting 会互相对冲）。
- 合约规格全部 spec 驱动（Exness 不同账户类型品种名/规格不同，不硬编码）。
- filling 旋转：symbol_info.filling_mode 位掩码（FOK=1/IOC=2）→ 请求 type_filling，
  retcode 10030（INVALID_FILL）时换下一档重发。
- 改单/下单距离按 trade_stops_level/trade_freeze_level（点数×point）校验并 clamp。
- 对账只认自己 magic 的仓；comment=chai_<direction>_<tradeNo>。
"""

import time
from dataclasses import dataclass, field

from .mt5_feed import server_offset_from_tick

try:
    import MetaTrader5 as mt5
except ImportError:  # CI/无终端：MockBroker 与纯函数可用
    mt5 = None

# 无终端环境的数值兜底（与 MetaTrader5 官方常量一致）
_BUY, _SELL = 0, 1
_ACTION_DEAL, _ACTION_SLTP = 1, 6
_FILL_FOK, _FILL_IOC, _FILL_RETURN = 0, 1, 2
_RC_DONE, _RC_INVALID_STOPS, _RC_INVALID_FILL = 10009, 10016, 10030
_MODE_DEMO, _MODE_REAL = 0, 2
_MARGIN_HEDGING = 2


@dataclass
class OrderResult:
    """下单/改单/平仓结果（统一口径，含失败）。"""
    ok: bool
    retcode: int = 0
    retcomment: str = ""
    position_ticket: int = None
    deal_price: float = None
    deal_volume: float = None
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 规格驱动的纯函数（Mock/实盘共用）
# ---------------------------------------------------------------------------

def normalize_volume(v, spec):
    """按 volume_step 向下取整并夹在 [volume_min, volume_max]。"""
    step = spec["volume_step"] or 0.01
    vmin, vmax = spec["volume_min"], spec["volume_max"]
    n = int(round(v / step, 9))          # 先吃掉浮点噪声（0.03/0.01=2.9999…）
    n = max(n, int(round(vmin / step, 9)))
    n = min(n, int(round(vmax / step, 9)))
    return round(n * step, 8)


def normalize_price(p, spec):
    """按 point 取整到 digits 精度。"""
    pt = spec["point"] or 0.01
    return round(round(p / pt) * pt, 8)


def clamp_sl(ref_price, sl, direction, spec):
    """SL 距离不足 stops_level 时 clamp 到合法最小距离（方向感知）。

    direction="long"：SL 在下方，距离=ref-sl；"short"：SL 在上方，距离=sl-ref。"""
    min_d = (spec.get("stops_level") or 0) * spec["point"]
    if min_d <= 0:
        return sl
    if direction == "long":
        return min(sl, normalize_price(ref_price - min_d, spec))
    return max(sl, normalize_price(ref_price + min_d, spec))


def fld(obj, name):
    """MT5 返回元素的通用字段读取：兼容 np.void（下标式）与 namedtuple（属性式）。
    2026-09-24 真机实测：copy_rates/history_deals 为 numpy 结构化元素（仅下标），
    positions_get 为 namedtuple（仅属性），版本间还可能互变，故两者都试。"""
    try:
        return obj[name]
    except (TypeError, KeyError, IndexError):
        return getattr(obj, name)


def filling_candidates(bitmask):
    """symbol_info.filling_mode 位掩码 → 请求 type_filling 候选序列（FOK→IOC）。"""
    out = []
    if bitmask & 1:
        out.append(_FILL_FOK)
    if bitmask & 2:
        out.append(_FILL_IOC)
    return out or [_FILL_RETURN]


# ---------------------------------------------------------------------------
# MT5Broker：真终端
# ---------------------------------------------------------------------------

class MT5Broker:
    """全部 MT5 交易 API 收口于此。attach 本机已登录终端（密码存终端凭据）。"""

    def __init__(self, symbol="XAUUSD", magic=20260923, deviation_pts=50, log=None):
        self.symbol = symbol
        self.magic = magic
        self.deviation_pts = deviation_pts
        self.log = log or (lambda *a, **k: print(*a))
        self._spec = None
        self._fills = None      # filling 候选缓存
        self._srv_offset = None  # 服务器-UTC 偏移缓存（server_now 用）

    # -- 连接与守卫 -----------------------------------------------------------

    def connect(self, expect_login=None, expect_server=None, require_mode="demo"):
        """attach 终端 + 账号守卫。失配抛 RuntimeError（拒绝启动）。

        require_mode: "demo"=仅模拟户、"real"=仅实盘户、"any"=不限（不推荐）。"""
        if mt5 is None:
            raise RuntimeError("未安装 MetaTrader5 库（pip install MetaTrader5）")
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize 失败：{mt5.last_error()}")
        acc = mt5.account_info()
        if acc is None:
            raise RuntimeError(f"account_info 失败（终端未登录？）：{mt5.last_error()}")
        if expect_login and int(expect_login) != acc.login:
            raise RuntimeError(f"账号守卫：期望 login={expect_login}，实际={acc.login}，拒启")
        if expect_server and expect_server not in acc.server:
            raise RuntimeError(f"账号守卫：期望 server 含『{expect_server}』，"
                               f"实际『{acc.server}』，拒启")
        want = {"demo": _MODE_DEMO, "real": _MODE_REAL}.get(require_mode)
        if want is not None and acc.trade_mode != want:
            raise RuntimeError(f"账号守卫：require_mode={require_mode}，"
                               f"实际 trade_mode={acc.trade_mode}，拒启")
        if acc.margin_mode != _MARGIN_HEDGING:
            raise RuntimeError(f"账号守卫：margin_mode={acc.margin_mode} 非 hedging"
                               f"（netting 会多空对冲），拒启")
        if not mt5.symbol_select(self.symbol, True):
            raise RuntimeError(f"symbol_select {self.symbol} 失败：{mt5.last_error()}")
        return {"login": acc.login, "server": acc.server,
                "trade_mode": acc.trade_mode, "margin_mode": acc.margin_mode,
                "currency": acc.currency, "equity": acc.equity,
                "leverage": acc.leverage}

    def close(self):
        if mt5 is not None:
            mt5.shutdown()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- 规格/行情 -----------------------------------------------------------

    def spec(self):
        """合约规格（缓存；digits/point/volume/stops/filling 位掩码/乘数）。"""
        if self._spec is not None:
            return self._spec
        si = mt5.symbol_info(self.symbol)
        if si is None:
            raise RuntimeError(f"symbol_info({self.symbol}) 失败")
        self._spec = {
            "symbol": si.name, "digits": si.digits, "point": si.point,
            "volume_min": si.volume_min, "volume_step": si.volume_step,
            "volume_max": si.volume_max,
            "stops_level": si.trade_stops_level, "freeze_level": si.trade_freeze_level,
            "filling_bitmask": si.filling_mode,
            "contract_size": si.trade_contract_size,
        }
        self._fills = filling_candidates(si.filling_mode)
        return self._spec

    def quote(self):
        t = mt5.symbol_info_tick(self.symbol)
        if t is None:
            raise RuntimeError(f"symbol_info_tick 失败：{mt5.last_error()}")
        return (t.bid, t.ask)

    def spread(self):
        bid, ask = self.quote()
        return ask - bid

    def account_equity(self):
        acc = mt5.account_info()
        return None if acc is None else acc.equity

    def server_now(self):
        """服务器时间（epoch 秒）——时段阻断窗以服务器时间为准。
        Python 包无 TimeTradeServer（MQL5 专属）：UTC 当前时刻 + 最近 tick 实测
        偏移（整点量化，见 mt5_feed.server_offset_from_tick）；tick 不可用/停盘
        残 tick 时退化为上次偏移（首次为 0，仅影响阻断窗分钟级精度）。"""
        try:
            t = mt5.symbol_info_tick(self.symbol)
        except Exception:               # pragma: no cover - IPC 异常兜底
            t = None
        now = int(time.time())
        off = server_offset_from_tick(
            int(t.time) if (t is not None and t.time) else None,
            now, prev=self._srv_offset)
        if off is not None:
            self._srv_offset = off
        return now + (off or 0)

    # -- 归一化（spec 驱动） --------------------------------------------------

    def normalize_price(self, p):
        return normalize_price(p, self.spec())

    def normalize_volume(self, v):
        return normalize_volume(v, self.spec())

    # -- 交易动作 -------------------------------------------------------------

    def positions(self, magic=None):
        """本品种持仓（默认只认自己 magic）。返回 [{ticket,type,volume,sl,price_open,
        profit,comment,magic,time}]。"""
        magic = self.magic if magic is None else magic
        args = {"symbol": self.symbol}
        if magic:
            args["magic"] = magic  # 部分版本支持按 magic 过滤；不支持时下面兜底
        ps = mt5.positions_get(**args) or []
        # 字段访问走 fld()（np.void 下标式 / namedtuple 属性式，见注释）
        return [p for p in ps if not magic or fld(p, "magic") == magic]

    def market_order(self, direction, volume, sl=None, comment=""):
        """市价单（带 SL；filling 旋转；成交以返回结果实际量价回填）。"""
        self.spec()
        volume = self.normalize_volume(volume)
        bid, ask = self.quote()
        ref = ask if direction == "long" else bid
        if sl is not None:
            sl = clamp_sl(ref, self.normalize_price(sl), direction, self._spec)
        req = {
            "action": _ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": _BUY if direction == "long" else _SELL,
            "price": ref,
            "deviation": self.deviation_pts,
            "magic": self.magic,
            "comment": comment[:31],           # MT5 comment 上限
            "type_time": 0,                     # GTC
            "type_filling": self._fills[0],
        }
        if sl is not None:
            req["sl"] = sl
        last = None
        for fill in self._fills:
            req["type_filling"] = fill
            res = mt5.order_send(req)
            last = res
            if res is None:
                return OrderResult(False, retcode=-1,
                                   retcomment=f"order_send None: {mt5.last_error()}")
            if res.retcode == _RC_INVALID_FILL:
                continue                      # 换下一档 filling 重发
            break
        ok = res.retcode == _RC_DONE
        if not ok:
            self.log(f"[broker] 市价单失败 retcode={res.retcode} {res.comment} req={req}")
        return OrderResult(ok, retcode=res.retcode, retcomment=res.comment,
                           # 5.0.6180 的 OrderSendResult 无 position 字段：对冲账户
                           # 首笔 position id = 开仓 order ticket，用 order 兜底
                           position_ticket=(getattr(res, "position", None)
                                            or getattr(res, "order", None)),
                           deal_price=(res.price or None) if ok else None,
                           deal_volume=(res.volume or None) if ok else None,
                           raw={"request": {k: v for k, v in req.items()},
                                "retcode": res.retcode, "comment": res.comment,
                                "deal": getattr(res, "deal", None),
                                "order": getattr(res, "order", None)})

    def modify_sl(self, position_ticket, sl, clamp=True, retry=3):
        """改持仓 SL（TRADE_ACTION_SLTP）。clamp=True 时按 stops_level 合法化。"""
        spec = self.spec()
        pos = self._position_of(position_ticket)
        direction = "long" if pos["type"] == _BUY else "short"
        sl = self.normalize_price(sl)
        if clamp:
            bid, ask = self.quote()
            ref = bid if direction == "long" else ask
            sl = clamp_sl(ref, sl, direction, spec)
        req = {"action": _ACTION_SLTP, "symbol": self.symbol,
               "position": position_ticket, "sl": sl, "tp": 0.0}
        for i in range(max(1, retry)):
            res = mt5.order_send(req)
            if res is not None and res.retcode == _RC_DONE:
                return OrderResult(True, retcode=res.retcode, retcomment=res.comment,
                                   position_ticket=position_ticket,
                                   raw={"request": dict(req)})
            self.log(f"[broker] 改SL重试 {i + 1}/{retry} retcode="
                     f"{res.retcode if res else -1} {getattr(res, 'comment', '')}")
            time.sleep(0.5)
        return OrderResult(False, retcode=res.retcode if res else -1,
                           retcomment=getattr(res, "comment", ""), position_ticket=position_ticket,
                           raw={"request": dict(req)})

    def close_position(self, position_ticket, volume=None, comment=""):
        """平仓（volume=None 全平；否则部分平，向下归一到 volume_step）。"""
        spec = self.spec()
        pos = self._position_of(position_ticket)
        vol = normalize_volume(pos["volume"] if volume is None else
                               min(volume, pos["volume"]), spec)
        direction = "short" if pos["type"] == _BUY else "long"   # 反向单
        bid, ask = self.quote()
        ref = ask if direction == "long" else bid
        req = {
            "action": _ACTION_DEAL, "symbol": self.symbol, "volume": vol,
            "type": _BUY if direction == "long" else _SELL, "position": position_ticket,
            "price": ref, "deviation": self.deviation_pts, "magic": self.magic,
            "comment": comment[:31], "type_time": 0, "type_filling": self._fills[0],
        }
        for fill in self._fills:
            req["type_filling"] = fill
            res = mt5.order_send(req)
            if res is None:
                return OrderResult(False, retcode=-1,
                                   retcomment=f"order_send None: {mt5.last_error()}")
            if res.retcode == _RC_INVALID_FILL:
                continue
            break
        ok = res.retcode == _RC_DONE
        if not ok:
            self.log(f"[broker] 平仓失败 ticket={position_ticket} vol={vol} "
                     f"retcode={res.retcode} {res.comment}")
        return OrderResult(ok, retcode=res.retcode, retcomment=res.comment,
                           position_ticket=position_ticket,
                           deal_price=(res.price or None) if ok else None,
                           deal_volume=(res.volume or None) if ok else None,
                           raw={"request": {k: v for k, v in req.items()},
                                "retcode": res.retcode})

    def deals_since(self, ts_utc, magic=None):
        """自 ts_utc 起的本品种成交流水（默认只认自己 magic）。"""
        from datetime import datetime, timedelta, timezone
        magic = self.magic if magic is None else magic
        d0 = datetime.fromtimestamp(int(ts_utc), timezone.utc)
        d1 = datetime.now(timezone.utc) + timedelta(hours=1)
        deals = mt5.history_deals_get(d0, d1) or []
        out = []
        for d in deals:
            if fld(d, "symbol") != self.symbol:
                continue
            if magic and fld(d, "magic") != magic:
                continue
            out.append({"ticket": fld(d, "ticket"), "order": fld(d, "order"),
                        "deal_time": int(fld(d, "time")), "entry": fld(d, "entry"),
                        "direction": "long" if fld(d, "type") == _BUY else "short",
                        "volume": fld(d, "volume"), "price": fld(d, "price"),
                        "profit": fld(d, "profit"),
                        "position_id": fld(d, "position_id"),
                        "comment": fld(d, "comment")})
        return out

    # -- 内部 -----------------------------------------------------------------

    def _position_of(self, ticket):
        ps = mt5.positions_get(ticket=ticket)
        if not ps:
            raise RuntimeError(f"持仓不存在：ticket={ticket}")
        p = ps[0]
        return {"ticket": fld(p, "ticket"), "type": fld(p, "type"),
                "volume": fld(p, "volume"), "sl": fld(p, "sl"),
                "price_open": fld(p, "price_open"), "magic": fld(p, "magic"),
                "comment": fld(p, "comment")}


# ---------------------------------------------------------------------------
# MockBroker：测试/演练（无终端）
# ---------------------------------------------------------------------------

class MockBroker:
    """与 MT5Broker 同接口的内存模拟。

    - fill_policy(direction, ref_price) -> 成交价（滑点函数）
    - inject_bar(bar)：用 bar high/low 判各持仓 SL 盘中触发（触发价=SL，价序模拟）
    - fail_queue：依次注入 retcode/异常，驱动失败路径测试
    """

    DEFAULT_SPEC = {
        "symbol": "XAUUSD", "digits": 2, "point": 0.01,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0,
        "stops_level": 0, "freeze_level": 0, "filling_bitmask": 2,  # IOC
        "contract_size": 100.0,
    }

    def __init__(self, symbol="XAUUSD", magic=20260923, deviation_pts=50, log=None,
                 fill_policy=None):
        self.symbol = symbol
        self.magic = magic
        self.deviation_pts = deviation_pts
        self.log = log or (lambda *a, **k: None)
        self.quote_now = (4000.00, 4000.20)
        self.fill_policy = fill_policy or (lambda direction, ref: ref)
        self.fail_queue = []          # 每项: int retcode 或 Exception
        self.positions_store = {}     # ticket -> {ticket,type,volume,sl,price_open,...}
        self.deals = []               # 流水（含进出场）
        self._next_ticket = 700000
        self.equity = 10000.0
        self._spec = dict(self.DEFAULT_SPEC)

    # -- 守卫/规格 ------------------------------------------------------------

    def connect(self, expect_login=None, expect_server=None, require_mode="demo"):
        return {"login": 12345678, "server": "Exness-MT5Trial7 (Mock)",
                "trade_mode": {"demo": _MODE_DEMO, "real": _MODE_REAL}.get(require_mode, 0),
                "margin_mode": _MARGIN_HEDGING, "currency": "USD",
                "equity": self.equity, "leverage": 500}

    def close(self):
        pass

    def spec(self):
        return dict(self._spec)

    def quote(self):
        return self.quote_now

    def spread(self):
        bid, ask = self.quote_now
        return ask - bid

    def account_equity(self):
        return self.equity

    def server_now(self):
        """模拟 EEST（UTC+3）服务器时间。"""
        return int(time.time()) + 3 * 3600

    def set_quote(self, bid, ask):
        self.quote_now = (bid, ask)

    def normalize_price(self, p):
        return normalize_price(p, self._spec)

    def normalize_volume(self, v):
        return normalize_volume(v, self._spec)

    # -- 交易动作 -------------------------------------------------------------

    def _maybe_fail(self):
        if self.fail_queue:
            f = self.fail_queue.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        return None

    def positions(self, magic=None):
        magic = self.magic if magic is None else magic
        return [dict(p) for p in self.positions_store.values() if p["magic"] == magic]

    def market_order(self, direction, volume, sl=None, comment=""):
        rc = self._maybe_fail()
        if rc is not None:
            return OrderResult(False, retcode=rc, retcomment="mock 注入失败")
        volume = self.normalize_volume(volume)
        bid, ask = self.quote_now
        ref = ask if direction == "long" else bid
        price = self.fill_policy(direction, ref)
        if sl is not None:
            sl = clamp_sl(ref, self.normalize_price(sl), direction, self._spec)
        self._next_ticket += 1
        ticket = self._next_ticket
        ptype = _BUY if direction == "long" else _SELL
        self.positions_store[ticket] = {
            "ticket": ticket, "type": ptype, "volume": volume, "sl": sl,
            "price_open": price, "magic": self.magic, "comment": comment,
            "time": int(time.time()), "profit": 0.0,
        }
        self.deals.append({"ticket": ticket, "order": ticket, "deal_time": int(time.time()),
                           "entry": 0, "direction": direction, "volume": volume,
                           "price": price, "profit": 0.0, "position_id": ticket,
                           "comment": comment})
        return OrderResult(True, retcode=_RC_DONE, retcomment="mock done",
                           position_ticket=ticket, deal_price=price, deal_volume=volume)

    def modify_sl(self, position_ticket, sl, clamp=True, retry=3):
        rc = self._maybe_fail()
        if rc is not None:
            return OrderResult(False, retcode=rc, retcomment="mock 注入失败",
                               position_ticket=position_ticket)
        p = self.positions_store.get(position_ticket)
        if p is None:
            return OrderResult(False, retcode=10036, retcomment="持仓不存在",
                               position_ticket=position_ticket)
        direction = "long" if p["type"] == _BUY else "short"
        sl = self.normalize_price(sl)
        if clamp:
            bid, ask = self.quote_now
            ref = bid if direction == "long" else ask
            sl = clamp_sl(ref, sl, direction, self._spec)
        p["sl"] = sl
        return OrderResult(True, retcode=_RC_DONE, retcomment="mock done",
                           position_ticket=position_ticket,
                           raw={"request": {"position": position_ticket, "sl": sl}})

    def close_position(self, position_ticket, volume=None, comment=""):
        rc = self._maybe_fail()
        if rc is not None:
            return OrderResult(False, retcode=rc, retcomment="mock 注入失败",
                               position_ticket=position_ticket)
        p = self.positions_store.get(position_ticket)
        if p is None:
            return OrderResult(False, retcode=10036, retcomment="持仓不存在",
                               position_ticket=position_ticket)
        vol = self.normalize_volume(p["volume"] if volume is None
                                    else min(volume, p["volume"]))
        if vol <= 0:
            return OrderResult(False, retcode=10014, retcomment="无效手数",
                               position_ticket=position_ticket)
        direction = "long" if p["type"] == _BUY else "short"
        bid, ask = self.quote_now
        ref = bid if direction == "long" else ask      # 平多按 bid、平空按 ask
        price = self.fill_policy(("short" if direction == "long" else "long"), ref)
        p["volume"] = round(p["volume"] - vol, 8)
        if p["volume"] <= 0:
            del self.positions_store[position_ticket]
        self.deals.append({"ticket": position_ticket, "order": position_ticket,
                           "deal_time": int(time.time()), "entry": 1,
                           "direction": "short" if direction == "long" else "long",
                           "volume": vol, "price": price, "profit": 0.0,
                           "position_id": position_ticket, "comment": comment})
        return OrderResult(True, retcode=_RC_DONE, retcomment="mock done",
                           position_ticket=position_ticket, deal_price=price,
                           deal_volume=vol)

    def deals_since(self, ts_utc, magic=None):
        magic = self.magic if magic is None else magic
        return [dict(d) for d in self.deals
                if d["deal_time"] >= ts_utc and d["price"] is not None]

    # -- 模拟驱动 -------------------------------------------------------------

    def inject_bar(self, bar):
        """用 bar {high,low} 判各持仓 SL 盘中触发：long 触 low<=SL、short 触 high>=SL。

        触发即按 SL 价全平该仓（记流水，entry=1）；近似：SL 触发时未区分部分仓。"""
        for ticket in list(self.positions_store):
            p = self.positions_store[ticket]
            if p["sl"] is None:
                continue
            hit = (p["type"] == _BUY and bar["low"] <= p["sl"]) or \
                  (p["type"] == _SELL and bar["high"] >= p["sl"])
            if not hit:
                continue
            vol = p["volume"]
            direction = "long" if p["type"] == _BUY else "short"
            del self.positions_store[ticket]
            self.deals.append({"ticket": ticket, "order": ticket,
                               "deal_time": bar.get("time", int(time.time())),
                               "entry": 1,
                               "direction": "short" if direction == "long" else "long",
                               "volume": vol, "price": p["sl"], "profit": 0.0,
                               "position_id": ticket, "comment": "sl hit (mock)"})
