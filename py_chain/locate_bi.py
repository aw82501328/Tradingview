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
# 每级一色：60 橙 / 15 蓝 / 3 青（CHAN_BI 工作台层不动，这是定位临时叠层）
BI_COLORS = {"60": "#FF9800", "15": "#2962FF", "3": "#00B8D4"}

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
          first:items&&items.length?items[0].value[0]:null};
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
    // 全开周期可见性（与单行标记同模板：六类全 true 且 From/To 铺满——只写
    // 布尔位时 TV 沿用工具模板残留的 From/To 会把部分周期挡在外面）
    try{
      const iv=chart.getShapeById(id)._source._properties.intervalsVisibilities;
      iv.ticks.setValue(false);
      iv.seconds.setValue(true);iv.secondsFrom.setValue(1);iv.secondsTo.setValue(59);
      iv.minutes.setValue(true);iv.minutesFrom.setValue(1);iv.minutesTo.setValue(59);
      iv.hours.setValue(true);iv.hoursFrom.setValue(1);iv.hoursTo.setValue(24);
      iv.days.setValue(true);iv.daysFrom.setValue(1);iv.daysTo.setValue(366);
      iv.weeks.setValue(true);iv.weeksFrom.setValue(1);iv.weeksTo.setValue(52);
      iv.months.setValue(true);iv.monthsFrom.setValue(1);iv.monthsTo.setValue(12);
      iv.ranges.setValue(false);
    }catch(e){}
    // 读回校验：创建成功≠端点正确（TV 会把超出数据范围的时间静默吸附到数据
    // 边缘），端点不符的笔删除跳过并计数
    let good=false;
    try{
      const pts=chart.getShapeById(id)._source._points;
      if(pts&&pts.length>=2&&tOk(pts[0].time,b.startTime+p.tz)&&
         tOk(pts[1].time,b.endTime+p.tz))good=true;
    }catch(e){}
    if(good)okIds.push(id);
    else{try{chart.removeEntity(id)}catch(e){};failed++;}
  }
  try{
    const old=p.clearFirst?[]:JSON.parse(localStorage.getItem(p.idsKey)||'[]');
    localStorage.setItem(p.idsKey,JSON.stringify(old.concat(okIds)));
  }catch(e){}
  return {drawn:okIds.length,failed,cleared};
})()"""


def draw_level(c, res, bis, tz_off=0, clear_first=False):
    """把一级笔画成折线（图表须已在本级周期上：折线只能锚定当前周期K线边界）。

    clear_first=True（首级）先清上一次的 ML·BI 折线（替换语义），后续级追加
    id。tz_off 为回放态图表时间轴整体平移量（正常图为 0）。失败不抛异常，
    计入返回值 {drawn, failed, cleared}。
    """
    if not bis:
        return {"drawn": 0, "failed": 0, "cleared": 0}
    payload = {
        "bis": [{"startTime": int(b["startTime"]), "startPrice": float(b["startPrice"]),
                 "endTime": int(b["endTime"]), "endPrice": float(b["endPrice"])}
                for b in bis],
        "color": BI_COLORS.get(str(res), "#2962FF"),
        "title": f"{BI_PREFIX}·{res}",
        "titlePrefix": BI_PREFIX,
        "idsKey": BI_IDS_KEY,
        "tz": int(tz_off),
        "clearFirst": bool(clear_first),
    }
    expr = _DRAW_LEVEL_JS.replace("PAYLOAD", json.dumps(payload, ensure_ascii=False))
    try:
        r = c.evaluate(expr, timeout=60000, read_timeout=90.0)
    except Exception as exc:
        return {"drawn": 0, "failed": len(bis), "cleared": 0, "error": str(exc)}
    return r if isinstance(r, dict) else {"drawn": 0, "failed": len(bis), "cleared": 0,
                                          "error": "画笔无返回"}
