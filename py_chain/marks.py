# -*- coding: utf-8 -*-
"""
信号列表进场点标记（Web 控制台专用）

把「进场信号记录」表格里的全部信号，按各自背驰周期（markRes）画到
TradingView 图表对应周期K线上（箭头锚点为背驰点 time，已对齐 markRes K线起点）：
  - 做多 = 向上箭头（arrow_up，文本 ML·BUY + 价）
  - 做空 = 向下箭头（arrow_down，文本 ML·SELL + 价）
颜色可由调用方指定（默认买红 #F23645、卖绿 #089981）。

标记的周期可见性（intervalsVisibilities）= 背驰周期（markRes）+ 检测周期
（periodX）的并集：同一箭头/横线在两个周期的图表上同时显示（同类周期合并为
连续区间时，中间周期也会显示，如 3m+15m → 5m/10m 可见）；无法识别周期时
保持默认不限。

出场标记（行内带 exits 出场事件时追加，统一灰 #787B86，同款双周期显示）：
  - 与进场同款箭头，方向=平仓方向：多头出场（平多）= 向下箭头 ↓、空头出场（平空）= 向上箭头 ↑
    （文本 ML·止损/保损/全平/半平 + 价；与进场红/绿箭头用灰色区分）
  - 保本/仍持仓不画图；同向过滤（status=同向过滤）的信号不画箭头。

与回测 BT·（tv_draw）、实时 RT·（monitor）标记隔离：
使用独立前缀 ML· 与独立 localStorage 键 mark_list_ids，
删除时只删自己创建的标记，不影响用户图形与其它标记。

支阻横线（「标记支阻位」按钮，draw_sr_marks）：把信号列表「近支阻」（nearSr）
画成全宽横线（shape='horizontal_line' 单点锚定，天然横贯全图、不限长度，锚=进场点
K线），默认灰可调，双周期显示。前缀 ML·SR 与独立键 mark_sr_ids —— 重画横线只清
上次的横线，重画箭头（ML·）只清箭头，两个标记按钮互不清除。「删除标记」按钮
（clear_all_marks）则把 ML·（含横线）/BT·/RT· 全部系统标记一次清空。

单行标记（点击信号行随定位一起画，draw_single_mark）：该行的进场箭头 + 出场
箭头 + 近支阻全宽横线，前缀 ML·单 / ML·单SR、独立键 mark_single_ids；替换
语义——每次点击先清上一次的单行标记，始终只保留最近点击一行。「标记进出场」
「删除标记」会一并清掉单行标记（前缀同属 ML· 族）。
"""

import json
import math
import time

from .chan_core import intervalSecOf, fmtT
from .data_loader import CDPClient, CDPConfig
from .monitor import _ensure_res, RES_WAIT

# 箭头前缀与 localStorage 键（与 tv_draw / monitor 隔离）
MARK_PREFIX = "ML·"
IDS_KEY = "mark_list_ids"
CHUNK = 50

# 支阻横线（「标记支阻位」按钮）：独立前缀/键，与箭头按钮互不清除。
# 横线 = 近支阻价位（nearSr）的全宽横线（shape='horizontal_line' 单点锚定，
# 天然横贯全图、不限长度），锚=进场点K线，双周期（markRes+periodX）显示，
# 1px 默认灰（可调）。
SR_PREFIX = "ML·SR"
SR_IDS_KEY = "mark_sr_ids"
SR_CHUNK = 50
DEFAULT_SR_COLOR = "#787B86"

# 单行标记（点击信号行随定位一起画，draw_single_mark）：前缀 ML·单 仍属 ML·
# 前缀族——「标记进出场」重画（_clear_marks 兜底扫 ML·）与「删除标记」
# （clear_all_marks 前缀 ML·）都会一并清掉；替换清除只扫 ML·单（含 ML·单SR
# 支阻横线），不碰批量 ML· 箭头与 ML·SR 横线（ML·单SR 不以 ML·SR 开头）。
SINGLE_PREFIX = "ML·单"
SINGLE_IDS_KEY = "mark_single_ids"

# 默认颜色：做多红、做空绿（与 tv_draw / monitor 约定一致）；出场默认黄（可调，
# 与进场红/绿箭头区分：平多 ↓ / 平空 ↑）
DEFAULT_BUY_COLOR = "#F23645"
DEFAULT_SELL_COLOR = "#089981"
DEFAULT_EXIT_COLOR = "#FFEB3B"

# 出场事件 → 是否绘制与文本名。
# 出场点绘制（用户规则 2026-09-06）：与进场同款箭头（createShape），方向 = 平仓方向
# （多头出场 ↓ / 空头出场 ↑），统一灰（DEFAULT_EXIT_COLOR），只在本背驰周期显示。
# breakeven（保本）/ 仍持仓不在集合内 → 仅落盘不画图。
EXIT_SHAPES = {"stopSr": True, "stopBe": True, "close": True, "half": True}
EXIT_NAMES = {"stopSr": "止损", "stopBe": "保损", "close": "全平", "half": "半平"}


def _colors(colors=None):
    """规范化颜色配置：返回 {'buy': '#...', 'sell': '#...', 'exit': '#...', 'sr': '#...'}。"""
    colors = colors or {}
    return {
        "buy": colors.get("buy") or DEFAULT_BUY_COLOR,
        "sell": colors.get("sell") or DEFAULT_SELL_COLOR,
        "exit": colors.get("exit") or DEFAULT_EXIT_COLOR,
        "sr": colors.get("sr") or DEFAULT_SR_COLOR,
    }


_IV_CATEGORIES = ("seconds", "minutes", "hours", "days", "weeks", "months")


