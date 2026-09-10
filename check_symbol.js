const CDP = require("E:/AI_Projects/TRADINGVIEW/server-cdp/node_modules/chrome-remote-interface");

(async () => {
  let client;
  try {
    const targets = await CDP.List({ port: 9222 });
    const pg = targets.find(t => t.type === "page" && t.url.includes("tradingview.com"));
    if (!pg) { console.log("no page"); process.exit(1); }
    client = await CDP({ target: pg.id, port: 9222 });
    await client.Page.enable();
    await client.Runtime.enable();
    const r = await client.Runtime.evaluate({
      expression: `(function() {
        const ch = TradingViewApi.activeChart();
        const symExt = ch.symbolExt();
        return {
          symbol: ch.symbol(),
          full_name: symExt ? symExt.full_name : null,
          resolution: String(ch.resolution()),
          description: symExt ? symExt.description : null
        };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    console.log(JSON.stringify(r.result.value, null, 2));
    await client.close();
  } catch (e) { console.log("Error:", e.message); if (client) await client.close(); }
})();
