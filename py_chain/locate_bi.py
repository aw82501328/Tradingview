"""点击定位后的次级别笔链：大周期覆盖检测、逐级计算与逐级绘制。

定位落地周期不动；用开仓时间在工作台大周期结构缓存（.cursor/cache/
bis_<symbol>.json，「更新全部」画到图上的 CHAN_BI_* 同源数据）里找覆盖该
时间的 60/240 大周期笔。找到 → 从 bars.db 逐级计算其下各级（240→[60,15,3]、
60→[15,3]）笔，逐级区间套锁定（首级锁覆盖笔两端点，之后每级锁上一级端点），
逐级切到该级周期创建折线（TV polyline 只能锚定「当前图表周期」的K线边界，
chan_bi.js 同款约束），画完由调用方切回定位周期。未覆盖（无缓存/不含该周期/
开仓时间不在任何笔区间内，如信号晚于缓存生成时间）→ 不画。

与 marks.py 的前缀/键约定：折线 title 前缀 ML·BI（「删除标记」的 ML· 前缀
兜底可清到），localStorage 键 mark_locate_bi_ids（需列入 marks.CLEAR_IDS_KEYS）。
"""
import json
import os
import re
import time

# 折线 title 前缀 / localStorage 键（marks.py 按相同值引用，改动须两处同步）
BI_PREFIX = "ML·BI"
BI_IDS_KEY = "mark_locate_bi_ids"
# 颜色与可见周期与 chan_bi.js resolutionColor / intervalVisibility 一致：
# 3 青蓝，只在 30秒+3分；15 紫，只在 3分+15分；60 黄，只在 15分+1小时；240 蓝，只在 1小时+4小时
BI_COLORS = {"3": "#00BCD4", "15": "#8A2BE2", "60": "#FFD700", "240": "#2962FF"}
_IV_OFF = {
    "ticks": False,
    "seconds": False, "secondsFrom": 1, "secondsTo": 59,
    "minutes": False, "minutesFrom": 1, "minutesTo": 59,
    "hours": False, "hoursFrom": 1, "hoursTo": 24,
    "days": False, "daysFrom": 1, "daysTo": 366,
    "weeks": False, "weeksFrom": 1, "weeksTo": 52,
    "months": False, "monthsFrom": 1, "monthsTo": 12,
}
BI_VISIBILITY = {
    "3": {**_IV_OFF, "seconds": True, "secondsFrom": 30, "secondsTo": 30,
          "minutes": True, "minutesFrom": 3, "minutesTo": 3},
    "15": {**_IV_OFF, "minutes": True, "minutesFrom": 3, "minutesTo": 15},
    "60": {**_IV_OFF, "minutes": True, "minutesFrom": 15, "minutesTo": 15,
           "hours": True, "hoursFrom": 1, "hoursTo": 1},
    "240": {**_IV_OFF, "hours": True, "hoursFrom": 1, "hoursTo": 4},
}

CHAIN_OF = {"240": ["60", "15", "3"], "60": ["15", "3"]}
BUFFER_BARS = 60   # 每级分型缓冲：上一级范围起点前再留的本级K线根数
DRAW_BARS = 40     # 每级绘制窗口：开仓时间往前留的本级K线根数（3m 不至于过密）
ATR_FILTER = 0.5   # 稳定ATR幅度过滤系数（与 chan_bi.js --atr 默认一致）

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cursor", "cache")