def _iv_descriptor(res):
    """单个周期 → (category, from, to)；无法限定时 → None。

    category ∈ seconds/minutes/hours/days；from/to 为该类周期数值范围
    （日线 days 不带范围 → (None, None)，与旧输出「days: true 不带 From/To」一致）；
    W/M/空/无法识别 → None（保持默认不限）。
    """
    res = str(res or "").strip()
    if not res:
        return None
    up = res.upper()
    if up in ("D", "1D"):
        return ("days", None, None)
    if up in ("W", "1W", "M", "1M"):
        # 周/月线暂无对应范围字段约定，保持默认不限
        return None
    if "S" in up and up[:-1].isdigit():
        # 秒级周期（如 30S）：仅显示在该秒级图表上（minutes=0 会误返回 None 导致全周期可见）
        s = int(up[:-1])
        return ("seconds", s, s)
    if res.isdigit():
        minutes = int(res)
    else:
        sec = intervalSecOf(res) or 0
        minutes = sec // 60 if sec else 0
    if minutes <= 0:
        return None
    if minutes < 60:
        # 分钟级周期（如 3m/5m/15m/30m）
        return ("minutes", minutes, minutes)
    if minutes < 1440:
        # 小时级周期（TV 将 60m/240m 归入 hours 类）
        return ("hours", minutes // 60, minutes // 60)
    # 日线及以上
    return ("days", None, None)


def _iv_merged(res):
    """周期并集计算：[markRes, periodX] → {category: (from, to)}；无法限定 → None。

    首个周期（markRes，锚定周期）无法识别 → None（保持默认不限，绝不退化成
    只限后面的 periodX）；后续项无法识别（如缺 periodX）则丢弃，退化为单周期。
    同类周期合并：from=min、to=max；任一侧不带范围（如日线 days）→ 合并后仍不限。
    """
    if isinstance(res, (list, tuple)):
        res_list = list(res)
    else:
        res_list = [res]
    descs = [_iv_descriptor(r) for r in res_list if str(r or "").strip()]
    if not descs or descs[0] is None:
        return None
    merged = {}
    for d in descs:
        if d is None:
            continue
        cat, f, t = d
        if cat not in merged:
            merged[cat] = (f, t)
            continue
        pf, pt = merged[cat]
        nf = None if (pf is None or f is None) else min(pf, f)
        nt = None if (pt is None or t is None) else max(pt, t)
        merged[cat] = (nf, nt)
    return merged


def _interval_visibility_js(res):
    """生成「标记在背驰周期+检测周期同时显示」的 intervalsVisibilities 字面量。

    TradingView 图形可通过 intervalsVisibilities 控制其显示的周期范围，
    每一类周期（seconds/minutes/hours/days/weeks/months）独立开关并可用
    from/to 限定精确范围。这里接受单个周期或周期列表（[markRes, periodX]），
    取并集：同一箭头/横线在背驰周期与检测周期图表上同时显示；同类周期
    合并为连续区间（from=min、to=max，中间周期也会显示，与 chan-bi 的
    intervalVisibility 并集先例一致）。

    单周期输入的输出与旧实现逐字节一致；整体无法识别时返回 None（保持默认不限制）。
    """
    merged = _iv_merged(res)
    if merged is None:
        return None
    parts = []
    for cat in _IV_CATEGORIES:
        if cat not in merged:
            parts.append(f"{cat}: false")
            continue
        f, t = merged[cat]
        if f is None or t is None:
            parts.append(f"{cat}: true")
        else:
            parts.append(f"{cat}: true, {cat}From: {f}, {cat}To: {t}")
    return "{ " + ", ".join(parts) + " }"


def _iv_json(res):
    """同 _interval_visibility_js 的并集，但返回可 json.dumps 的 dict（供 JS 内引用）。

    支阻横线按行把 IV 放进 items 数组（json.dumps 后是真正的 JS 对象，
    可直接赋给 overrides.intervalsVisibilities），而不是内嵌源码字面量。
    """
    merged = _iv_merged(res)
    if merged is None:
        return None
    out = {}
    for cat in _IV_CATEGORIES:
        if cat not in merged:
            out[cat] = False
            continue
        out[cat] = True
        f, t = merged[cat]
        if f is not None:
            out[cat + "From"] = f
        if t is not None:
            out[cat + "To"] = t
    return out


def _clear_marks(c):
    """清除本模块画的 ML· 标记：localStorage id 精准删除 + getAllShapes 文本前缀兜底。

    背景（2026-09-05）：TradingView 图表重载后 shape id 全部变化，localStorage 里
    记录的旧 id 全部失效（dataSourceForId 返回 null）→ 只删到 0 个但清空了记录，
    再画一遍就在图上叠加重复箭头（用户实测：删除显示成功、图上残留 6 个 ML· 且两两重复）。
    兜底：遍历 chart.getAllShapes()，shape 的 text/title 以 ML· 开头即 removeEntity
    （与 mark-entry SKILL 按 title 清除同款机制，不依赖 localStorage）。

    返回删除数量；不误删用户图形与 BT·/RT·/CHAN_BI/买卖点/支阻位/ENTRY_ 等标记，
    也不删本模块的 ML·SR 支阻横线（见 _clear_sr_marks，两个标记按钮互不清除）。
    单行标记（ML·单，前缀同属 ML·）会被前缀兜底一并删除——重画箭头时清掉
    单行标记，其键 mark_single_ids 同步置空（避免留下死 id）。
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "const PREFIX = '" + MARK_PREFIX + "'; "
        "let removed = 0; "
        "let ids = []; "
        "try { ids = JSON.parse(localStorage.getItem('" + IDS_KEY + "') || '[]'); } catch (e) {} "
        "for (const id of ids) { "
        "  try { "
        "    const ds = cm.dataSourceForId(id); "
        "    if (ds) { cm.removeSource(ds); removed++; } "
        "  } catch (err) {} "
        "} "
        "try { localStorage.setItem('" + IDS_KEY + "', '[]'); } catch (e) {} "
        # 前缀兜底会删掉 ML·单 单行标记（同属 ML· 前缀），其键一并置空避免死 id
        "try { localStorage.setItem('" + SINGLE_IDS_KEY + "', '[]'); } catch (e) {} "
        "const readPrefix = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    const props = sh && sh._source && sh._source._properties; "
        "    if (!props) return ''; "
        "    if (props.text && props.text._value) return String(props.text._value); "
        "    if (props.title && props.title._value) return String(props.title._value); "
        "    return ''; "
        "  } catch (e) { return ''; } "
        "}; "
        "try { "
        "  for (const s of chart.getAllShapes()) { "
        "    const p = readPrefix(s.id); "
        # 排除支阻横线（ML·SR）：箭头按钮只清箭头，与「标记支阻位」按钮互不清除
        "    if (p.startsWith(PREFIX) && !p.startsWith('" + SR_PREFIX + "')) { "
        "      try { chart.removeEntity(s.id); removed++; } catch (e) {} "
        "    } "
        "  } "
        "} catch (e) {} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def _clear_sr_marks(c):
    """清除本模块画的 ML·SR 支阻横线：localStorage id 精删 + getAllShapes 前缀兜底。

    与 _clear_marks 同款双保险（图表重载后 id 失效靠前缀兜底），但使用独立的
    SR_IDS_KEY（mark_sr_ids 与箭头的 mark_list_ids 隔离），且前缀匹配不排除条件
    ——「标记支阻位」重画时把上次的横线全清掉，不影响 ML· 箭头。

    返回删除数量。
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "const PREFIX = '" + SR_PREFIX + "'; "
        "let removed = 0; "
        "let ids = []; "
        "try { ids = JSON.parse(localStorage.getItem('" + SR_IDS_KEY + "') || '[]'); } catch (e) {} "
        "for (const id of ids) { "
        "  try { "
        "    const ds = cm.dataSourceForId(id); "
        "    if (ds) { cm.removeSource(ds); removed++; } "
        "  } catch (err) {} "
        "} "
        "try { localStorage.setItem('" + SR_IDS_KEY + "', '[]'); } catch (e) {} "
        "const readPrefix = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    const props = sh && sh._source && sh._source._properties; "
        "    if (!props) return ''; "
        "    if (props.text && props.text._value) return String(props.text._value); "
        "    if (props.title && props.title._value) return String(props.title._value); "
        "    return ''; "
        "  } catch (e) { return ''; } "
        "}; "
        "try { "
        "  for (const s of chart.getAllShapes()) { "
        "    if (readPrefix(s.id).startsWith(PREFIX)) { "
        "      try { chart.removeEntity(s.id); removed++; } catch (e) {} "
        "    } "
        "  } "
        "} catch (e) {} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def _clear_single_marks(c):
    """清除上一次点击行画的单行标记（ML·单 / ML·单SR）：id 精删 + 前缀兜底。

    与 _clear_marks 同款双保险（图表重载后 id 失效靠前缀兜底），但只扫 ML·单
    前缀——绝不触碰批量 ML· 箭头 / ML·SR 横线 / 用户图形。替换语义：每次
    draw_single_mark 先调它，图上始终只保留最近点击一行的单行标记。

    返回删除数量。
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "const PREFIX = '" + SINGLE_PREFIX + "'; "
        "let removed = 0; "
        "let ids = []; "
        "try { ids = JSON.parse(localStorage.getItem('" + SINGLE_IDS_KEY + "') || '[]'); } catch (e) {} "
        "for (const id of ids) { "
        "  try { "
        "    const ds = cm.dataSourceForId(id); "
        "    if (ds) { cm.removeSource(ds); removed++; } "
        "  } catch (err) {} "
        "} "
        "try { localStorage.setItem('" + SINGLE_IDS_KEY + "', '[]'); } catch (e) {} "
        "const readPrefix = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    const props = sh && sh._source && sh._source._properties; "
        "    if (!props) return ''; "
        "    if (props.text && props.text._value) return String(props.text._value); "
        "    if (props.title && props.title._value) return String(props.title._value); "
        "    return ''; "
        "  } catch (e) { return ''; } "
        "}; "
        "try { "
        "  for (const s of chart.getAllShapes()) { "
        "    if (readPrefix(s.id).startsWith(PREFIX)) { "
        "      try { chart.removeEntity(s.id); removed++; } catch (e) {} "
        "    } "
        "  } "
        "} catch (e) {} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def draw_single_mark(c, row, colors=None, log=None, purge=True):
    """点击信号行后，在已定位到该行的图表上画单行标记（替换语义）。

    画：该行进场箭头 + exits 出场箭头（_draw_chunk 同款视觉，背驰+检测双周期
    可见）+ 近支阻（nearSr）全宽横线（ML·单SR）。先 _clear_single_marks 清上
    一次点击留下的单行标记——始终只保留最近点击一行；「同向过滤」或缺价格/
    方向的行只清不画（与「标记进出场」按钮的批量口径一致）。

    必须在定位完成的图表上调用（当前周期=row['markRes']、目标K线已居中、与
    定位同一 CDP 会话）：锚点 time 需已在当前数据源内，且不能像
    draw_signal_marks 那样切周期/清全部/滚回实时（会破坏定位视口）。

    @param c      已连接的 CDPClient（定位所用会话）
    @param row    信号行（SignalLog 记录）
    @param colors {'buy','sell','exit','sr'}（可选，缺省 _colors()）
    @param log    日志回调
    @param purge  True=画完后删空锚半成品（实时图）；回放态 getPoints() 常为空，
                  会把刚画上的进场箭头误删，须传 False
    @returns {'drawn': n, 'cleared': n[, 'skipped'/'error': 原因]}（各步失败降级不抛出）
    """
    log = log or (lambda *a, **k: None)
    colors = _colors(colors)
    # 替换语义：无论本行是否可画，先清掉上一次的单行标记
    cleared = 0
    try:
        cleared = int(_clear_single_marks(c) or 0)
    except Exception as e:
        log(f"清除上次单行标记失败（忽略）：{e}")
    if row.get("status") == "同向过滤":
        return {"drawn": 0, "cleared": cleared, "skipped": "同向过滤"}
    if row.get("price") is None or not row.get("direction") or not row.get("time"):
        return {"drawn": 0, "cleared": cleared, "skipped": "缺价格/方向/时间"}
    ids = []
    draw_err = None
    # 进场 + 出场箭头（与批量标记同款视觉，双周期可见，id 记入单行键）
    try:
        r = _draw_chunk(c, [row], colors, prefix=SINGLE_PREFIX,
                        ids_key=SINGLE_IDS_KEY, dual_tf=True)
        ids += list(r) if isinstance(r, list) else []
    except Exception as e:
        draw_err = str(e)
        log(f"画单行进出场箭头失败：{e}")
    # 回放态个别版本会因 intervalsVisibilities 创建失败（返回空 id）——去掉可见性再试一次
    if not ids:
        try:
            r = _draw_chunk(c, [row], colors, prefix=SINGLE_PREFIX,
                            ids_key=SINGLE_IDS_KEY, dual_tf=True, with_iv=False)
            ids += list(r) if isinstance(r, list) else []
            if ids:
                draw_err = None
        except Exception as e:
            draw_err = draw_err or str(e)
            log(f"画单行进出场箭头（无周期限制）失败：{e}")
    # 近支阻全宽横线（nearSr 缺失/非法则跳过）
    near_sr = row.get("nearSr")
    try:
        near_sr = float(near_sr) if near_sr is not None else None
    except (TypeError, ValueError):
        near_sr = None
    if near_sr is not None and math.isfinite(near_sr):
        try:
            r = _draw_sr_chunk(c, [row], colors["sr"],
                               prefix=SINGLE_PREFIX + "SR", ids_key=SINGLE_IDS_KEY)
            if isinstance(r, dict):
                ids += list(r.get("ids") or [])
        except Exception as e:
            log(f"画单行支阻横线失败：{e}")
    # 清理锚点为空的半成品（当前周期=markRes，在 IV 并集内，满足 purge 的周期约束）
    n_broken = 0
    if purge:
        try:
            n_broken = int(_purge_broken_marks(c, ids) or 0)
        except Exception:
            n_broken = 0
        if n_broken:
            log(f"清理锚点异常的单行标记 {n_broken} 个")
    out = {"drawn": max(0, len(ids) - n_broken), "cleared": cleared}
    if out["drawn"] == 0 and draw_err:
        out["error"] = draw_err
    elif out["drawn"] == 0 and n_broken:
        out["error"] = f"锚点未挂上K线（已清 {n_broken} 个）"
    return out


def _draw_chunk(c, chunk, colors, prefix=MARK_PREFIX, ids_key=IDS_KEY, dual_tf=False,
                with_iv=True):
    """一次 CDP 执行画出一批箭头 + 出场标记，并把新 shape id 累积记录到 localStorage。

    @param colors  {'buy': '#..', 'sell': '#..', 'exit': '#..'} 做多/做空/出场颜色
    @param prefix  文本前缀（批量 ML· / 单行 ML·单），清除按前缀扫
    @param ids_key localStorage 记录 shape id 的键（批量/单行隔离）
    @param dual_tf True = 可见性取 [markRes, periodX] 并集（背驰+检测双周期显示）；
                   False = 仅 markRes（旧行为）
    @param with_iv False = 不写 intervalsVisibilities（回放态个别版本会因此创建失败）
    @returns 新画的 shape id 列表
    """
    calls = []
    for s in chunk:
        shape = "arrow_up" if s["direction"] == "long" else "arrow_down"
        color = colors["buy"] if s["direction"] == "long" else colors["sell"]
        label = "BUY" if s["direction"] == "long" else "SELL"
        text = f"{prefix}{label} {float(s['price']):.2f}"
        # 箭头可见性：背驰周期（markRes）+ 检测周期（periodX）并集；无法识别时保持默认不限
        iv = None
        if with_iv:
            iv = _interval_visibility_js(
                [s.get("markRes"), s.get("periodX")] if dual_tf else s.get("markRes"))
        iv_part = f", intervalsVisibilities: {iv}" if iv else ""
        t, p = int(s["time"]), float(s["price"])
        calls.append(
            "chart.createShape("
            f"{{ time: {t}, price: {p} }}, "
            f"{{ shape: '{shape}', text: '{text}', lock: false, "
            f"color: '{color}', textColor: '{color}', "
            # arrow_up/arrow_down 工具的箭头图标颜色是独立字段 arrowColor，
            # 不走顶层 color/textColor（否则恒为默认黄 #FFEB3B），必须用 overrides 指定
            f"overrides: {{ arrowColor: '{color}'{iv_part} }} }})"
        )
        # 出场标记：与进场同款箭头（统一灰，同款周期可见性），方向=平仓方向：
        #   多头出场（平多）= 向下箭头 ↓、空头出场（平空）= 向上箭头 ↑，
        #   与进场箭头用颜色区分（进场红/绿、出场灰）——用户要求"出场点也变成箭头"。
        # 保本（breakeven）/ 仍持仓仅落盘不画图（不在 EXIT_SHAPES 中）。
        eov_part = f", intervalsVisibilities: {iv}" if iv else ""
        exit_shape = "arrow_down" if s["direction"] == "long" else "arrow_up"
        for ev in (s.get("exits") or []):
            et = ev.get("type")
            if et not in EXIT_SHAPES or ev.get("price") is None or ev.get("time") is None:
                continue
            ex_text = f"{prefix}{EXIT_NAMES[et]} {float(ev['price']):.2f}"
            xt, xp = int(ev["time"]), float(ev["price"])
            calls.append(
                "chart.createShape("
                f"{{ time: {xt}, price: {xp} }}, "
                f"{{ shape: '{exit_shape}', text: '{ex_text}', lock: false, "
                f"color: '{colors['exit']}', textColor: '{colors['exit']}', "
                # 箭头图标颜色独立字段 arrowColor（同进场箭头）
                f"overrides: {{ arrowColor: '{colors['exit']}'{eov_part} }} }})"
            )
    expr = (
        "(async () => { const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; const ids = []; "
        # 只记录有效 id（createShape 偶发返回 undefined 时避免存入死 id）
        + "; ".join(f"{{ const v = await ({call}); if (v) ids.push(v); }}" for call in calls) +
        "; "
        "try { const old = JSON.parse(localStorage.getItem('" + ids_key + "') || '[]'); "
        "localStorage.setItem('" + ids_key + "', JSON.stringify(old.concat(ids))); } catch (e) {} "
        "; return ids; })()"
    )
    return c.evaluate(expr)


def _draw_sr_chunk(c, chunk, color, prefix=SR_PREFIX, ids_key=SR_IDS_KEY):
    """一次 CDP 画一批支阻横线（全宽 horizontal_line，背驰+检测双周期显示）。

    每条横线锚在信号 time 的近支阻价位（nearSr）——horizontal_line 单点锚定、
    天然横贯全图不限长度（不再画限定宽度的 trend_line 线段，无需读取K线数组
    算端点）。文本走顶层 text（与箭头同款 createShape 生产路径，_clear_sr_marks
    读 text/title 均可命中）；颜色 1px 走 overrides.linecolor/linewidth。
    周期可见性 = 每行自己的 [markRes, periodX] 并集，以 JSON 对象随 items
    传入、赋给 overrides.intervalsVisibilities（单点 createShape 与箭头同
    路径；无法识别时保持默认不限）。

    新 shape id 累积记录到 localStorage（默认 SR_IDS_KEY 与箭头隔离；单行
    标记复用时传 prefix=ML·单SR、ids_key=mark_single_ids）。
    @returns {'ids': [...], 'skipped': n} 或 {'error': ...}
    """
    items = [{"time": int(s["time"]), "price": float(s["nearSr"]),
              "iv": _iv_json([s.get("markRes"), s.get("periodX")])}
             for s in chunk]
    expr = (
        "(async () => { const chart = TradingViewApi.activeChart(); "
        "if (!chart) return { error: 'no_chart' }; "
        "const ids = []; let skipped = 0; "
        "for (const s of " + json.dumps(items) + ") { "
        "  const label = '" + prefix + " ' + s.price.toFixed(2); "
        # 横线样式与周期可见性都在 overrides 上（createShape 单点路径，与箭头一致）
        "  const ov = { linecolor: '" + color + "', linewidth: 1, "
        "               linestyle: 0, showPriceLabels: false }; "
        "  if (s.iv) { ov.intervalsVisibilities = s.iv; } "
        "  try { "
        "    const v = await chart.createShape( "
        "      { time: s.time, price: s.price }, "
        "      { shape: 'horizontal_line', lock: false, text: label, overrides: ov }); "
        "    if (v) { ids.push(v); } else { skipped++; } "
        "  } catch (e) { skipped++; } "
        "} "
        "try { const old = JSON.parse(localStorage.getItem('" + ids_key + "') || '[]'); "
        "localStorage.setItem('" + ids_key + "', JSON.stringify(old.concat(ids))); } catch (e) {} "
        "return { ids: ids, skipped: skipped }; })()"
    )
    return c.evaluate(expr)


def _dedup_rows(rows):
    """画图前去重，避免同一根K线上出现多个重叠箭头。

    信号重复的两个来源：
      1) 同一进场点（同一背驰点 time/price）会被多个检测周期（15m/60m/...）
         各自命中 → 同 (time, direction) 只保留一个箭头；
      2) 同一背驰周期在同一根K线内多次触发（箭头按 markRes 画图，time 已
         对齐 markRes K线起点）→ 同 (markRes, K线起点, direction) 只保留
         一个箭头；不同背驰K线的信号（如同一检测周期内 8:12 与 8:21）都保留。

    按 time 升序、先到先得保留第一条（不做成交/统计，仅影响标记显示）。
    """
def _read_loaded_range(c):
    """读取当前周期数据源已加载K线范围 {first, last}（UTC秒）；无K线返回 None。"""
    return c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const it=c.chartModel().mainSeries().data().m_bars._items; "
        "if(!it || !it.length) return null; "
        "return { first: it[0].value[0], last: it[it.length-1].value[0] }; })()"
    )


def _scroll_first_bar(c):
    """滚动到第一根K线，触发该周期历史数据加载。"""
    c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); "
        "const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); "
        "ts.scrollToFirstBar(); return 'ok'; })()"
    )


def _scroll_realtime(c):
    """滚动回最新K线（画图时切周期/滚动历史后会停在远古位置，画完恢复视图用）。"""
    c.evaluate(
        "(function(){ const c=TradingViewApi.activeChart(); "
        "const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); "
        "const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); "
        "ts.scrollToRealtime(); return 'ok'; })()"
    )


def _ensure_hist_loaded(c, res, min_ts, cfg=None, log=None):
    """画图前确保当前 res 周期数据源已加载到 min_ts 对应的历史K线。

    TradingView 切到某个周期后，数据源只预加载可见窗口附近的K线（约几百根）；
    若信号时间点早于已加载范围，createShape 会拿到数据范围内不存在的时间——
    shape 锚点直接丢失（getPoints() 返回空、图上不可见/错位，永久损坏）。

    教训（与 mark-entry ensureBarsCover 一致）：TV 历史是分批异步加载的，加载中途
    会短暂暂停（len/first 看似稳定），短 deadline（旧实现 15s）远不够加载数月深度的
    小周期历史，且提前退出会产生损坏 shape。必须轮询等 first 真正 <= min_ts：
      - 每 15 次重新 scrollToFirstBar（可视范围被回弹到实时时数据会停止前进）
      - 连续 30 次（约 36 秒）无任何进展才兜底放弃（数据源到头）

    @returns True 数据源已覆盖目标时间；False 尽力仍未覆盖（跳过画该组，避免坏 shape）
    """
    log = log or (lambda *a, **k: None)
    cfg = cfg or CDPConfig()
    # 切周期后数据源可能尚未填充K线，先等到读得到范围
    rng = None
    t0 = time.time()
    while time.time() - t0 < max(cfg.res_wait, 4.0):
        rng = _read_loaded_range(c)
        if rng:
            break
        time.sleep(0.5)
    if not rng:
        log(f"  {res}: 数据源暂未就绪，无法确认历史加载范围（跳过该组，避免坏标记）")
        return False
    if min_ts >= rng["first"]:
        return True
    log(f"  {res}: 信号最早 {fmtT(min_ts)} 早于已加载范围起点 {fmtT(rng['first'])}，"
        f"滚动加载 {res} 历史（分批加载，可能需要数分钟）...")
    _scroll_first_bar(c)
    prev_len = rng["last"] and 0 or 0
    prev_first = rng["first"]
    no_progress = 0
    covered = False
    for i in range(300):
        time.sleep(1.2)
        rng = _read_loaded_range(c)
        if not rng:
            continue
        if min_ts >= rng["first"]:
            covered = True
            break
        # 可视范围被回弹到实时会导致加载停滞，周期性重新触发滚动
        if i > 0 and i % 15 == 0:
            _scroll_first_bar(c)
        if rng["first"] == prev_first and rng["last"] == prev_len:
            no_progress += 1
            if no_progress >= 30:
                break  # 数据源已到头，加载不到更早历史
        else:
            no_progress = 0
        prev_first = rng["first"]
        prev_len = rng["last"]
    # 恢复可视范围到实时
    _scroll_realtime(c)
    rng = _read_loaded_range(c)
    if covered:
        log(f"  {res}: 历史已覆盖到 {fmtT(rng['first'])}，开始标记")
    else:
        log(f"  {res}: 加载到数据源最早期限 {fmtT(rng['first'] if rng else 0)}，"
            f"仍未覆盖 {fmtT(min_ts)}（跳过该组更早信号，避免坏标记）")
    return covered


def _dedup_rows(rows):
    """画图前去重，避免同一根K线上出现多个重叠箭头。

    信号重复的两个来源：
      1) 同一进场点（同一背驰点 time/price）会被多个检测周期（15m/60m/...）
         各自命中 → 同 (time, direction) 只保留一个箭头；
      2) 同一背驰周期在同一根K线内多次触发（箭头按 markRes 画图，time 已
         对齐 markRes K线起点）→ 同 (markRes, K线起点, direction) 只保留
         一个箭头；不同背驰K线的信号（如同一检测周期内 8:12 与 8:21）都保留。

    按 time 升序、先到先得保留第一条（不做成交/统计，仅影响标记显示）。
    同时刻同向共振中「同向过滤」的行排在最后——确保去重保留的是可绘制
    （实际成交）的那条，过滤行随后被剔除。
    """
    deduped = []
    seen_time_dir = set()   # (time, direction)：跨检测周期的同一信号
    seen_bar = set()        # (markRes, K线起点, direction)：同一根K线同方向
    for s in sorted(rows, key=lambda x: (x.get("time") or 0,
                                         x.get("status") == "同向过滤")):
        t = s.get("time") or 0
        d = s.get("direction")
        if (t, d) in seen_time_dir:
            continue
        res = str(s.get("markRes") or "")
        bar_sec = intervalSecOf(res) or 0
        bar_start = (t // bar_sec) * bar_sec if bar_sec else t
        if (res, bar_start, d) in seen_bar:
            continue
        seen_time_dir.add((t, d))
        seen_bar.add((res, bar_start, d))
        deduped.append(s)
    return deduped


def _dedup_sr_rows(rows):
    """支阻横线的行筛选 + 去重（与箭头的 _dedup_rows 语义不同）。

    筛选：近支阻（nearSr）非空且带 markRes 的行——含「同向过滤」的行（用户口径：
    直接用列表近支阻列的数据，过滤行命中的支阻位同样是有效结构）。
    去重键 =（markRes, 近支阻价, periodX）：横线是全宽的，同周期同价位画多条
    完全重叠无意义，只画一条；periodX 参与键（不同检测周期的并集可见性不同，
    各画各的）。按 time 升序，先到先得。
    """
    seen = set()
    out = []
    for s in sorted(rows, key=lambda x: x.get("time") or 0):
        if s.get("nearSr") is None or not s.get("markRes"):
            continue
        key = (str(s["markRes"]), round(float(s["nearSr"]), 4),
               str(s.get("periodX") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _purge_broken_marks(c, ids=None):
    """校验刚创建的 shape 锚点，删除「创建了但锚点为空」的半成品。

    **必须在标记所属周期调用**（画完该组、切走之前）：shape 的 getPoints() 只在
    其可见周期（markRes/intervalsVisibilities）下非空，跨周期检查会把有效标记
    误判为空锚点删掉（2026-09-05 曾误杀 47/50 个——收尾时周期已切到最后一组，
    前面的 30S/3m 标记全被判 broken）。
    @param ids 本组创建返回的 shape id 列表（只校验这些，不误伤）
    @returns 删除数量
    """
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "let removed = 0; "
        "const broken = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    if (!sh) return true; "
        "    const pts = sh.getPoints(); "
        "    return !pts || pts.length === 0; "
        "  } catch (e) { return true; } "
        "}; "
        "const ids = " + json.dumps(ids or []) + "; "
        "for (const id of ids) { "
        "  if (broken(id)) { try { chart.removeEntity(id); removed++; } catch (e) {} } "
        "} "
        "return removed; })()"
    )
    return c.evaluate(expr)


def draw_signal_marks(rows, cfg=None, clear_first=True, colors=None, log=None):
    """把信号列表全部进场点 + 出场标记按背驰周期画到图表（背驰+检测双周期显示）。

    箭头锚在背驰点 time（markRes K线起点），intervalsVisibilities 取
    [markRes, periodX] 并集——同一箭头在背驰周期与检测周期图表上同时可见
    （同类周期合并为连续区间时，中间周期也会显示）。

    @param rows         信号行列表，每行含 time/price/direction/periodX/markRes，
                        可选 exits=[{type,time,price}]（出场事件）与 status（同向过滤不画箭头）
    @param cfg          CDPConfig
    @param clear_first  画前先清除上次 ML· 标记（含单行标记 ML·单）
    @param colors       {'buy': '#..', 'sell': '#..', 'exit': '#..'} 做多/做空/出场颜色（可选）
    @param log          日志回调（默认 print）
    @returns { drawn, cleared, errors, skipped }
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    colors = _colors(colors)
    if not rows:
        log("信号列表为空，无可标记的进场点")
        return {"drawn": 0, "cleared": 0, "errors": 0, "skipped": 0}

    # 画图前去重：同一进场点被多周期命中 / 同一根背驰K线同方向多次触发只画一个箭头
    n_raw = len(rows)
    rows = _dedup_rows(rows)
    if len(rows) < n_raw:
        log(f"去重：{n_raw} 条信号合并为 {len(rows)} 条（同一进场点/同一背驰K线只画一个箭头）")
    # 同向持仓互斥过滤的信号：无仓位，不画箭头（出场标记自然也不存在）
    n_dedup = len(rows)
    rows = [s for s in rows if s.get("status") != "同向过滤"]
    if len(rows) < n_dedup:
        log(f"同向过滤 {n_dedup - len(rows)} 条信号不画箭头")
    n_exits = sum(len([e for e in (s.get("exits") or [])
                       if e.get("type") in EXIT_SHAPES and e.get("price") is not None])
                  for s in rows)
    if n_exits:
        log(f"将一并画出 {n_exits} 个出场箭头（灰：平多 ↓ / 平空 ↑）")

    # 按背驰周期分组（无 markRes 的直接跳过并在日志提示）
    by_res = {}
    skipped = 0
    for s in rows:
        res = s.get("markRes")
        if not res:
            skipped += 1
            continue
        by_res.setdefault(str(res), []).append(s)
    for res in by_res:
        by_res[res].sort(key=lambda s: s.get("time") or 0)

    errors = 0
    drawn = 0
    cleared = 0
    with CDPClient(cfg, log=log) as c:
        # 记录初始周期，标记期间逐周期切换，结束恢复
        try:
            display_res = str(c.evaluate("String(TradingViewApi.activeChart().resolution());"))
        except Exception:
            display_res = None
        try:
            if clear_first:
                try:
                    cleared = int(_clear_marks(c) or 0)
                    log(f"已清除上次 ML· 标记 {cleared} 个")
                except Exception as e:
                    log(f"清除 ML· 标记失败（忽略）：{e}")
            for res in sorted(by_res, key=lambda r: int(r) if str(r).isdigit() else 0):
                chunk_list = by_res[res]
                log(f"标记背驰 {res} 周期：{len(chunk_list)} 个进场点")
                try:
                    if _ensure_res(c, res):
                        time.sleep(RES_WAIT)
                    # 画图前先确保该周期历史已加载到本组最早信号：加载不到位就
                    # createShape 会拿到数据范围外的时间 → 锚点丢失（shape 永久损坏，
                    # 图上不可见/错位）。加载失败时跳过该组，宁可少画不画坏的。
                    min_ts = chunk_list[0].get("time") or 0
                    if not _ensure_hist_loaded(c, res, min_ts, cfg, log):
                        errors += len(chunk_list)
                        log(f"  {res}: 历史未覆盖最早信号 {fmtT(min_ts)}，跳过该组 {len(chunk_list)} 个标记")
                        continue
                except Exception as e:
                    errors += len(chunk_list)
                    log(f"切换到 {res} 周期失败：{e}")
                    continue
                for i in range(0, len(chunk_list), CHUNK):
                    chunk = chunk_list[i:i + CHUNK]
                    batch_ids = []
                    try:
                        r = _draw_chunk(c, chunk, colors, dual_tf=True)
                        batch_ids = list(r) if isinstance(r, list) else []
                    except Exception as e:
                        # 批量失败时拆单重试一轮（TV _createMultipointShape 偶发
                        # Value is undefined 等内部错误，重试往往能过）
                        log(f"  {res}: 第 {i + 1}~{i + len(chunk)} 批失败（{e}），拆单重试...")
                        for s in chunk:
                            try:
                                r = _draw_chunk(c, [s], colors, dual_tf=True)
                                batch_ids += list(r) if isinstance(r, list) else []
                            except Exception as e2:
                                errors += 1
                                log(f"  {res}: 单个标记仍失败（{e2}）：{s.get('direction')} {s.get('time')}")
                    # 组内即时校验（当前周期仍为该组 res）：创建了但锚点空的半成品
                    # 删掉（不要等到收尾跨周期检查——getPoints 只在本周期可见，会误杀）
                    try:
                        n_broken = int(_purge_broken_marks(c, batch_ids) or 0)
                        if n_broken:
                            log(f"  {res}: 清理锚点异常的半成品 {n_broken} 个")
                    except Exception:
                        n_broken = 0
                    drawn += max(0, len(batch_ids) - n_broken)
                    log(f"  {res}: 已画 {drawn}/{sum(len(v) for v in by_res.values())} 个进场箭头")
        finally:
            if display_res:
                try:
                    if _ensure_res(c, display_res):
                        time.sleep(RES_WAIT)
                    log(f"已恢复图表周期：{display_res}")
                except Exception:
                    pass
            try:
                _scroll_realtime(c)
            except Exception:
                pass
    log(f"标记完成：共画 {drawn} 个，清除 {cleared} 个，失败 {errors} 个"
        + (f"，跳过无周期信号 {skipped} 个" if skipped else ""))
    return {"drawn": drawn, "cleared": cleared, "errors": errors, "skipped": skipped}


