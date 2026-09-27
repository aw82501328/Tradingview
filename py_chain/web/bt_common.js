// 三模式控制台（index.html）与方案明细独立页（bt_detail.html）共享的渲染/格式化核心。
// 单一事实源：exitDisplayRows（手数×合约乘数口径）、signalRowsHtml（表格列）、drawEquityChart
// 等改动必须只改这里，两页同时生效。页面各自的内联脚本只放页面专属逻辑。
// 注意：本文件不访问页面专属全局（sigRows/currentSymFilter 等）——需要页面上下文的值
// 一律由调用方通过参数传入（如 signalRowsHtml 的 opts.noOf / opts.showSym）。

function el(id) { return document.getElementById(id); }

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const pnlClass = v => (v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '');
const fmtPnlText = v => v == null ? '-' : (v > 0 ? '+' : '') + Number(v).toFixed(2);

function fmtTime(t) {
  if (!t) return '-';
  // UTC+8（北京时间）显示：China Standard Time 无夏令时，偏移直接加 8 小时
  const d = new Date((t + 8 * 3600) * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth()+1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

function signalDirectionName(r) {
  // 顺势过滤开启（信号带 trendReason）：方向列显示参考周期锚方向 + 成因，如「多（4小时2买）」
  if (r.trendReason) return `${r.direction === 'short' ? '空' : '多'}（${r.trendReason}）`;
  const directions = ['多头多', '空头空', '多头空', '空头多'];
  if (directions.includes(r.planDirection)) return r.planDirection;
  // 旧记录未保存 planDirection，按 trading_plan.strategyOf / entryStrategyOf 的固定对应关系显示。
  // 2026-09-25 键拆分：wait3Buy/waitLike2Buy/wait3Sell/waitLike2Sell 均为顺势（多头多/空头空）。
  return {wait2Buy:'多头多',waitBuy:'多头多',wait3Buy:'多头多',waitLike2Buy:'多头多',
    wait2Sell:'空头空',waitSell:'空头空',wait3Sell:'空头空',waitLike2Sell:'空头空',
    wait1Sell:'多头空',wait1Buy:'空头多'}[r.strategyKey] || '-';
}

// 信号候选角标（SPEC_divergence_fallback）：回退/近等/预期够笔口径标记
function candidateBadges(r) {
  const b = [];
  if (r.fallback) b.push(['回退', '下沉停止级无候选，沿链上一级回退命中（M1）']);
  if (r.nearEqual) b.push(['近等', '未严格创新极值、近等容差+三判据AND命中（M2）']);
  if (r.expectBi) b.push(['预期', '检测周期末笔反向、按预期够笔口径放行（M4）']);
  return b.map(([t, tip]) => `<span class="mini-badge" title="${tip}">${t}</span>`).join('');
}

// 出场类型：stopSr=支阻位止损（支阻位±止损滑点，再按最大止损夹紧）
//          | stopBe=保本止损（beStop=进场K线极值±保本滑点）| close=全平（顺势=有利方向破前高/低；
//          逆势=形成段≥5合并K）；exits 含 half=平一半（顺势形成段≥5合并K）/breakeven=保本
function exitTypeName(t) {
  if (t === 'half') return '半平';
  if (t === 'stopSr') return '支阻位止损';
  if (t === 'stopBe') return '保本止损';
  if (t === 'close') return '全平';
  return t || '-';
}
function exitEventsDesc(r) {
  const names = { breakeven: '保本', half: '半', close: '平', stopSr: '损', stopBe: '保损' };
  return (r.exits || []).map(e => names[e.type] || e.type).join('·') || '';
}
// 分批出场只拆分展示，仍按整笔交易计数、汇总和排序。
function exitDisplayRows(r) {
  const numeric = value => value != null && Number.isFinite(Number(value)) ? Number(value) : null;
  const totalPnl = numeric(r.pnl), lots = numeric(r.lots);
  const mult = numeric(r.mult) ?? 1;   // 合约乘数快照（2026-09-23：1手=0.01标准手；旧行无此键 → 1）
  const final = {time:r.exitTime,price:r.exitPrice,type:r.exitType,
    label:r.exitType ? exitTypeName(r.exitType) : (r.state === 'open' ? '持仓中' : '-'),lots,pnl:totalPnl};
  // 现有结算按首次 half 平掉原持仓一半；breakeven 仅移动止损，不是出场。
  const half = (r.exits || []).find(e=>e.type === 'half');
  if (!half) return [final];
  const halfLots = lots == null ? null : lots / 2;
  const entry = numeric(r.entryPrice), price = numeric(half.price);
  const direction = r.direction === 'long' ? 1 : r.direction === 'short' ? -1 : null;
  const halfPnl = entry == null || price == null || halfLots == null || direction == null
    ? null : (price - entry) * direction * halfLots * mult;
  final.lots = halfLots;
  final.pnl = totalPnl == null || halfPnl == null ? null : totalPnl - halfPnl;
  if (!r.exitType) final.label = '剩余持仓';
  return [{time:half.time,price:half.price,type:'half',label:'半平',lots:halfLots,pnl:halfPnl},final];
}

// 资金曲线点列：与后端 compute_equity 同口径——从 0 起步，按出场事件（含半平拆分）
// 时间累加；持仓浮盈无 exitTime 排末尾，终点 = 汇总「合计」。
function equityPoints(rows) {
  const timed = [], floating = [];
  for (const r of rows) {
    if (r.status !== '已平仓' && r.status !== '持仓中') continue;
    if (r.pnl == null) continue;
    for (const exit of exitDisplayRows(r)) {
      if (exit.pnl == null) continue;
      if (exit.time == null) floating.push({ t: null, pnl: exit.pnl });
      else timed.push({ t: Number(exit.time), pnl: exit.pnl });
    }
  }
  timed.sort((a, b) => a.t - b.t);
  const events = timed.concat(floating);
  if (!events.length) return [];
  const lastT = timed.length ? timed[timed.length - 1].t : 0;
  const firstT = timed.length ? timed[0].t : lastT;
  const out = [{ t: firstT, v: 0 }];
  let cum = 0, floatIdx = 0;
  for (const ev of events) {
    const t = ev.t == null ? lastT + 1 + floatIdx++ : ev.t;
    cum += ev.pnl;
    out.push({ t, v: Math.round(cum * 100) / 100 });
  }
  return out;
}

// 对比/多方案默认配色；用户可选色按方案 id 记住（localStorage）
const EQ_COLORS = ['#7cb0ff', '#7fe0b0', '#c9a0ff', '#ffb86c', '#ff8fa3'];
const EQ_COLOR_STORE_KEY = 'bt-compare-eq-colors';
function loadCompareColors() {
  try { return JSON.parse(localStorage.getItem(EQ_COLOR_STORE_KEY) || '{}') || {}; }
  catch { return {}; }
}
function saveCompareColor(id, color) {
  const map = loadCompareColors();
  map[id] = color;
  localStorage.setItem(EQ_COLOR_STORE_KEY, JSON.stringify(map));
}
function compareColorOf(id, index) {
  const saved = loadCompareColors()[id];
  return saved || EQ_COLORS[index % EQ_COLORS.length];
}

// 定位后保留K线（根数）：控制台三模式卡与明细页共用同一份设置，localStorage 持久化。
// 输入框用 class 定位（三张模式卡同时渲染，不能用 id）；storage/querySelectorAll 均
// 防御性访问——回归测试的 vm 桩里没有 localStorage，缺了就回退默认 60。
const LOCATE_AFTER_BARS_KEY = 'bt-locate-after-bars';
const LOCATE_AFTER_BARS_DEFAULT = 60;
function readStoredAfterBars() {
  try {
    const n = parseInt(localStorage.getItem(LOCATE_AFTER_BARS_KEY), 10);
    return Number.isFinite(n) && n >= 0 ? n : null;
  } catch { return null; }
}
function storeAfterBars(n) {
  try { localStorage.setItem(LOCATE_AFTER_BARS_KEY, String(n)); } catch {}
}
function locateAfterBarsInputs() {
  return typeof document.querySelectorAll === 'function'
    ? [...document.querySelectorAll('input.locate-after-bars')] : [];
}
// 存储值回填所有实例（建卡/boot 后调用一次）
function applyStoredAfterBars() {
  const n = readStoredAfterBars();
  if (n == null) return;
  for (const inp of locateAfterBarsInputs()) inp.value = n;
}
// 改任一实例即落盘并同步其余实例（控制台三卡 + 明细页同值）
function bindLocateAfterBars() {
  for (const inp of locateAfterBarsInputs()) {
    inp.addEventListener('input', () => {
      const n = parseInt(inp.value, 10);
      if (!Number.isFinite(n) || n < 0) return;
      storeAfterBars(n);
      for (const other of locateAfterBarsInputs()) if (other !== inp) other.value = n;
    });
  }
}
// 点行定位时取值：第一个合法输入框 → 存储 → 默认 60
function locateAfterBars() {
  for (const inp of locateAfterBarsInputs()) {
    const n = parseInt(inp.value, 10);
    if (Number.isFinite(n) && n >= 0) return n;
  }
  return readStoredAfterBars() ?? LOCATE_AFTER_BARS_DEFAULT;
}
function escAttr(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// 自绘 SVG 资金曲线：seriesList = [{id?, name, points:[{t,v}], color?}]；opts.height 大图约 280 / 小图约 140
// opts.pickColors=true 时图例旁出颜色选择器，改色触发 opts.onColorChange(id, color, series)
function drawEquityChart(host, seriesList, opts = {}) {
  const series = (seriesList || []).filter(s => s.points && s.points.length);
  if (!host) return;
  if (!series.length) { host.hidden = true; host.innerHTML = ''; return; }
  host.hidden = false;
  host._eqSeries = series;
  host._eqOpts = opts;
  const H = opts.height || 280;
  const pad = { l: 52, r: 14, t: 18, b: 26 };
  // 用容器宽度做 viewBox；无宽时退回 720
  const W = Math.max(320, Math.floor(host.clientWidth || host.parentElement?.clientWidth || 720));
  const plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
  let tMin = Infinity, tMax = -Infinity, vMin = 0, vMax = 0;
  for (const s of series) for (const p of s.points) {
    if (p.t < tMin) tMin = p.t;
    if (p.t > tMax) tMax = p.t;
    if (p.v < vMin) vMin = p.v;
    if (p.v > vMax) vMax = p.v;
  }
  if (tMax <= tMin) tMax = tMin + 1;
  const vPad = Math.max((vMax - vMin) * 0.08, 1);
  vMin -= vPad; vMax += vPad;
  const xOf = t => pad.l + ((t - tMin) / (tMax - tMin)) * plotW;
  const yOf = v => pad.t + (1 - (v - vMin) / (vMax - vMin)) * plotH;
  const fmtV = v => (v > 0 ? '+' : '') + v.toFixed(2);
  const pathOf = pts => pts.map((p, i) =>
    `${i ? 'L' : 'M'}${xOf(p.t).toFixed(1)},${yOf(p.v).toFixed(1)}`).join(' ');
  const zeroY = yOf(0);
  const yTicks = [vMin, 0, vMax].filter((v, i, a) =>
    i === 0 || Math.abs(v - a[i - 1]) > (vMax - vMin) * 0.05);
  // 极值：单线时标最大/最小点
  let extrema = '';
  if (series.length === 1) {
    const pts = series[0].points;
    let hi = pts[0], lo = pts[0];
    for (const p of pts) { if (p.v > hi.v) hi = p; if (p.v < lo.v) lo = p; }
    const mark = (p, label, dy) =>
      `<circle cx="${xOf(p.t).toFixed(1)}" cy="${yOf(p.v).toFixed(1)}" r="3" fill="${series[0].color || EQ_COLORS[0]}"/>` +
      `<text x="${xOf(p.t).toFixed(1)}" y="${(yOf(p.v) + dy).toFixed(1)}" text-anchor="middle" fill="var(--muted)" font-size="10">${label} ${fmtV(p.v)}</text>`;
    if (hi !== lo || hi.v !== 0) {
      extrema = mark(hi, '高', -8) + (lo.v !== hi.v ? mark(lo, '低', 14) : '');
    }
  }
  const lines = series.map((s, i) => {
    const color = s.color || EQ_COLORS[i % EQ_COLORS.length];
    const sid = s.id != null ? ` data-eq-sid="${escAttr(s.id)}"` : ` data-eq-idx="${i}"`;
    return `<path${sid} d="${pathOf(s.points)}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  }).join('');
  const legend = series.map((s, i) => {
    const color = s.color || EQ_COLORS[i % EQ_COLORS.length];
    const last = s.points[s.points.length - 1];
    const swatch = opts.pickColors && s.id
      ? `<label class="eq-swatch" title="选择曲线颜色"><input type="color" data-eq-color="${escAttr(s.id)}" value="${escAttr(color)}"></label>`
      : `<i style="background:${color}"></i>`;
    return `<span>${swatch}${escAttr(s.name || '曲线')}` +
      (last ? ` <b class="${last.v > 0 ? 'pnl-pos' : last.v < 0 ? 'pnl-neg' : ''}">${fmtV(last.v)}</b>` : '') +
      `</span>`;
  }).join('');
  const title = opts.title || '资金曲线';
  const note = opts.note || '出场盈亏累加（含半平）；终点含浮盈';
  host.innerHTML = `
    <div class="eq-head"><strong>${escAttr(title)}</strong><span>${escAttr(note)}</span></div>
    <div class="eq-legend">${legend}</div>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img" aria-label="${escAttr(title)}">
      <line x1="${pad.l}" y1="${zeroY.toFixed(1)}" x2="${W - pad.r}" y2="${zeroY.toFixed(1)}"
            stroke="var(--border)" stroke-dasharray="4 3"/>
      ${yTicks.map(v =>
        `<text x="${pad.l - 6}" y="${(yOf(v) + 3).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${fmtV(v)}</text>`
      ).join('')}
      <text x="${pad.l}" y="${H - 6}" fill="var(--muted)" font-size="10">${escAttr(fmtTime(tMin))}</text>
      <text x="${W - pad.r}" y="${H - 6}" text-anchor="end" fill="var(--muted)" font-size="10">${escAttr(fmtTime(tMax))}</text>
      ${lines}${extrema}
      <line class="eq-cross" x1="0" y1="${pad.t}" x2="0" y2="${H - pad.b}" stroke="var(--muted)" stroke-opacity=".35" visibility="hidden"/>
      <rect class="eq-hit" x="${pad.l}" y="${pad.t}" width="${plotW}" height="${plotH}" fill="transparent"/>
    </svg>
    <div class="eq-tip"></div>`;
  // 悬停读数：找各系列最近时间点
  const svg = host.querySelector('svg');
  const tip = host.querySelector('.eq-tip');
  const cross = host.querySelector('.eq-cross');
  const hit = host.querySelector('.eq-hit');
  const nearest = (pts, t) => {
    let best = pts[0], bestD = Math.abs(pts[0].t - t);
    for (const p of pts) {
      const d = Math.abs(p.t - t);
      if (d < bestD) { best = p; bestD = d; }
    }
    return best;
  };
  const onMove = e => {
    const rect = svg.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * W;
    if (x < pad.l || x > W - pad.r) { tip.classList.remove('on'); cross.setAttribute('visibility', 'hidden'); return; }
    const t = tMin + ((x - pad.l) / plotW) * (tMax - tMin);
    cross.setAttribute('x1', x.toFixed(1));
    cross.setAttribute('x2', x.toFixed(1));
    cross.setAttribute('visibility', 'visible');
    // 用 host._eqSeries：取色过程中会就地改 color，悬停读数跟新色
    const live = host._eqSeries || series;
    const rows = live.map((s, i) => {
      const p = nearest(s.points, t);
      const color = s.color || EQ_COLORS[i % EQ_COLORS.length];
      return `<div><span style="color:${color}">●</span> ${escAttr(s.name || '曲线')}：` +
        `<b class="${p.v > 0 ? 'pnl-pos' : p.v < 0 ? 'pnl-neg' : ''}">${fmtV(p.v)}</b>` +
        ` <span style="color:var(--muted)">${escAttr(fmtTime(p.t))}</span></div>`;
    }).join('');
    tip.innerHTML = rows;
    tip.classList.add('on');
    const tipW = tip.offsetWidth || 120;
    let left = e.clientX - host.getBoundingClientRect().left + 12;
    if (left + tipW > host.clientWidth - 8) left = left - tipW - 24;
    tip.style.left = Math.max(4, left) + 'px';
    tip.style.top = Math.max(4, e.clientY - host.getBoundingClientRect().top - 10) + 'px';
  };
  const onLeave = () => { tip.classList.remove('on'); cross.setAttribute('visibility', 'hidden'); };
  hit.addEventListener('mousemove', onMove);
  hit.addEventListener('mouseleave', onLeave);
  // 对比图改色：禁止 input 时整图重绘（会拆掉系统取色窗）；就地改 path stroke，change 时再落盘
  if (opts.pickColors) {
    host.querySelectorAll('input[data-eq-color]').forEach(inp => {
      const applyColor = (id, color, persist) => {
        host._eqSeries = (host._eqSeries || series).map(s => s.id === id ? { ...s, color } : s);
        const path = host.querySelector(`path[data-eq-sid="${CSS.escape(id)}"]`);
        if (path) path.setAttribute('stroke', color);
        if (persist && typeof opts.onColorChange === 'function') {
          opts.onColorChange(id, color, host._eqSeries);
        }
      };
      // 阻止冒泡，避免外层点击逻辑干扰取色窗
      inp.addEventListener('click', e => e.stopPropagation());
      inp.addEventListener('mousedown', e => e.stopPropagation());
      inp.addEventListener('input', () => applyColor(inp.dataset.eqColor, inp.value, false));
      inp.addEventListener('change', () => applyColor(inp.dataset.eqColor, inp.value, true));
    });
  }
}

function statusClass(s) {
  return { '已平仓': 'closed', '持仓中': 'open', '同向过滤': 'filtered', '已成交': 'filled' }[s] || 'sig';
}

// 零点居中双向迷你条：maxAbs 为展示行（含分批出场行）|pnl| 最大值；0 只画空轨道
function pnlBarHtml(pnl, maxAbs) {
  if (pnl == null || !maxAbs) return '';
  const v = Number(pnl);
  if (!Number.isFinite(v)) return '';
  if (v === 0) return '<span class="pnl-bar-track" aria-hidden="true"></span>';
  const w = Math.min(50, Math.abs(v) / maxAbs * 50).toFixed(2);
  return `<span class="pnl-bar-track" aria-hidden="true"><i class="pnl-bar ${v > 0 ? 'pos' : 'neg'}" style="width:${w}%"></i></span>`;
}
// 环形图：segments [{n, color, label}]，中心主/副文本；段间留缝，单段画整环，空数据显示空轨道
function donutHtml(segments, c1, c2) {
  const R = 24, C = 32, SW = 9, TAU = Math.PI * 2;
  const total = segments.reduce((s, g) => s + g.n, 0);
  const track = `<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="var(--input)" stroke-width="${SW}"/>`;
  let arcs = '';
  const items = total > 0 ? segments.filter(g => g.n > 0) : [];
  if (items.length === 1) {
    arcs = `<circle cx="${C}" cy="${C}" r="${R}" fill="none" stroke="${items[0].color}" stroke-width="${SW}"><title>${items[0].label} ${items[0].n}</title></circle>`;
  } else if (items.length > 1) {
    let a = -Math.PI / 2;
    const GAP = TAU / 240;  // 段间缝隙约 1.5°，露出轨道底色
    const pt = ang => `${(C + R * Math.cos(ang)).toFixed(2)} ${(C + R * Math.sin(ang)).toFixed(2)}`;
    arcs = items.map(g => {
      const seg = g.n / total * TAU;
      const gap = Math.min(GAP, seg / 4);
      const path = `<path d="M ${pt(a + gap)} A ${R} ${R} 0 ${seg > Math.PI ? 1 : 0} 1 ${pt(a + seg - gap)}" fill="none" stroke="${g.color}" stroke-width="${SW}"><title>${g.label} ${g.n}</title></path>`;
      a += seg;
      return path;
    }).join('');
  }
  return `<svg width="64" height="64" viewBox="0 0 64 64" role="img" aria-label="${c1} ${c2}">${track}${arcs}
    <text x="${C}" y="31" text-anchor="middle" font-size="11.5" font-weight="600" fill="var(--text)">${c1}</text>
    <text x="${C}" y="41.5" text-anchor="middle" font-size="7.5" fill="var(--muted)">${c2}</text></svg>`;
}

// 行 → <tr> HTML：控制台实时表格与方案明细页共用。
// opts.selectedId：选中行 id（不传=无选中；不回退任何页面全局）。
// opts.noOf(row)：控制台传 displayNo（会话内序号）；opts.labelById：明细页用方案内序号。
// opts.showLossReason：明细表在盈亏后显示亏损原因列（rowspan 与交易级字段一致）。
// opts.showSym：品种角标（控制台多品种混合视图时 true；明细页不传）。
function signalRowsHtml(rows, opts = {}) {
  const interactive = opts.interactive !== false;
  const selectedId = 'selectedId' in opts ? opts.selectedId : null;
  const showLoss = !!opts.showLossReason;
  const showSym = !!(interactive && opts.showSym);
  return rows.map(r => {
    const exits = exitDisplayRows(r);
    const no = opts.labelById ? (opts.labelById[r.id] ?? r.id)
      : (interactive && opts.noOf ? opts.noOf(r) : r.id);
    const symBadge = showSym && r.mode === 'backtest' && r.symbol
      ? `<span class="mini-badge" title="${escAttr(r.symbol)}">${esc(String(r.symbol).split(':').pop())}</span>` : '';
    const shared = `
      <td title="内部记录 id：${r.id}">${no}</td>
      <td>${fmtTime(r.time)}</td>
      <td class="${r.direction === 'long' ? 'long' : 'short'}">${signalDirectionName(r)}</td>
      <td>${r.periodX || '-'}</td>
      <td>${r.strategyKey || '-'}${candidateBadges(r)}${symBadge}</td>
      <td>${r.markRes || '-'}</td>
      <td>${r.price != null ? Number(r.price).toFixed(2) : '-'}</td>
      <td>${r.nearSr != null ? Number(r.nearSr).toFixed(2) : '-'}</td>
      <td><span class="status-tag ${statusClass(r.status)}">${r.status}</span></td>
      <td>${fmtTime(r.entryTime)}</td>
      <td>${r.entryPrice != null ? Number(r.entryPrice).toFixed(2) : '-'}</td>
    `.replace(/<td(?=[ >])/g, `<td rowspan="${exits.length}"`);
    const lossTitle = r.lossReason
      ? (r.lossEndClose != null
        ? `第 ${r.lossLookahead || '?'} 根收盘 ${Number(r.lossEndClose).toFixed(2)} @ ${fmtTime(r.lossEndTime)}`
        : `回看 ${r.lossLookahead || '?'} 根，数据不足`)
      : '仅已平仓亏损单参与归因';
    const lossCell = showLoss
      ? `<td rowspan="${exits.length}" title="${esc(lossTitle)}">${r.lossReason || '-'}</td>`
      : '';
    return exits.map((exit, i) => {
      const pnl = exit.pnl;
      const pnlCls = pnl == null || pnl === 0 ? '' : (pnl > 0 ? 'pnl-pos' : 'pnl-neg');
      const pnlTxt = pnl == null ? '-' : (pnl > 0 ? '+' : '') + pnl.toFixed(2);
      // 明细页（opts.pnlBars=展示行|pnl|最大值）数字旁加零点双向条；主表不传则纯文本
      const pnlBody = opts.pnlBars && pnl != null
        ? `<span class="pnl-cell">${pnlTxt}${pnlBarHtml(pnl, opts.pnlBars)}</span>`
        : pnlTxt;
      const totalTxt = r.pnl == null ? '-' : Number(r.pnl).toFixed(2);
      const selected = interactive && selectedId === r.id;
      const classes = [selected ? 'selected' : '', i ? 'exit-continuation' : ''].filter(Boolean).join(' ');
      const attrs = interactive
        ? ` data-signal-id="${r.id}" tabindex="0" aria-selected="${selected}" title="点击定位信号K线并标记该行进出场"`
        : '';
      return `<tr${attrs}${classes ? ` class="${classes}"` : ''}>
        ${i === 0 ? shared : ''}
        <td>${fmtTime(exit.time)}</td>
        <td>${exit.price != null ? Number(exit.price).toFixed(2) : '-'}</td>
        <td title="${exitEventsDesc(r)}">${exit.label}</td>
        <td>${exit.lots != null ? exit.lots : '-'}</td>
        <td class="${pnlCls}" title="${exits.length > 1 ? (exit.type ? '本次出场盈亏' : '剩余持仓浮盈') + '；整笔交易合计：' + totalTxt : '整笔交易盈亏'}">${pnlBody}</td>
        ${i === 0 ? lossCell : ''}
      </tr>`;
    }).join('');
  }).join('');
}

// 行 → 右击复制的交易信息文本：列名：值 用「；」分割，口径与 signalRowsHtml 逐列一致（所见即所得）。
// 分批出场的交易整笔全展：每条出场行（半平/剩余持仓）的出场五列依次展开，列名重复出现。
function signalCopyText(r, no) {
  const px = v => v != null ? Number(v).toFixed(2) : '-';
  const pnlTxt = v => v == null ? '-' : (v > 0 ? '+' : '') + v.toFixed(2);
  const parts = [
    `#：${no}`,
    `信号时间：${fmtTime(r.time)}`,
    `方向：${signalDirectionName(r)}`,
    `检测周期：${r.periodX || '-'}`,
    `策略：${r.strategyKey || '-'}`,
    `背驰级别：${r.markRes || '-'}`,
    `价格：${px(r.price)}`,
    `近支阻：${px(r.nearSr)}`,
    `状态：${r.status}`,
    `成交时间：${fmtTime(r.entryTime)}`,
    `成交价：${px(r.entryPrice)}`,
  ];
  for (const exit of exitDisplayRows(r)) {
    parts.push(`出场时间：${fmtTime(exit.time)}`, `出场价：${px(exit.price)}`,
      `出场类型：${exit.label}`, `手数：${exit.lots != null ? exit.lots : '-'}`, `盈亏：${pnlTxt(exit.pnl)}`);
  }
  // 明细归因后带亏损原因（主表行无此字段则跳过）
  if (r.lossReason) parts.push(`亏损原因：${r.lossReason}`);
  return parts.join('；');
}

// 剪贴板写入核心：优先 clipboard API（localhost 安全上下文），失败回退 execCommand（局域网 http 访问）。
// 返回 Promise<boolean>（true=已写入），不弹提示——提示由调用方决定（右键菜单两个动作共用）。
function copyTextToClipboard(text) {
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0;';
    const focused = document.activeElement;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    if (focused?.isConnected) focused.focus({ preventScroll: true });
    return ok;
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(() => true, fallback);
  }
  return Promise.resolve(fallback());
}

// 右击复制执行（index/bt_detail 两页信号表）：复制 + 浮动提示
function copySignalInfo(event, text) {
  event.preventDefault();
  copyTextToClipboard(text).then(ok =>
    showCopyTip(ok ? '已复制交易信息' : '复制失败，请手动复制'));
}
let copyTipTimer = null;
function showCopyTip(msg) {
  let tip = document.getElementById('copy-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'copy-tip';
  }
  document.body.appendChild(tip);
  tip.textContent = msg;
  tip.classList.add('show');
  clearTimeout(copyTipTimer);
  copyTipTimer = setTimeout(() => tip.classList.remove('show'), 1500);
}

// ---------- 典型案例（bt_errors 表留档，右击菜单「复制并加入典型案例」） ----------

// 加入典型案例（两页共用）：POST /api/bt/errors。
// payload = {source_type:'live'|'run', run_id?, row, copy_text}；返回
// {dup:true}（409 已在列表）/ {dup:false, entry}；网络/服务端错误抛 Error（调用方提示）。
async function addBtError(payload) {
  const r = await fetch('/api/bt/errors', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (r.status === 409) return { dup: true };
  if (!r.ok || !d.ok) throw Error(d.error || `请求失败 ${r.status}`);
  return { dup: false, entry: d.entry };
}

// 右击菜单：仅复制信息 / 复制并加入典型案例（index 信号表与 bt_detail 明细表共用）。
// getCtx(tr) → {row, no, sourceType:'live'|'run', runId?, onAdded?(res)} 或 null
// （null 不拦截，表头/空白处保持浏览器默认菜单）。绑在静态 tbody 容器上，
// 表格整体重绘 innerHTML 不丢监听；exit-continuation 续行带同一 data-signal-id，
// closest 天然归到整笔交易，复制文本整笔全展（signalCopyText 口径）。
let _sigCtxMenu = null;
function _ensureSigCtxMenu() {
  if (_sigCtxMenu) return _sigCtxMenu;
  const style = document.createElement('style');
  style.textContent = `
#sig-ctx-menu{position:fixed;z-index:80;min-width:170px;padding:4px;background:var(--card);
  border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
#sig-ctx-menu[hidden]{display:none}
#sig-ctx-menu button{display:block;width:100%;text-align:left;background:transparent;border:0;
  color:var(--text);padding:7px 12px;font-size:13px;border-radius:5px;cursor:pointer}
#sig-ctx-menu button:hover{background:var(--card-2)}`;
  document.head.appendChild(style);
  const menu = document.createElement('div');
  menu.id = 'sig-ctx-menu';
  menu.role = 'menu';
  menu.hidden = true;
  menu.innerHTML = `
  <button type="button" role="menuitem" data-ctx-act="copy">仅复制信息</button>
  <button type="button" role="menuitem" data-ctx-act="addError">复制并加入典型案例</button>`;
  document.body.appendChild(menu);
  menu.onclick = e => {
    const btn = e.target.closest('button[data-ctx-act]');
    if (!btn) return;
    const ctx = menu._ctx;
    closeSigCtxMenu();
    if (!ctx) return;
    if (btn.dataset.ctxAct === 'copy') {
      copyTextToClipboard(signalCopyText(ctx.row, ctx.no))
        .then(ok => showCopyTip(ok ? '已复制交易信息' : '复制失败，请手动复制'));
      return;
    }
    // 复制并加入典型案例：先复制（结果不单独提示，以加入结果收尾，避免两次 tip 互相覆盖）
    const text = signalCopyText(ctx.row, ctx.no);
    copyTextToClipboard(text);
    addBtError({
      source_type: ctx.sourceType || 'live',
      run_id: ctx.runId,
      row: ctx.row,
      copy_text: text,
    }).then(res => {
      showCopyTip(res.dup ? '该记录已在典型案例，未重复加入' : '已复制并加入典型案例');
      if (!res.dup && typeof ctx.onAdded === 'function') ctx.onAdded(res);
    }).catch(err => alert('加入典型案例失败：' + err.message));
  };
  // 关闭：菜单外点击 / Esc / 滚动（capture 才能收到 .scroll 容器内滚动）/ 尺寸变化 / 失焦
  document.addEventListener('click', e => {
    if (!_sigCtxMenu?.hidden && !_sigCtxMenu.contains(e.target)) closeSigCtxMenu();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !_sigCtxMenu?.hidden) closeSigCtxMenu();
  });
  window.addEventListener('scroll', () => closeSigCtxMenu(), true);
  window.addEventListener('resize', () => closeSigCtxMenu());
  window.addEventListener('blur', () => closeSigCtxMenu());
  _sigCtxMenu = menu;
  return menu;
}
function closeSigCtxMenu() {
  if (_sigCtxMenu) { _sigCtxMenu.hidden = true; _sigCtxMenu._ctx = null; }
}
function openSigCtxMenu(x, y, ctx) {
  const m = _ensureSigCtxMenu();
  m._ctx = ctx;
  m.hidden = false;
  // 先显示量尺寸，再按视口夹紧（8px 边距），防止贴边溢出
  const mw = m.offsetWidth, mh = m.offsetHeight;
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - mw - 8)) + 'px';
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - mh - 8)) + 'px';
}
function attachSignalContextMenu(container, getCtx) {
  container.addEventListener('contextmenu', event => {
    const tr = event.target.closest('tr[data-signal-id]');
    const ctx = tr ? getCtx(tr) : null;
    if (!ctx) return;
    event.preventDefault();
    event.stopPropagation();   // 防 document 级 contextmenu 立刻关掉刚打开的菜单
    openSigCtxMenu(event.clientX, event.clientY, ctx);
  });
}

