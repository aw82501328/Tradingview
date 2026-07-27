/**
 * TradingView 桌面控制 MCP Server
 *
 * 通过 CDP 连接本地 TradingView Desktop (Electron)，
 * 使用内部 TradingViewApi 操控图表。
 *
 * 前置条件：
 *   1. 安装 TradingView Desktop 官网版
 *   2. 以调试模式启动：TradingView.exe --remote-debugging-port=9222
 *   3. 打开至少一张图表
 */

const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require("@modelcontextprotocol/sdk/types.js");
const CDP = require("chrome-remote-interface");

const TV_DEBUG_PORT = parseInt(process.env.TV_MCP_PORT || "9222", 10);

let cdpClient = null;

async function connectCDP() {
  if (cdpClient) return cdpClient;
  const targets = await CDP.List({ port: TV_DEBUG_PORT });
  const pageTarget = targets.find(
    (t) => t.type === "page" && (t.url.includes("tradingview.com") || t.url.includes("tv."))
  );
  if (!pageTarget) throw new Error("未找到 TradingView 页面，请确保已打开图表标签");
  cdpClient = await CDP({ target: pageTarget.id, port: TV_DEBUG_PORT });
  await cdpClient.Page.enable();
  await cdpClient.Runtime.enable();
  return cdpClient;
}

async function evalInPage(code) {
  const client = await connectCDP();
  const result = await client.Runtime.evaluate({
    expression: `(function() { try { return (${code}); } catch(e) { return { __error: e.message }; } })()`,
    returnByValue: true,
    awaitPromise: true,
    timeout: 10000,
  });
  if (result.exceptionDetails) {
    throw new Error(`执行异常: ${JSON.stringify(result.exceptionDetails)}`);
  }
  return result.result.value;
}

// ============================================================
// 工具实现 — 使用 TradingViewApi (正确的内部 API)
// ============================================================

async function tvHealthCheck() {
  try {
    await connectCDP();
    const info = await evalInPage(`{
      title: document.title,
      hasApi: typeof TradingViewApi !== 'undefined',
      chartsCount: typeof TradingViewApi !== 'undefined' ? TradingViewApi.chartsCount() : 0,
      layout: typeof TradingViewApi !== 'undefined' ? TradingViewApi.layoutName() : null
    }`);
    return {
      status: "ok",
      server: "tradingview-cdp",
      ...info,
    };
  } catch (e) {
    return { status: "error", message: e.message };
  }
}

async function tvGetChart() {
  const r = await evalInPage(`{
    const chart = TradingViewApi.activeChart();
    if (!chart) return { error: "no_chart" };
    const symExt = chart.symbolExt();
    return {
      symbol: chart.symbol(),
      full_name: symExt ? symExt.full_name : null,
      exchange: symExt ? symExt.exchange : null,
      description: symExt ? symExt.description : null,
      type: symExt ? symExt.type : null,
      resolution: String(chart.resolution()),
      chartType: chart.chartType(),
      timezone: chart.getTimezone(),
      studies: chart.getAllStudies()
    };
  }`);
  return r;
}

async function tvGetBars(n_bars = 100) {
  const r = await evalInPage(`
    (() => {
      const chart = TradingViewApi.activeChart();
      if (!chart) return { error: "no_chart" };
      // 通过 mainSeries().data() 获取 OHLCV 数据
      const mainSeries = chart.chartModel().mainSeries();
      if (!mainSeries) return { error: "no_main_series" };
      const raw = mainSeries.data();
      if (!raw || !raw.m_bars || !raw.m_bars._items) return { error: "no_data", raw: JSON.stringify(raw).slice(0,200) };
      const items = raw.m_bars._items;
      if (items.length === 0) return { error: "no_items" };
      const limit = Math.min(${n_bars}, items.length);
      // value 数组: [timestamp, open, high, low, close, volume]
      const bars = items.slice(-limit).map(item => {
        const v = item.value;
        return {
          time: v[0],
          open: v[1],
          high: v[2],
          low: v[3],
          close: v[4],
          volume: v[5]
        };
      });
      return { bars, count: bars.length, total: items.length, symbol: chart.symbol(), resolution: String(chart.resolution()) };
    })()`
  );
  return r;
}

async function tvSwitchSymbol(symbol) {
  const r = await evalInPage(`
    (() => {
      const chart = TradingViewApi.activeChart();
      if (!chart) return { error: "no_chart" };
      chart.setSymbol("${symbol}");
      return { success: true, symbol: "${symbol}" };
    })()`);
  return r;
}

async function tvSwitchTimeframe(interval) {
  const r = await evalInPage(`
    (() => {
      const chart = TradingViewApi.activeChart();
      if (!chart) return { error: "no_chart" };
      chart.setResolution("${interval}");
      return { success: true, interval: "${interval}" };
    })()`);
  return r;
}

let createdShapeIds = [];  // 记录本 session 创建的图形 ID

async function tvEvalJS(code) {
  return await evalInPage(code);
}

/**
 * 在图表上标记买卖信号（只删除自己创建的标记）
 * @param {Array} signals - [{time, price, type: "buy"|"sell", text?}]
 */