def _sanitize(symbol):
    """品种 → 缓存文件名安全字符（与 chan_bi.js bisCacheFile 同规则）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(symbol))


def find_covering_bi(symbol, upper_res, t, cache_dir=None):
    """在工作台大周期结构缓存里找覆盖时间 t 的上级笔。

    覆盖 = startTime <= t <= endTime；t 恰在两笔交界（前笔endTime==后笔startTime）
    时取后一根（顺序扫描保留最后命中）。无缓存/无该周期/未覆盖返回 None
    （信号晚于缓存生成时间即落入此分支：缓存末笔 endTime 早于 t）。
    """
    path = os.path.join(cache_dir or CACHE_DIR, f"bis_{_sanitize(symbol)}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    bis = (data.get("periods") or {}).get(str(upper_res)) or []
    hit = None
    for b in bis:
        st, et = b.get("startTime"), b.get("endTime")
        if st is None or et is None:
            continue
        if st <= t <= et:
            hit = b
    return hit


def _stable_atr(bars):
    """全窗口平均真实波幅（结构与行情无关的过滤基准，chan_bi.js 同款）。"""
    if len(bars) < 2:
        return 0.0
    tr = 0.0
    for i in range(1, len(bars)):
        h, lo, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        tr += max(h - lo, abs(h - pc), abs(lo - pc))
    return tr / (len(bars) - 1)


def _merge_aligned_gaps(bis, threshold):
    """对齐重建后的断口治理（镜像 chan_bi.js）：①相邻同向笔只可能来自被过滤
    中间笔的断口（alignBiToUpper 按「后笔start覆盖前笔end」重建端点会把断口
    缝合），合并为一笔，循环直到无连续同向；②清除仍低于阈值的桥接残余。"""
    i = len(bis) - 2
    while i >= 0:
        if i + 1 < len(bis) and bis[i]["type"] == bis[i + 1]["type"]:
            a, b = bis[i], bis[i + 1]
            bis[i] = {**a,
                      "endTime": b["endTime"], "endPrice": b["endPrice"],
                      "span": abs(b["endPrice"] - a["startPrice"]),
                      "rawCount": (a.get("rawCount") or 0) + (b.get("rawCount") or 0),
                      "gapLocked": bool(a.get("gapLocked")) or bool(b.get("gapLocked")),
                      "macdCross": bool(a.get("macdCross")) and bool(b.get("macdCross"))}
            del bis[i + 1]
            i += 1  # 合并后同位置再查一次（可能连续多段需合并）
        i -= 1
    return [b for b in bis if b["span"] >= threshold]


def _build_level(cc, res, bars, upper_bis, upper_res):
    """单级画笔管线（镜像 chan_bi.js 各周期步骤，除跨周期端点时间校准外全同）：
    buildBi(上级锁定) → fixBiExtremes → 稳定ATR过滤 → extendLastBi →
    alignBiToUpper(对上级，幽灵端点防御用长影剔除后K线) → 断口合并+补过滤。

    不做 calibrateBiTimes：各级端点均为本级K线原生时间（共享拐点已由
    alignBiToUpper 对齐），在本级周期图上创建折线锚点天然落在K线边界。
    """
    trimmed = cc.markWickBars(bars)
    merged = cc.mergeBars(trimmed)
    fractals = cc.findFractals(merged)
    bis = cc.buildBi(fractals, merged, cc.calcATR(bars, 14), cc.calcMACD(bars),
                     cc.lockedPivotsOf(upper_bis), cc.nearDoubleOn(res), res)
    bis = cc.fixBiExtremes(bis, merged) or bis
    threshold = _stable_atr(bars) * ATR_FILTER
    bis = [b for b in bis if b["span"] >= threshold]
    bis = cc.extendLastBi(bis, trimmed)
    bis = cc.alignBiToUpper(bis, upper_bis, cc.intervalSecOf(upper_res), trimmed)
    return _merge_aligned_gaps(bis, threshold)


def compute_lower_chain(symbol, upper_res, cover_bi, t_entry, t_to):
    """逐级计算覆盖笔之下的次级别笔链（区间套逐级锁定）。

    每级K线窗口 = 上一级范围起点前 BUFFER_BARS 根缓冲 → t_to；绘制窗口 =
    [max(覆盖笔起点, 开仓时间 − DRAW_BARS×本级秒), t_to]。某级缺数据/无成笔
    时记入 errors 并终止更深层（下级依赖上级笔）。返回
    {"upper", "levels": {res: [笔]}, "errors": {res: 原因}}。
    """
    from . import chan_core, data_store
    from .param_center import chan_cfg_effective
    chan_core.apply_cfg(chan_cfg_effective(symbol))
    out = {"upper": str(upper_res), "levels": {}, "errors": {}}
    prev_res, prev_bis = str(upper_res), [cover_bi]
    prev_from = cover_bi["startTime"]
    for res in CHAIN_OF.get(prev_res, []):
        sec = chan_core.intervalSecOf(res)
        if not sec:
            break
        try:
            bars = data_store.load_store(symbol, [res],
                                         from_ts=int(prev_from - BUFFER_BARS * sec),
                                         to_ts=int(t_to))[res]
        except ValueError as exc:
            out["errors"][res] = str(exc)
            break
        if len(bars) < 10:
            out["errors"][res] = f"{res}级K线不足（{len(bars)}根）"
            break
        bis = _build_level(chan_core, res, bars, prev_bis, prev_res)
        draw_from = max(cover_bi["startTime"], t_entry - DRAW_BARS * sec)
        bis = [b for b in bis if b["endTime"] >= draw_from and b["startTime"] <= t_to]
        if not bis:
            out["errors"][res] = "窗口内无成笔"
            break
        out["levels"][res] = bis
        prev_res, prev_bis, prev_from = res, bis, bis[0]["startTime"]
    return out


_WAIT_RES_JS = """(()=>{
  const c=TradingViewApi.activeChart(),m=c.chartModel(),s=m.mainSeries();
  const items=s.data().m_bars._items;
  return {res:String(c.resolution()),loading:!!s.isLoading(),len:items?items.length:0,
          first:items&&items.length?items[0].value[0]:null,
          last:items&&items.length?items[items.length-1].value[0]:null};
})()"""


def wait_resolution(c, res, need_from=None, timeout=40.0):
    """切换周期后等待K线加载：长度连续两次采样一致（稳定）且（可选）已覆盖
    need_from。已稳定 ~3.5s 仍覆盖不到（图表深度限制）也返回现状——未覆盖的
    笔由 draw_level 读回校验兜底跳过。读取失败/超时返回最后状态或 None。"""
    res, deadline = str(res), time.monotonic() + timeout
    last_len, stable, st = -1, 0, None
    while time.monotonic() < deadline:
        try:
            st = c.evaluate(_WAIT_RES_JS)
        except Exception:
            st = None
        if isinstance(st, dict) and st.get("res") == res and not st.get("loading"):
            stable = stable + 1 if st.get("len") == last_len else 0
            last_len = st.get("len")
            covered = need_from is None or (st.get("first") is not None and st["first"] <= need_from)
            if stable >= 2 and covered:
                return st
            if stable >= 5:  # 稳定但覆盖不到：带现状返回
                return st
        time.sleep(0.7)
    return st if isinstance(st, dict) else None


def _range_covers(st, need_from, need_to, sec=0):
    """已加载K线是否盖住 [need_from, need_to]（时间均为 unix 秒）。

    右缘按本级 K 线长度判断：末根开盘虽早于 need_to，但这根 K 线的区间已经
    含住 need_to 时视为已覆盖。
    """
    if not isinstance(st, dict):
        return False
    first, last = st.get("first"), st.get("last")
    if first is None or last is None:
        return False
    if need_from is not None and first > int(need_from):
        return False
    if need_to is not None and last < int(need_to) and last + max(int(sec), 0) <= int(need_to):
        return False
    return True


def ensure_level_bars(c, res, need_from, need_to, timeout=35.0):
    """当前图应已切到 res：保证主图K线盖住 [need_from, need_to]。

    实时图小周期只有约两个月，回放定位默认只带光标前约 300 根。盖不住时把
    回放光标放到 need_to（含该根K线），再 scrollToFirstBar 往前补。补不到
    返回现状，超范围的笔由 draw_level 读回校验跳过。
    """
    from .chan_core import intervalSecOf
    res = str(res)
    sec = intervalSecOf(res) or 60
    deadline = time.monotonic() + timeout
    st = wait_resolution(c, res, need_from=need_from,
                         timeout=min(8.0, max(1.0, deadline - time.monotonic())))
    if _range_covers(st, need_from, need_to, sec):
        return st
    from .data_loader import _scroll_to_first_bar
    from .monitor import replay_enter
    anchor = int(need_to if need_to is not None else need_from)
    cursor = anchor + max(1, sec - 1)
    now = int(time.time())
    if cursor > now:
        cursor = now
    try:
        replay_enter(c, cursor, fallback=False,
                     timeout=min(10.0, max(2.0, deadline - time.monotonic())))
    except Exception:
        return st if isinstance(st, dict) else None
    prev, stalls = None, 0
    while time.monotonic() < deadline:
        st = wait_resolution(c, res, need_from=need_from,
                             timeout=min(8.0, max(1.0, deadline - time.monotonic())))
        if _range_covers(st, need_from, need_to, sec):
            return st
        first = st.get("first") if isinstance(st, dict) else None
        if first is not None and first == prev:
            stalls += 1
            if stalls >= 2:
                return st
        else:
            stalls = 0
        prev = first
        if time.monotonic() >= deadline:
            break
        try:
            _scroll_to_first_bar(c)
        except Exception:
            return st
    return st if isinstance(st, dict) else None


_DRAW_LEVEL_JS = """(async()=>{
  const chart=TradingViewApi.activeChart();
  if(!chart)return {drawn:0,failed:0,cleared:0,error:'no_chart'};
  const cm=chart.chartModel();
  const p=PAYLOAD;
  let cleared=0;
  if(p.clearFirst){
    // 替换语义：id 精删（图表未重载）+ title 前缀兜底（重载后 id 失效）
    let ids=[];try{ids=JSON.parse(localStorage.getItem(p.idsKey)||'[]')}catch(e){}
    for(const id of ids){try{const ds=cm.dataSourceForId(id);if(ds){cm.removeSource(ds);cleared++}}catch(e){}}
    try{localStorage.setItem(p.idsKey,'[]')}catch(e){}
    const readTitle=(id)=>{try{const sh=chart.getShapeById(id);
      const pr=sh&&sh._source&&sh._source._properties;
      return pr&&pr.title?String(pr.title._value):''}catch(e){return ''}};
    try{for(const s of chart.getAllShapes()){
      if(s.name!=='polyline')continue;
      if(readTitle(s.id).indexOf(p.titlePrefix)===0){
        try{chart.removeEntity(s.id);cleared++}catch(e){}}}}catch(e){}
  }
  const okIds=[];let failed=0;
  const made=[];
  // 读回校验容差：秒级 ±2s；图表时间为毫秒形态时按毫秒比（防御，勿扩大）
  const tOk=(got,want)=>got!=null&&want!=null&&
    (Math.abs(got-want)<=2||Math.abs(got-want*1000)<=2000);
  for(const b of p.bis){
    let id=null;
    try{
      id=await chart.createMultipointShape(
        [{time:b.startTime+p.tz,price:b.startPrice},{time:b.endTime+p.tz,price:b.endPrice}],
        {shape:'polyline',lock:false,
         overrides:{linecolor:p.color,linewidth:1,title:p.title}});
    }catch(e){id=null;}
    if(!id){failed++;continue;}
    // 可见周期与画笔相同：只在本级和低一级（iv 由 chan_bi.js intervalVisibility 同口径传入）
    try{
      const iv=chart.getShapeById(id)._source._properties.intervalsVisibilities;
      const c=p.iv;
      iv.ticks.setValue(c.ticks);
      iv.seconds.setValue(c.seconds);iv.secondsFrom.setValue(c.secondsFrom);iv.secondsTo.setValue(c.secondsTo);
      iv.minutes.setValue(c.minutes);iv.minutesFrom.setValue(c.minutesFrom);iv.minutesTo.setValue(c.minutesTo);
      iv.hours.setValue(c.hours);iv.hoursFrom.setValue(c.hoursFrom);iv.hoursTo.setValue(c.hoursTo);
      iv.days.setValue(c.days);iv.daysFrom.setValue(c.daysFrom);iv.daysTo.setValue(c.daysTo);
      iv.weeks.setValue(c.weeks);iv.weeksFrom.setValue(c.weeksFrom);iv.weeksTo.setValue(c.weeksTo);
      iv.months.setValue(c.months);iv.monthsFrom.setValue(c.monthsFrom);iv.monthsTo.setValue(c.monthsTo);
      iv.ranges.setValue(false);
    }catch(e){}
    made.push({id, start:b.startTime, end:b.endTime});
  }
  // 回放定位后折线端点要等吸附：刚创建时 _points 只有 index、没有 time，
  // 约 2 秒后 time 才写成墙钟（逻辑开盘 +8 小时）。拿 time 和请求时间比会把
  // 锚对的笔全部判失败删掉。等沉降后用 index 回查主图 K 线开盘时间。
  if(made.length) await new Promise(r=>setTimeout(r,2500));
  const byIndex=new Map();
  const items=cm.mainSeries().data().m_bars._items||[];
  for(const bar of items) byIndex.set(bar.index, bar.value[0]);
  for(const it of made){
    // 读回校验：创建成功≠端点正确（TV 会把超出数据范围的时间静默吸附到数据
    // 边缘），端点不符的笔删除跳过并计数
    let good=false;
    try{
      const pts=chart.getShapeById(it.id)._source._points;
      if(pts&&pts.length>=2){
        const g0=byIndex.get(pts[0].index), g1=byIndex.get(pts[1].index);
        if(tOk(g0, it.start+p.tz)&&tOk(g1, it.end+p.tz))good=true;
      }
    }catch(e){}
    if(good)okIds.push(it.id);
    else{try{chart.removeEntity(it.id)}catch(e){};failed++;}
  }
  try{
    const old=p.clearFirst?[]:JSON.parse(localStorage.getItem(p.idsKey)||'[]');
    localStorage.setItem(p.idsKey,JSON.stringify(old.concat(okIds)));
  }catch(e){}
  return {drawn:okIds.length,failed,cleared};
})()"""


_COVER_JS = """(()=>{
  const chart=TradingViewApi.activeChart();
  if(!chart)return {n:0};
  const title='CHAN_BI_'+RES;
  const norm=(t)=>t==null?null:(t>1e12?t/1000:t);
  let n=0, minS=null, maxE=null;
  try{
    for(const s of chart.getAllShapes()){
      if(s.name!=='polyline')continue;
      let ttl='', pts=null;
      try{
        const src=chart.getShapeById(s.id)._source;
        ttl=src._properties&&src._properties.title?String(src._properties.title._value):'';
        pts=src._points;
      }catch(e){continue;}
      if(ttl!==title||!pts||pts.length<2)continue;
      const a=norm(pts[0].time), b=norm(pts[1].time);
      if(a==null||b==null)continue;
      n++;
      const lo=Math.min(a,b), hi=Math.max(a,b);
      if(minS===null||lo<minS)minS=lo;
      if(maxE===null||hi>maxE)maxE=hi;
    }
  }catch(e){return {n:0,error:String(e)};}
  return {n, minS, maxE};
})()"""


def chart_bi_covers(c, res, t):
    """图上画笔 CHAN_BI_<res> 的端点跨度是否已含时间 t。

    更新全部已经画过这段 15 分钟/3 分钟时，定位不再补同一段。回放里端点
    time 有的是逻辑开盘、有的是逻辑开盘加 8 小时，两种都认。读不到返回 False。
    """
    expr = _COVER_JS.replace("RES", json.dumps(str(res)))
    try:
        st = c.evaluate(expr)
    except Exception:
        return False
    if not isinstance(st, dict) or not st.get("n"):
        return False
    min_s, max_e = st.get("minS"), st.get("maxE")
    if min_s is None or max_e is None:
        return False
    t = int(t)
    for off in (0, 8 * 3600, -8 * 3600):
        x = t + off
        if min_s <= x <= max_e:
            return True
    return False


_CLEAR_BI_JS = """(()=>{
  const chart=TradingViewApi.activeChart();
  if(!chart)return {cleared:0};
  const cm=chart.chartModel();
  const prefix=PREFIX, idsKey=IDS;
  let cleared=0;
  let ids=[];try{ids=JSON.parse(localStorage.getItem(idsKey)||'[]')}catch(e){}
  for(const id of ids){try{const ds=cm.dataSourceForId(id);if(ds){cm.removeSource(ds);cleared++}}catch(e){}}
  try{localStorage.setItem(idsKey,'[]')}catch(e){}
  const readTitle=(id)=>{try{const sh=chart.getShapeById(id);
    const pr=sh&&sh._source&&sh._source._properties;
    return pr&&pr.title?String(pr.title._value):''}catch(e){return ''}};
  try{for(const s of chart.getAllShapes()){
    if(s.name!=='polyline')continue;
    if(readTitle(s.id).indexOf(prefix)===0){
      try{chart.removeEntity(s.id);cleared++}catch(e){}}}}catch(e){}
  return {cleared};
})()"""


def clear_locate_bis(c):
    """清掉定位补上的 ML·BI 折线（画笔 CHAN_BI 不动）。"""
    expr = (_CLEAR_BI_JS
            .replace("PREFIX", json.dumps(BI_PREFIX))
            .replace("IDS", json.dumps(BI_IDS_KEY)))
    try:
        r = c.evaluate(expr)
    except Exception as exc:
        return {"cleared": 0, "error": str(exc)}
    return r if isinstance(r, dict) else {"cleared": 0}


def draw_level(c, res, bis, tz_off=0, clear_first=False):
    """把一级笔画成折线（图表须已在本级周期上：折线只能锚定当前周期K线边界）。

    clear_first=True（首级）先清上一次的 ML·BI 折线（替换语义），后续级追加
    id。tz_off 为回放态图表时间轴整体平移量（正常图为 0）。端点校验等吸附
    沉降后按 index 回查 K 线开盘时间（回放里 _points.time 是墙钟，不能直接比）。
    失败不抛异常，计入返回值 {drawn, failed, cleared}。
    """
    if not bis:
        return {"drawn": 0, "failed": 0, "cleared": 0}
    payload = {
        "bis": [{"startTime": int(b["startTime"]), "startPrice": float(b["startPrice"]),
                 "endTime": int(b["endTime"]), "endPrice": float(b["endPrice"])}
                for b in bis],
        "color": BI_COLORS[str(res)],
        "iv": BI_VISIBILITY[str(res)],
        "title": f"{BI_PREFIX}·{res}",
        "titlePrefix": BI_PREFIX,
        "idsKey": BI_IDS_KEY,
        "tz": int(tz_off),
        "clearFirst": bool(clear_first),
    }
    expr = _DRAW_LEVEL_JS.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False))
    try:
        # 套接字读超时上限是 EVALUATE_READ_TIMEOUT（45s）；这里显式顶满，
        # 含创建后等 2.5s 吸附沉降
        r = c.evaluate(expr, timeout=45000, read_timeout=45.0)
    except Exception as exc:
        return {"drawn": 0, "failed": len(bis), "cleared": 0, "error": str(exc)}
    return r if isinstance(r, dict) else {"drawn": 0, "failed": len(bis), "cleared": 0,
                                          "error": "画笔无返回"}
