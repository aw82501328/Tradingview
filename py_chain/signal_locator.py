"""Locate a signal on the shared TradingView chart without changing drawings."""
import json
import math
import re
import threading
import time
import uuid

from .data_loader import CDPClient, CDPError, _set_symbol, _set_resolution


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


def locate_signal(row, cfg=None, timeout=360):
    symbol, res, stamp = validate_signal(row)
    with CDPClient(cfg, log=lambda *_: None) as c:
        _set_symbol(c, symbol)
        _set_resolution(c, res)
        deadline = time.monotonic() + timeout
        target = json.dumps({'symbol': symbol, 'res': res, 'time': stamp})
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
        while time.monotonic() < deadline:
            remaining = max(0.001, deadline - time.monotonic())
            data = c.evaluate(read, timeout=min(30000, max(1, int(remaining * 1000))), read_timeout=remaining)
            if data:
                if data['bar']:
                    # A timestamp within the latest candle is valid; gaps and
                    # timestamps beyond available history must not match stale bars.
                    if stamp >= data['barEnd']:
                        raise CDPError('目标时间超出图表数据范围，历史数据不可用')
                    break
                if data['end']:
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
          const ts=m.timeScale();
          if(typeof m.setTimeViewport!=='function')throw Error('当前TradingView版本不支持图表定位');
          s.priceScale().setMode({autoScale:true});
          m.setTimeViewport(bar.index-50,bar.index+50);
          const range=ts.visibleBarsStrictRange();
          if(!range || bar.index<range.firstBar() || bar.index>range.lastBar())throw Error('目标K线未进入可视范围');
          return {symbol:c.symbol(),markRes:String(c.resolution()),time:bar.value[0],
                  index:bar.index,fromIndex:range.firstBar(),toIndex:range.lastBar()};
        })()""".replace('TARGET', target))
        return result


class LocateManager:
    def __init__(self, signals, chart_lock, emit):
        self.signals, self.chart_lock, self.emit = signals, chart_lock, emit
        self.lock = threading.Lock()
        self.job = None

    def snapshot(self):
        with self.lock:
            return dict(self.job) if self.job else None

    def start(self, mode, row_id):
        row = self.signals.get(row_id, mode)
        if row is None:
            raise LookupError('记录已清空或不存在，请刷新列表')
        validate_signal(row)
        if not self.chart_lock.acquire(blocking=False):
            raise RuntimeError('图表正被任务占用，请结束运行或暂停中的任务，并等待图表操作完成后再定位')
        job = {'jobId': uuid.uuid4().hex, 'mode': mode, 'id': row_id, 'state': 'running', 'error': None}
        with self.lock:
            self.job = job

        def work():
            result, error = None, None
            try:
                if self.signals.get(row_id, mode) is None:
                    raise LookupError('记录已清空，请刷新列表')
                result = locate_signal(row)
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
