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
    const r = await client.Runtime.evaluate({
      expression: `(function() {
        const chart = TradingViewApi.activeChart();
        chart.setResolution("15");
        return { success: true };
      })()`,
      returnByValue: true, awaitPromise: true, timeout: 10000,
    });
    await sleep(4000);
    console.log("switched to 15");
    await client.close();
  } catch (e) { console.log("Error:", e.message); if (client) await client.close(); }
})();
