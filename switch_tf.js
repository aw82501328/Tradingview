const CDP = require("E:/AI_Projects/TRADINGVIEW/server-cdp/node_modules/chrome-remote-interface");
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

(async () => {
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("no page"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();

    // 切换周期到 1 小时
    const r = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        if (!chart) return { error: 'no_chart' };
        chart.setResolution("60");
        return { success: true };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    console.log("switch:", JSON.stringify(r.result.value));

    // 等待数据加载
    await sleep(4000);

    // 确认
    const c = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        const ms = chart.chartModel().mainSeries();
        const items = ms.data().m_bars._items;
        return { symbol: chart.symbol(), resolution: String(chart.resolution()), total: items ? items.length : 0 };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    console.log("confirm:", JSON.stringify(c.result.value));

    await client.close();
  } catch (e) {
    console.log("Error:", e.message);
    if (client) await client.close();
  }
})();
