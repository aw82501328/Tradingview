"""Locate a signal on the shared TradingView chart and mark that row's entries/exits.

定位本身只切品种/周期并居中目标K线；居中完成后在同一 CDP 会话内画该行的
单行标记（进场箭头+窗口内出场+近支阻横线）。回放定位用图表该根 K 线的
真实时间做锚点、不写周期可见性（回放里 IV 会把箭头藏掉）。
标记失败不否定定位结果（result['mark'] 带 error 说明）。

实时图历史不够时（尤其 30S 约一周、更早只能回放拿到）：用 Bar Replay
跳到最后出场之后（无出场则进场后再留一段K线），这样进场之后的走势和离场
标记都在窗口内。跳转成功后图表停在回放态。
"""
import json
import math
import re
import threading
import time
import uuid

from .data_loader import CDPClient, CDPError, _set_symbol, _set_resolution
from .marks import (
    EXIT_NAMES, EXIT_SHAPES, SINGLE_IDS_KEY, SINGLE_PREFIX,
    _clear_single_marks, _colors,
)

# 定位视口：最后出场（无出场则进场）之后再留的 K 线根数
DEFAULT_AFTER_BARS = 60


def parse_after_bars(v, default=DEFAULT_AFTER_BARS):
    """解析定位后保留 K 线根数；缺省 60，须为非负整数。"""
    if v is None or v == '':
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError('定位后保留K线须为非负整数') from None
    if n < 0:
        raise ValueError('定位后保留K线须为非负整数')
    return n


def validate_signal(row):
    if not isinstance(row.get('symbol'), str) or not row['symbol'].strip():
        raise ValueError('旧记录缺少品种，请重新回测后定位')
    stamp = row.get('time')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
        raise ValueError('记录缺少有效信号时间，无法定位')
    res = str(row.get('markRes') or '')
    if not re.fullmatch(r'(?:[1-9]\d*(?:S|D|W|M)?|D|W|M)', res):
        raise ValueError('记录缺少有效背驰周期，无法定位')
    return row['symbol'].strip(), res, stamp


def _view_until(row, stamp, res, after_bars=DEFAULT_AFTER_BARS):
    """回放光标与视口右端：覆盖进场到最后出场，并再留一段K线。

    回放只加载 ≤光标 的K线。光标停在进场根则进场之后全无数据，离场也无法标记。
    有出场时拉到最后离场后再留 after_bars 根；无出场时从进场再往后 after_bars 根。
    """
    from .chan_core import intervalSecOf
    times = [int(stamp)]
    for key in ('entryTime', 'exitTime'):
        t = row.get(key)
        if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t > 0:
            times.append(int(t))
    for ev in (row.get('exits') or []):
        t = ev.get('time')
        if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and t > 0:
            times.append(int(t))
    last = max(times)
    sec = intervalSecOf(res) or 60
    # 离场（或尚无出场的进场）后再留 after_bars 根，方便看后续走势
    return last + sec * after_bars


def _replay_to_signal(c, stamp, deadline, res=None, until=None):
    """实时图未覆盖目标K线时，用 Bar Replay 跳到【出场之后】加载窗口。

    光标必须晚于最后出场（或进场后再留一段），否则进场之后的K线不存在、离场画不上。
    进场根本身用 stamp 在窗口内查找。
    """
    from .chan_core import intervalSecOf
    from .monitor import replay_check, replay_enter
    remaining = deadline - time.monotonic()
    if remaining < 2:
        raise CDPError('历史数据加载超时（360秒），无法定位信号K线')
    replay_check(c, log=lambda *_: None)
    sec = intervalSecOf(res) if res else 0
    end = int(until) if until else int(stamp)
    cursor = end + max(1, (sec or 1) - 1)
    replay_enter(c, cursor, fallback=False, timeout=min(10.0, remaining))


def _row_for_mark(row, bar_time, last_time=None):
    """锚点钉到已定位K线开盘；回放窗口之外的出场不画（画了也是空锚）。"""
    out = dict(row)
    out['time'] = int(bar_time)
    if last_time is None:
        return out
    last_time = int(last_time)
    kept = []
    for ev in (row.get('exits') or []):
        t = ev.get('time')
        if t is None or ev.get('price') is None:
            continue
        try:
            t = int(t)
        except (TypeError, ValueError):
            continue
        if t > last_time:
            continue
        kept.append({**ev, 'time': t})
    out['exits'] = kept
    return out