// 方案运行墙钟时长：45秒 / 3分12秒 / 1小时05分；缺字段（旧方案）→ —
function fmtDuration(sec) {
  if (sec == null || !Number.isFinite(Number(sec)) || Number(sec) < 0) return '—';
  const s = Math.round(Number(sec));
  if (s < 60) return `${s}秒`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (h > 0) return `${h}小时${String(m).padStart(2, '0')}分`;
  return r ? `${m}分${String(r).padStart(2, '0')}秒` : `${m}分`;
}
function btRunStateLabel(run) {
  const base = run.worker_state === 'stopped' ? '中途停止' : '完整';
  const dur = fmtDuration(run.summary?.duration_sec);
  return dur === '—' ? base : `${base} · ${dur}`;
}

// 参数对比展示顺序与汉化（键为保存的规范化 cfg 字段名）
const BT_CFG_KEYS = ['symbol', 'from', 'to', 'periods', 'data_source', 'lead_days', 'warmup', 'lots', 'contract_mult',
                     'slip_stop', 'slip_stop_atr_k', 'slip_fallback', 'slip_fallback_atr_k', 'slip_be', 'slip_be_atr_k',
                     'near', 'sr_preset', 'diverge_confirm', 'expect_bi', 'entry_macd_shrink', 'stop_entry_bar_floor', 'with_30s'];
const BT_CFG_LABELS = { symbol: '品种', from: '起始日期', to: '结束日期', periods: '周期', data_source: '数据源',
                        lead_days: '预热提前(天)', warmup: '预热根数', lots: '手数', contract_mult: '合约乘数', slip_stop: '止损滑点', slip_stop_atr_k: '止损滑点ATR系数',
                        slip_fallback: '最大止损', slip_fallback_atr_k: '最大止损ATR系数',
                        slip_be: '保本滑点', slip_be_atr_k: '保本滑点ATR系数',
                        near: '近支阻阈值', sr_preset: '支阻预设', diverge_confirm: '背驰进场', expect_bi: '检测周期够笔',
                        entry_macd_shrink: '柱缩闸', stop_entry_bar_floor: '止损下限', with_30s: '30秒级别' };
function btCfgVal(k, v) {
  if (k === 'periods') return Array.isArray(v) ? v.join('+') : (v ?? '—');
  if (k === 'data_source') return { store: '本地数据存储', live: '实时拉取', cache: '本地缓存' }[v] || v || '—';
  if (k === 'to') return v || '到最新';
  if (typeof v === 'boolean') return v ? '开' : '关';
  return v ?? '—';
}