def clear_signal_marks(cfg=None, log=None):
    """清除所有 ML· 标记（只删自己创建的，不影响用户图形与 BT·/RT· 标记）。

    @returns 删除数量
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    with CDPClient(cfg, log=log) as c:
        removed = int(_clear_marks(c) or 0)
    log(f"已清除 ML· 标记 {removed} 个")
    return removed


def draw_sr_marks(rows, cfg=None, clear_first=True, colors=None, log=None):
    """把信号列表「近支阻」画成全宽支阻横线（「标记支阻位」按钮）。

    横线：近支阻价位的 horizontal_line（单点锚定、横贯全图不限长度，锚=进场点
    K线），1px 默认灰（可调），背驰周期+检测周期（[markRes, periodX] 并集）显示；
    行口径与去重见 _dedup_sr_rows。主流程与 draw_signal_marks 同骨架（切周期→
    加载历史→分块画→恢复周期），但只清上次的 ML·SR 横线（_clear_sr_marks），
    不动 ML· 箭头——两个标记按钮互不清除。

    @param rows         信号行列表（含 time/markRes/nearSr，periodX 参与 IV 并集）
    @param clear_first  画前先清除上次 ML·SR 横线
    @param colors       {'sr': '#..'} 横线颜色（可选）
    @returns { drawn, cleared, errors, skipped }
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    colors = _colors(colors)
    if not rows:
        log("信号列表为空，无可标记的支阻位")
        return {"drawn": 0, "cleared": 0, "errors": 0, "skipped": 0}
    rows = _dedup_sr_rows(rows)
    if not rows:
        log("信号列表没有「近支阻」非空的行，无可标记的支阻位")
        return {"drawn": 0, "cleared": 0, "errors": 0, "skipped": 0}
    log(f"支阻位：{len(rows)} 条横线待画（近支阻非空且带背驰周期，含同向过滤行，已去重）")

    # 按背驰周期分组（同组横线共用同一周期K线数组与 IV 配置）
    by_res = {}
    for s in rows:
        by_res.setdefault(str(s["markRes"]), []).append(s)
    for res in by_res:
        by_res[res].sort(key=lambda s: s.get("time") or 0)

    errors = 0
    drawn = 0
    skipped = 0
    cleared = 0
    total = len(rows)
    with CDPClient(cfg, log=log) as c:
        # 记录初始周期，标记期间逐周期切换，结束恢复
        try:
            display_res = str(c.evaluate("String(TradingViewApi.activeChart().resolution());"))
        except Exception:
            display_res = None
        try:
            if clear_first:
                try:
                    cleared = int(_clear_sr_marks(c) or 0)
                    log(f"已清除上次支阻横线 {cleared} 条")
                except Exception as e:
                    log(f"清除支阻横线失败（忽略）：{e}")
            for res in sorted(by_res, key=lambda r: int(r) if str(r).isdigit() else 0):
                chunk_list = by_res[res]
                log(f"标记背驰 {res} 周期：{len(chunk_list)} 条支阻横线")
                try:
                    if _ensure_res(c, res):
                        time.sleep(RES_WAIT)
                    # 与箭头同款：历史加载不到位，横线锚点会拿到数据范围外的时间
                    # → 锚点丢失（坏 shape），宁可跳过不画坏的
                    min_ts = chunk_list[0].get("time") or 0
                    if not _ensure_hist_loaded(c, res, min_ts, cfg, log):
                        errors += len(chunk_list)
                        log(f"  {res}: 历史未覆盖最早信号 {fmtT(min_ts)}，跳过该组 {len(chunk_list)} 条横线")
                        continue
                except Exception as e:
                    errors += len(chunk_list)
                    log(f"切换到 {res} 周期失败：{e}")
                    continue
                for i in range(0, len(chunk_list), SR_CHUNK):
                    chunk = chunk_list[i:i + SR_CHUNK]
                    batch_ids = []
                    batch_skipped = 0
                    try:
                        r = _draw_sr_chunk(c, chunk, colors["sr"])
                        if isinstance(r, dict):
                            batch_ids = list(r.get("ids") or [])
                            batch_skipped = int(r.get("skipped") or 0)
                        else:
                            batch_skipped = len(chunk)
                    except Exception as e:
                        # 批量失败时拆单重试一轮（TV createShape 偶发内部错误）
                        log(f"  {res}: 第 {i + 1}~{i + len(chunk)} 批失败（{e}），拆单重试...")
                        for s in chunk:
                            try:
                                r = _draw_sr_chunk(c, [s], colors["sr"])
                                if isinstance(r, dict):
                                    batch_ids += list(r.get("ids") or [])
                                    batch_skipped += int(r.get("skipped") or 0)
                                else:
                                    batch_skipped += 1
                            except Exception as e2:
                                errors += 1
                                log(f"  {res}: 单条横线仍失败（{e2}）：nearSr={s.get('nearSr')}")
                    # 组内即时校验（当前周期仍为该组 res）：锚点空的半成品删掉
                    try:
                        n_broken = int(_purge_broken_marks(c, batch_ids) or 0)
                        if n_broken:
                            log(f"  {res}: 清理锚点异常的半成品 {n_broken} 条")
                    except Exception:
                        n_broken = 0
                    drawn += max(0, len(batch_ids) - n_broken)
                    skipped += batch_skipped
                    log(f"  {res}: 已画 {drawn}/{total} 条支阻横线")
        finally:
            if display_res:
                try:
                    if _ensure_res(c, display_res):
                        time.sleep(RES_WAIT)
                    log(f"已恢复图表周期：{display_res}")
                except Exception:
                    pass
            try:
                _scroll_realtime(c)
            except Exception:
                pass
    log(f"支阻位标记完成：共画 {drawn} 条，清除 {cleared} 条，失败 {errors} 条"
        + (f"，跳过 {skipped} 条（边缘bar不足/创建失败）" if skipped else ""))
    return {"drawn": drawn, "cleared": cleared, "errors": errors, "skipped": skipped}