def _draw_after_locate(c, row, bar_time, colors=None, last_time=None):
    """在已居中的目标K线上画单行标记（定位专用，带全开周期可见性）。

    进/出场箭头与支阻横线写全开 intervalsVisibilities（六类周期全 true 且
    From/To 铺满全范围），切到任意周期都显示——由代码保证，不靠 TV 沿用工具
    默认模板（不写或只写布尔位时，模板残留的 From/To 会把 30m/240m 等周期
    挡在外面，2026-09-25 真机读图实测）。
    锚点用图表里该根 bar.value[0]、lock=true；秒/毫秒两种时间各试一次。
    出场若晚于当前窗口则跳过。
    箭头不压K线（用户规则 2026-10-02，与 marks._draw_chunk 同口径）：
    arrow_up 锚在该根 low 下方、arrow_down 锚在 high 上方，外移量 =
    max(本根振幅, 近邻±10根均幅) × 0.6；算不出有效外移量时回退信号价。
    """
    colors = _colors(colors)
    if row.get('status') == '同向过滤':
        try:
            cleared = int(_clear_single_marks(c) or 0)
        except Exception:
            cleared = 0
        return {'drawn': 0, 'cleared': cleared, 'skipped': '同向过滤'}
    if row.get('price') is None or not row.get('direction') or not row.get('time'):
        return {'drawn': 0, 'cleared': 0, 'skipped': '缺价格/方向/时间'}
    cleared = 0
    try:
        cleared = int(_clear_single_marks(c) or 0)
    except Exception:
        pass
    exits = []
    for ev in (_row_for_mark(row, bar_time, last_time).get('exits') or []):
        et = ev.get('type')
        if et not in EXIT_SHAPES or ev.get('price') is None or ev.get('time') is None:
            continue
        exits.append({
            'time': int(ev['time']), 'price': float(ev['price']),
            'text': f"{SINGLE_PREFIX}{EXIT_NAMES[et]} {float(ev['price']):.2f}",
        })
    near = row.get('nearSr')
    try:
        near = float(near) if near is not None else None
        if near is not None and not math.isfinite(near):
            near = None
    except (TypeError, ValueError):
        near = None
    payload = {
        'time': int(bar_time),
        'price': float(row['price']),
        'long': row['direction'] == 'long',
        'text': f"{SINGLE_PREFIX}{'BUY' if row['direction'] == 'long' else 'SELL'} {float(row['price']):.2f}",
        'color': colors['buy'] if row['direction'] == 'long' else colors['sell'],
        'exitColor': colors['exit'],
        'sr': colors['sr'],
        'nearSr': near,
        'srText': None if near is None else f"{SINGLE_PREFIX}SR {near:.2f}",
        'exits': exits,
        'idsKey': SINGLE_IDS_KEY,
        'exitDown': row['direction'] == 'long',
        # 全开周期可见性：六类全 true 且 From/To 写满全范围——只写布尔位不够，
        # TV 会沿用工具默认模板的 From/To（实测残留 minutesTo:15 / hoursTo:1，
        # 把 30m/240m 挡在外面），必须显式铺满才真正做到任意周期都显示
        'iv': {'seconds': True, 'secondsFrom': 1, 'secondsTo': 59,
               'minutes': True, 'minutesFrom': 1, 'minutesTo': 59,
               'hours': True, 'hoursFrom': 1, 'hoursTo': 24,
               'days': True, 'daysFrom': 1, 'daysTo': 366,
               'weeks': True, 'weeksFrom': 1, 'weeksTo': 52,
               'months': True, 'monthsFrom': 1, 'monthsTo': 12},
    }
    expr = """(async()=>{
      const p=PAYLOAD;
      const chart=TradingViewApi.activeChart();
      if(!chart)return {drawn:0,error:'no_chart'};
      const bars=chart.chartModel().mainSeries().data().m_bars._items;
      if(!bars||!bars.length)return {drawn:0,error:'no_bars'};
      let lo=0,hi=bars.length;
      while(lo<hi){const mid=(lo+hi)>>1;if(bars[mid].value[0]<=p.time)lo=mid+1;else hi=mid;}
      const bar=bars[lo-1];
      if(!bar)return {drawn:0,error:'no_bar'};
      const t0=bar.value[0];
      const times=t0>1e12?[t0/1000,t0]:[t0,t0*1000];
      // 锚点外移（不压K线）：先按二分定位 q 所在根，再按箭头方向放到
      // low 下方（arrow_up）/ high 上方（arrow_down），margin=max(本根振幅,近邻±10根均幅)*0.6
      const norm=b=>{const t0=b.value[0];return t0>1e12?t0/1000:t0;};
      function margin4(i){
        const hiP=bars[i].value[2],loP=bars[i].value[3];
        let sum=0,n=0;
        for(let k=Math.max(0,i-10);k<Math.min(bars.length,i+11);k++)
          {sum+=bars[k].value[2]-bars[k].value[3];n++;}
        const m=Math.max(hiP-loP,n?sum/n:0)*0.6;
        return (isFinite(m)&&m>0)?m:null;
      }
      function anchorOff(q,up,ref){
        let l2=0,h2=bars.length;
        while(l2<h2){const mid=(l2+h2)>>1;if(norm(bars[mid])<=q)l2=mid+1;else h2=mid;}
        const i=l2-1;
        if(i<0)return ref;
        const m=margin4(i);
        if(m==null)return ref;
        const hiP=bars[i].value[2],loP=bars[i].value[3];
        return up?loP-m:hiP+m;
      }
      // 进场：做多 arrow_up → low 下方；做空 arrow_down → high 上方（无有效振幅回退信号价）
      let price=anchorOff(p.time,p.long,p.price);
      const ids=[]; let lastErr='';
      async function put(time,pr,spec){
        try{
          const v=await chart.createShape({time:time,price:pr},spec);
          if(v){ids.push(v);return true;}
          lastErr='createShape返回空'; return false;
        }catch(e){ lastErr=String(e&&e.message||e); return false; }
      }
      const arrow=p.long?'arrow_up':'arrow_down';
      const spec={shape:arrow,text:p.text,lock:true,color:p.color,textColor:p.color,
                  overrides:{arrowColor:p.color,intervalsVisibilities:p.iv}};
      let ok=false;
      for(const t of times){ if(await put(t,price,spec)){ok=true;break;} }
      if(!ok){
        spec.shape=p.long?'arrow_mark_up':'arrow_mark_down';
        for(const t of times){ if(await put(t,price,spec)){ok=true;break;} }
      }
      const exitShape=p.exitDown?'arrow_down':'arrow_up';
      for(const ev of p.exits){
        const es={shape:exitShape,text:ev.text,lock:true,color:p.exitColor,textColor:p.exitColor,
                  overrides:{arrowColor:p.exitColor,intervalsVisibilities:p.iv}};
        // 出场与进场同口径外移：平多 ↓ 在 high 上方、平空 ↑ 在 low 下方
        const pr=anchorOff(ev.time,!p.exitDown,ev.price);
        for(const t of (ev.time>1e12?[ev.time/1000,ev.time]:[ev.time,ev.time*1000])){
          if(await put(t,pr,es))break;
        }
      }
      if(p.nearSr!=null){
        const hs={shape:'horizontal_line',lock:true,text:p.srText,
                  overrides:{linecolor:p.sr,linewidth:1,linestyle:0,showPriceLabels:false,
                             intervalsVisibilities:p.iv}};
        for(const t of times){ if(await put(t,p.nearSr,hs))break; }
      }
      try{
        const old=JSON.parse(localStorage.getItem(p.idsKey)||'[]');
        localStorage.setItem(p.idsKey,JSON.stringify(old.concat(ids)));
      }catch(e){}
      const out={drawn:ids.length,ids:ids,barTime:t0,price:price};
      if(!ids.length) out.error=lastErr||'createShape未返回id';
      return out;
    })()""".replace('PAYLOAD', json.dumps(payload, ensure_ascii=False))
    r = c.evaluate(expr)
    if not isinstance(r, dict):
        return {'drawn': 0, 'cleared': cleared, 'error': '画标记无返回'}
    r['cleared'] = cleared
    return r