async function tvMarkSignals(signals) {
  if (!signals || signals.length === 0) {
    return { error: "signals 不能为空" };
  }

  const results = [];
  const newIds = [];

  for (const sig of signals) {
    // 买入: 绿色向上箭头, 卖出: 红色向下箭头
    const shape = sig.type === "buy" ? "arrow_up" : "arrow_down";
    const text = sig.text || (sig.type === "buy" ? "BUY" : "SELL");
    const rawTime = sig.time;
    const time = typeof rawTime === "number" && rawTime > 1e12 ? rawTime / 1000 : rawTime;

    try {
      // createShape 返回 Promise<shapeId>，用 awaitPromise 拿到 ID
      const shapeId = await evalInPage(`
        (async () => {
          const chart = TradingViewApi.activeChart();
          if (!chart) throw new Error("no_chart");
          return await chart.createShape(
            { time: ${time}, price: ${sig.price} },
            { shape: "${shape}", text: "${text}", lock: true }
          );
        })()`);
      newIds.push(shapeId);
      results.push({ index: results.length, type: sig.type, time, price: sig.price, id: shapeId });
    } catch (e) {
      results.push({ index: results.length, type: sig.type, time, price: sig.price, error: e.message });
    }
  }

  createdShapeIds = createdShapeIds.concat(newIds);

  return {
    total: signals.length,
    success: newIds.length,
    failed: results.filter((r) => r.error).length,
    shape_ids: newIds,
    details: results,
  };
}

/**
 * 清除本 session 创建的图形标记（不影响用户自己画的线）
 */
async function tvClearSignals() {
  if (createdShapeIds.length === 0) {
    return { cleared: 0, message: "没有需要清除的标记" };
  }

  const idsToRemove = [...createdShapeIds];
  createdShapeIds = [];

  const r = await evalInPage(`
    (() => {
      const chart = TradingViewApi.activeChart();
      if (!chart) return { error: "no_chart" };
      const ids = ${JSON.stringify(idsToRemove)};
      let removed = 0;
      for (const id of ids) {
        try { chart.removeEntity(id); removed++; } catch(e) {}
      }
      return { removed, total: ids.length, remaining_shapes: chart.getAllShapes().length };
    })()`);
  return r;
}

// ============================================================
// MCP Server
// ============================================================

const server = new Server(
  { name: "tradingview-cdp", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "tv_health_check",
      description: "检查与 TradingView 桌面端的 CDP 连接，返回图表数量、布局名称等。",
      inputSchema: { type: "object", properties: {}, required: [] },
    },
    {
      name: "tv_get_chart",
      description: "获取当前图表信息：品种、周期、图表类型、时区、已加载指标。",
      inputSchema: { type: "object", properties: {}, required: [] },
    },
    {
      name: "tv_get_bars",
      description: "从当前图表获取 K 线数据（OHLCV）。",
      inputSchema: {
        type: "object",
        properties: {
          n_bars: { type: "integer", description: "K线数量（默认100，最大5000）", default: 100 },
        },
        required: [],
      },
    },
    {
      name: "tv_switch_symbol",
      description: "切换图表品种。示例: NASDAQ:AAPL, BINANCE:BTCUSDT",
      inputSchema: {
        type: "object",
        properties: {
          symbol: { type: "string", description: "完整品种代码，含交易所，如 NASDAQ:AAPL" },
        },
        required: ["symbol"],
      },
    },
    {
      name: "tv_switch_timeframe",
      description: "切换图表周期: 1/5/15/30/60/120/240/1D/1W/1M",
      inputSchema: {
        type: "object",
        properties: {
          interval: { type: "string", description: "周期代码，如 1D、60、240" },
        },
        required: ["interval"],
      },
    },
    {
      name: "tv_eval_js",
      description: "在图表页面执行任意 JavaScript（高级用法，可访问 TradingViewApi）。",
      inputSchema: {
        type: "object",
        properties: {
          code: { type: "string", description: "要执行的 JS 代码" },
        },
        required: ["code"],
      },
    },
    {
      name: "tv_mark_signals",
      description: "在图表上根据策略信号自动标记买卖点。买入=绿色向上箭头，卖出=红色向下箭头。",
      inputSchema: {
        type: "object",
        properties: {
          signals: {
            type: "array",
            description: "信号列表",
            items: {
              type: "object",
              properties: {
                time: { type: "number", description: "K线时间戳(秒 或 毫秒)" },
                price: { type: "number", description: "标记价格" },
                type: { type: "string", enum: ["buy", "sell"], description: "买卖类型" },
                text: { type: "string", description: "标记文字（可选，默认 BUY/SELL）" },
              },
              required: ["time", "price", "type"],
            },
          },
        },
        required: ["signals"],
      },
    },
    {
      name: "tv_clear_signals",
      description: "清除本 session 通过 tv_mark_signals 创建的图形标记（不影响用户自己画的线）。",
      inputSchema: { type: "object", properties: {}, required: [] },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  let result;
  try {
    switch (name) {
      case "tv_health_check":
        result = await tvHealthCheck();
        break;
      case "tv_get_chart":
        result = await tvGetChart();
        break;
      case "tv_get_bars":
        result = await tvGetBars(args?.n_bars || 100);
        break;
      case "tv_switch_symbol":
        result = await tvSwitchSymbol(args?.symbol || "NASDAQ:AAPL");
        break;
      case "tv_switch_timeframe":
        result = await tvSwitchTimeframe(args?.interval || "1D");
        break;
      case "tv_eval_js":
        result = await tvEvalJS(args?.code || "return 'no code'");
        break;
      case "tv_mark_signals":
        result = await tvMarkSignals(args?.signals || []);
        break;
      case "tv_clear_signals":
        result = await tvClearSignals();
        break;
      default:
        return { content: [{ type: "text", text: `未知工具: ${name}` }], isError: true };
    }
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  } catch (e) {
    return {
      content: [{ type: "text", text: JSON.stringify({ error: e.message }) }],
      isError: true,
    };
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