# 「删除标记」按钮：清除全部系统标记的前缀与 localStorage 键
# （Web 控制台 ML·/ML·SR/ML·单、回测 BT·、实时 RT·、支阻调试页 SRT·/RAW·；与各
# 模块自身前缀/键定义保持一致——sr_draw.py 的 SRT_PREFIX/SRT_IDS_KEY/RAW_IDS_KEY 相同值）
CLEAR_PREFIXES = ["ML·", "BT·", "RT·", "SRT·", "RAW·"]
CLEAR_IDS_KEYS = ["mark_list_ids", "mark_sr_ids", "mark_single_ids", "bt_arrow_ids",
                  "rt_arrow_ids", "sr_test_ids", "sr_raw_ids"]


def clear_all_marks(cfg=None, log=None):
    """清除图上所有系统画的进出场箭头与支阻横线（「删除标记」按钮）。

    覆盖 ML·（含 ML·SR 支阻横线与 ML·单 单行标记）/ BT·（回测）/ RT·（实时）等
    前缀，用户手画的图形不碰。一条 JS 表达式、一次 CDP 连接完成（顺序调各模块
    清除函数要多次建连，失败面更大且非原子）：
      ① CLEAR_IDS_KEYS 各 localStorage 键记录的 id 精删（dataSourceForId + removeSource）
      ② 各键置空
      ③ getAllShapes 文本/标题前缀兜底（图表重载后 id 全失效，靠前缀删）
    顺带修复 BT·/RT· 此前只有 id 删除、图表重载后残留的历史问题。

    @returns 删除数量
    """
    cfg = cfg or CDPConfig()
    log = log or (lambda *a, **k: None)
    expr = (
        "(async () => { "
        "const chart = TradingViewApi.activeChart(); "
        "if (!chart) return -1; "
        "const cm = chart.chartModel(); "
        "const PREFIXES = " + json.dumps(CLEAR_PREFIXES) + "; "
        "const IDS_KEYS = " + json.dumps(CLEAR_IDS_KEYS) + "; "
        "let removed = 0; "
        # ① id 精删 + ② 置空
        "for (const key of IDS_KEYS) { "
        "  let ids = []; "
        "  try { ids = JSON.parse(localStorage.getItem(key) || '[]'); } catch (e) {} "
        "  for (const id of ids) { "
        "    try { "
        "      const ds = cm.dataSourceForId(id); "
        "      if (ds) { cm.removeSource(ds); removed++; } "
        "    } catch (err) {} "
        "  } "
        "  try { localStorage.setItem(key, '[]'); } catch (e) {} "
        "} "
        # ③ 前缀兜底（text 优先、title 兜底，与 _clear_marks 同款）
        "const readPrefix = (id) => { "
        "  try { "
        "    const sh = chart.getShapeById(id); "
        "    const props = sh && sh._source && sh._source._properties; "
        "    if (!props) return ''; "
        "    if (props.text && props.text._value) return String(props.text._value); "
        "    if (props.title && props.title._value) return String(props.title._value); "
        "    return ''; "
        "  } catch (e) { return ''; } "
        "}; "
        "try { "
        "  for (const s of chart.getAllShapes()) { "
        "    const p = readPrefix(s.id); "
        "    if (PREFIXES.some(pre => p.startsWith(pre))) { "
        "      try { chart.removeEntity(s.id); removed++; } catch (e) {} "
        "    } "
        "  } "
        "} catch (e) {} "
        "return removed; })()"
    )
    with CDPClient(cfg, log=log) as c:
        removed = int(c.evaluate(expr) or 0)
    log(f"已清除全部系统标记（ML·/BT·/RT·/SRT·/RAW·）{removed} 个")
    return removed