def locate_signal(row, cfg=None, timeout=360, colors=None, after_bars=DEFAULT_AFTER_BARS):
    from .monitor import replay_started
    symbol, res, stamp = validate_signal(row)
    after_bars = parse_after_bars(after_bars)
    view_until = _view_until(row, stamp, res, after_bars)
    with CDPClient(cfg, log=lambda *_: None) as c:
        _set_symbol(c, symbol)
        _set_resolution(c, res)
        deadline = time.monotonic() + timeout
        target = json.dumps({'symbol': symbol, 'res': res, 'time': stamp, 'until': view_until})
        # Data bars carry logical indices; array positions are not chart indices.
        read = """(()=>{
          const target=TARGET, c=TradingViewApi.activeChart(), m=c.chartModel(), s=m.mainSeries();
          if(s.isSymbolInvalid())throw Error('品种不可用');
          if(c.symbol()!==target.symbol || String(c.resolution())!==target.res ||
             s.symbolInfo()?.full_name!==target.symbol || s.isLoading())return null;
          const bars=s.data().m_bars._items;
          if(!bars?.length)return null;
          let lo=0,hi=bars.length;
          while(lo<hi){const mid=(lo+hi)>>1;if(bars[mid].value[0]<=target.time)lo=mid+1;else hi=mid;}
          const i=lo-1;
          let end=null;
          if(i>=0){
            const start=bars[i].value[0], count=parseInt(target.res,10)||1, unit=target.res.slice(-1);
            if(unit==='M'){const d=new Date(start*1000);d.setUTCMonth(d.getUTCMonth()+count);end=d.getTime()/1000;}
            else end=start+count*({S:1,D:86400,W:604800}[unit]||60);
          }
          return {first:bars[0].value[0],last:bars[bars.length-1].value[0],
                  bar:i>=0?{time:bars[i].value[0],index:bars[i].index}:null,
                  barEnd:end,next:bars[i+1]?.value[0],end:!!s.endOfData()};
        })()""".replace('TARGET', target)
        last_scroll = -float('inf')
        jumped = False
        jump_at = None
        # 回放态二次定位：上次光标封死的窗口不够本次 after_bars 时已按新 until 扩窗
        # （只扩一次——last 总比 until 早开盘若干秒，不设防会反复重跳）
        extended = False
        while time.monotonic() < deadline:
            remaining = max(0.001, deadline - time.monotonic())
            data = c.evaluate(read, timeout=min(30000, max(1, int(remaining * 1000))), read_timeout=remaining)
            if data:
                if data['bar']:
                    # A timestamp within the latest candle is valid; gaps must
                    # not match stale bars. 晚于最后一根则回放跳转（含信号开盘根），
                    # 不能在已跳到历史时刻后因 stamp>=barEnd 放弃画标记。
                    if stamp < data['barEnd']:
                        # 回放态下上次定位的光标封死了已加载窗口：保留根数调大时
                        # 目标K线虽已覆盖，仍须按本次 until 重跳扩窗，否则视口右缘不动
                        # （实时图 until 在未来时不进回放，维持钳到最新一根的预期行为）。
                        if (not extended and data['last'] is not None
                                and data['last'] < view_until
                                and replay_started(c)):
                            try:
                                _replay_to_signal(c, stamp, deadline, res, until=view_until)
                            except CDPError:
                                raise
                            except Exception as exc:
                                raise CDPError(f'按保留根数扩窗失败：{exc}') from exc
                            extended = True
                            last_scroll = -float('inf')
                            continue
                        break
                    if data.get('next') is not None:
                        raise CDPError('目标时间超出图表数据范围，历史数据不可用')
                    if not jumped:
                        try:
                            _replay_to_signal(c, stamp, deadline, res, until=view_until)
                        except CDPError:
                            raise
                        except Exception as exc:
                            raise CDPError('目标时间超出图表数据范围，历史数据不可用') from exc
                        jumped = True
                        jump_at = time.monotonic()
                        last_scroll = -float('inf')
                        continue
                    if jump_at is None or time.monotonic() - jump_at >= 12:
                        raise CDPError('目标时间超出图表数据范围，历史数据不可用')
                elif data['end']:
                    # 目标早于已加载第一根：实时图宣称到头时先 Bar Replay 跳转
                    if not jumped:
                        try:
                            _replay_to_signal(c, stamp, deadline, res, until=view_until)
                        except CDPError:
                            raise
                        except Exception as exc:
                            raise CDPError(
                                '历史数据不可用，无法加载目标信号K线（30秒历史可能受限）'
                                f'；回放跳转失败：{exc}') from exc
                        jumped = True
                        jump_at = time.monotonic()
                        last_scroll = -float('inf')
                        continue
                    # selectDate 后窗口替换需要沉降；到期仍找不到再判失败
                    if jump_at is None or time.monotonic() - jump_at >= 12:
                        raise CDPError('历史数据不可用，无法加载目标信号K线（30秒历史可能受限）')
                if time.monotonic() - last_scroll >= 18:
                    remaining = max(0.001, deadline - time.monotonic())
                    c.evaluate('TradingViewApi.activeChart().chartModel().timeScale().scrollToFirstBar()',
                               timeout=min(30000, max(1, int(remaining * 1000))), read_timeout=remaining)
                    last_scroll = time.monotonic()
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        else:
            raise CDPError('历史数据加载超时（360秒），无法定位信号K线')
        # Re-read and locate within one evaluation, as loading can reindex all bars.
        result = c.evaluate("""(()=>{
          const target=TARGET,c=TradingViewApi.activeChart(),m=c.chartModel(),s=m.mainSeries();
          if(c.symbol()!==target.symbol || String(c.resolution())!==target.res || s.isLoading())
            throw Error('图表已变化，请重新定位');
          const bars=s.data().m_bars._items;
          let lo=0,hi=bars.length;
          while(lo<hi){const mid=(lo+hi)>>1;if(bars[mid].value[0]<=target.time)lo=mid+1;else hi=mid;}
          const bar=bars[lo-1];if(!bar)throw Error('目标K线不可用');
          let uLo=0,uHi=bars.length, until=target.until||target.time;
          while(uLo<uHi){const mid=(uLo+uHi)>>1;if(bars[mid].value[0]<=until)uLo=mid+1;else uHi=mid;}
          const uBar=bars[uLo-1]||bar;
          const ts=m.timeScale();
          if(typeof m.setTimeViewport!=='function')throw Error('当前TradingView版本不支持图表定位');
          s.priceScale().setMode({autoScale:true});
          const from=Math.min(bar.index,uBar.index)-50;
          const to=Math.max(bar.index,uBar.index);
          m.setTimeViewport(from,to);
          const range=ts.visibleBarsStrictRange();
          if(!range || bar.index<range.firstBar() || bar.index>range.lastBar())throw Error('目标K线未进入可视范围');
          return {symbol:c.symbol(),markRes:String(c.resolution()),time:bar.value[0],
                  index:bar.index,fromIndex:range.firstBar(),toIndex:range.lastBar(),
                  until:uBar.value[0]};
        })()""".replace('TARGET', target))
        # 视口已居中：同一 CDP 会话内画该行的单行标记（替换语义，见
        # marks.draw_single_mark）。失败不否定定位本身，只把错误带回 result['mark']。
        if jumped:
            result['replay'] = True
        try:
            result['mark'] = _draw_after_locate(
                c, row, result['time'], colors=colors,
                last_time=data.get('last'))
        except Exception as exc:
            result['mark'] = {'drawn': 0, 'cleared': 0, 'error': str(exc)}
        return result


