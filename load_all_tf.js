const CDP = require('./server-cdp/node_modules/chrome-remote-interface');
(async () => {
  const targets = await CDP.List({ port: 9222 });
  const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
  if (!pg) { console.log("ERROR: no page"); process.exit(1); }
  const client = await CDP({ target: pg.id, port: 9222 });
  await client.Page.enable();
  await client.Runtime.enable();
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const PERIODS = ["D", "240", "60", "15", "3"];
  const FROM = Math.floor(Date.UTC(2026, 6, 2) / 1000); // 2026-07-02 UTC
  const data = {};
  for (const res of PERIODS) {
    await client.Runtime.evaluate({
      expression: `TradingViewApi.activeChart().setResolution(${JSON.stringify(res)});`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    await sleep(4000);
    await client.Runtime.evaluate({
      expression: `(function(){ const c=TradingViewApi.activeChart(); const w=c._chartWidget||(c.chartModel&&c.chartModel()._chartWidget); const ts=w&&w.model?w.model().timeScale():c.chartModel().timeScale(); ts.scrollToFirstBar(); return 'ok'; })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    await sleep(15000);
    const r = await client.Runtime.evaluate({
      expression: `(function() {
        const c = TradingViewApi.activeChart();
        const items = c.chartModel().mainSeries().data().m_bars._items;
        const out = [];
        for (const it of items) {
          const v = it.value;
          out.push({ time: v[0], open: v[1], high: v[2], low: v[3], close: v[4] });
        }
        return { res: String(c.resolution()), bars: out, total: items.length };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 15000,
    });
    const d = r.result.value;
    data[res] = d.bars.filter(b => b.time >= FROM);
    console.log("loaded", res, d.total, "-> filtered", data[res].length);
  }
  const fs = require('fs');
  fs.writeFileSync('bars_all_tf.json', JSON.stringify(data));
  console.log("saved bars_all_tf.json");
  await client.close();
})();
