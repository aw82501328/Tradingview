const CDP = require('chrome-remote-interface');

(async () => {
  const targets = await CDP.List({ port: 9222 });
  const pg = targets.find(t => t.type === 'page' && t.url.includes('tradingview.com/chart'));
  if (!pg) process.exit(1);
  const client = await CDP({ target: pg.id, port: 9222 });
  await client.Runtime.enable();
  await client.Page.enable();

  // 1. 检查图形是否在数据层
  const shapes = await client.Runtime.evaluate({
    expression: '(() => { const s = TradingViewApi.activeChart().getAllShapes(); const c={}; s.forEach(x=>c[x.name]=(c[x.name]||0)+1); return {total:s.length, counts:c}; })()',
    returnByValue: true, timeout: 5000
  });
  console.log('数据层图形:', JSON.stringify(shapes.result.value));

  // 2. CDP 原生 reload（绕过缓存）
  console.log('强制刷新页面...');
  await client.Page.reload({ ignoreCache: true });
  console.log('已发送刷新指令');

  client.close();
})().catch(e => console.log('错误:', e.message));