class LocateManager:
    def __init__(self, signals, chart_lock, emit):
        self.signals, self.chart_lock, self.emit = signals, chart_lock, emit
        self.lock = threading.Lock()
        self.job = None

    def snapshot(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def start(self, mode, row_id, colors=None, row=None, after_bars=None):
        """启动定位任务。

        row 非空时直接用该快照（历史方案明细），不再查内存 SignalLog；
        否则按 mode+row_id 取服务端当前记录。
        after_bars：出场（无出场则进场）之后再留的 K 线根数，缺省 60。
        """
        after_bars = parse_after_bars(after_bars)
        if row is not None:
            if not isinstance(row, dict):
                raise ValueError('无效的记录快照')
            row = dict(row)
            if type(row_id) is not int or row_id <= 0:
                rid = row.get('id')
                if type(rid) is not int or rid <= 0:
                    raise ValueError('无效的记录ID')
                row_id = rid
            row['id'] = row_id
            if mode not in ('backtest', 'replay', 'live'):
                mode = row.get('mode') if row.get('mode') in ('backtest', 'replay', 'live') else 'backtest'
            validate_signal(row)
            from_log = False
        else:
            row = self.signals.get(row_id, mode)
            if row is None:
                raise LookupError('记录已清空或不存在，请刷新列表')
            validate_signal(row)
            from_log = True
        if not self.chart_lock.acquire(blocking=False):
            raise RuntimeError('图表正被任务占用，请结束运行或暂停中的任务，并等待图表操作完成后再定位')
        job = {'jobId': uuid.uuid4().hex, 'mode': mode, 'id': row_id, 'state': 'running', 'error': None}
        with self.lock:
            self.job = job

        def work():
            result, error = None, None
            try:
                # 内存表定位：任务期间若被清空则中止；快照定位不依赖内存表
                if from_log and self.signals.get(row_id, mode) is None:
                    raise LookupError('记录已清空，请刷新列表')
                result = locate_signal(row, colors=colors, after_bars=after_bars)
            except Exception as exc:
                error = str(exc)
            finally:
                with self.lock:
                    self.job = {**job, 'state': 'error' if error else 'done', 'error': error, 'result': result}
                    completed = dict(self.job)
                self.chart_lock.release()
                self.emit('locate_done', completed)
        try:
            threading.Thread(target=work, daemon=True, name='signal-locate').start()
        except Exception as exc:
            with self.lock:
                self.job = {**job, 'state': 'error', 'error': str(exc)}
            self.chart_lock.release()
            raise
        return dict(job)
